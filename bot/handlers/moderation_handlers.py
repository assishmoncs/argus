"""
Command handlers for moderation actions (/warn, /unwarn, /warnings, /mute, /unmute, /tmute, /ban, /unban, /tban, /kick).
"""

import re
import logging
from typing import Optional, Tuple
from aiogram import Router, Bot, types
from aiogram.filters import Command

logger = logging.getLogger("argus.handlers.moderation")

from database.connection import db
from database.models import UserRepository, WarningRepository, GroupRepository
from bot.filters.permissions import RoleFilter
from moderation.actions import ModerationActions

router = Router()

# Regular expression to parse duration strings like 10m, 2h, 1d, 30s
DURATION_REGEX = re.compile(r"^(\d+)([smhd])$")


def parse_duration(duration_str: str) -> Optional[int]:
    """Parse a duration string (e.g., '10m', '2h') into seconds."""
    match = DURATION_REGEX.match(duration_str.strip().lower())
    if not match:
        return None
    val, unit = match.groups()
    val = int(val)
    if unit == "s":
        return val
    elif unit == "m":
        return val * 60
    elif unit == "h":
        return val * 3600
    elif unit == "d":
        return val * 86400
    return None


async def resolve_target_user(message: types.Message, args: list[str]) -> Tuple[Optional[types.User], str]:
    """
    Resolve the target user of a moderation command.
    Checks reply first, then database lookup by username.
    Returns (User, remaining_reason_text).
    """
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        reason = " ".join(args).strip()
        return target, reason or "No reason provided."

    if not args:
        return None, ""

    # Check if first argument is a username or user ID
    first_arg = args[0].strip()
    reason = " ".join(args[1:]).strip() or "No reason provided."

    # User ID lookup
    if first_arg.isdigit():
        user_id = int(first_arg)
        rows = await db.execute("SELECT * FROM users WHERE user_id = ?;", (user_id,))
        if rows:
            user = types.User(
                id=rows[0]["user_id"],
                is_bot=False,
                first_name=rows[0]["first_name"] or f"User {user_id}",
                username=rows[0]["username"],
            )
            return user, reason
        else:
            # Create a shell User object if not in DB
            user = types.User(id=user_id, is_bot=False, first_name=f"User {user_id}")
            return user, reason

    # Username lookup
    if first_arg.startswith("@"):
        username = first_arg[1:].lower()
        rows = await db.execute("SELECT * FROM users WHERE LOWER(username) = ?;", (username,))
        if rows:
            user = types.User(
                id=rows[0]["user_id"],
                is_bot=False,
                first_name=rows[0]["first_name"] or username,
                username=rows[0]["username"],
            )
            return user, reason

    return None, ""


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@router.message(Command("warn"), RoleFilter("moderator"))
async def cmd_warn(message: types.Message, bot: Bot):
    """Handle /warn command."""
    args = message.text.split()[1:]
    target, reason = await resolve_target_user(message, args)
    
    if not target:
        await message.reply("❌ **Usage**: Reply to a message with `/warn [reason]` or use `/warn @username [reason]`")
        return

    err = await ModerationActions.warn(message.chat.id, target, reason, message.from_user.id, bot)
    if err:
        await message.reply(err)


@router.message(Command("unwarn"), RoleFilter("moderator"))
async def cmd_unwarn(message: types.Message, bot: Bot):
    """Handle /unwarn command."""
    args = message.text.split()[1:]
    target, _ = await resolve_target_user(message, args)

    if not target:
        await message.reply("❌ **Usage**: Reply to a message with `/unwarn` or use `/unwarn @username`")
        return

    err = await ModerationActions.unwarn(message.chat.id, target, message.from_user.id, bot)
    if err:
        await message.reply(err)


@router.message(Command("warnings"))
async def cmd_warnings(message: types.Message):
    """Show warning count for a user."""
    args = message.text.split()[1:]
    
    # Non-moderators can only check their own warnings
    # Moderators can check anyone's
    target = message.from_user
    is_mod = False
    try:
        from database.models import GroupMemberRepository
        role = await GroupMemberRepository.get_member_role(message.chat.id, message.from_user.id)
        from moderation.actions import ROLE_VALUES
        is_mod = ROLE_VALUES.get(role, 10) >= ROLE_VALUES["moderator"]
    except Exception:
        pass

    if is_mod:
        resolved, _ = await resolve_target_user(message, args)
        if resolved:
            target = resolved

    count = await WarningRepository.get_warnings_count(message.chat.id, target.id)
    group_settings = await GroupRepository.get_group(message.chat.id)
    limit = group_settings.get("warning_limit", 3)

    await message.reply(
        f"⚠️ **{target.first_name}** has **{count}/{limit}** warnings in this chat."
    )


@router.message(Command("mute"), RoleFilter("moderator"))
async def cmd_mute(message: types.Message, bot: Bot):
    """Handle permanent /mute command."""
    args = message.text.split()[1:]
    target, reason = await resolve_target_user(message, args)

    if not target:
        await message.reply("❌ **Usage**: Reply to a message with `/mute [reason]` or use `/mute @username [reason]`")
        return

    err = await ModerationActions.mute(message.chat.id, target, None, reason, message.from_user.id, bot)
    if err:
        await message.reply(err)


@router.message(Command("tmute"), RoleFilter("moderator"))
async def cmd_tmute(message: types.Message, bot: Bot):
    """Handle temporary /tmute command."""
    args = message.text.split()[1:]
    
    # Parsing duration out of args
    if message.reply_to_message:
        if not args:
            await message.reply("❌ **Usage**: Reply to a message with `/tmute [duration] [reason]` (e.g. `30m`, `2h`)")
            return
        duration_str = args[0]
        reason = " ".join(args[1:]).strip() or "No reason provided."
        target = message.reply_to_message.from_user
    else:
        if len(args) < 2:
            await message.reply("❌ **Usage**: `/tmute @username [duration] [reason]` (e.g. `30m`, `2h`)")
            return
        target_arg = args[0]
        duration_str = args[1]
        reason = " ".join(args[2:]).strip() or "No reason provided."
        
        # Resolve target dummy mock
        resolved, _ = await resolve_target_user(message, [target_arg])
        target = resolved

    if not target:
        await message.reply("❌ User could not be resolved.")
        return

    duration = parse_duration(duration_str)
    if not duration:
        await message.reply("❌ **Invalid Duration format**: Use e.g. `30s`, `15m`, `2h`, `1d`")
        return

    err = await ModerationActions.mute(message.chat.id, target, duration, reason, message.from_user.id, bot)
    if err:
        await message.reply(err)


@router.message(Command("unmute"), RoleFilter("moderator"))
async def cmd_unmute(message: types.Message, bot: Bot):
    """Handle /unmute command."""
    args = message.text.split()[1:]
    target, _ = await resolve_target_user(message, args)

    if not target:
        await message.reply("❌ **Usage**: Reply to a message with `/unmute` or use `/unmute @username`")
        return

    err = await ModerationActions.unmute(message.chat.id, target, message.from_user.id, bot)
    if err:
        await message.reply(err)


@router.message(Command("ban"), RoleFilter("moderator"))
async def cmd_ban(message: types.Message, bot: Bot):
    """Handle permanent /ban command."""
    args = message.text.split()[1:]
    target, reason = await resolve_target_user(message, args)

    if not target:
        await message.reply("❌ **Usage**: Reply to a message with `/ban [reason]` or use `/ban @username [reason]`")
        return

    err = await ModerationActions.ban(message.chat.id, target, None, reason, message.from_user.id, bot)
    if err:
        await message.reply(err)


@router.message(Command("tban"), RoleFilter("moderator"))
async def cmd_tban(message: types.Message, bot: Bot):
    """Handle temporary /tban command."""
    args = message.text.split()[1:]
    
    # Parsing duration out of args
    if message.reply_to_message:
        if not args:
            await message.reply("❌ **Usage**: Reply to a message with `/tban [duration] [reason]` (e.g. `30m`, `2h`)")
            return
        duration_str = args[0]
        reason = " ".join(args[1:]).strip() or "No reason provided."
        target = message.reply_to_message.from_user
    else:
        if len(args) < 2:
            await message.reply("❌ **Usage**: `/tban @username [duration] [reason]` (e.g. `30m`, `2h`)")
            return
        target_arg = args[0]
        duration_str = args[1]
        reason = " ".join(args[2:]).strip() or "No reason provided."
        
        resolved, _ = await resolve_target_user(message, [target_arg])
        target = resolved

    if not target:
        await message.reply("❌ User could not be resolved.")
        return

    duration = parse_duration(duration_str)
    if not duration:
        await message.reply("❌ **Invalid Duration format**: Use e.g. `30s`, `15m`, `2h`, `1d`")
        return

    err = await ModerationActions.ban(message.chat.id, target, duration, reason, message.from_user.id, bot)
    if err:
        await message.reply(err)


@router.message(Command("unban"), RoleFilter("moderator"))
async def cmd_unban(message: types.Message, bot: Bot):
    """Handle /unban command."""
    args = message.text.split()[1:]
    target, _ = await resolve_target_user(message, args)

    if not target:
        await message.reply("❌ **Usage**: Reply to a message with `/unban` or use `/unban @username`")
        return

    err = await ModerationActions.unban(message.chat.id, target, message.from_user.id, bot)
    if err:
        await message.reply(err)


@router.message(Command("kick"), RoleFilter("moderator"))
async def cmd_kick(message: types.Message, bot: Bot):
    """Handle /kick command."""
    args = message.text.split()[1:]
    target, reason = await resolve_target_user(message, args)

    if not target:
        await message.reply("❌ **Usage**: Reply to a message with `/kick [reason]` or use `/kick @username [reason]`")
        return

    err = await ModerationActions.kick(message.chat.id, target, reason, message.from_user.id, bot)
    if err:
        await message.reply(err)


@router.message(Command("reset"))
async def cmd_reset(message: types.Message, bot: Bot):
    """Reset warnings for the calling user (admin only)."""
    user = message.from_user
    if not user:
        return

    # Verify the caller is an admin or group creator
    try:
        from aiogram.enums import ChatMemberStatus
        member = await bot.get_chat_member(message.chat.id, user.id)
    except Exception as exc:
        logger.error("Failed to fetch chat member status for user %d: %s", user.id, exc)
        await message.answer("❌ Could not verify your permissions. Please try again.")
        return

    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.answer(
            "❌ **Permission Denied**\n"
            "Only group administrators can reset warnings."
        )
        return

    # Reset warnings using WarningRepository
    await WarningRepository.clear_warnings(message.chat.id, user.id)
    await message.answer(f"✅ {user.first_name}, your warnings have been reset.")
