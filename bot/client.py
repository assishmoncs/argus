"""
Bot client and dispatcher initialization.
"""

from aiogram import Bot, Dispatcher
from config.settings import settings

bot = Bot(token=settings.TELEGRAM_TOKEN)
dp = Dispatcher()
