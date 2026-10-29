"""
Extensible plugin system base class and lifecycle state definitions for Argus.
"""

import abc
import logging
from enum import Enum
from typing import Any, Dict, List, Optional
from aiogram import Bot, Dispatcher, Router


class PluginState(str, Enum):
    """Lifecycle states of an Argus plugin."""
    DISCOVERED = "discovered"
    LOADED = "loaded"
    ACTIVE = "active"
    FAILED = "failed"
    UNLOADED = "unloaded"
    DISABLED = "disabled"


class BasePlugin(abc.ABC):
    """
    Abstract Base Class for all dynamic Argus plugins.
    Supports metadata, dependency declarations, priority ordering, router tracking,
    health checks, and event hooks while preserving backward compatibility.
    """

    def __init__(
        self,
        name: str,
        description: str = "",
        version: str = "1.0.0",
        author: str = "Argus Contributors",
        priority: int = 100,
        dependencies: Optional[List[str]] = None,
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.description = description
        self.version = version
        self.author = author
        self.priority = priority
        self.dependencies: List[str] = list(dependencies) if dependencies else []
        self.enabled = enabled
        self.state: PluginState = PluginState.DISCOVERED if enabled else PluginState.DISABLED
        self.last_error: Optional[str] = None
        self.router: Optional[Router] = None
        self._registered_routers: List[Router] = []
        self.logger = logging.getLogger(f"argus.plugins.{self.name}")

    def register_router(self, dp: Dispatcher, router: Router) -> None:
        """Helper to include a router in the dispatcher and track it for clean unload."""
        self.router = router
        if router not in self._registered_routers:
            self._registered_routers.append(router)
        dp.include_router(router)

    @abc.abstractmethod
    async def load(self, bot: Bot, dp: Dispatcher) -> None:
        """Called during bot startup. Register routers, command handlers, or database migrations here."""
        pass

    async def unload(self, bot: Bot, dp: Dispatcher) -> None:
        """Called during bot shutdown or reload to clean up resources and detach routers."""
        sub_routers = getattr(dp, "sub_routers", None)
        for r in list(self._registered_routers):
            if isinstance(sub_routers, list) and r in sub_routers:
                sub_routers.remove(r)
            if hasattr(r, "_parent_router"):
                r._parent_router = None
        self._registered_routers.clear()

    async def on_startup(self, bot: Bot, dp: Dispatcher) -> None:
        """Optional hook invoked after all plugins have been loaded."""
        return None

    async def on_shutdown(self, bot: Bot, dp: Dispatcher) -> None:
        """Optional hook invoked right before plugins are unloaded."""
        return None

    async def on_pre_message(self, message: Any) -> Optional[Dict[str, Any]]:
        """
        Optional hook invoked before the core message moderation pipeline.
        If a dict with an 'action' key is returned, the core pipeline can act on it.
        """
        return None

    async def on_post_moderation(
        self, chat_id: int, user_id: int, action: str, reason: str
    ) -> None:
        """Optional hook invoked after a moderation action (warn/mute/ban/kick) is executed."""
        return None

    async def health_check(self) -> Dict[str, Any]:
        """Return health status and metadata for telemetry inspection."""
        return {
            "name": self.name,
            "version": self.version,
            "state": self.state.value,
            "enabled": self.enabled,
            "priority": self.priority,
            "dependencies": self.dependencies,
            "last_error": self.last_error,
        }
