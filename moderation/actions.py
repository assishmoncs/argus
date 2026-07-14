"""
Core moderation actions: ban, mute, kick, warn, and automated escalation logic.
"""

import asyncio
import logging
import time
from typing import Any, Optional
from datetime import datetime, timezone
from aiogram import Bot, types
from aiogram.types import ChatPermissions
from database.models import (
    GroupRepository,
    WarningRepository,
    PunishmentRepository,
    GroupMemberRepository,
)

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


def can_moderate(mod_role: str, target_role: str) -> bool:
    """Return True if moderator role has higher privilege than target role."""
    return ROLE_VALUES.get(mod_role, 10) > ROLE_VALUES.get(target_role, 10)


async def auto_delete_message(message: types.Message, delay: int) -> None:
    """Delete message after delay to reduce chat clutter."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass


class ModerationActions:
    """Orchestrates moderation actions and safety checks."""

    @staticmethod
    async def warn(
        chat_id: int,
        user: types.User,
        reason: str,
        moderator_id: int,
        bot: Bot,
        silent: bool = False,
    ) -> Optional[str]:
        """Issue a warning, incrementing database count, and escalate if needed."""
        # Privilege check
        mod_role = await GroupMemberRepository.get_member_role(chat_id, moderator_id)
        target_role = await GroupMemberRepository.get_member_role(chat_id, user.id)
        if moderator_id != 0 and not can_moderate(mod_role, target_role):
            return "❌ Privilege Escalation Denied: You cannot warn someone with equal or higher permissions."

        # Add warning to DB
        count = await WarningRepository.add_warning(chat_id, user.id, reason, moderator_id)
        logger.info("Issued warning %d to user %d in chat %d. Reason: %s", count, user.id, chat_id, reason)

        # Retrieve group warning limit
        group_settings = await GroupRepository.get_group(chat_id)
        limit = group_settings.get("warning_limit", 3)

        if not silent:
            msg_text = f"⚠️ **{user.first_name}** has been warned ({count}/{limit}).\nReason: {reason}"
            sent_msg = await bot.send_message(chat_id, msg_text)
            # Auto delete warn notification in 30 seconds
            asyncio.create_task(auto_delete_message(sent_msg, 30))

        # Escalation check
        if count >= limit:
            escalation_reason = f"Exceeded warning limit ({count}/{limit})"
            logger.info("Escalating user %d in chat %d due to warning limit.", user.id, chat_id)
            if limit >= 5:
                # Ban
                await ModerationActions.ban(chat_id, user, None, escalation_reason, 0, bot)
            else:
                # Temp Mute for 1 day
                await ModerationActions.mute(chat_id, user, 86400, escalation_reason, 0, bot)
            
            # Reset warnings after escalation
            await WarningRepository.clear_warnings(chat_id, user.id)

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

        removed = await WarningRepository.remove_last_warning(chat_id, user.id)
        if not removed:
            return "❌ User has no active warnings to remove."

        count = await WarningRepository.get_warnings_count(chat_id, user.id)
        if not silent:
            await bot.send_message(
                chat_id,
                f"✅ Last warning removed for **{user.first_name}** (Current warnings: {count})."
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

        # Perform restrict member call (empty permissions = mute)
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
            logger.error("Telegram API restrict failed: %s", exc)
            return f"❌ Failed to restrict chat member on Telegram: {exc}"

        # Write to punishments table
        await PunishmentRepository.add_punishment(
            chat_id, user.id, "mute", reason, moderator_id, duration_seconds
        )
        await GroupMemberRepository.set_member_role(chat_id, user.id, "muted")

        if not silent:
            dur_str = f"for {duration_seconds}s" if duration_seconds else "permanently"
            await bot.send_message(
                chat_id,
                f"🔇 **{user.first_name}** has been muted {dur_str}.\nReason: {reason}"
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
            logger.error("Telegram API unmute failed: %s", exc)
            return f"❌ Failed to restore permissions on Telegram: {exc}"

        await PunishmentRepository.revoke_active_punishments(chat_id, user.id, "mute")
        await GroupMemberRepository.set_member_role(chat_id, user.id, "member")

        if not silent:
            await bot.send_message(chat_id, f"🔊 **{user.first_name}** has been unmuted.")
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
            # If duration is provided, we pass the unix timestamp for ban expiration
            until_date = None
            if duration_seconds:
                until_date = int(time.time() + duration_seconds)
            await bot.ban_chat_member(chat_id=chat_id, user_id=user.id, until_date=until_date)
        except Exception as exc:
            logger.error("Telegram API ban failed: %s", exc)
            return f"❌ Failed to ban user on Telegram: {exc}"

        await PunishmentRepository.add_punishment(
            chat_id, user.id, "ban", reason, moderator_id, duration_seconds
        )
        await GroupMemberRepository.set_member_role(chat_id, user.id, "banned")

        if not silent:
            dur_str = f"for {duration_seconds}s" if duration_seconds else "permanently"
            await bot.send_message(
                chat_id,
                f"🚫 **{user.first_name}** has been banned {dur_str}.\nReason: {reason}"
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
            logger.error("Telegram API unban failed: %s", exc)
            return f"❌ Failed to unban user on Telegram: {exc}"

        await PunishmentRepository.revoke_active_punishments(chat_id, user.id, "ban")
        await GroupMemberRepository.set_member_role(chat_id, user.id, "member")

        if not silent:
            await bot.send_message(chat_id, f"✅ **{user.first_name}** has been unbanned.")
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
            # Ban
            await bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
            # Unban immediately to let them return later
            await bot.unban_chat_member(chat_id=chat_id, user_id=user.id)
        except Exception as exc:
            logger.error("Telegram API kick failed: %s", exc)
            return f"❌ Failed to kick user: {exc}"

        await PunishmentRepository.add_punishment(
            chat_id, user.id, "kick", reason, moderator_id
        )

        if not silent:
            await bot.send_message(
                chat_id,
                f"👢 **{user.first_name}** has been kicked.\nReason: {reason}"
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
            # Ban (which deletes recent messages automatically on Telegram)
            await bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
            # Unban
            await bot.unban_chat_member(chat_id=chat_id, user_id=user.id)
        except Exception as exc:
            logger.error("Telegram API softban failed: %s", exc)
            return f"❌ Failed to softban user: {exc}"

        await PunishmentRepository.add_punishment(
            chat_id, user.id, "softban", reason, moderator_id
        )

        if not silent:
            await bot.send_message(
                chat_id,
                f"🧹 **{user.first_name}** has been softbanned (kicked and messages cleared).\nReason: {reason}"
            )
        return None
