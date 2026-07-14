"""
Argus — configuration loader with startup validation.

Imports of this module will exit immediately with a clear, human-readable
error message if any required environment variable is missing or invalid.
No other module needs to handle missing-config cases.
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()


def validate_config() -> dict:
    """
    Validate all required environment variables.

    Collects all errors before printing so the user sees every problem in
    one shot rather than fix-and-retry one variable at a time.

    Raises SystemExit(1) when validation fails.
    """
    errors = []

    telegram_token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not telegram_token:
        errors.append(
            "❌ TELEGRAM_TOKEN is not set.\n"
            "   Get it from @BotFather on Telegram: https://t.me/BotFather"
        )

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        errors.append(
            "❌ GEMINI_API_KEY is not set.\n"
            "   Get it from Google AI Studio: https://makersuite.google.com/app/apikey"
        )

    group_chat_id_raw = os.getenv("GROUP_CHAT_ID", "0").strip()
    try:
        group_chat_id = int(group_chat_id_raw)
    except ValueError:
        errors.append(
            f"❌ GROUP_CHAT_ID must be an integer (got '{group_chat_id_raw}').\n"
            "   Set it to the numeric Telegram group ID, or 0 to moderate all groups."
        )
        group_chat_id = 0  # keep a safe default so we can collect further errors

    if errors:
        print("\n🚨 Argus cannot start — configuration errors found:\n")
        for error in errors:
            print(f"  {error}\n")
        print("  Copy .env.example to .env and fill in the required values.\n")
        sys.exit(1)

    return {
        "TELEGRAM_TOKEN": telegram_token,
        "GEMINI_API_KEY": gemini_key,
        "GROUP_CHAT_ID": group_chat_id,
    }


# Validate at import time — any missing var exits before the bot even tries
# to connect to Telegram.
_config = validate_config()

TELEGRAM_TOKEN: str = _config["TELEGRAM_TOKEN"]
GEMINI_API_KEY: str = _config["GEMINI_API_KEY"]
GROUP_CHAT_ID: int = _config["GROUP_CHAT_ID"]  # 0 = moderate all groups
