"""
General commands (/start, /help) and the Group Notes / Saved Replies system.
"""

from aiogram import F, Router, types
from aiogram.filters import Command
from bot.filters.permissions import RoleFilter
from database.models import NoteRepository

router = Router()


@router.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    """Handle /start command."""
    await message.answer(
        "🤖 **Argus - The All-Seeing AI Guardian**\n\n"
        "I'm an enterprise-grade group moderator bot powered by Google Gemini AI and resilient local heuristics.\n\n"
        "**Core Capabilities:**\n"
        "• AI-driven content analysis with LRU+TTL caching & circuit-breaker resilience\n"
        "• Unicode homoglyph normalization, zero-width stripping & obfuscation detection\n"
        "• Flood rate checks, stickers, GIFs, files, and link spam limiters\n"
        "• Captcha verification gatekeeper & Anti-raid lockouts\n"
        "• Custom word/wildcard/regex filters & whitelists per group\n"
        "• Group Notes, Audit Logs & Real-time Health Telemetry\n\n"
        "Type `/help` to see list of commands."
    )


@router.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
    """Provide helper guide for admin and moderation commands."""
    help_text = (
        "🤖 **Argus Command Reference Guide**\n\n"
        "**Moderation Controls:**\n"
        "• `/warnings` — view current warning count\n"
        "• `/warn [reason]` — issue warning to user (moderator+)\n"
        "• `/unwarn` — revoke last warning (moderator+)\n"
        "• `/mute [reason]` — permanently mute user (moderator+)\n"
        "• `/tmute [duration] [reason]` — temp mute (`30s`, `15m`, `2h`, `1d`, `1w`) (moderator+)\n"
        "• `/unmute` — lift mute restriction (moderator+)\n"
        "• `/ban [reason]` — permanently ban user (moderator+)\n"
        "• `/tban [duration] [reason]` — temp ban user (moderator+)\n"
        "• `/unban` — lift ban restriction (moderator+)\n"
        "• `/kick` / `/softban` — kick or softban member (moderator+)\n"
        "• `/reset` — reset your own warnings (admin+)\n\n"
        "**Group & Filter Configurations:**\n"
        "• `/settings` — show/update chat settings (`limit`, `ai`, `captcha`, `raid`) (moderator+)\n"
        "• `/filter [pattern] [exact|wildcard|regex] [action]` — add custom filter (moderator+)\n"
        "• `/delfilter [id]` / `/filters` — remove or list custom filters (moderator+)\n"
        "• `/whitelist [word]` / `/unwhitelist [word]` — manage filter whitelist (moderator+)\n"
        "• `/promote [role]` / `/demote` — manage database roles (admin+)\n"
        "• `/lock` / `/unlock` — lock/unlock message sending (moderator+)\n"
        "• `/pin` / `/unpin` — pin/unpin messages (moderator+)\n"
        "• `/purge [count]` — purge recent messages in chat (moderator+)\n"
        "• `/welcome [template]` / `/goodbye [template]` — customize join/leave messages (moderator+)\n"
        "• `/health` — view live system health & telemetry metrics (moderator+)\n\n"
        "**Saved Replies / Notes System:**\n"
        "• `/note [name] [text]` — save a note trigger (moderator+)\n"
        "• `/delnote [name]` — delete note trigger (moderator+)\n"
        "• `/notes` — list saved note triggers in chat\n"
        "• Use `#note_name` in chat to trigger a saved reply!"
    )
    await message.answer(help_text)


# ---------------------------------------------------------------------------
# Group Notes / Saved Replies System
# ---------------------------------------------------------------------------

@router.message(Command("note"), RoleFilter("moderator"))
async def cmd_save_note(message: types.Message) -> None:
    """Save note trigger for the group (/note [trigger] [content])."""
    args = (message.text or "").split(maxsplit=2)
    if len(args) < 3:
        await message.reply("❌ **Usage**: `/note [trigger_name] [content of note]`")
        return

    trigger = args[1].lower().strip().lstrip("#")
    content = args[2].strip()
    if not trigger:
        await message.reply("❌ Trigger name cannot be empty.")
        return

    await NoteRepository.add_note(message.chat.id, trigger, content)
    await message.reply(f"✅ Saved note reply for **#{trigger}**.")


@router.message(Command("delnote"), RoleFilter("moderator"))
async def cmd_delete_note(message: types.Message) -> None:
    """Delete a saved note trigger (/delnote [trigger])."""
    args = (message.text or "").split()
    if len(args) < 2:
        await message.reply("❌ **Usage**: `/delnote [trigger_name]`")
        return

    trigger = args[1].lower().strip().lstrip("#")
    deleted = await NoteRepository.delete_note(message.chat.id, trigger)
    if deleted:
        await message.reply(f"✅ Deleted note for **#{trigger}**.")
    else:
        await message.reply(f"❌ Note trigger **#{trigger}** not found.")


@router.message(Command("notes"))
async def cmd_list_notes(message: types.Message) -> None:
    """List all saved notes triggers in the group."""
    notes = await NoteRepository.list_notes(message.chat.id)
    if not notes:
        await message.reply("📝 No saved notes in this group chat.")
        return

    triggers_list = ", ".join(f"`#{n}`" for n in notes)
    await message.reply(f"📝 **Saved replies in this chat:**\n\n{triggers_list}")


@router.message(F.text.startswith("#"))
async def on_hash_trigger(message: types.Message) -> None:
    """Intercept #trigger to send corresponding saved reply note."""
    text = (message.text or "").strip()
    if len(text) <= 1:
        return
    trigger = text[1:].split()[0].lower()

    content = await NoteRepository.get_note(message.chat.id, trigger)
    if content:
        target_msg = message.reply_to_message or message
        await target_msg.reply(content)
