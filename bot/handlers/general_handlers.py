"""
General commands (/start, /help) and the Group Notes / Saved Replies system.
"""

from aiogram import Router, F, types
from aiogram.filters import Command
from database.models import NoteRepository
from bot.filters.permissions import RoleFilter

router = Router()


@router.message(Command("start"))
async def cmd_start(message: types.Message):
    """Handle /start command."""
    await message.answer(
        "🤖 **Argus - The All-Seeing AI Guardian**\n\n"
        "I'm an advanced group moderator bot powered by AI and robust heuristics.\n\n"
        "**Core Capabilities:**\n"
        "• AI-driven content analysis and sentiment scaling\n"
        "• Unicode homoglyph normalization & obfuscation bypass detection\n"
        "• Flood rate checks, stickers, GIFs, files, and link spam limiters\n"
        "• Captcha verification gatekeeper & Anti-raid lockouts\n"
        "• Custom word/regex filters per group\n"
        "• Group Notes & Saved replies system\n\n"
        "Type `/help` to see list of commands."
    )


@router.message(Command("help"))
async def cmd_help(message: types.Message):
    """Provide helper guide for admin commands."""
    help_text = (
        "🤖 **Argus Command Reference Guide**\n\n"
        "**Moderation Controls:**\n"
        "• `/warnings` — view current warning count\n"
        "• `/warn [reason]` — issue warning to user (moderator+)\n"
        "• `/unwarn` — revoke last warning (moderator+)\n"
        "• `/mute [reason]` — permanently mute user (moderator+)\n"
        "• `/tmute [duration] [reason]` — temp mute (e.g. `30m`, `2h`, `1d`) (moderator+)\n"
        "• `/unmute` — lift mute restriction (moderator+)\n"
        "• `/ban [reason]` — permanently ban user (moderator+)\n"
        "• `/tban [duration] [reason]` — temp ban user (moderator+)\n"
        "• `/unban` — lift ban restriction (moderator+)\n"
        "• `/kick` — kick member from chat (moderator+)\n\n"
        "**Group Configurations:**\n"
        "• `/settings` — show/update chat settings (limit, ai, captcha, raid) (moderator+)\n"
        "• `/promote [role]` — promote database role (owner/admin/moderator/trusted) (admin+)\n"
        "• `/demote` — demote user to member (admin+)\n"
        "• `/lock` / `/unlock` — lock/unlock message sending (moderator+)\n"
        "• `/pin` / `/unpin` — pin/unpin messages (moderator+)\n"
        "• `/purge` [count] — purge recent messages in chat (moderator+)\n"
        "• `/welcome [template]` — customize welcome message (moderator+)\n"
        "• `/goodbye [template]` — customize goodbye message (moderator+)\n\n"
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
async def cmd_save_note(message: types.Message):
    """Save note trigger for the group (/note [trigger] [content])."""
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.reply("❌ **Usage**: `/note [trigger_name] [content of note]`")
        return

    trigger = args[1].lower().strip()
    content = args[2].strip()

    if trigger.startswith("#"):
        trigger = trigger[1:]

    await NoteRepository.add_note(message.chat.id, trigger, content)
    await message.reply(f"✅ Saved note reply for **#{trigger}**.")


@router.message(Command("delnote"), RoleFilter("moderator"))
async def cmd_delete_note(message: types.Message):
    """Delete a saved note trigger (/delnote [trigger])."""
    args = message.text.split()
    if len(args) < 2:
        await message.reply("❌ **Usage**: `/delnote [trigger_name]`")
        return

    trigger = args[1].lower().strip()
    if trigger.startswith("#"):
        trigger = trigger[1:]

    deleted = await NoteRepository.delete_note(message.chat.id, trigger)
    if deleted:
        await message.reply(f"✅ Deleted note for **#{trigger}**.")
    else:
        await message.reply(f"❌ Note trigger **#{trigger}** not found.")


@router.message(Command("notes"))
async def cmd_list_notes(message: types.Message):
    """List all saved notes triggers in the group."""
    notes = await NoteRepository.list_notes(message.chat.id)
    if not notes:
        await message.reply("📝 No saved notes in this group chat.")
        return

    triggers_list = ", ".join(f"`#{n}`" for n in notes)
    await message.reply(f"📝 **Saved replies in this chat:**\n\n{triggers_list}")


@router.message(F.text.startswith("#"))
async def on_hash_trigger(message: types.Message):
    """Intercept #trigger to send corresponding saved reply note."""
    text = message.text.strip()
    # Extract the trigger name (split by space to ignore arguments)
    trigger = text[1:].split()[0].lower()
    
    content = await NoteRepository.get_note(message.chat.id, trigger)
    if content:
        # Reply to the user or reply-to message
        target_msg = message.reply_to_message or message
        await target_msg.reply(content)
