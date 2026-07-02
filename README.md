# 🤖 Argus - The All-Seeing AI Guardian

An intelligent AI-powered Telegram group moderator bot built using Google Gemini API.

## Features

- **Smart Content Moderation**: Automatically detects and deletes spam, marketing, adult/NSFW content
- **Toxic Language Detection**: Warns users for toxic language, harassment, or rule-breaking
- **Auto-Ban System**: Automatically bans repeat offenders after 3 warnings
- **Context Awareness**: Understands conversation context and group flow to make better moderation decisions
- **De-escalation**: Calms down heated arguments before they escalate
- **User Commands**:
  - `/start` - Get information about the bot
  - `/warnings` - Check your warning count
  - `/reset` - Reset your warnings (admin feature)

## Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd argus-bot
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up environment variables**
   
   Create a `.env` file in the project root:
   ```bash
   cp .env.example .env
   ```
   
   Edit `.env` with your credentials:
   ```
   TELEGRAM_TOKEN=your_telegram_bot_token_here
   GEMINI_API_KEY=your_gemini_api_key_here
   GROUP_CHAT_ID=0
   ```

4. **Get your Telegram Bot Token**
   - Open Telegram and search for `@BotFather`
   - Send `/newbot` and follow the instructions
   - Copy the token you receive

5. **Get your Gemini API Key**
   - Visit [Google AI Studio](https://makersuite.google.com/app/apikey)
   - Create an API key
   - Copy it to your `.env` file

6. **Run the bot**
   ```bash
   python main.py
   ```

## How It Works

1. **Message Analysis**: Every message is sent to Google Gemini AI for analysis
2. **Context Understanding**: The bot maintains a history of recent messages (last 10) to understand conversation context
3. **Decision Making**: AI returns a JSON response with:
   - `action`: "none", "warn", "delete", or "ban"
   - `reason`: Brief explanation of the decision
   - `severity`: 1-5 scale of violation severity
   - `user_message`: Polite message to show the user if warned
4. **Enforcement**: Based on the AI's decision, the bot:
   - Deletes inappropriate messages
   - Sends warnings to users
   - Bans users after 3 warnings or for severe violations

## Configuration

Edit `config.py` or `.env` to customize:

- `TELEGRAM_TOKEN`: Your Telegram bot token
- `GEMINI_API_KEY`: Your Google Gemini API key
- `GROUP_CHAT_ID`: Set to specific chat ID to moderate only one group, or `0` for all groups

## Project Structure

```
argus-bot/
├── main.py          # Main bot logic and message handlers
├── moderation.py    # AI-powered moderation logic
├── config.py        # Configuration and environment variables
├── requirements.txt # Python dependencies
├── .env.example     # Example environment variables
└── README.md        # This file
```

## Best For

- Community groups
- Study groups
- Project teams
- Any active Telegram group that wants smart, always-on moderation

## License

MIT License - See LICENSE file for details

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
