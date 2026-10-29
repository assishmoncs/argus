"""
Core moderation actions: ban, mute, kick, warn, and automated escalation logic.
Enhanced with per-user concurrency locks, tracked background tasks, audit logging,
and resilient Telegram API error handling.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, Coroutine, Optional, Set, Tuple
from aiogram import Bot, types
from aiogram.types import ChatPermissions

from database.models import (
    AuditLogRepository,
    GroupMemberRepository,
    GroupRepository,
    PunishmentRepository,
    WarningRepository,
)
from utils.telemetry import telemetry

logger = logging.getLogger("argus.moderation")

# Role hierarchy value for privilege checking
ROLE_VALUES = {
    "owner": 100,
    "admin": 80,
    "moderator": 50,
    "trusted": 30,
    "member": 10,
    "muted": 5,
    "banned": 0,
}


class BackgroundTaskManager:
    """Tracks fire-and-forget background tasks and cancels them cleanly on shutdown."""

    def __init__(self) -> None:
        self._tasks: Set[asyncio.Task[Any]] = set()

    def create_task(
        self, coro: Coroutine[Any, Any, Any], name: Optional[str] = None
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        telemetry.set_gauge("active_background_tasks", len(self._tasks))

        def _on_done(t: asyncio.Task[Any]) -> None:
            self._tasks.discard(t)
            telemetry.set_gauge("active_background_tasks", len(self._tasks))
            if not t.cancelled():
                exc = t.exception()
                if exc:
                    logger.warning("Background task %s failed: %s", t.get_name(), exc)

        task.add_done_callback(_on_done)
        return task

    async def cancel_all(self, timeout: float = 5.0) -> None:
        if not self._tasks:
            return
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.wait(tasks, timeout=timeout)
        self._tasks.clear()
        telemetry.set_gauge("active_background_tasks", 0)


task_manager = BackgroundTaskManager()

# Bounded per-(chat_id, user_id) lock map to prevent concurrent warning escalation races
_user_mod_locks: OrderedDict[Tuple[int, int], asyncio.Lock] = OrderedDict()
_MAX_MOD_LOCKS = 2048


def _get_user_mod_lock(chat_id: int, user_id: int) -> asyncio.Lock:
    key = (chat_id, user_id)
    lock = _user_mod_locks.get(key)
    if lock is not None:
        _user_mod_locks.move_to_end(key)
        return lock
    lock = asyncio.Lock()
    _user_mod_locks[key] = lock
    while len(_user_mod_locks) > _MAX_MOD_LOCKS:
        _user_mod_locks.popitem(last=False)
    return lock


def can_moderate(mod_role: str, target_role: str) -> bool:
    """Return True if moderator role has higher privilege than target role."""
    return ROLE_VALUES.get(mod_role, 10) > ROLE_VALUES.get(target_role, 10)


async def auto_delete_message(message: types.Message, delay: int) -> None:
    """Delete message after delay to reduce chat clutter."""
    try:
        await asyncio.sleep(delay)
        await message.delete()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _safe_send_notification(bot: Bot, chat_id: int, text: str) -> Optional[types.Message]:
    """Send a chat notification without failing the parent moderation action on network error."""
    try:
        return await bot.send_message(chat_id, text)
    except Exception as exc:
        logger.warning("Failed to send moderation notification to chat %d: %s", chat_id, exc)
        return None


async def _safe_audit_log(
    chat_id: int, actor_id: int, target_id: int, action: str, details: str
) -> None:
    """Write audit log without failing moderation flow if audit write encounters an error."""
    try:
        await AuditLogRepository.log_event(chat_id, actor_id, target_id, action, details)
    except Exception as exc:
        logger.debug("Audit log write skipped: %s", exc)


class ModerationActions:
    """Orchestrates moderation actions, privilege verification, and escalation logic."""

    @staticmethod
    async def warn(
        chat_id: int,
        user: types.User,
        reason: str,
        moderator_id: int,
        bot: Bot,
        silent: bool = False,
    ) -> Optional[str]:
        """Issue a warning, incrementing database count atomically, and escalate if needed."""
        mod_role = await GroupMemberRepository.get_member_role(chat_id, moderator_id)
        target_role = await GroupMemberRepository.get_member_role(chat_id, user.id)
        if moderator_id != 0 and not can_moderate(mod_role, target_role):
            return "❌ Privilege Escalation Denied: You cannot warn someone with equal or higher permissions."

        user_lock = _get_user_mod_lock(chat_id, user.id)
        should_escalate = False
        limit = 3
        count = 0

        async with user_lock:
            count = await WarningRepository.add_warning(chat_id, user.id, reason, moderator_id)
            telemetry.increment("moderation_actions_total", action="warn")
            logger.info(
                "Issued warning %d to user %d in chat %d. Reason: %s",
                count,
                user.id,
                chat_id,
                reason,
            )
            await _safe_audit_log(chat_id, moderator_id, user.id, "warn", f"({count}) {reason}")

            group_settings = await GroupRepository.get_group(chat_id)
            limit = int(group_settings.get("warning_limit", 3))
            if count >= limit:
                should_escalate = True
                await WarningRepository.clear_warnings(chat_id, user.id)

        if not silent:
            msg_text = (
                f"⚠️ **{user.first_name}** has been warned ({count}/{limit}).\nReason: {reason}"
            )
            sent_msg = await _safe_send_notification(bot, chat_id, msg_text)
            if sent_msg is not None:
                task_manager.create_task(
                    auto_delete_message(sent_msg, 30),
                    name=f"autodel_warn_{chat_id}_{user.id}",
                )

        if should_escalate:
            escalation_reason = f"Exceeded warning limit ({count}/{limit})"
            logger.info("Escalating user %d in chat %d due to warning limit.", user.id, chat_id)
            telemetry.increment("moderation_actions_total", action="escalation")
            if limit >= 5:
                await ModerationActions.ban(chat_id, user, None, escalation_reason, 0, bot)
            else:
                await ModerationActions.mute(chat_id, user, 86400, escalation_reason, 0, bot)

        return None

    @staticmethod
    async def unwarn(
        chat_id: int,
        user: types.User,
        moderator_id: int,
        bot: Bot,
        silent: bool = False,
    ) -> Optional[str]:
        """Remove the last warning from user."""
        mod_role = await GroupMemberRepository.get_member_role(chat_id, moderator_id)
        target_role = await GroupMemberRepository.get_member_role(chat_id, user.id)
        if moderator_id != 0 and not can_moderate(mod_role, target_role):
            return "❌ Privilege Escalation Denied: You cannot unwarn someone with equal or higher permissions."

        user_lock = _get_user_mod_lock(chat_id, user.id)
        async with user_lock:
            removed = await WarningRepository.remove_last_warning(chat_id, user.id)
            if not removed:
                return "❌ User has no active warnings to remove."
            count = await WarningRepository.get_warnings_count(chat_id, user.id)

        telemetry.increment("moderation_actions_total", action="unwarn")
        await _safe_audit_log(chat_id, moderator_id, user.id, "unwarn", f"Remaining: {count}")

        if not silent:
            await _safe_send_notification(
                bot,
                chat_id,
                f"✅ Last warning removed for **{user.first_name}** (Current warnings: {count}).",
            )
        return None

    @staticmethod
    async def mute(
        chat_id: int,
        user: types.User,
        duration_seconds: Optional[int],
        reason: str,
        moderator_id: int,
        bot: Bot,
        silent: bool = False,
    ) -> Optional[str]:
        """Mute a user on Telegram and log it in the database."""
        mod_role = await GroupMemberRepository.get_member_role(chat_id, moderator_id)
        target_role = await GroupMemberRepository.get_member_role(chat_id, user.id)
        if moderator_id != 0 and not can_moderate(mod_role, target_role):
            return "❌ Privilege Escalation Denied: You cannot mute someone with equal or higher permissions."

        mute_permissions = ChatPermissions(
            can_send_messages=False,
            can_send_media_messages=False,
            can_send_polls=False,
            can_send_other_messages=False,
            can_add_web_page_previews=False,
        )

        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user.id,
                permissions=mute_permissions,
            )
        except Exception as exc:
            telemetry.increment("moderation_errors_total", action="mute")
            logger.error("Telegram API restrict failed: %s", exc)
            return f"❌ Failed to restrict chat member on Telegram: {exc}"

        await PunishmentRepository.add_punishment(
            chat_id, user.id, "mute", reason, moderator_id, duration_seconds
        )
        await GroupMemberRepository.set_member_role(chat_id, user.id, "muted")
        telemetry.increment("moderation_actions_total", action="mute")
        await _safe_audit_log(chat_id, moderator_id, user.id, "mute", reason)

        if not silent:
            dur_str = f"for {duration_seconds}s" if duration_seconds else "permanently"
            await _safe_send_notification(
                bot,
                chat_id,
                f"🔇 **{user.first_name}** has been muted {dur_str}.\nReason: {reason}",
            )
        return None

    @staticmethod
    async def unmute(
        chat_id: int,
        user: types.User,
        moderator_id: int,
        bot: Bot,
        silent: bool = False,
    ) -> Optional[str]:
        """Unmute a user on Telegram."""
        mod_role = await GroupMemberRepository.get_member_role(chat_id, moderator_id)
        target_role = await GroupMemberRepository.get_member_role(chat_id, user.id)
        if moderator_id != 0 and not can_moderate(mod_role, target_role):
            return "❌ Privilege Escalation Denied: You cannot unmute someone with equal or higher permissions."

        unmute_permissions = ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_invite_users=True,
        )

        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user.id,
                permissions=unmute_permissions,
            )
        except Exception as exc:
            telemetry.increment("moderation_errors_total", action="unmute")
            logger.error("Telegram API unmute failed: %s", exc)
            return f"❌ Failed to restore permissions on Telegram: {exc}"

        await PunishmentRepository.revoke_active_punishments(chat_id, user.id, "mute")
        await GroupMemberRepository.set_member_role(chat_id, user.id, "member")
        telemetry.increment("moderation_actions_total", action="unmute")
        await _safe_audit_log(chat_id, moderator_id, user.id, "unmute", "Manual unmute")

        if not silent:
            await _safe_send_notification(
                bot, chat_id, f"🔊 **{user.first_name}** has been unmuted."
            )
        return None

    @staticmethod
    async def ban(
        chat_id: int,
        user: types.User,
        duration_seconds: Optional[int],
        reason: str,
        moderator_id: int,
        bot: Bot,
        silent: bool = False,
    ) -> Optional[str]:
        """Ban a user on Telegram and log it."""
        mod_role = await GroupMemberRepository.get_member_role(chat_id, moderator_id)
        target_role = await GroupMemberRepository.get_member_role(chat_id, user.id)
        if moderator_id != 0 and not can_moderate(mod_role, target_role):
            return "❌ Privilege Escalation Denied: You cannot ban someone with equal or higher permissions."

        try:
            until_date = int(time.time() + duration_seconds) if duration_seconds else None
            await bot.ban_chat_member(chat_id=chat_id, user_id=user.id, until_date=until_date)
        except Exception as exc:
            telemetry.increment("moderation_errors_total", action="ban")
            logger.error("Telegram API ban failed: %s", exc)
            return f"❌ Failed to ban user on Telegram: {exc}"

        await PunishmentRepository.add_punishment(
            chat_id, user.id, "ban", reason, moderator_id, duration_seconds
        )
        await GroupMemberRepository.set_member_role(chat_id, user.id, "banned")
        telemetry.increment("moderation_actions_total", action="ban")
        await _safe_audit_log(chat_id, moderator_id, user.id, "ban", reason)

        if not silent:
            dur_str = f"for {duration_seconds}s" if duration_seconds else "permanently"
            await _safe_send_notification(
                bot,
                chat_id,
                f"🚫 **{user.first_name}** has been banned {dur_str}.\nReason: {reason}",
            )
        return None

    @staticmethod
    async def unban(
        chat_id: int,
        user: types.User,
        moderator_id: int,
        bot: Bot,
        silent: bool = False,
    ) -> Optional[str]:
        """Unban a user on Telegram."""
        mod_role = await GroupMemberRepository.get_member_role(chat_id, moderator_id)
        target_role = await GroupMemberRepository.get_member_role(chat_id, user.id)
        if moderator_id != 0 and not can_moderate(mod_role, target_role):
            return "❌ Privilege Escalation Denied: You cannot unban someone with equal or higher permissions."

        try:
            await bot.unban_chat_member(chat_id=chat_id, user_id=user.id, only_if_banned=True)
        except Exception as exc:
            telemetry.increment("moderation_errors_total", action="unban")
            logger.error("Telegram API unban failed: %s", exc)
            return f"❌ Failed to unban user on Telegram: {exc}"

        await PunishmentRepository.revoke_active_punishments(chat_id, user.id, "ban")
        await GroupMemberRepository.set_member_role(chat_id, user.id, "member")
        telemetry.increment("moderation_actions_total", action="unban")
        await _safe_audit_log(chat_id, moderator_id, user.id, "unban", "Manual unban")

        if not silent:
            await _safe_send_notification(
                bot, chat_id, f"✅ **{user.first_name}** has been unbanned."
            )
        return None

    @staticmethod
    async def kick(
        chat_id: int,
        user: types.User,
        reason: str,
        moderator_id: int,
        bot: Bot,
        silent: bool = False,
    ) -> Optional[str]:
        """Kick a user on Telegram (ban and then unban immediately)."""
        mod_role = await GroupMemberRepository.get_member_role(chat_id, moderator_id)
        target_role = await GroupMemberRepository.get_member_role(chat_id, user.id)
        if moderator_id != 0 and not can_moderate(mod_role, target_role):
            return "❌ Privilege Escalation Denied: You cannot kick someone with equal or higher permissions."

        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
            await bot.unban_chat_member(chat_id=chat_id, user_id=user.id)
        except Exception as exc:
            telemetry.increment("moderation_errors_total", action="kick")
            logger.error("Telegram API kick failed: %s", exc)
            return f"❌ Failed to kick user: {exc}"

        await PunishmentRepository.add_punishment(
            chat_id, user.id, "kick", reason, moderator_id
        )
        telemetry.increment("moderation_actions_total", action="kick")
        await _safe_audit_log(chat_id, moderator_id, user.id, "kick", reason)

        if not silent:
            await _safe_send_notification(
                bot,
                chat_id,
                f"👢 **{user.first_name}** has been kicked.\nReason: {reason}",
            )
        return None

    @staticmethod
    async def softban(
        chat_id: int,
        user: types.User,
        reason: str,
        moderator_id: int,
        bot: Bot,
        silent: bool = False,
    ) -> Optional[str]:
        """Soft-ban a user: ban them, which deletes recent messages, then unban them."""
        mod_role = await GroupMemberRepository.get_member_role(chat_id, moderator_id)
        target_role = await GroupMemberRepository.get_member_role(chat_id, user.id)
        if moderator_id != 0 and not can_moderate(mod_role, target_role):
            return "❌ Privilege Escalation Denied: You cannot softban someone with equal or higher permissions."

        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
            await bot.unban_chat_member(chat_id=chat_id, user_id=user.id)
        except Exception as exc:
            telemetry.increment("moderation_errors_total", action="softban")
            logger.error("Telegram API softban failed: %s", exc)
            return f"❌ Failed to softban user: {exc}"

        await PunishmentRepository.add_punishment(
            chat_id, user.id, "softban", reason, moderator_id
        )
        telemetry.increment("moderation_actions_total", action="softban")
        await _safe_audit_log(chat_id, moderator_id, user.id, "softban", reason)

        if not silent:
            await _safe_send_notification(
                bot,
                chat_id,
                f"🧹 **{user.first_name}** has been softbanned (kicked and messages cleared).\nReason: {reason}",
            )
        return None
