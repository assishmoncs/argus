from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import asyncio
from moderation import analyze_message
import sqlite3
from config import TELEGRAM_TOKEN, GROUP_CHAT_ID

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Warnings database setup
conn = sqlite3.connect('warnings.db')
conn.execute('''CREATE TABLE IF NOT EXISTS warnings 
                (user_id INTEGER PRIMARY KEY, username TEXT, count INTEGER DEFAULT 0)''')
conn.commit()

# Message history cache for context (last 10 messages)
message_history = []
MAX_HISTORY = 10

def get_recent_history():
    """Get formatted recent message history for context."""
    if not message_history:
        return "No recent messages."
    
    history_lines = []
    for msg in message_history[-MAX_HISTORY:]:
        history_lines.append(f"- {msg}")
    
    return "\n".join(history_lines)

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
    """Show user's warning count."""
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
    # In production, add admin check here
    user = message.from_user
    cur = conn.cursor()
    cur.execute("UPDATE warnings SET count = 0 WHERE user_id=?", (user.id,))
    conn.commit()
    await message.answer(f"✅ {user.first_name}, your warnings have been reset.")

@dp.message()
async def moderate_message(message: types.Message):
    """Main handler for all incoming messages."""
    # Skip if group chat ID is set and doesn't match
    if GROUP_CHAT_ID != 0 and message.chat.id != GROUP_CHAT_ID:
        return
    
    # Skip commands
    if message.text and message.text.startswith('/'):
        return
    
    user = message.from_user
    text = (message.text or message.caption or "").strip()
    
    # Skip empty messages
    if not text:
        return
    
    # Update message history for context
    message_history.append(f"{user.first_name}: {text}")
    if len(message_history) > MAX_HISTORY:
        message_history.pop(0)
    
    # Get recent history for context
    history = get_recent_history()
    
    result = await analyze_message(text, history)
    
    action = result.get("action", "none")
    reason = result.get("reason", "")
    user_msg = result.get("user_message", "please follow the group rules and stay respectful.")
    
    # Delete message if needed
    if action in ["delete", "warn", "ban"]:
        try:
            await message.delete()
        except Exception as e:
            print(f"[Argus] Delete failed: {e}")
    
    if action == "warn":
        warning_text = f"⚠️ {user.first_name}, {user_msg}"
        sent_msg = await message.answer(warning_text)
        
        # Auto-delete warning after 30 seconds to reduce clutter
        asyncio.create_task(auto_delete_warning(sent_msg, 30))
        
        # Update warning count
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO warnings (user_id, username, count) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET count = count + 1, username = ?",
            (user.id, user.username or '', user.username or '')
        )
        conn.commit()
        
        cur.execute("SELECT count FROM warnings WHERE user_id=?", (user.id,))
        row = cur.fetchone()
        warnings = row[0] if row else 0
        
        if warnings >= 3:
            await ban_user(message.chat.id, user, f"Multiple warnings ({warnings}/3)")
    
    elif action == "ban":
        await ban_user(message.chat.id, user, reason)


async def auto_delete_warning(message: types.Message, delay: int):
    """Auto-delete warning messages after a delay."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass


async def ban_user(chat_id: int, user: types.User, reason: str):
    """Ban a user from the chat."""
    try:
        await bot.ban_chat_member(chat_id, user.id)
        await bot.send_message(
            chat_id,
            f"🚫 **{user.first_name}** has been banned.\n"
            f"Reason: {reason}"
        )
    except Exception as e:
        print(f"[Argus] Ban failed: {e}")


async def main():
    print("🤖 Argus is now guarding the group!")
    print("All-seeing AI Moderator active...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
