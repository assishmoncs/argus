"""
Enterprise configuration management and environment validation for Argus.
Supports typed environment variables, range validation, and secret redaction.
"""

import os
import sys
from typing import Any, Optional
from dotenv import load_dotenv

# Only load .env if not running in pytest to avoid overriding test-mocked env vars
if "PYTEST_CURRENT_TEST" not in os.environ:
    load_dotenv()

_SECRET_KEYS = frozenset({
    "TELEGRAM_TOKEN",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
})


class Settings:
    """Argus application settings loader and validator for production scale."""

    def __init__(self) -> None:
        self.errors: list[str] = []

        # Core Required Credentials
        self.TELEGRAM_TOKEN: str = self._get_required_str(
            "TELEGRAM_TOKEN",
            "Get it from @BotFather on Telegram: https://t.me/BotFather",
        )
        self.GEMINI_API_KEY: str = self._get_required_str(
            "GEMINI_API_KEY",
            "Get it from Google AI Studio: https://makersuite.google.com/app/apikey",
        )

        # Scope & Paths
        self.GROUP_CHAT_ID: int = self._get_int("GROUP_CHAT_ID", default=0)
        self.DATABASE_PATH: str = os.getenv("DATABASE_PATH", "warnings.db").strip() or "warnings.db"
        self.LOG_DIR: str = os.getenv("LOG_DIR", "logs").strip() or "logs"
        self.PLUGINS_DIR: str = os.getenv("PLUGINS_DIR", "plugins").strip() or "plugins"
        self.ENVIRONMENT: str = self._get_choice(
            "ENVIRONMENT",
            default="production",
            choices={"development", "staging", "production", "test"},
        )

        # Logging & Telemetry
        self.LOG_LEVEL: str = self._get_choice(
            "LOG_LEVEL",
            default="INFO",
            choices={"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
            case_upper=True,
        )
        self.LOG_FORMAT: str = self._get_choice(
            "LOG_FORMAT",
            default="text",
            choices={"text", "json"},
        )
        self.LOG_BACKUP_COUNT: int = self._get_int("LOG_BACKUP_COUNT", default=7, min_val=1, max_val=365)
        self.ENABLE_TELEMETRY: bool = self._get_bool("ENABLE_TELEMETRY", default=True)

        # Database Layer & Connection Pooling
        self.DB_POOL_SIZE: int = self._get_int("DB_POOL_SIZE", default=5, min_val=1, max_val=64)
        self.DB_BUSY_TIMEOUT_MS: int = self._get_int("DB_BUSY_TIMEOUT_MS", default=5000, min_val=100, max_val=60000)
        self.DB_ENABLE_WAL: bool = self._get_bool("DB_ENABLE_WAL", default=True)
        self.DB_CACHE_SIZE_KB: int = self._get_int("DB_CACHE_SIZE_KB", default=64000, min_val=1024, max_val=512000)
        self.DB_MAX_RETRIES: int = self._get_int("DB_MAX_RETRIES", default=3, min_val=0, max_val=20)
        self.DB_RETRY_BASE_DELAY: float = self._get_float("DB_RETRY_BASE_DELAY", default=0.05, min_val=0.001, max_val=5.0)
        self.DB_REPO_CACHE_TTL: float = self._get_float("DB_REPO_CACHE_TTL", default=30.0, min_val=0.0, max_val=3600.0)

        # Gemini AI Integration, Caching, Circuit Breaker & Fallback Heuristics
        self.OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "").strip()
        self.ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.DEFAULT_AI_PROVIDER: str = os.getenv("DEFAULT_AI_PROVIDER", "gemini").strip().lower() or "gemini"
        self.GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash"
        self.GEMINI_FALLBACK_MODELS: list[str] = self._get_csv_list(
            "GEMINI_FALLBACK_MODELS",
            default=["gemini-3.5-flash-lite"],
        )
        self.AI_TIMEOUT_SECONDS: float = self._get_float("AI_TIMEOUT_SECONDS", default=10.0, min_val=0.5, max_val=120.0)
        self.AI_MAX_RETRIES: int = self._get_int("AI_MAX_RETRIES", default=2, min_val=0, max_val=10)
        self.AI_RETRY_BACKOFF_BASE: float = self._get_float("AI_RETRY_BACKOFF_BASE", default=0.4, min_val=0.01, max_val=10.0)
        self.AI_CACHE_TTL_SECONDS: float = self._get_float("AI_CACHE_TTL_SECONDS", default=300.0, min_val=0.0, max_val=86400.0)
        self.AI_CACHE_MAX_SIZE: int = self._get_int("AI_CACHE_MAX_SIZE", default=1024, min_val=16, max_val=100000)
        self.AI_CB_FAILURE_THRESHOLD: int = self._get_int("AI_CB_FAILURE_THRESHOLD", default=5, min_val=1, max_val=100)
        self.AI_CB_RECOVERY_SECONDS: float = self._get_float("AI_CB_RECOVERY_SECONDS", default=30.0, min_val=1.0, max_val=3600.0)
        self.AI_RATE_LIMIT_MAX_REQUESTS: int = self._get_int("AI_RATE_LIMIT_MAX_REQUESTS", default=10, min_val=1, max_val=10000)
        self.AI_RATE_LIMIT_WINDOW_SECONDS: float = self._get_float("AI_RATE_LIMIT_WINDOW_SECONDS", default=60.0, min_val=1.0, max_val=3600.0)
        self.AI_GLOBAL_RATE_LIMIT_PER_MIN: int = self._get_int("AI_GLOBAL_RATE_LIMIT_PER_MIN", default=120, min_val=1, max_val=100000)

        # Concurrency, Rate Limiting & Bot Stability
        self.MAX_CONCURRENT_UPDATES: int = self._get_int("MAX_CONCURRENT_UPDATES", default=64, min_val=1, max_val=2048)
        self.PER_CHAT_CONCURRENCY_LIMIT: int = self._get_int("PER_CHAT_CONCURRENCY_LIMIT", default=8, min_val=1, max_val=256)
        self.TELEGRAM_API_RETRIES: int = self._get_int("TELEGRAM_API_RETRIES", default=3, min_val=0, max_val=10)
        self.TELEGRAM_API_RETRY_DELAY: float = self._get_float("TELEGRAM_API_RETRY_DELAY", default=0.5, min_val=0.05, max_val=30.0)
        self.SCHEDULER_INTERVAL_SECONDS: float = self._get_float("SCHEDULER_INTERVAL_SECONDS", default=30.0, min_val=1.0, max_val=3600.0)
        self.CAPTCHA_TIMEOUT_SECONDS: int = self._get_int("CAPTCHA_TIMEOUT_SECONDS", default=90, min_val=10, max_val=3600)
        self.SHUTDOWN_TIMEOUT_SECONDS: float = self._get_float("SHUTDOWN_TIMEOUT_SECONDS", default=10.0, min_val=1.0, max_val=120.0)
        self.ADMIN_CACHE_TTL_SECONDS: float = self._get_float("ADMIN_CACHE_TTL_SECONDS", default=60.0, min_val=0.0, max_val=3600.0)
        self.PLUGIN_LOAD_TIMEOUT_SECONDS: float = self._get_float("PLUGIN_LOAD_TIMEOUT_SECONDS", default=10.0, min_val=1.0, max_val=120.0)

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

    def _get_int(
        self,
        var_name: str,
        default: int = 0,
        min_val: Optional[int] = None,
        max_val: Optional[int] = None,
    ) -> int:
        val_raw = os.getenv(var_name, str(default)).strip()
        try:
            parsed = int(val_raw)
        except ValueError:
            self.errors.append(f"❌ {var_name} must be an integer (got '{val_raw}').")
            return default

        if min_val is not None and parsed < min_val:
            self.errors.append(f"❌ {var_name} must be >= {min_val} (got {parsed}).")
            return default
        if max_val is not None and parsed > max_val:
            self.errors.append(f"❌ {var_name} must be <= {max_val} (got {parsed}).")
            return default
        return parsed

    def _get_float(
        self,
        var_name: str,
        default: float = 0.0,
        min_val: Optional[float] = None,
        max_val: Optional[float] = None,
    ) -> float:
        val_raw = os.getenv(var_name, str(default)).strip()
        try:
            parsed = float(val_raw)
        except ValueError:
            self.errors.append(f"❌ {var_name} must be a number (got '{val_raw}').")
            return default

        if min_val is not None and parsed < min_val:
            self.errors.append(f"❌ {var_name} must be >= {min_val} (got {parsed}).")
            return default
        if max_val is not None and parsed > max_val:
            self.errors.append(f"❌ {var_name} must be <= {max_val} (got {parsed}).")
            return default
        return parsed

    def _get_bool(self, var_name: str, default: bool = False) -> bool:
        val_raw = os.getenv(var_name, str(default)).strip().lower()
        if val_raw in ("1", "true", "yes", "on", "enabled"):
            return True
        if val_raw in ("0", "false", "no", "off", "disabled"):
            return False
        self.errors.append(f"❌ {var_name} must be a boolean (got '{val_raw}').")
        return default

    def _get_choice(
        self,
        var_name: str,
        default: str,
        choices: set[str],
        case_upper: bool = False,
    ) -> str:
        val_raw = os.getenv(var_name, default).strip()
        normalized = val_raw.upper() if case_upper else val_raw.lower()
        if normalized not in choices:
            self.errors.append(
                f"❌ {var_name} must be one of {sorted(choices)} (got '{val_raw}')."
            )
            return default
        return normalized

    @staticmethod
    def _get_csv_list(var_name: str, default: list[str]) -> list[str]:
        val_raw = os.getenv(var_name, "").strip()
        if not val_raw:
            return list(default)
        return [item.strip() for item in val_raw.split(",") if item.strip()]

    def to_redacted_dict(self) -> dict[str, Any]:
        """Return a dictionary of all settings with sensitive secrets masked."""
        out: dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if key == "errors":
                continue
            if key in _SECRET_KEYS:
                out[key] = "***REDACTED***" if value else ""
            else:
                out[key] = value
        return out


# Singleton configuration instance
settings = Settings()
