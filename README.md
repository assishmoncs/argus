# 🤖 Argus — Advanced AI-Powered Telegram Group Manager

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Telegram Bot](https://img.shields.io/badge/Telegram-Bot-blue.svg)](https://t.me/Argus_Group_Manager_Bot)

Argus is a production-ready, highly modular, and extensible Telegram group management and moderation bot. It leverages the **Groq** API with `llama-3.1-8b-instant` to perform context-aware message analysis alongside a robust multi-tiered defensive pipeline (lexical, pattern, flood, repeat, raid, and CAPTCHA filters).

Experience Argus live on Telegram at: **[t.me/Argus_Group_Manager_Bot](https://t.me/Argus_Group_Manager_Bot)**

---

## 🚀 Key Features

### 🛡️ Multi-Tiered Content Moderation Pipeline
Every incoming message flows through a three-stage verification pipeline before delivery:
1. **Filter Service**: Real-time evaluation of custom filters (exact match, wildcard, regex), invite links, and obfuscation techniques.
2. **Spam & Flood Protection**: Heavy-duty rate limits, mention capping, repetitive pattern identification, and media frequency controls.
3. **AI Service**: Contextual checking utilizing Groq to analyze intent, tone, toxicity, or complex rules violations, with built-in heuristics-based fallback on API failure.

### 🛡️ Anti-Raid & Welcome CAPTCHA Gatekeeper
- Protects your groups from bots and coordinated user raids.
- Automatically restricts new members and issues a 90-second interactive inline button challenge.
- Auto-kicks bots and unverified users upon timeout.

### ⚙️ Extensible Plugin & Command System
Built on a clean modular architecture (SOLID principles, Repository pattern), Argus includes a plug-and-play plugin loader. Adding a new module or bot command requires zero modifications to the core engine.

---

## 🛠️ Command Reference

### Moderation Commands
* `/warn [reason]` — Issues a warning to the replied-to user (auto-escalates at limit).
* `/unwarn` — Removes the last warning from the replied-to user.
* `/warnings` — Displays the warning counts of the replied-to user.
* `/mute [reason]` — Permanently mutes the replied-to user.
* `/tmute [duration] [reason]` — Temporarily mutes the replied-to user (e.g. `30m`, `2h`, `1d`).
* `/unmute` — Lifts mute restrictions from the replied-to user.
* `/ban [reason]` — Bans the replied-to user.
* `/tban [duration] [reason]` — Temporarily bans the replied-to user.
* `/unban` — Lifts ban restrictions from the target user.
* `/kick [reason]` — Kicks the replied-to user (ban and immediate unban).
* `/reset` — Resets a user's warning history (admin-only).

### Administrative Commands
* `/promote [owner|admin|moderator|trusted]` — Sets a group member's authorization role.
* `/demote` — Revokes all privileges from a group member.
* `/lock` / `/unlock` — Restricts/restores message sending permissions in the group.
* `/pin` / `/unpin` — Pins or unpins the replied-to message.
* `/purge [N]` — Purges `N` messages or all messages up to the replied-to message.
* `/settings [key] [value]` — Configures group settings (`limit`, `ai`, `captcha`, `raid`).
* `/welcome [template]` — Custom welcome template (supports placeholders: `{first_name}`, `{username}`).
* `/goodbye [template]` — Custom goodbye template.

### Notes & Custom Triggers
* `/note [trigger] [content]` — Saves a text reply linked to a trigger.
* `/delnote [trigger]` — Deletes the specified note.
* `/notes` — Lists all registered notes for the chat.
* `#trigger` — Triggers a saved note response in the active chat.

---

## 🔧 Installation & Setup

### 1. Prerequisites
- Python 3.10 or higher
- SQLite3
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A Groq API Key (from [Groq Console](https://console.groq.com/keys))

### 2. Quickstart
Clone the repository and install dependencies:
```bash
git clone https://github.com/yourusername/argus.git
cd argus
pip install -r requirements.txt
```

### 3. Environment Configuration
Copy the sample configuration file and populate your keys:
```bash
cp .env.example .env
```
Fill in the following keys inside your `.env`:
```ini
TELEGRAM_TOKEN=your_telegram_bot_token
GROQ_API_KEY=your_groq_api_key
GROUP_CHAT_ID=0 # Set to 0 to moderate all chats, or a specific group chat ID
DATABASE_PATH=argus.db
LOG_DIR=logs
```

### 4. Running the Bot
Start the application:
```bash
python main.py
```

### 5. Running the Test Suite
The codebase includes comprehensive unit and integration tests (54 test cases covering filters, spam detection, config validation, and concurrency):
```bash
python -m pytest
```

---

## 📁 Project Architecture

```
argus/
├── main.py                     # Application entry point
├── config/                     # Configuration loading & validation
├── database/                   # Database SQLite wrapper, models, & repositories
├── services/                   # AI integration, text processing, spam & scheduler services
├── moderation/                 # Core moderation actions (bans, mutes, warnings)
├── bot/                        # Telegram router, handlers, filters, and middleware
├── plugins/                    # Modular plugin system
├── utils/                      # Helper utilities (text normalization, logging setup)
└── tests/                      # Extensive test suites
```

---

## 📄 License

This project is licensed under the **MIT License**. For the full license text, see the [LICENSE](LICENSE) file.

---

## 🤝 Contributing

Contributions are welcome! Please fork the repository, make your changes, and submit a Pull Request. Ensure that all tests pass (`python -m pytest`) before submission.
