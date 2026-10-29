"""
Demo dynamic plugin registering a basic /hello command with lifecycle cleanup.
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
            version="2.0.0",
            author="Argus Team",
            priority=50,
        )
        self.router: Optional[Router] = None

    async def load(self, bot: Any, dp: Any) -> None:
        router = Router(name="hello_plugin_router")

        @router.message(Command("hello"))
        async def hello_cmd(message: types.Message) -> None:
            await message.reply("Hello! 👋 I am running from a dynamically loaded plugin.")

        self.register_router(dp, router)

    async def unload(self, bot: Any, dp: Any) -> None:
        await super().unload(bot, dp)
        self.router = None
