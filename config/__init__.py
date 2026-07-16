"""
Configuration package for Argus.
"""

from config.settings import settings

# Expose fields at package level for backward compatibility
TELEGRAM_TOKEN: str = settings.TELEGRAM_TOKEN
GROQ_API_KEY: str = settings.GROQ_API_KEY
GEMINI_API_KEY: str = settings.GEMINI_API_KEY
GROUP_CHAT_ID: int = settings.GROUP_CHAT_ID
