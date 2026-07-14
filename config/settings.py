"""
Configuration management and validation for Argus.
"""

import os
import sys
from dotenv import load_dotenv

# Only load .env if not running in pytest to avoid overriding test-mocked env vars
if "PYTEST_CURRENT_TEST" not in os.environ:
    load_dotenv()


class Settings:
    """Argus application settings loader and validator."""

    def __init__(self) -> None:
        self.errors: list[str] = []

        self.TELEGRAM_TOKEN = self._get_required_str("TELEGRAM_TOKEN", "Get it from @BotFather on Telegram: https://t.me/BotFather")
        self.GEMINI_API_KEY = self._get_required_str("GEMINI_API_KEY", "Get it from Google AI Studio: https://makersuite.google.com/app/apikey")
        
        self.GROUP_CHAT_ID = self._get_int("GROUP_CHAT_ID", default=0)
        self.DATABASE_PATH = os.getenv("DATABASE_PATH", "warnings.db").strip()
        self.LOG_DIR = os.getenv("LOG_DIR", "logs").strip()
        
        # Optional AI Provider keys
        self.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
        self.ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.DEFAULT_AI_PROVIDER = os.getenv("DEFAULT_AI_PROVIDER", "gemini").strip().lower()

        if self.errors:
            print("\n🚨 Argus cannot start — configuration errors found:\n", file=sys.stderr)
            for error in self.errors:
                print(f"  {error}\n", file=sys.stderr)
            print("  Please fix the environment variables and try again.\n", file=sys.stderr)
            sys.exit(1)

    def _get_required_str(self, var_name: str, hint: str = "") -> str:
        val = os.getenv(var_name, "").strip()
        if not val:
            err = f"❌ {var_name} is not set."
            if hint:
                err += f"\n   {hint}"
            self.errors.append(err)
        return val

    def _get_int(self, var_name: str, default: int = 0) -> int:
        val_raw = os.getenv(var_name, str(default)).strip()
        try:
            return int(val_raw)
        except ValueError:
            self.errors.append(
                f"❌ {var_name} must be an integer (got '{val_raw}')."
            )
            return default


# Singleton configuration instance
settings = Settings()
