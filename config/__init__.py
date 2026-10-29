"""
Configuration package for Argus.
"""

from config.settings import settings, Settings

# Expose fields at package level for backward compatibility
TELEGRAM_TOKEN: str = settings.TELEGRAM_TOKEN
GEMINI_API_KEY: str = settings.GEMINI_API_KEY
GROUP_CHAT_ID: int = settings.GROUP_CHAT_ID

__all__ = [
    "Settings",
    "settings",
    "TELEGRAM_TOKEN",
    "GEMINI_API_KEY",
    "GROUP_CHAT_ID",
]
