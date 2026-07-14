"""
Demo dynamic plugin registering a basic /hello command.
"""

from typing import Any, Optional
from aiogram import Router, types
from aiogram.filters import Command
from plugins.base import BasePlugin


class HelloPlugin(BasePlugin):
    """Dynamic Plugin providing a simple greeting hook."""

    def __init__(self) -> None:
        super().__init__(
            name="HelloPlugin",
            description="Dynamic demo plugin that greets users on /hello command.",
        )
        self.router: Optional[Router] = None

    async def load(self, bot: Any, dp: Any) -> None:
        self.router = Router()

        @self.router.message(Command("hello"))
        async def hello_cmd(message: types.Message):
            await message.reply("Hello! 👋 I am running from a dynamically loaded plugin.")

        dp.include_router(self.router)

    async def unload(self, bot: Any, dp: Any) -> None:
        # Note: aiogram doesn't provide a direct unregister router method,
        # but we could remove hooks or simply allow garbage-collection
        pass
