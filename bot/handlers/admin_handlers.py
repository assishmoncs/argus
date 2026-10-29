"""
Command handlers for admin-only controls (/promote, /demote, /lock, /unlock, /pin, /unpin,
/purge, /settings, /welcome, /goodbye, /filter, /delfilter, /filters, /whitelist, /unwhitelist, /health).
"""

import logging
from aiogram import Bot, Router, types
from aiogram.filters import Command
from aiogram.types import ChatPermissions

from bot.filters.permissions import RoleFilter
from bot.handlers.moderation_handlers import resolve_target_user
from database.connection import db
from database.models import (
    CustomFilterRepository,
    GroupMemberRepository,
    GroupRepository,
    WhitelistedWordsRepository,
)
from moderation.actions import ROLE_VALUES
from services.ai_service import ai_service
from utils.telemetry import telemetry

logger = logging.getLogger("argus.handlers.admin")
router = Router()

MAX_PURGE_COUNT = 500


@router.message(Command("promote"), RoleFilter("admin"))
async def cmd_promote(message: types.Message) -> None:
    """Promote user role in database (/promote [role])."""
    args = (message.text or "").split()[1:]

    role_to_promote = "moderator"
    if args and args[-1].lower() in ROLE_VALUES:
        role_to_promote = args[-1].lower()
        args = args[:-1]

    target, _ = await resolve_target_user(message, args)
    if not target:
        await message.reply(
            "❌ **Usage**: Reply to a message with `/promote [role]` or use `/promote @username [role]`"
        )
        return

    caller_role = await GroupMemberRepository.get_member_role(
        message.chat.id, message.from_user.id
    )
    if ROLE_VALUES.get(caller_role, 10) <= ROLE_VALUES.get(role_to_promote, 10) and caller_role != "owner":
        await message.reply(
            "❌ You cannot promote someone to a role equal to or higher than your own."
        )
        return

    await GroupMemberRepository.set_member_role(message.chat.id, target.id, role_to_promote)
    await message.reply(
        f"⭐ **{target.first_name}** has been promoted to **{role_to_promote.capitalize()}**."
    )


@router.message(Command("demote"), RoleFilter("admin"))
async def cmd_demote(message: types.Message) -> None:
    """Demote user role back to member."""
    args = (message.text or "").split()[1:]
    target, _ = await resolve_target_user(message, args)

    if not target:
        await message.reply(
            "❌ **Usage**: Reply to a message with `/demote` or use `/demote @username`"
        )
        return

    caller_role = await GroupMemberRepository.get_member_role(
        message.chat.id, message.from_user.id
    )
    target_role = await GroupMemberRepository.get_member_role(message.chat.id, target.id)
    if ROLE_VALUES.get(caller_role, 10) <= ROLE_VALUES.get(target_role, 10) and caller_role != "owner":
        await message.reply(
            "❌ You cannot demote someone with equal or higher permissions than yourself."
        )
        return

    await GroupMemberRepository.set_member_role(message.chat.id, target.id, "member")
    await message.reply(f"✅ **{target.first_name}** has been demoted to **Member**.")


@router.message(Command("lock"), RoleFilter("moderator"))
async def cmd_lock(message: types.Message, bot: Bot) -> None:
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
        await message.reply(
            "🔒 **Chat Locked**: Sending messages has been disabled for regular members."
        )
    except Exception as exc:
        logger.error("Failed to lock chat: %s", exc)
        await message.reply(f"❌ Failed to lock chat: {exc}")


@router.message(Command("unlock"), RoleFilter("moderator"))
async def cmd_unlock(message: types.Message, bot: Bot) -> None:
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
async def cmd_pin(message: types.Message, bot: Bot) -> None:
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
async def cmd_unpin(message: types.Message, bot: Bot) -> None:
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
async def cmd_purge(message: types.Message, bot: Bot) -> None:
    """Purge messages (either up to reply, or a specific bounded count)."""
    chat_id = message.chat.id

    if message.reply_to_message:
        start_id = max(message.reply_to_message.message_id, message.message_id - MAX_PURGE_COUNT)
        end_id = message.message_id
        message_ids = list(range(start_id, end_id + 1))
        for i in range(0, len(message_ids), 100):
            chunk = message_ids[i : i + 100]
            try:
                await bot.delete_messages(chat_id, chunk)
            except Exception:
                for mid in chunk:
                    try:
                        await bot.delete_message(chat_id, mid)
                    except Exception:
                        pass
        return

    args = (message.text or "").split()[1:]
    if not args or not args[0].isdigit():
        await message.reply(
            "❌ **Usage**: Reply to a message with `/purge` or use `/purge [count]` (e.g. `/purge 20`)"
        )
        return

    count = min(MAX_PURGE_COUNT, max(1, int(args[0])))
    current_id = message.message_id
    message_ids = list(range(max(1, current_id - count), current_id + 1))

    for i in range(0, len(message_ids), 100):
        chunk = message_ids[i : i + 100]
        try:
            await bot.delete_messages(chat_id, chunk)
        except Exception:
            for mid in chunk:
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception:
                    pass


@router.message(Command("settings"), RoleFilter("moderator"))
async def cmd_settings(message: types.Message) -> None:
    """Get or change group configurations (/settings [key] [value])."""
    chat_id = message.chat.id
    args = (message.text or "").split()[1:]

    if not args:
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
        if not value.isdigit() or int(value) < 1:
            await message.reply("❌ Warning limit must be a positive integer.")
            return
        limit = int(value)
        await GroupRepository.update_group(chat_id, warning_limit=limit)
        await message.reply(f"✅ Warning limit updated to **{limit}**.")

    elif key == "ai":
        enabled = 1 if value in ("on", "true", "1", "enable") else 0
        await GroupRepository.update_group(chat_id, ai_enabled=enabled)
        await message.reply(
            f"✅ AI Moderation has been **{'enabled' if enabled else 'disabled'}**."
        )

    elif key == "captcha":
        enabled = 1 if value in ("on", "true", "1", "enable") else 0
        await GroupRepository.update_group(chat_id, welcome_captcha=enabled)
        await message.reply(
            f"✅ Welcome Captcha verification has been **{'enabled' if enabled else 'disabled'}**."
        )

    elif key == "raid":
        enabled = 1 if value in ("on", "true", "1", "enable") else 0
        await GroupRepository.update_group(chat_id, anti_raid=enabled)
        await message.reply(
            f"✅ Anti-raid join monitoring has been **{'enabled' if enabled else 'disabled'}**."
        )

    else:
        await message.reply(
            "❌ Unknown setting key. Choose from: `limit`, `ai`, `captcha`, `raid`."
        )


@router.message(Command("welcome"), RoleFilter("moderator"))
async def cmd_welcome(message: types.Message) -> None:
    """Set custom welcome message template."""
    template = (message.text or "").split(maxsplit=1)
    if len(template) < 2:
        await message.reply(
            "❌ **Usage**: `/welcome [your message template]`\n"
            "Supported placeholders: `[first_name]`, `[username]`, `[group_title]`"
        )
        return

    await GroupRepository.update_group(
        message.chat.id, welcome_message=template[1].strip()
    )
    await message.reply("✅ Welcome message template updated successfully.")


@router.message(Command("goodbye"), RoleFilter("moderator"))
async def cmd_goodbye(message: types.Message) -> None:
    """Set custom goodbye message template."""
    template = (message.text or "").split(maxsplit=1)
    if len(template) < 2:
        await message.reply(
            "❌ **Usage**: `/goodbye [your message template]`\n"
            "Supported placeholders: `[first_name]`, `[username]`, `[group_title]`"
        )
        return

    await GroupRepository.update_group(
        message.chat.id, goodbye_message=template[1].strip()
    )
    await message.reply("✅ Goodbye message template updated successfully.")


# ---------------------------------------------------------------------------
# Custom Filter & Whitelist Management Commands
# ---------------------------------------------------------------------------

@router.message(Command("filter"), RoleFilter("moderator"))
async def cmd_add_filter(message: types.Message) -> None:
    """Add a custom word/wildcard/regex filter (/filter [pattern] [exact|wildcard|regex] [delete|warn|mute|ban])."""
    args = (message.text or "").split()[1:]
    if not args:
        await message.reply(
            "❌ **Usage**: `/filter [pattern] [match_type=exact|wildcard|regex] [action=delete|warn|mute|ban]`"
        )
        return

    pattern = args[0].strip()
    match_type = args[1].lower().strip() if len(args) >= 2 else "exact"
    action = args[2].lower().strip() if len(args) >= 3 else "delete"

    if match_type not in ("exact", "wildcard", "regex"):
        await message.reply("❌ `match_type` must be `exact`, `wildcard`, or `regex`.")
        return
    if action not in ("delete", "warn", "mute", "ban"):
        await message.reply("❌ `action` must be `delete`, `warn`, `mute`, or `ban`.")
        return

    await CustomFilterRepository.add_filter(message.chat.id, pattern, match_type, action)
    await message.reply(
        f"✅ Added **{match_type}** filter `{pattern}` with action **{action}**."
    )


@router.message(Command("delfilter"), RoleFilter("moderator"))
async def cmd_del_filter(message: types.Message) -> None:
    """Delete a custom filter by ID (/delfilter [id])."""
    args = (message.text or "").split()[1:]
    if not args or not args[0].isdigit():
        await message.reply("❌ **Usage**: `/delfilter [filter_id]`")
        return

    deleted = await CustomFilterRepository.delete_filter(message.chat.id, int(args[0]))
    if deleted:
        await message.reply(f"✅ Deleted filter ID **#{args[0]}**.")
    else:
        await message.reply(f"❌ Filter ID **#{args[0]}** not found in this chat.")


@router.message(Command("filters"), RoleFilter("moderator"))
async def cmd_list_filters(message: types.Message) -> None:
    """List all active custom filters in the group."""
    filters = await CustomFilterRepository.get_filters(message.chat.id)
    if not filters:
        await message.reply("🛡️ No custom word filters configured in this chat.")
        return

    lines = [
        f"• `#{f['id']}` — `{f['pattern']}` ({f['match_type']} → **{f['action']}**)"
        for f in filters
    ]
    await message.reply("🛡️ **Active Custom Filters:**\n\n" + "\n".join(lines))


@router.message(Command("whitelist"), RoleFilter("moderator"))
async def cmd_add_whitelist(message: types.Message) -> None:
    """Add a word to the group whitelist (/whitelist [word])."""
    args = (message.text or "").split()[1:]
    if not args:
        words = sorted(await WhitelistedWordsRepository.get_whitelist(message.chat.id))
        if not words:
            await message.reply("📝 No whitelisted words in this chat. Use `/whitelist [word]` to add one.")
            return
        await message.reply("📝 **Whitelisted words:** " + ", ".join(f"`{w}`" for w in words))
        return

    word = args[0].strip().lower()
    await WhitelistedWordsRepository.add_whitelist(message.chat.id, word)
    await message.reply(f"✅ Added `{word}` to the filter whitelist.")


@router.message(Command("unwhitelist"), RoleFilter("moderator"))
async def cmd_remove_whitelist(message: types.Message) -> None:
    """Remove a word from the group whitelist (/unwhitelist [word])."""
    args = (message.text or "").split()[1:]
    if not args:
        await message.reply("❌ **Usage**: `/unwhitelist [word]`")
        return

    word = args[0].strip().lower()
    removed = await WhitelistedWordsRepository.remove_whitelist(message.chat.id, word)
    if removed:
        await message.reply(f"✅ Removed `{word}` from the filter whitelist.")
    else:
        await message.reply(f"❌ `{word}` was not in the whitelist.")


@router.message(Command("health", "status"), RoleFilter("moderator"))
async def cmd_health(message: types.Message) -> None:
    """Display enterprise health, database pool, AI cache/circuit-breaker, and telemetry status."""
    db_health = await db.health_check()
    ai_health = ai_service.get_health_status()
    snap = telemetry.snapshot()

    text = (
        "📊 **Argus System Health & Telemetry**\n\n"
        f"• **Uptime**: `{snap['uptime_seconds']}s`\n"
        f"• **Database**: `{'Healthy' if db_health.get('healthy') else 'Degraded'}` "
        f"(mode=`{db_health.get('journal_mode', 'n/a')}`, latency=`{db_health.get('latency_ms', 0)}ms`, "
        f"pool=`{db_health.get('created_connections', 0)}/{db_health.get('pool_size', 0)}`)\n"
        f"• **Gemini AI Circuit Breaker**: `{ai_health['circuit_breaker_state'].upper()}`\n"
        f"• **AI Cache**: `{ai_health['cache']['size']}/{ai_health['cache']['max_size']}` entries "
        f"(hit rate `{ai_health['cache']['hit_rate_pct']}%`)\n"
        f"• **Messages Processed**: `{snap['counters'].get('messages_processed_total{status=ok}', 0)}`"
    )
    await message.reply(text)
