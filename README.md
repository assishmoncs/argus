# Argus - AI Powered Telegram Group Moderator

**Argus** is an intelligent Telegram bot that uses **Google's Gemini AI** to moderate group chats in real-time.

## ✨ Features

- Real-time toxicity, harassment, and bad language detection
- Auto-delete spam, marketing, affiliate links, adult/NSFW content
- Smart warning system + auto-ban on repeated violations
- Detects when discussions are getting too heated or chaotic
- Context-aware moderation (understands group dynamics)
- Highly customizable rules

## 🛠 Tech Stack

- Python 3
- [aiogram](https://aiogram.dev) — modern Telegram Bot framework
- Google Gemini 1.5 Flash (fast & efficient)
- SQLite for persistent warning tracking

## 🚀 Setup Instructions

1. **Clone** this repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Rename `.env.example` to `.env` and fill your keys
4. Create a Telegram bot with [@BotFather](https://t.me/BotFather)
5. Add **Argus** to your group as **Administrator** (with delete messages + ban users rights)
6. Run the bot:
   ```bash
   python main.py
   ```

## Configuration

- Main rules are defined in `moderation.py` (edit the `SYSTEM_PROMPT`)
- You can switch to `gemini-1.5-pro` for better reasoning (more expensive)
- Add your group ID in `.env` to restrict the bot

## Project Structure

```
argus/
├── main.py              # Bot startup and message handler
├── moderation.py        # Gemini AI analysis logic
├── config.py            # Environment configuration
├── .env.example         # Template for credentials
├── requirements.txt     # Dependencies
└── warnings.db          # Created automatically
```

---

**Built with ❤️ using Google AI Studio (Gemini API)**

Perfect for communities that want smart, always-on moderation.