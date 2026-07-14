"""
Database models and repository patterns for Argus.
"""

from typing import Any, Optional
from datetime import datetime, timezone, timedelta
from database.connection import db


class GroupRepository:
    """Operations on groups table."""

    @staticmethod
    async def get_group(chat_id: int) -> dict[str, Any]:
        """Fetch group settings, creating default ones if not present."""
        rows = await db.execute("SELECT * FROM groups WHERE chat_id = ?;", (chat_id,))
        if not rows:
            # Create default settings
            await db.execute(
                "INSERT OR IGNORE INTO groups (chat_id, title) VALUES (?, ?);",
                (chat_id, f"Group {chat_id}"),
            )
            rows = await db.execute("SELECT * FROM groups WHERE chat_id = ?;", (chat_id,))
        return dict(rows[0])

    @staticmethod
    async def update_group(chat_id: int, **settings: Any) -> None:
        """Update group settings dynamically."""
        if not settings:
            return
        fields = []
        values = []
        for key, val in settings.items():
            fields.append(f"{key} = ?")
            values.append(val)
        values.append(chat_id)
        query = f"UPDATE groups SET {', '.join(fields)} WHERE chat_id = ?;"
        await db.execute(query, tuple(values))


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


class GroupMemberRepository:
    """Operations on group_members table (role management)."""

    @staticmethod
    async def get_member_role(chat_id: int, user_id: int) -> str:
        """Get member's role in a group, default to 'member'."""
        rows = await db.execute(
            "SELECT role FROM group_members WHERE chat_id = ? AND user_id = ?;",
            (chat_id, user_id),
        )
        if not rows:
            return "member"
        return rows[0]["role"]

    @staticmethod
    async def set_member_role(chat_id: int, user_id: int, role: str) -> None:
        """Set member role (e.g. admin, moderator, banned)."""
        # Ensure user exists in users table
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, first_name) VALUES (?, ?);",
            (user_id, f"User {user_id}"),
        )
        # Ensure group exists
        await GroupRepository.get_group(chat_id)

        await db.execute(
            """
            INSERT INTO group_members (chat_id, user_id, role) 
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET role = ?;
            """,
            (chat_id, user_id, role, role),
        )


class WarningRepository:
    """Operations on warnings table."""

    @staticmethod
    async def add_warning(
        chat_id: int, user_id: int, reason: str, moderator_id: int, severity: int = 1
    ) -> int:
        """Insert warning and return total warning count for the user in this group."""
        # Ensure group & user exist
        await GroupRepository.get_group(chat_id)
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, first_name) VALUES (?, ?);",
            (user_id, f"User {user_id}"),
        )

        await db.execute(
            """
            INSERT INTO warnings (chat_id, user_id, reason, moderator_id, severity) 
            VALUES (?, ?, ?, ?, ?);
            """,
            (chat_id, user_id, reason, moderator_id, severity),
        )
        return await WarningRepository.get_warnings_count(chat_id, user_id)

    @staticmethod
    async def get_warnings_count(chat_id: int, user_id: int) -> int:
        """Get warnings count for user in chat."""
        rows = await db.execute(
            "SELECT COUNT(*) as cnt FROM warnings WHERE chat_id = ? AND user_id = ?;",
            (chat_id, user_id),
        )
        return rows[0]["cnt"] if rows else 0

    @staticmethod
    async def get_warnings(chat_id: int, user_id: int) -> list[dict[str, Any]]:
        """Get detailed warnings for user in chat."""
        rows = await db.execute(
            "SELECT * FROM warnings WHERE chat_id = ? AND user_id = ? ORDER BY timestamp DESC;",
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
        """Remove most recent warning. Return True if warning was removed."""
        rows = await db.execute(
            "SELECT id FROM warnings WHERE chat_id = ? AND user_id = ? ORDER BY timestamp DESC LIMIT 1;",
            (chat_id, user_id),
        )
        if not rows:
            return False
        warning_id = rows[0]["id"]
        await db.execute("DELETE FROM warnings WHERE id = ?;", (warning_id,))
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
        """Record punishment and compute expiry."""
        expires_at = None
        if duration_seconds:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
            ).isoformat()

        # Deactivate previous active punishments of this action
        await PunishmentRepository.revoke_active_punishments(chat_id, user_id, action)

        # Insert new punishment
        await db.execute(
            """
            INSERT INTO punishments (chat_id, user_id, action, reason, moderator_id, duration_seconds, expires_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1);
            """,
            (chat_id, user_id, action, reason, moderator_id, duration_seconds, expires_at),
        )
        rows = await db.execute(
            "SELECT * FROM punishments WHERE chat_id = ? AND user_id = ? ORDER BY timestamp DESC LIMIT 1;",
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
    """Operations on custom_filters table."""

    @staticmethod
    async def get_filters(chat_id: int) -> list[dict[str, Any]]:
        """Fetch all word filters for a group."""
        rows = await db.execute(
            "SELECT * FROM custom_filters WHERE chat_id = ? ORDER BY created_at DESC;",
            (chat_id,),
        )
        return [dict(r) for r in rows]

    @staticmethod
    async def add_filter(
        chat_id: int, pattern: str, match_type: str, action: str
    ) -> None:
        """Add custom word filter."""
        await GroupRepository.get_group(chat_id)
        await db.execute(
            "INSERT INTO custom_filters (chat_id, pattern, match_type, action) VALUES (?, ?, ?, ?);",
            (chat_id, pattern.strip(), match_type, action),
        )

    @staticmethod
    async def delete_filter(chat_id: int, filter_id: int) -> bool:
        """Delete custom word filter."""
        rows = await db.execute(
            "SELECT 1 FROM custom_filters WHERE id = ? AND chat_id = ?;",
            (filter_id, chat_id),
        )
        if not rows:
            return False
        await db.execute("DELETE FROM custom_filters WHERE id = ?;", (filter_id,))
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
    """Operations on whitelisted_words table."""

    @staticmethod
    async def get_whitelist(chat_id: int) -> set[str]:
        """Fetch all whitelisted words for a group."""
        rows = await db.execute(
            "SELECT word FROM whitelisted_words WHERE chat_id = ?;", (chat_id,)
        )
        return {r["word"] for r in rows}

    @staticmethod
    async def add_whitelist(chat_id: int, word: str) -> None:
        """Add word to group whitelist."""
        await GroupRepository.get_group(chat_id)
        await db.execute(
            "INSERT OR IGNORE INTO whitelisted_words (chat_id, word) VALUES (?, ?);",
            (chat_id, word.strip().lower()),
        )

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
        return True
