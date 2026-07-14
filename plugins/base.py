"""
Plugin system base class definition.
"""

import abc
from aiogram import Bot, Dispatcher


class BasePlugin(abc.ABC):
    """Abstract Base Class for all dynamic Argus plugins."""

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description

    @abc.abstractmethod
    async def load(self, bot: Bot, dp: Dispatcher) -> None:
        """Called during bot startup. Register routers, command handlers, or database migrations here."""
        pass

    async def unload(self, bot: Bot, dp: Dispatcher) -> None:
        """Called during bot shutdown or reload to clean up resources (optional)."""
        pass
