"""
Argus — The All-Seeing AI Guardian.
Main bot entry point: handlers, warning database, dispatcher.
"""

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ChatMemberStatus
import asyncio
import logging
from datetime import datetime, timezone
import sqlite3

from logging_config import setup_logging
from moderation import analyze_message
from config import TELEGRAM_TOKEN, GROUP_CHAT_ID

# ---------------------------------------------------------------------------
# Logging — must be set up before any other module emits log records
# ---------------------------------------------------------------------------
setup_logging()
logger = logging.getLogger("argus.main")

# ---------------------------------------------------------------------------
# Bot / Dispatcher
# ---------------------------------------------------------------------------
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# ---------------------------------------------------------------------------
# Warning database
# ---------------------------------------------------------------------------
conn = sqlite3.connect("warnings.db")
conn.execute(
    """CREATE TABLE IF NOT EXISTS warnings
       (user_id INTEGER PRIMARY KEY, username TEXT, count INTEGER DEFAULT 0)"""
)
conn.commit()

# Async lock — ensures warning increments are atomic under concurrent load
warning_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Message history cache (last 10 messages for AI context)
# ---------------------------------------------------------------------------
message_history: list[str] = []
MAX_HISTORY = 10


def get_recent_history() -> str:
    """Return formatted recent message history for AI context."""
    if not message_history:
        return "No recent messages."
    return "\n".join(f"- {msg}" for msg in message_history[-MAX_HISTORY:])


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Handle /start command."""
    await message.answer(
        "🤖 **Argus - The All-Seeing AI Guardian**\n\n"
        "I'm here to keep this group safe and respectful.\n"
        "I automatically monitor messages for:\n"
        "• Spam and marketing\n"
        "• Adult/NSFW content\n"
        "• Toxic language and harassment\n"
        "• Heated arguments\n\n"
        "Just chat normally - I'll handle the rest!"
    )


@dp.message(Command("warnings"))
async def cmd_warnings(message: types.Message):
    """Show the requesting user's current warning count."""
    user = message.from_user
    cur = conn.cursor()
    cur.execute("SELECT count FROM warnings WHERE user_id=?", (user.id,))
    row = cur.fetchone()
    warnings = row[0] if row else 0

    await message.answer(
        f"⚠️ **{user.first_name}**, you have **{warnings}/3** warnings.\n"
        "Please follow the group rules to avoid being banned."
    )


@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    """Reset warnings for a user (admin only)."""
    user = message.from_user

    # Verify the caller is an admin or group creator
    try:
        member = await bot.get_chat_member(message.chat.id, user.id)
    except Exception as exc:
        logger.error(
            "Failed to fetch chat member status for user=%s: %s", user.id, exc
        )
        await message.answer("❌ Could not verify your permissions. Please try again.")
        return

    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        logger.info(
            "/reset denied — non-admin user=%s (%s) in chat=%s",
            user.id,
            user.username or user.first_name,
            message.chat.id,
        )
        await message.answer(
            "❌ **Permission Denied**\n"
            "Only group administrators can reset warnings.\n"
            "Contact your group admin for assistance.",
            parse_mode="Markdown",
        )
        return

    # Admin confirmed — reset warnings
    cur = conn.cursor()
    cur.execute("UPDATE warnings SET count = 0 WHERE user_id=?", (user.id,))
    conn.commit()

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    logger.info(
        "Admin reset — admin=%s (%s), target=%s, chat=%s, timestamp=%s",
        user.id,
        user.username or user.first_name,
        user.id,
        message.chat.id,
        timestamp,
    )
    await message.answer(f"✅ {user.first_name}, your warnings have been reset.")


# ---------------------------------------------------------------------------
# Main message handler
# ---------------------------------------------------------------------------

@dp.message()
async def moderate_message(message: types.Message):
    """Analyse every non-command message and take moderation action if needed."""
    # Skip if GROUP_CHAT_ID is set and message is from a different chat
    if GROUP_CHAT_ID != 0 and message.chat.id != GROUP_CHAT_ID:
        return

    # Skip commands
    if message.text and message.text.startswith("/"):
        return

    user = message.from_user
    text = (message.text or message.caption or "").strip()
    if not text:
        return

    # Update context history
    message_history.append(f"{user.first_name}: {text}")
    if len(message_history) > MAX_HISTORY:
        message_history.pop(0)

    result = await analyze_message(text, get_recent_history())

    action = result.get("action", "none")
    reason = result.get("reason", "")
    user_msg = result.get("user_message", "please follow the group rules and stay respectful.")

    # Delete flagged messages
    if action in ("delete", "warn", "ban"):
        try:
            await message.delete()
        except Exception as exc:
            logger.warning("Could not delete message from user=%s: %s", user.id, exc)

    if action == "warn":
        warning_text = f"⚠️ {user.first_name}, {user_msg}"
        sent_msg = await message.answer(warning_text)
        asyncio.create_task(auto_delete_warning(sent_msg, 30))

        # Atomic warning increment — lock prevents race conditions under
        # concurrent message bursts where two handlers could otherwise both
        # read the same count, both increment it, and produce an incorrect total.
        async with warning_lock:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO warnings (user_id, username, count) VALUES (?, ?, 1) "
                "ON CONFLICT(user_id) DO UPDATE SET count = count + 1, username = ?",
                (user.id, user.username or "", user.username or ""),
            )
            conn.commit()

            cur.execute("SELECT count FROM warnings WHERE user_id=?", (user.id,))
            row = cur.fetchone()
            warnings = row[0] if row else 0

        logger.info(
            "Warning issued — user=%s (%s) warnings=%s/3 reason=%.80s",
            user.id,
            user.username or user.first_name,
            warnings,
            reason,
        )

        if warnings >= 3:
            await ban_user(message.chat.id, user, f"Multiple warnings ({warnings}/3)")

    elif action == "ban":
        await ban_user(message.chat.id, user, reason)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def auto_delete_warning(message: types.Message, delay: int):
    """Delete a warning message after `delay` seconds to reduce clutter."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass  # Message may already be gone; silently ignore


async def ban_user(chat_id: int, user: types.User, reason: str):
    """Ban a user and announce it in the chat."""
    try:
        await bot.ban_chat_member(chat_id, user.id)
        await bot.send_message(
            chat_id,
            f"🚫 **{user.first_name}** has been banned.\nReason: {reason}",
        )
        logger.info(
            "User banned — user=%s (%s) chat=%s reason=%.80s",
            user.id,
            user.username or user.first_name,
            chat_id,
            reason,
        )
    except Exception as exc:
        logger.error("Failed to ban user=%s: %s", user.id, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    logger.info("🤖 Argus is now guarding the group!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
