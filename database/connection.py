"""
Enterprise database connection pool, transaction manager, and schema migration runner.
Supports SQLite WAL concurrency, connection pooling, automatic lock retries, and telemetry.
"""

import asyncio
import contextlib
import logging
import queue
import random
import sqlite3
import time
from typing import Any, AsyncIterator, Callable, List, Optional, Sequence, Tuple

from config.settings import settings
from utils.telemetry import telemetry

logger = logging.getLogger("argus.database")

_WRITE_PREFIXES = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "REPLACE",
    "CREATE",
    "DROP",
    "ALTER",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "PRAGMA",
    "VACUUM",
)


def _is_write_query(query: str) -> bool:
    stripped = query.lstrip().upper()
    return stripped.startswith(_WRITE_PREFIXES)


class _TransactionSession:
    """Represents an active atomic database transaction bound to a pooled connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def execute(self, query: str, parameters: Tuple[Any, ...] = ()) -> List[sqlite3.Row]:
        """Execute a statement inside the active transaction."""
        def _run() -> List[sqlite3.Row]:
            cursor = self._conn.cursor()
            cursor.execute(query, parameters)
            try:
                return cursor.fetchall()
            except sqlite3.ProgrammingError:
                return []

        return await asyncio.to_thread(_run)


class Database:
    """
    Production-scale async SQLite database manager with connection pooling,
    WAL mode optimization, atomic transactions, and automatic retry on lock contention.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        pool_size: Optional[int] = None,
        busy_timeout_ms: Optional[int] = None,
        enable_wal: Optional[bool] = None,
        max_retries: Optional[int] = None,
        retry_base_delay: Optional[float] = None,
    ) -> None:
        self.db_path = db_path or settings.DATABASE_PATH
        self.pool_size = pool_size if pool_size is not None else settings.DB_POOL_SIZE
        self.busy_timeout_ms = (
            busy_timeout_ms if busy_timeout_ms is not None else settings.DB_BUSY_TIMEOUT_MS
        )
        self.enable_wal = enable_wal if enable_wal is not None else settings.DB_ENABLE_WAL
        self.max_retries = max_retries if max_retries is not None else settings.DB_MAX_RETRIES
        self.retry_base_delay = (
            retry_base_delay if retry_base_delay is not None else settings.DB_RETRY_BASE_DELAY
        )

        self._is_memory = self.db_path == ":memory:" or self.db_path.startswith("file::memory:")
        self._memory_uri = f"file:argus_mem_{id(self)}?mode=memory&cache=shared"

        # For in-memory DBs, keep one anchor connection open so the shared memory DB persists
        self._anchor_conn: Optional[sqlite3.Connection] = None
        if self._is_memory:
            self._anchor_conn = self._create_raw_connection()

        self._pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(maxsize=self.pool_size)
        self._all_connections: List[sqlite3.Connection] = []
        self._write_lock_obj: Optional[asyncio.Lock] = None
        self._write_lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self._closed = False

    @property
    def _write_lock(self) -> asyncio.Lock:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if self._write_lock_obj is None or self._write_lock_loop is not current_loop:
            self._write_lock_obj = asyncio.Lock()
            self._write_lock_loop = current_loop
        return self._write_lock_obj

    def _create_raw_connection(self) -> sqlite3.Connection:
        """Create and configure a new sqlite3.Connection with production pragmas."""
        if self._is_memory:
            conn = sqlite3.connect(
                self._memory_uri,
                uri=True,
                timeout=max(1.0, self.busy_timeout_ms / 1000.0),
                check_same_thread=False,
            )
        else:
            conn = sqlite3.connect(
                self.db_path,
                timeout=max(1.0, self.busy_timeout_ms / 1000.0),
                check_same_thread=False,
            )

        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)};")
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute(f"PRAGMA cache_size = -{int(settings.DB_CACHE_SIZE_KB)};")
        if not self._is_memory and self.enable_wal:
            try:
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = NORMAL;")
            except sqlite3.Error as exc:
                logger.warning("Could not enable WAL mode on %s: %s", self.db_path, exc)
        return conn

    def get_connection(self) -> sqlite3.Connection:
        """Acquire a connection from the pool or create a new one."""
        try:
            conn = self._pool.get_nowait()
            return conn
        except queue.Empty:
            conn = self._create_raw_connection()
            self._all_connections.append(conn)
            telemetry.set_gauge("db_pool_connections", len(self._all_connections))
            return conn

    def _release_connection(self, conn: sqlite3.Connection) -> None:
        """Return a healthy connection back to the pool, or close if pool is full/closed."""
        if self._closed:
            try:
                conn.close()
            except Exception:
                pass
            return

        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            try:
                conn.close()
            except Exception:
                pass

    async def _run_with_retry(self, op_name: str, fn: Callable[[], Any]) -> Any:
        """Run synchronous DB callable in a worker thread with exponential backoff on lock errors."""
        attempt = 0
        start_time = time.perf_counter()
        while True:
            try:
                result = await asyncio.to_thread(fn)
                telemetry.increment("db_queries_total", op=op_name, status="ok")
                telemetry.observe_latency("db_query_latency", time.perf_counter() - start_time, op=op_name)
                return result
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                is_locked = "locked" in err_msg or "busy" in err_msg
                if is_locked and attempt < self.max_retries:
                    attempt += 1
                    telemetry.increment("db_retries_total", op=op_name)
                    delay = self.retry_base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.02)
                    logger.warning(
                        "SQLite busy/locked during %s (attempt %d/%d), retrying in %.3fs: %s",
                        op_name,
                        attempt,
                        self.max_retries,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                telemetry.increment("db_queries_total", op=op_name, status="error")
                telemetry.increment("db_errors_total", type="OperationalError")
                raise
            except Exception as exc:
                telemetry.increment("db_queries_total", op=op_name, status="error")
                telemetry.increment("db_errors_total", type=exc.__class__.__name__)
                raise

    async def execute(self, query: str, parameters: Tuple[Any, ...] = ()) -> List[sqlite3.Row]:
        """Execute a SQL query and return fetched rows."""
        is_write = _is_write_query(query)

        def _work() -> List[sqlite3.Row]:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(query, parameters)
                if is_write:
                    conn.commit()
                try:
                    return cursor.fetchall()
                except sqlite3.ProgrammingError:
                    return []
            except Exception:
                if is_write:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                raise
            finally:
                self._release_connection(conn)

        if is_write:
            async with self._write_lock:
                return await self._run_with_retry("write", _work)
        return await self._run_with_retry("read", _work)

    async def execute_one(
        self, query: str, parameters: Tuple[Any, ...] = ()
    ) -> Optional[sqlite3.Row]:
        """Execute a SQL query and return the first row or None."""
        rows = await self.execute(query, parameters)
        return rows[0] if rows else None

    async def execute_many(self, query: str, parameters_list: Sequence[Tuple[Any, ...]]) -> None:
        """Execute a parameterized query across a batch of parameter tuples atomically."""
        def _work() -> None:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()
                cursor.executemany(query, parameters_list)
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                self._release_connection(conn)

        async with self._write_lock:
            await self._run_with_retry("execute_many", _work)

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[_TransactionSession]:
        """
        Async context manager executing multiple statements inside a single atomic
        SQLite transaction (BEGIN IMMEDIATE ... COMMIT / ROLLBACK).
        """
        async with self._write_lock:
            conn = await asyncio.to_thread(self.get_connection)
            await asyncio.to_thread(conn.execute, "BEGIN IMMEDIATE;")
            session = _TransactionSession(conn)
            try:
                yield session
                await asyncio.to_thread(conn.commit)
                telemetry.increment("db_transactions_total", status="committed")
            except Exception:
                await asyncio.to_thread(conn.rollback)
                telemetry.increment("db_transactions_total", status="rolled_back")
                raise
            finally:
                self._release_connection(conn)

    async def health_check(self) -> dict[str, Any]:
        """Verify database connectivity and return pool/WAL diagnostics."""
        start = time.perf_counter()
        try:
            row = await self.execute_one("SELECT 1 AS ok;")
            mode_row = await self.execute_one("PRAGMA journal_mode;")
            journal_mode = mode_row[0] if mode_row else "unknown"
            latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
            return {
                "healthy": bool(row and row["ok"] == 1),
                "latency_ms": latency_ms,
                "journal_mode": journal_mode,
                "pool_size": self.pool_size,
                "created_connections": len(self._all_connections),
                "available_connections": self._pool.qsize(),
            }
        except Exception as exc:
            logger.error("Database health check failed: %s", exc)
            return {
                "healthy": False,
                "error": str(exc),
            }

    async def close(self) -> None:
        """Close all pooled connections cleanly on shutdown."""
        self._closed = True
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except Exception:
                pass
        for conn in self._all_connections:
            try:
                conn.close()
            except Exception:
                pass
        self._all_connections.clear()
        if self._anchor_conn is not None:
            try:
                self._anchor_conn.close()
            except Exception:
                pass
            self._anchor_conn = None

    async def run_migrations(self) -> None:
        """Run database schema setups, backward migrations, and index creation."""
        def _migrate() -> None:
            conn = self.get_connection()
            try:
                cursor = conn.cursor()

                # Schema version tracking table
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)

                # Check if old warning table structure exists (only user_id, username, count)
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='warnings';"
                )
                warnings_exists = cursor.fetchone()

                old_warnings = []
                if warnings_exists:
                    cursor.execute("PRAGMA table_info(warnings);")
                    columns = {col[1] for col in cursor.fetchall()}
                    if "count" in columns and "chat_id" not in columns:
                        logger.info(
                            "Database migration: Detected legacy 'warnings' table. Fetching records to migrate..."
                        )
                        cursor.execute("SELECT user_id, username, count FROM warnings;")
                        old_warnings = cursor.fetchall()
                        cursor.execute("DROP TABLE warnings;")
                        conn.commit()

                # Create core tables
                cursor.execute("""
                CREATE TABLE IF NOT EXISTS groups (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT,
                    warning_limit INTEGER DEFAULT 3,
                    welcome_message TEXT DEFAULT 'Welcome to the group!',
                    goodbye_message TEXT DEFAULT 'Goodbye!',
                    welcome_captcha INTEGER DEFAULT 0,
                    anti_raid INTEGER DEFAULT 0,
                    slow_mode INTEGER DEFAULT 0,
                    is_locked INTEGER DEFAULT 0,
                    ai_enabled INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS group_members (
                    chat_id INTEGER,
                    user_id INTEGER,
                    role TEXT DEFAULT 'member',
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id),
                    FOREIGN KEY(chat_id) REFERENCES groups(chat_id) ON DELETE CASCADE,
                    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS warnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    user_id INTEGER,
                    reason TEXT,
                    moderator_id INTEGER,
                    severity INTEGER DEFAULT 1,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(chat_id) REFERENCES groups(chat_id) ON DELETE CASCADE,
                    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS punishments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    user_id INTEGER,
                    action TEXT,
                    reason TEXT,
                    moderator_id INTEGER,
                    duration_seconds INTEGER,
                    expires_at TIMESTAMP,
                    is_active INTEGER DEFAULT 1,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(chat_id) REFERENCES groups(chat_id) ON DELETE CASCADE,
                    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS custom_filters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    pattern TEXT,
                    match_type TEXT DEFAULT 'exact',
                    action TEXT DEFAULT 'delete',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(chat_id) REFERENCES groups(chat_id) ON DELETE CASCADE
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS notes (
                    chat_id INTEGER,
                    trigger TEXT,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, trigger),
                    FOREIGN KEY(chat_id) REFERENCES groups(chat_id) ON DELETE CASCADE
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS whitelisted_words (
                    chat_id INTEGER,
                    word TEXT,
                    PRIMARY KEY (chat_id, word),
                    FOREIGN KEY(chat_id) REFERENCES groups(chat_id) ON DELETE CASCADE
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_announcements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    text TEXT,
                    cron_expression TEXT,
                    next_run TIMESTAMP,
                    is_active INTEGER DEFAULT 1,
                    FOREIGN KEY(chat_id) REFERENCES groups(chat_id) ON DELETE CASCADE
                );
                """)

                cursor.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    actor_id INTEGER,
                    target_id INTEGER,
                    action TEXT NOT NULL,
                    details TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)

                # Production composite indexes for high-throughput lookups
                index_statements = [
                    "CREATE INDEX IF NOT EXISTS idx_warnings_chat_user ON warnings(chat_id, user_id, timestamp DESC);",
                    "CREATE INDEX IF NOT EXISTS idx_punishments_active_expiry ON punishments(is_active, expires_at);",
                    "CREATE INDEX IF NOT EXISTS idx_punishments_chat_user_active ON punishments(chat_id, user_id, action, is_active);",
                    "CREATE INDEX IF NOT EXISTS idx_custom_filters_chat ON custom_filters(chat_id);",
                    "CREATE INDEX IF NOT EXISTS idx_notes_chat_trigger ON notes(chat_id, trigger);",
                    "CREATE INDEX IF NOT EXISTS idx_whitelisted_words_chat ON whitelisted_words(chat_id);",
                    "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);",
                    "CREATE INDEX IF NOT EXISTS idx_group_members_chat_role ON group_members(chat_id, role);",
                    "CREATE INDEX IF NOT EXISTS idx_audit_logs_chat_ts ON audit_logs(chat_id, timestamp DESC);",
                ]
                for stmt in index_statements:
                    cursor.execute(stmt)

                # Record migration versions
                cursor.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (1, 'initial_schema');"
                )
                cursor.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (2, 'production_indexes_and_audit_logs');"
                )

                conn.commit()

                # Insert old warning records back if found
                if old_warnings:
                    logger.info(
                        "Database migration: Re-inserting %d warning records...", len(old_warnings)
                    )
                    cursor.execute(
                        "INSERT OR IGNORE INTO groups (chat_id, title) VALUES (0, 'Global Migration Chat');"
                    )
                    for u_id, u_name, count in old_warnings:
                        cursor.execute(
                            "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?);",
                            (u_id, u_name, u_name or "MigratedUser"),
                        )
                        cursor.execute(
                            "INSERT OR IGNORE INTO group_members (chat_id, user_id, role) VALUES (0, ?, 'member');",
                            (u_id,),
                        )
                        for _ in range(count):
                            cursor.execute(
                                "INSERT INTO warnings (chat_id, user_id, reason, moderator_id) VALUES (0, ?, 'Migrated warning', 0);",
                                (u_id,),
                            )
                    conn.commit()
                    logger.info("Database migration: Finished warning data migration successfully!")
            finally:
                self._release_connection(conn)

        async with self._write_lock:
            await self._run_with_retry("migrations", _migrate)


# Singleton database instance
db = Database()
