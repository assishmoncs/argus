"""
Dynamic plugin loader scanning, importing, and registering plugins.
"""

import os
import sys
import logging
import inspect
import importlib
from typing import Dict, Type
from aiogram import Bot, Dispatcher
from plugins.base import BasePlugin

logger = logging.getLogger("argus.plugins")


class PluginLoader:
    """Discovers and orchestrates runtime loading of plugins."""

    def __init__(self, bot: Bot, dp: Dispatcher) -> None:
        self.bot = bot
        self.dp = dp
        self.loaded_plugins: Dict[str, BasePlugin] = {}

    async def load_plugins(self, plugins_dir: str = "plugins") -> None:
        """Scan plugins folder and import BasePlugin subclasses dynamically."""
        if not os.path.exists(plugins_dir):
            os.makedirs(plugins_dir, exist_ok=True)
            return

        # Add plugins directory to python search path
        sys.path.insert(0, os.path.abspath(plugins_dir))

        for entry in os.listdir(plugins_dir):
            entry_path = os.path.join(plugins_dir, entry)
            if not os.path.isdir(entry_path) or entry.startswith("__"):
                continue

            # Try to import plugin package
            module_name = f"{plugins_dir}.{entry}"
            try:
                module = importlib.import_module(module_name)
                # Find all classes subclassing BasePlugin
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        inspect.isclass(attr)
                        and issubclass(attr, BasePlugin)
                        and attr is not BasePlugin
                    ):
                        plugin_instance: BasePlugin = attr()
                        logger.info("Discovered plugin: %s", plugin_instance.name)
                        await plugin_instance.load(self.bot, self.dp)
                        self.loaded_plugins[plugin_instance.name] = plugin_instance
            except Exception as exc:
                logger.error("Failed to load plugin from package %s: %s", entry, exc, exc_info=True)

    async def unload_all_plugins(self) -> None:
        """Call unload on all loaded plugins."""
        for name, plugin in list(self.loaded_plugins.items()):
            try:
                await plugin.unload(self.bot, self.dp)
                logger.info("Unloaded plugin: %s", name)
            except Exception as exc:
                logger.error("Failed to unload plugin %s cleanly: %s", name, exc)
        self.loaded_plugins.clear()
