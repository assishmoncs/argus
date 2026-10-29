"""
Database models and repository patterns for Argus with read-through TTL caching,
strict column whitelisting, and atomic transaction execution.
"""

import copy
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from config.settings import settings
from database.connection import db
from utils.telemetry import telemetry

# Explicit whitelist of mutable columns on the groups table to prevent SQL injection
ALLOWED_GROUP_COLUMNS = frozenset({
    "title",
    "warning_limit",
    "welcome_message",
    "goodbye_message",
    "welcome_captcha",
    "anti_raid",
    "slow_mode",
    "is_locked",
    "ai_enabled",
})


class _RepositoryTTLCache:
    """Thread-safe TTL cache scoped by active Database instance ID."""

    def __init__(self, ttl_seconds: float = 30.0, max_entries: int = 2048) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.RLock()
        # Key: (db_instance_id, namespace, key) -> (expires_at, value)
        self._store: Dict[Tuple[int, str, Any], Tuple[float, Any]] = {}

    def get(self, namespace: str, key: Any) -> Optional[Any]:
        if self._ttl <= 0:
            return None
        full_key = (id(db), namespace, key)
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(full_key)
            if entry is None:
                telemetry.increment("repo_cache_misses_total", namespace=namespace)
                return None
            expires_at, val = entry
            if now >= expires_at:
                del self._store[full_key]
                telemetry.increment("repo_cache_misses_total", namespace=namespace)
                return None
            telemetry.increment("repo_cache_hits_total", namespace=namespace)
            return copy.deepcopy(val)

    def set(self, namespace: str, key: Any, value: Any) -> None:
        if self._ttl <= 0:
            return
        full_key = (id(db), namespace, key)
        now = time.monotonic()
        with self._lock:
            if len(self._store) >= self._max_entries:
                # Evict expired entries first, or oldest 10%
                expired = [k for k, (exp, _) in self._store.items() if now >= exp]
                for k in expired:
                    self._store.pop(k, None)
                if len(self._store) >= self._max_entries:
                    for k in list(self._store.keys())[: max(1, self._max_entries // 10)]:
                        self._store.pop(k, None)
            self._store[full_key] = (now + self._ttl, copy.deepcopy(value))

    def invalidate(self, namespace: str, key: Any) -> None:
        full_key = (id(db), namespace, key)
        with self._lock:
            self._store.pop(full_key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


repo_cache = _RepositoryTTLCache(ttl_seconds=settings.DB_REPO_CACHE_TTL)


class GroupRepository:
    """Operations on groups table with read-through caching and column validation."""

    @staticmethod
    async def get_group(chat_id: int) -> dict[str, Any]:
        """Fetch group settings, creating default ones if not present."""
        cached = repo_cache.get("group", chat_id)
        if cached is not None:
            return cached

        rows = await db.execute("SELECT * FROM groups WHERE chat_id = ?;", (chat_id,))
        if not rows:
            await db.execute(
                "INSERT OR IGNORE INTO groups (chat_id, title) VALUES (?, ?);",
                (chat_id, f"Group {chat_id}"),
            )
            rows = await db.execute("SELECT * FROM groups WHERE chat_id = ?;", (chat_id,))
        result = dict(rows[0])
        repo_cache.set("group", chat_id, result)
        return result

    @staticmethod
    async def update_group(chat_id: int, **group_settings: Any) -> None:
        """Update group settings dynamically with strict column whitelist validation."""
        if not group_settings:
            return

        invalid_cols = set(group_settings.keys()) - ALLOWED_GROUP_COLUMNS
        if invalid_cols:
            raise ValueError(f"Disallowed group setting columns: {sorted(invalid_cols)}")

        # Ensure group row exists before update
        await GroupRepository.get_group(chat_id)

        fields = []
        values = []
        for key, val in group_settings.items():
            fields.append(f"{key} = ?")
            values.append(val)
        values.append(chat_id)
        query = f"UPDATE groups SET {', '.join(fields)} WHERE chat_id = ?;"
        await db.execute(query, tuple(values))
        repo_cache.invalidate("group", chat_id)


class UserRepository:
    """Operations on users table."""

    @staticmethod
    async def get_or_create_user(
        user_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Insert or update user details, returning user record."""
        await db.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name) 
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET 
                username = COALESCE(?, username),
                first_name = COALESCE(?, first_name),
                last_name = COALESCE(?, last_name);
            """,
            (user_id, username, first_name, last_name, username, first_name, last_name),
        )
        rows = await db.execute("SELECT * FROM users WHERE user_id = ?;", (user_id,))
        return dict(rows[0])

    @staticmethod
    async def get_user_by_id(user_id: int) -> Optional[dict[str, Any]]:
        """Lookup user by Telegram user_id."""
        rows = await db.execute("SELECT * FROM users WHERE user_id = ?;", (user_id,))
        return dict(rows[0]) if rows else None

    @staticmethod
    async def get_user_by_username(username: str) -> Optional[dict[str, Any]]:
        """Case-insensitive lookup by Telegram username."""
        clean = username.lstrip("@").strip().lower()
        if not clean:
            return None
        rows = await db.execute(
            "SELECT * FROM users WHERE LOWER(username) = ?;", (clean,)
        )
        return dict(rows[0]) if rows else None


class GroupMemberRepository:
    """Operations on group_members table (role management)."""

    @staticmethod
    async def get_member_role(chat_id: int, user_id: int) -> str:
        """Get member's role in a group, default to 'member'."""
        cache_key = (chat_id, user_id)
        cached = repo_cache.get("member_role", cache_key)
        if cached is not None:
            return cached

        rows = await db.execute(
            "SELECT role FROM group_members WHERE chat_id = ? AND user_id = ?;",
            (chat_id, user_id),
        )
        role = rows[0]["role"] if rows else "member"
        repo_cache.set("member_role", cache_key, role)
        return role

    @staticmethod
    async def set_member_role(chat_id: int, user_id: int, role: str) -> None:
        """Set member role (e.g. owner, admin, moderator, trusted, member, muted, banned)."""
        async with db.transaction() as tx:
            await tx.execute(
                "INSERT OR IGNORE INTO groups (chat_id, title) VALUES (?, ?);",
                (chat_id, f"Group {chat_id}"),
            )
            await tx.execute(
                "INSERT OR IGNORE INTO users (user_id, first_name) VALUES (?, ?);",
                (user_id, f"User {user_id}"),
            )
            await tx.execute(
                """
                INSERT INTO group_members (chat_id, user_id, role) 
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET role = ?;
                """,
                (chat_id, user_id, role, role),
            )
        repo_cache.invalidate("member_role", (chat_id, user_id))


class WarningRepository:
    """Operations on warnings table with atomic transaction guarantees."""

    @staticmethod
    async def add_warning(
        chat_id: int, user_id: int, reason: str, moderator_id: int, severity: int = 1
    ) -> int:
        """Atomically insert warning and return total warning count for the user in this group."""
        async with db.transaction() as tx:
            await tx.execute(
                "INSERT OR IGNORE INTO groups (chat_id, title) VALUES (?, ?);",
                (chat_id, f"Group {chat_id}"),
            )
            await tx.execute(
                "INSERT OR IGNORE INTO users (user_id, first_name) VALUES (?, ?);",
                (user_id, f"User {user_id}"),
            )
            await tx.execute(
                """
                INSERT INTO warnings (chat_id, user_id, reason, moderator_id, severity) 
                VALUES (?, ?, ?, ?, ?);
                """,
                (chat_id, user_id, reason, moderator_id, severity),
            )
            rows = await tx.execute(
                "SELECT COUNT(*) as cnt FROM warnings WHERE chat_id = ? AND user_id = ?;",
                (chat_id, user_id),
            )
            return int(rows[0]["cnt"]) if rows else 0

    @staticmethod
    async def get_warnings_count(chat_id: int, user_id: int) -> int:
        """Get warnings count for user in chat."""
        rows = await db.execute(
            "SELECT COUNT(*) as cnt FROM warnings WHERE chat_id = ? AND user_id = ?;",
            (chat_id, user_id),
        )
        return int(rows[0]["cnt"]) if rows else 0

    @staticmethod
    async def get_warnings(chat_id: int, user_id: int) -> list[dict[str, Any]]:
        """Get detailed warnings for user in chat."""
        rows = await db.execute(
            "SELECT * FROM warnings WHERE chat_id = ? AND user_id = ? ORDER BY timestamp DESC, id DESC;",
            (chat_id, user_id),
        )
        return [dict(r) for r in rows]

    @staticmethod
    async def clear_warnings(chat_id: int, user_id: int) -> None:
        """Clear all warnings for user in chat."""
        await db.execute(
            "DELETE FROM warnings WHERE chat_id = ? AND user_id = ?;", (chat_id, user_id)
        )

    @staticmethod
    async def remove_last_warning(chat_id: int, user_id: int) -> bool:
        """Atomically remove most recent warning. Return True if warning was removed."""
        async with db.transaction() as tx:
            rows = await tx.execute(
                "SELECT id FROM warnings WHERE chat_id = ? AND user_id = ? ORDER BY timestamp DESC, id DESC LIMIT 1;",
                (chat_id, user_id),
            )
            if not rows:
                return False
            warning_id = rows[0]["id"]
            await tx.execute("DELETE FROM warnings WHERE id = ?;", (warning_id,))
            return True


class PunishmentRepository:
    """Operations on punishments table."""

    @staticmethod
    async def add_punishment(
        chat_id: int,
        user_id: int,
        action: str,
        reason: str,
        moderator_id: int,
        duration_seconds: Optional[int] = None,
    ) -> dict[str, Any]:
        """Atomically record punishment and compute expiry."""
        expires_at = None
        if duration_seconds:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
            ).isoformat()

        async with db.transaction() as tx:
            await tx.execute(
                "INSERT OR IGNORE INTO groups (chat_id, title) VALUES (?, ?);",
                (chat_id, f"Group {chat_id}"),
            )
            await tx.execute(
                "INSERT OR IGNORE INTO users (user_id, first_name) VALUES (?, ?);",
                (user_id, f"User {user_id}"),
            )
            await tx.execute(
                "UPDATE punishments SET is_active = 0 WHERE chat_id = ? AND user_id = ? AND action = ? AND is_active = 1;",
                (chat_id, user_id, action),
            )
            await tx.execute(
                """
                INSERT INTO punishments (chat_id, user_id, action, reason, moderator_id, duration_seconds, expires_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1);
                """,
                (chat_id, user_id, action, reason, moderator_id, duration_seconds, expires_at),
            )
            rows = await tx.execute(
                "SELECT * FROM punishments WHERE chat_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1;",
                (chat_id, user_id),
            )
            return dict(rows[0])

    @staticmethod
    async def get_active_punishments(chat_id: int, user_id: int) -> list[dict[str, Any]]:
        """Get all active punishments for user."""
        rows = await db.execute(
            "SELECT * FROM punishments WHERE chat_id = ? AND user_id = ? AND is_active = 1;",
            (chat_id, user_id),
        )
        return [dict(r) for r in rows]

    @staticmethod
    async def revoke_active_punishments(chat_id: int, user_id: int, action: str) -> None:
        """Deactivate active punishments of specific type (e.g. unban/unmute)."""
        await db.execute(
            "UPDATE punishments SET is_active = 0 WHERE chat_id = ? AND user_id = ? AND action = ? AND is_active = 1;",
            (chat_id, user_id, action),
        )

    @staticmethod
    async def get_expired_punishments() -> list[dict[str, Any]]:
        """Get list of active punishments that have expired."""
        now_str = datetime.now(timezone.utc).isoformat()
        rows = await db.execute(
            "SELECT * FROM punishments WHERE is_active = 1 AND expires_at IS NOT NULL AND expires_at < ?;",
            (now_str,),
        )
        return [dict(r) for r in rows]


class CustomFilterRepository:
    """Operations on custom_filters table with read-through caching."""

    @staticmethod
    async def get_filters(chat_id: int) -> list[dict[str, Any]]:
        """Fetch all word filters for a group."""
        cached = repo_cache.get("filters", chat_id)
        if cached is not None:
            return cached

        rows = await db.execute(
            "SELECT * FROM custom_filters WHERE chat_id = ? ORDER BY created_at DESC, id DESC;",
            (chat_id,),
        )
        result = [dict(r) for r in rows]
        repo_cache.set("filters", chat_id, result)
        return result

    @staticmethod
    async def add_filter(
        chat_id: int, pattern: str, match_type: str = "exact", action: str = "delete"
    ) -> None:
        """Add custom word filter."""
        await GroupRepository.get_group(chat_id)
        await db.execute(
            "INSERT INTO custom_filters (chat_id, pattern, match_type, action) VALUES (?, ?, ?, ?);",
            (chat_id, pattern.strip(), match_type, action),
        )
        repo_cache.invalidate("filters", chat_id)

    @staticmethod
    async def delete_filter(chat_id: int, filter_id: int) -> bool:
        """Delete custom word filter by ID."""
        rows = await db.execute(
            "SELECT 1 FROM custom_filters WHERE id = ? AND chat_id = ?;",
            (filter_id, chat_id),
        )
        if not rows:
            return False
        await db.execute("DELETE FROM custom_filters WHERE id = ?;", (filter_id,))
        repo_cache.invalidate("filters", chat_id)
        return True


class NoteRepository:
    """Operations on notes table."""

    @staticmethod
    async def get_note(chat_id: int, trigger: str) -> Optional[str]:
        """Get note content by trigger name."""
        rows = await db.execute(
            "SELECT content FROM notes WHERE chat_id = ? AND trigger = ?;",
            (chat_id, trigger.strip().lower()),
        )
        return rows[0]["content"] if rows else None

    @staticmethod
    async def add_note(chat_id: int, trigger: str, content: str) -> None:
        """Create or update note."""
        await GroupRepository.get_group(chat_id)
        await db.execute(
            """
            INSERT INTO notes (chat_id, trigger, content) VALUES (?, ?, ?)
            ON CONFLICT(chat_id, trigger) DO UPDATE SET content = ?;
            """,
            (chat_id, trigger.strip().lower(), content, content),
        )

    @staticmethod
    async def delete_note(chat_id: int, trigger: str) -> bool:
        """Delete note by trigger name."""
        rows = await db.execute(
            "SELECT 1 FROM notes WHERE chat_id = ? AND trigger = ?;",
            (chat_id, trigger.strip().lower()),
        )
        if not rows:
            return False
        await db.execute(
            "DELETE FROM notes WHERE chat_id = ? AND trigger = ?;",
            (chat_id, trigger.strip().lower()),
        )
        return True

    @staticmethod
    async def list_notes(chat_id: int) -> list[str]:
        """List all triggers in group."""
        rows = await db.execute(
            "SELECT trigger FROM notes WHERE chat_id = ? ORDER BY trigger ASC;", (chat_id,)
        )
        return [r["trigger"] for r in rows]


class WhitelistedWordsRepository:
    """Operations on whitelisted_words table with read-through caching."""

    @staticmethod
    async def get_whitelist(chat_id: int) -> set[str]:
        """Fetch all whitelisted words for a group."""
        cached = repo_cache.get("whitelist", chat_id)
        if cached is not None:
            return cached

        rows = await db.execute(
            "SELECT word FROM whitelisted_words WHERE chat_id = ?;", (chat_id,)
        )
        result: Set[str] = {r["word"] for r in rows}
        repo_cache.set("whitelist", chat_id, result)
        return result

    @staticmethod
    async def add_whitelist(chat_id: int, word: str) -> None:
        """Add word to group whitelist."""
        await GroupRepository.get_group(chat_id)
        await db.execute(
            "INSERT OR IGNORE INTO whitelisted_words (chat_id, word) VALUES (?, ?);",
            (chat_id, word.strip().lower()),
        )
        repo_cache.invalidate("whitelist", chat_id)

    @staticmethod
    async def remove_whitelist(chat_id: int, word: str) -> bool:
        """Remove word from group whitelist."""
        rows = await db.execute(
            "SELECT 1 FROM whitelisted_words WHERE chat_id = ? AND word = ?;",
            (chat_id, word.strip().lower()),
        )
        if not rows:
            return False
        await db.execute(
            "DELETE FROM whitelisted_words WHERE chat_id = ? AND word = ?;",
            (chat_id, word.strip().lower()),
        )
        repo_cache.invalidate("whitelist", chat_id)
        return True


class AuditLogRepository:
    """Operations on audit_logs table for enterprise compliance and traceability."""

    @staticmethod
    async def log_event(
        chat_id: int,
        actor_id: int,
        target_id: int,
        action: str,
        details: str = "",
    ) -> None:
        """Insert a structured audit log entry."""
        await db.execute(
            """
            INSERT INTO audit_logs (chat_id, actor_id, target_id, action, details)
            VALUES (?, ?, ?, ?, ?);
            """,
            (chat_id, actor_id, target_id, action, details),
        )

    @staticmethod
    async def get_recent_logs(chat_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        """Retrieve recent audit events for a group."""
        rows = await db.execute(
            "SELECT * FROM audit_logs WHERE chat_id = ? ORDER BY timestamp DESC, id DESC LIMIT ?;",
            (chat_id, limit),
        )
        return [dict(r) for r in rows]
