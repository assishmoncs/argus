"""
Database connection manager and migration runner.
"""

import sqlite3
import asyncio
import os
import logging
from config.settings import settings

logger = logging.getLogger("argus.database")


class Database:
    """Thread-safe SQLite wrapper for async application context."""

    def __init__(self, db_path: str = None) -> None:
        self.db_path = db_path or settings.DATABASE_PATH
        self._lock = asyncio.Lock()

    def get_connection(self) -> sqlite3.Connection:
        """Get synchronous sqlite3 connection with proper settings."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Enable Foreign Key constraints
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    async def execute(self, query: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        """Execute a query and return results, run inside thread-pool to avoid blocking event loop."""
        def _run():
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, parameters)
                conn.commit()
                try:
                    return cursor.fetchall()
                except sqlite3.ProgrammingError:
                    return []

        async with self._lock:
            return await asyncio.to_thread(_run)

    async def execute_many(self, query: str, parameters_list: list[tuple]) -> None:
        """Execute query many times in a thread-pool."""
        def _run():
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.executemany(query, parameters_list)
                conn.commit()

        async with self._lock:
            await asyncio.to_thread(_run)

    async def run_migrations(self) -> None:
        """Run database schema setups and handle backward migrations."""
        def _migrate():
            conn = self.get_connection()
            cursor = conn.cursor()

            # Check if old warning table structure exists (only user_id, username, count)
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='warnings';")
            warnings_exists = cursor.fetchone()
            
            old_warnings = []
            if warnings_exists:
                # Inspect columns of warnings table
                cursor.execute("PRAGMA table_info(warnings);")
                columns = {col[1] for col in cursor.fetchall()}
                if "count" in columns and "chat_id" not in columns:
                    logger.info("Database migration: Detected old 'warnings' table. Fetching records to migrate...")
                    cursor.execute("SELECT user_id, username, count FROM warnings;")
                    old_warnings = cursor.fetchall()
                    # Drop old warnings table to recreate it
                    cursor.execute("DROP TABLE warnings;")
                    conn.commit()

            # Create tables
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

            conn.commit()

            # Insert old warning records back if found
            if old_warnings:
                logger.info("Database migration: Re-inserting %d warning records...", len(old_warnings))
                # Make sure default global group settings exist for group_id=0 (or specific ones)
                cursor.execute("INSERT OR IGNORE INTO groups (chat_id, title) VALUES (0, 'Global Migration Chat');")
                for u_id, u_name, count in old_warnings:
                    # Insert user
                    cursor.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?);", 
                                   (u_id, u_name, u_name or "MigratedUser"))
                    cursor.execute("INSERT OR IGNORE INTO group_members (chat_id, user_id, role) VALUES (0, ?, 'member');", (u_id,))
                    # Insert 'count' warnings
                    for _ in range(count):
                        cursor.execute(
                            "INSERT INTO warnings (chat_id, user_id, reason, moderator_id) VALUES (0, ?, 'Migrated warning', 0);"
                        )
                conn.commit()
                logger.info("Database migration: Finished warning data migration successfully!")

            conn.close()

        async with self._lock:
            await asyncio.to_thread(_migrate)


# Singleton database instance
db = Database()
