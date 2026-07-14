"""
Command handlers for admin-only controls (/promote, /demote, /lock, /unlock, /pin, /unpin, /purge, /settings, /welcome, /goodbye).
"""

from aiogram import Router, Bot, types
from aiogram.filters import Command
from aiogram.types import ChatPermissions

from database.models import GroupRepository, GroupMemberRepository
from bot.filters.permissions import RoleFilter
from bot.handlers.moderation_handlers import resolve_target_user
from moderation.actions import ROLE_VALUES

import logging
logger = logging.getLogger("argus.handlers.admin")
router = Router()


@router.message(Command("promote"), RoleFilter("admin"))
async def cmd_promote(message: types.Message):
    """Promote user role in database (/promote [role])."""
    args = message.text.split()[1:]
    
    # Defaults to moderator if not specified
    role_to_promote = "moderator"
    if args and args[-1].lower() in ROLE_VALUES:
        role_to_promote = args[-1].lower()
        args = args[:-1]

    target, _ = await resolve_target_user(message, args)
    if not target:
        await message.reply("❌ **Usage**: Reply to a message with `/promote [role]` or use `/promote @username [role]`")
        return

    # Check caller privilege
    caller_role = await GroupMemberRepository.get_member_role(message.chat.id, message.from_user.id)
    if ROLE_VALUES[caller_role] <= ROLE_VALUES[role_to_promote] and caller_role != "owner":
        await message.reply("❌ You cannot promote someone to a role equal to or higher than your own.")
        return

    await GroupMemberRepository.set_member_role(message.chat.id, target.id, role_to_promote)
    await message.reply(f"⭐ **{target.first_name}** has been promoted to **{role_to_promote.capitalize()}**.")


@router.message(Command("demote"), RoleFilter("admin"))
async def cmd_demote(message: types.Message):
    """Demote user role back to member."""
    args = message.text.split()[1:]
    target, _ = await resolve_target_user(message, args)

    if not target:
        await message.reply("❌ **Usage**: Reply to a message with `/demote` or use `/demote @username`")
        return

    # Check caller privilege
    caller_role = await GroupMemberRepository.get_member_role(message.chat.id, message.from_user.id)
    target_role = await GroupMemberRepository.get_member_role(message.chat.id, target.id)
    if ROLE_VALUES[caller_role] <= ROLE_VALUES[target_role] and caller_role != "owner":
        await message.reply("❌ You cannot demote someone with equal or higher permissions than yourself.")
        return

    await GroupMemberRepository.set_member_role(message.chat.id, target.id, "member")
    await message.reply(f"✅ **{target.first_name}** has been demoted to **Member**.")


@router.message(Command("lock"), RoleFilter("moderator"))
async def cmd_lock(message: types.Message, bot: Bot):
    """Lock the group chat (disallows sending messages)."""
    chat_id = message.chat.id
    
    lock_permissions = ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )

    try:
        await bot.set_chat_permissions(chat_id, lock_permissions)
        await GroupRepository.update_group(chat_id, is_locked=1)
        await message.reply("🔒 **Chat Locked**: Sending messages has been disabled for regular members.")
    except Exception as exc:
        logger.error("Failed to lock chat: %s", exc)
        await message.reply(f"❌ Failed to lock chat: {exc}")


@router.message(Command("unlock"), RoleFilter("moderator"))
async def cmd_unlock(message: types.Message, bot: Bot):
    """Unlock the group chat (allows sending messages)."""
    chat_id = message.chat.id

    unlock_permissions = ChatPermissions(
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
        await bot.set_chat_permissions(chat_id, unlock_permissions)
        await GroupRepository.update_group(chat_id, is_locked=0)
        await message.reply("🔓 **Chat Unlocked**: Sending messages has been re-enabled.")
    except Exception as exc:
        logger.error("Failed to unlock chat: %s", exc)
        await message.reply(f"❌ Failed to unlock chat: {exc}")


@router.message(Command("pin"), RoleFilter("moderator"))
async def cmd_pin(message: types.Message, bot: Bot):
    """Pin the replied message."""
    if not message.reply_to_message:
        await message.reply("❌ Reply to the message you want to pin.")
        return

    try:
        await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
        await message.reply("📌 Message pinned successfully.")
    except Exception as exc:
        await message.reply(f"❌ Failed to pin message: {exc}")


@router.message(Command("unpin"), RoleFilter("moderator"))
async def cmd_unpin(message: types.Message, bot: Bot):
    """Unpin the current pinned message(s)."""
    try:
        if message.reply_to_message:
            await bot.unpin_chat_message(message.chat.id, message.reply_to_message.message_id)
            await message.reply("📌 Message unpinned.")
        else:
            await bot.unpin_all_chat_messages(message.chat.id)
            await message.reply("📌 All pinned messages have been unpinned.")
    except Exception as exc:
        await message.reply(f"❌ Failed to unpin: {exc}")


@router.message(Command("purge"), RoleFilter("moderator"))
async def cmd_purge(message: types.Message, bot: Bot):
    """Purge messages (either up to reply, or a specific count)."""
    chat_id = message.chat.id

    if message.reply_to_message:
        start_id = message.reply_to_message.message_id
        end_id = message.message_id
        
        # Collect IDs to delete
        message_ids = list(range(start_id, end_id + 1))
        # Delete in chunks of 100
        for i in range(0, len(message_ids), 100):
            chunk = message_ids[i : i + 100]
            try:
                await bot.delete_messages(chat_id, chunk)
            except Exception:
                # If bulk fails, delete one-by-one (ignores older than 48 hours silently)
                for mid in chunk:
                    try:
                        await bot.delete_message(chat_id, mid)
                    except Exception:
                        pass
        return

    args = message.text.split()[1:]
    if not args or not args[0].isdigit():
        await message.reply("❌ **Usage**: Reply to a message with `/purge` or use `/purge [count]` (e.g. `/purge 20`)")
        return

    count = int(args[0])
    current_id = message.message_id
    
    # Delete recent messages
    for mid in range(current_id - count, current_id + 1):
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


@router.message(Command("settings"), RoleFilter("moderator"))
async def cmd_settings(message: types.Message):
    """Get or change group configurations (/settings [key] [value])."""
    chat_id = message.chat.id
    args = message.text.split()[1:]

    if not args:
        # Display current settings
        grp = await GroupRepository.get_group(chat_id)
        text = (
            f"⚙️ **Group settings for {message.chat.title}**:\n\n"
            f"• **Warning Limit**: {grp.get('warning_limit', 3)}\n"
            f"• **AI Moderation**: {'Enabled' if grp.get('ai_enabled', 1) else 'Disabled'}\n"
            f"• **Welcome Captcha**: {'Enabled' if grp.get('welcome_captcha', 0) else 'Disabled'}\n"
            f"• **Anti Raid (Join threshold)**: {'Enabled' if grp.get('anti_raid', 0) else 'Disabled'}\n\n"
            f"To change a setting, use: `/settings [key] [value]`\n"
            f"Keys: `limit` (integer), `ai` (on/off), `captcha` (on/off), `raid` (on/off)"
        )
        await message.reply(text)
        return

    if len(args) < 2:
        await message.reply("❌ **Usage**: `/settings [key] [value]`")
        return

    key = args[0].lower().strip()
    value = args[1].lower().strip()

    if key == "limit":
        if not value.isdigit():
            await message.reply("❌ Warning limit must be a positive integer.")
            return
        limit = int(value)
        await GroupRepository.update_group(chat_id, warning_limit=limit)
        await message.reply(f"✅ Warning limit updated to **{limit}**.")
        
    elif key == "ai":
        enabled = 1 if value in ("on", "true", "1", "enable") else 0
        await GroupRepository.update_group(chat_id, ai_enabled=enabled)
        await message.reply(f"✅ AI Moderation has been **{'enabled' if enabled else 'disabled'}**.")

    elif key == "captcha":
        enabled = 1 if value in ("on", "true", "1", "enable") else 0
        await GroupRepository.update_group(chat_id, welcome_captcha=enabled)
        await message.reply(f"✅ Welcome Captcha verification has been **{'enabled' if enabled else 'disabled'}**.")

    elif key == "raid":
        enabled = 1 if value in ("on", "true", "1", "enable") else 0
        await GroupRepository.update_group(chat_id, anti_raid=enabled)
        await message.reply(f"✅ Anti-raid join monitoring has been **{'enabled' if enabled else 'disabled'}**.")

    else:
        await message.reply("❌ Unknown setting key. Choose from: `limit`, `ai`, `captcha`, `raid`.")


@router.message(Command("welcome"), RoleFilter("moderator"))
async def cmd_welcome(message: types.Message):
    """Set custom welcome message template."""
    # Strip the command prefix
    template = message.text.split(maxsplit=1)
    if len(template) < 2:
        await message.reply(
            "❌ **Usage**: `/welcome [your message template]`\n"
            "Supported placeholders: `[first_name]`, `[username]`, `[group_title]`"
        )
        return

    await GroupRepository.update_group(message.chat.id, welcome_message=template[1].strip())
    await message.reply("✅ Welcome message template updated successfully.")


@router.message(Command("goodbye"), RoleFilter("moderator"))
async def cmd_goodbye(message: types.Message):
    """Set custom goodbye message template."""
    template = message.text.split(maxsplit=1)
    if len(template) < 2:
        await message.reply(
            "❌ **Usage**: `/goodbye [your message template]`\n"
            "Supported placeholders: `[first_name]`, `[username]`, `[group_title]`"
        )
        return

    await GroupRepository.update_group(message.chat.id, goodbye_message=template[1].strip())
    await message.reply("✅ Goodbye message template updated successfully.")
