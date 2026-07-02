from aiogram import Bot, Dispatcher, types
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

@dp.message()
async def moderate_message(message: types.Message):
    """Main handler for all incoming messages."""
    if GROUP_CHAT_ID != 0 and message.chat.id != GROUP_CHAT_ID:
        return

    user = message.from_user
    text = (message.text or message.caption or "").strip()
    if not text:
        return

    # TODO: Implement proper recent history fetching for better context
    history = ""

    result = await analyze_message(text, history)

    action = result.get("action", "none")

    # Delete message if needed
    if action in ["delete", "warn"]:
        try:
            await message.delete()
        except Exception as e:
            print(f"[Argus] Delete failed: {e}")

    if action == "warn":
        warning_text = f"⚠️ {user.first_name}, {result.get('user_message', 'please follow the group rules and stay respectful.')}"
        await message.answer(warning_text)

        # Update warning count
        cur = conn.cursor()
        cur.execute("INSERT INTO warnings (user_id, username, count) VALUES (?, ?, 1) "
                    "ON CONFLICT(user_id) DO UPDATE SET count = count + 1, username = ?",
                    (user.id, user.username or '', user.username or ''))
        conn.commit()

        cur.execute("SELECT count FROM warnings WHERE user_id=?", (user.id,))
        row = cur.fetchone()
        warnings = row[0] if row else 0

        if warnings >= 3:
            try:
                await bot.ban_chat_member(message.chat.id, user.id)
                await message.answer(f"🚫 {user.first_name} has been banned after multiple warnings.")
            except Exception as e:
                print(f"[Argus] Ban failed: {e}")

    elif action == "ban":
        try:
            await bot.ban_chat_member(message.chat.id, user.id)
            await message.answer(f"🚫 {user.first_name} was banned for a severe violation.")
        except:
            pass


async def main():
    print("🤖 Argus is now guarding the group!")
    print("All-seeing AI Moderator active...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
