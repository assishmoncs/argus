"""
Custom filters and cached helpers for checking user permissions and roles.
"""

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Optional, Tuple

from aiogram.enums import ChatMemberStatus
from aiogram.filters import BaseFilter
from aiogram.types import Message

from config.settings import settings
from database.models import GroupMemberRepository
from moderation.actions import ROLE_VALUES
from utils.telemetry import telemetry

logger = logging.getLogger("argus.permissions")

# Thread-safe TTL cache for Telegram get_chat_member status lookups
# Key: (id(bot), chat_id, user_id) -> (expires_at, status)
_member_status_cache: OrderedDict[Tuple[int, int, int], Tuple[float, Any]] = OrderedDict()
_cache_lock = threading.RLock()
_MAX_STATUS_CACHE = 4096


def clear_permission_cache() -> None:
    """Clear the cached Telegram member statuses."""
    with _cache_lock:
        _member_status_cache.clear()


async def get_cached_chat_member_status(
    bot: Any, chat_id: int, user_id: int, ttl_seconds: Optional[float] = None
) -> Optional[Any]:
    """Fetch a user's Telegram ChatMemberStatus with short-lived TTL caching."""
    ttl = ttl_seconds if ttl_seconds is not None else settings.ADMIN_CACHE_TTL_SECONDS
    key = (id(bot), chat_id, user_id)
    now = time.monotonic()

    if ttl > 0:
        with _cache_lock:
            entry = _member_status_cache.get(key)
            if entry is not None:
                expires_at, status = entry
                if now < expires_at:
                    _member_status_cache.move_to_end(key)
                    return status
                del _member_status_cache[key]

    member = await bot.get_chat_member(chat_id, user_id)
    status = getattr(member, "status", None)

    if ttl > 0 and status is not None:
        with _cache_lock:
            _member_status_cache[key] = (now + ttl, status)
            while len(_member_status_cache) > _MAX_STATUS_CACHE:
                _member_status_cache.popitem(last=False)

    return status


async def is_user_chat_admin(bot: Any, chat_id: int, user_id: int) -> bool:
    """Return True if the user is a Telegram group administrator or creator."""
    try:
        status = await get_cached_chat_member_status(bot, chat_id, user_id)
        return status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception:
        return False


class RoleFilter(BaseFilter):
    """Filter that checks if a user has a required role or higher in the group."""

    def __init__(self, required_role: str) -> None:
        self.required_role = required_role.lower()

    async def __call__(self, message: Message) -> bool:
        chat = message.chat
        user = message.from_user
        if not user or not chat or chat.type not in ("group", "supergroup"):
            return False

        # Creator/Administrators are always synced to owner/admin roles
        try:
            if message.bot is not None:
                status = await get_cached_chat_member_status(message.bot, chat.id, user.id)
                if status == ChatMemberStatus.CREATOR:
                    await GroupMemberRepository.set_member_role(chat.id, user.id, "owner")
                elif status == ChatMemberStatus.ADMINISTRATOR:
                    role = await GroupMemberRepository.get_member_role(chat.id, user.id)
                    if ROLE_VALUES.get(role, 10) < ROLE_VALUES["admin"]:
                        await GroupMemberRepository.set_member_role(chat.id, user.id, "admin")
        except Exception as exc:
            logger.warning(
                "Could not check Telegram chat member status for user %d: %s",
                user.id,
                exc,
            )

        role = await GroupMemberRepository.get_member_role(chat.id, user.id)
        user_value = ROLE_VALUES.get(role, 10)
        required_value = ROLE_VALUES.get(self.required_role, 10)

        if user_value >= required_value:
            return True

        telemetry.increment("permission_denials_total", required_role=self.required_role)
        logger.info(
            "Denied access to command %s for user %d in chat %d (has role %s, needs %s)",
            message.text,
            user.id,
            chat.id,
            role,
            self.required_role,
        )

        try:
            await message.reply(
                f"❌ **Permission Denied**\n"
                f"This command requires **{self.required_role.capitalize()}** privilege or higher.\n"
                f"Your current role: **{role.capitalize()}**."
            )
        except Exception:
            pass
        return False
