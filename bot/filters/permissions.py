"""
Custom filters for checking user permissions and roles.
"""

from aiogram.filters import BaseFilter
from aiogram.types import Message
from aiogram.enums import ChatMemberStatus
from database.models import GroupMemberRepository
from moderation.actions import ROLE_VALUES

import logging

logger = logging.getLogger("argus.permissions")


class RoleFilter(BaseFilter):
    """Filter that checks if a user has a required role or higher in the group."""

    def __init__(self, required_role: str) -> None:
        self.required_role = required_role.lower()

    async def __call__(self, message: Message) -> bool:
        chat = message.chat
        user = message.from_user
        if not user or chat.type not in ("group", "supergroup"):
            # Commands only work in groups
            return False

        # Creator/Administrators are always bypassed or mapped
        try:
            member = await message.bot.get_chat_member(chat.id, user.id)
            if member.status == ChatMemberStatus.CREATOR:
                await GroupMemberRepository.set_member_role(chat.id, user.id, "owner")
            elif member.status == ChatMemberStatus.ADMINISTRATOR:
                # If they are currently recorded with lower permissions, sync them to admin
                role = await GroupMemberRepository.get_member_role(chat.id, user.id)
                if ROLE_VALUES.get(role, 10) < ROLE_VALUES["admin"]:
                    await GroupMemberRepository.set_member_role(chat.id, user.id, "admin")
        except Exception as exc:
            logger.warning("Could not check Telegram chat member status for user %d: %s", user.id, exc)

        # Get role from database (re-fetch to get updated role after sync)
        role = await GroupMemberRepository.get_member_role(chat.id, user.id)
        
        user_value = ROLE_VALUES.get(role, 10)
        required_value = ROLE_VALUES.get(self.required_role, 10)

        if user_value >= required_value:
            return True

        # Log denial
        logger.info("Denied access to command %s for user %d in chat %d (has role %s, needs %s)",
                    message.text, user.id, chat.id, role, self.required_role)
        
        await message.reply(
            f"❌ **Permission Denied**\n"
            f"This command requires **{self.required_role.capitalize()}** privilege or higher.\n"
            f"Your current role: **{role.capitalize()}**."
        )
        return False
