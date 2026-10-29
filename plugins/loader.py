"""
Dynamic plugin loader scanning, validating dependencies, importing, and orchestrating
plugin lifecycles and event hooks with error isolation and timeout guardrails.
"""

import asyncio
import importlib
import inspect
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Set, Tuple
from aiogram import Bot, Dispatcher

from config.settings import settings
from plugins.base import BasePlugin, PluginState
from utils.telemetry import telemetry

logger = logging.getLogger("argus.plugins")

_RESERVED_MODULES = frozenset({"__init__", "base", "loader"})


class PluginLoader:
    """Discovers and orchestrates runtime loading, hot-reloading, and hook execution of plugins."""

    def __init__(
        self,
        bot: Bot,
        dp: Dispatcher,
        load_timeout_seconds: Optional[float] = None,
    ) -> None:
        self.bot = bot
        self.dp = dp
        self.load_timeout_seconds = (
            load_timeout_seconds
            if load_timeout_seconds is not None
            else settings.PLUGIN_LOAD_TIMEOUT_SECONDS
        )
        self.loaded_plugins: Dict[str, BasePlugin] = {}
        self.failed_plugins: Dict[str, str] = {}
        self._plugin_modules: Dict[str, str] = {}

    @staticmethod
    def _sort_plugins_by_dependencies(plugins: List[BasePlugin]) -> Tuple[List[BasePlugin], Dict[str, str]]:
        """
        Order plugins by priority and resolve inter-plugin dependencies topologically.
        Returns (ordered_plugins, failed_dependency_reasons).
        """
        by_name: Dict[str, BasePlugin] = {p.name: p for p in plugins}
        failed: Dict[str, str] = {}

        # Sort initial candidates deterministically by (priority, name)
        candidates = sorted(plugins, key=lambda p: (p.priority, p.name))
        ordered: List[BasePlugin] = []
        visited: Set[str] = set()
        visiting: Set[str] = set()

        def _visit(plugin: BasePlugin) -> bool:
            if plugin.name in visited:
                return True
            if plugin.name in failed:
                return False
            if plugin.name in visiting:
                reason = f"Circular dependency detected involving plugin '{plugin.name}'"
                failed[plugin.name] = reason
                plugin.state = PluginState.FAILED
                plugin.last_error = reason
                return False

            visiting.add(plugin.name)
            for dep in plugin.dependencies:
                dep_plugin = by_name.get(dep)
                if dep_plugin is None or not dep_plugin.enabled:
                    reason = f"Missing or disabled dependency '{dep}' for plugin '{plugin.name}'"
                    failed[plugin.name] = reason
                    plugin.state = PluginState.FAILED
                    plugin.last_error = reason
                    visiting.remove(plugin.name)
                    return False
                if not _visit(dep_plugin):
                    reason = f"Dependency '{dep}' failed to load for plugin '{plugin.name}'"
                    failed[plugin.name] = reason
                    plugin.state = PluginState.FAILED
                    plugin.last_error = reason
                    visiting.remove(plugin.name)
                    return False

            visiting.remove(plugin.name)
            visited.add(plugin.name)
            ordered.append(plugin)
            return True

        for candidate in candidates:
            if not candidate.enabled:
                candidate.state = PluginState.DISABLED
                continue
            _visit(candidate)

        return ordered, failed

    def _discover_plugin_classes(self, plugins_dir: str) -> List[Tuple[str, BasePlugin]]:
        """Scan plugins folder and instantiate discovered BasePlugin subclasses."""
        discovered: List[Tuple[str, BasePlugin]] = []
        abs_plugins_dir = os.path.abspath(plugins_dir)
        parent_dir = os.path.dirname(abs_plugins_dir)
        pkg_prefix = os.path.basename(abs_plugins_dir)

        if parent_dir and parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        if abs_plugins_dir not in sys.path:
            sys.path.insert(0, abs_plugins_dir)

        for entry in sorted(os.listdir(plugins_dir)):
            if entry.startswith("__") or entry.startswith("."):
                continue
            entry_path = os.path.join(plugins_dir, entry)

            module_name: Optional[str] = None
            if os.path.isdir(entry_path):
                module_name = f"{pkg_prefix}.{entry}"
            elif entry.endswith(".py"):
                stem = entry[:-3]
                if stem in _RESERVED_MODULES:
                    continue
                module_name = f"{pkg_prefix}.{stem}"

            if not module_name:
                continue

            try:
                if module_name in sys.modules:
                    module = importlib.reload(sys.modules[module_name])
                else:
                    module = importlib.import_module(module_name)

                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        inspect.isclass(attr)
                        and issubclass(attr, BasePlugin)
                        and attr is not BasePlugin
                    ):
                        instance: BasePlugin = attr()
                        discovered.append((module_name, instance))
            except Exception as exc:
                self.failed_plugins[entry] = str(exc)
                telemetry.increment("plugins_failed_total", plugin=entry)
                logger.error(
                    "Failed to import plugin from '%s': %s", entry, exc, exc_info=True
                )

        return discovered

    async def load_plugins(self, plugins_dir: str = "plugins") -> None:
        """Scan plugins folder, validate dependencies, and load BasePlugin subclasses safely."""
        if not os.path.exists(plugins_dir):
            os.makedirs(plugins_dir, exist_ok=True)
            return

        discovered = self._discover_plugin_classes(plugins_dir)
        if not discovered:
            return

        # Deduplicate by plugin name
        unique_plugins: List[BasePlugin] = []
        seen_names: Set[str] = set()
        for mod_name, plugin in discovered:
            if plugin.name in seen_names:
                logger.warning("Duplicate plugin name '%s' in %s skipped.", plugin.name, mod_name)
                continue
            seen_names.add(plugin.name)
            self._plugin_modules[plugin.name] = mod_name
            unique_plugins.append(plugin)

        ordered_plugins, dep_failures = self._sort_plugins_by_dependencies(unique_plugins)
        for failed_name, reason in dep_failures.items():
            self.failed_plugins[failed_name] = reason
            telemetry.increment("plugins_failed_total", plugin=failed_name)
            logger.error("Plugin '%s' skipped due to dependency error: %s", failed_name, reason)

        for plugin in ordered_plugins:
            try:
                logger.info(
                    "Loading plugin: %s (v%s, priority=%d)",
                    plugin.name,
                    plugin.version,
                    plugin.priority,
                )
                await asyncio.wait_for(
                    plugin.load(self.bot, self.dp),
                    timeout=self.load_timeout_seconds,
                )
                if plugin.router and plugin.router not in plugin._registered_routers:
                    plugin._registered_routers.append(plugin.router)
                plugin.state = PluginState.ACTIVE
                self.loaded_plugins[plugin.name] = plugin
                telemetry.increment("plugins_loaded_total", plugin=plugin.name)
            except Exception as exc:
                plugin.state = PluginState.FAILED
                plugin.last_error = str(exc)
                self.failed_plugins[plugin.name] = str(exc)
                telemetry.increment("plugins_failed_total", plugin=plugin.name)
                logger.error("Failed to load plugin '%s': %s", plugin.name, exc, exc_info=True)

        # Invoke post-load startup hooks
        for plugin in self.loaded_plugins.values():
            try:
                await asyncio.wait_for(
                    plugin.on_startup(self.bot, self.dp),
                    timeout=self.load_timeout_seconds,
                )
            except Exception as exc:
                logger.error("Plugin '%s' on_startup hook failed: %s", plugin.name, exc)

        telemetry.set_gauge("active_plugins", len(self.loaded_plugins))

    async def reload_plugin(self, name: str) -> bool:
        """Hot-reload a loaded plugin by name."""
        plugin = self.loaded_plugins.get(name)
        mod_name = self._plugin_modules.get(name)
        if not plugin or not mod_name:
            return False

        try:
            await plugin.on_shutdown(self.bot, self.dp)
            await plugin.unload(self.bot, self.dp)
            plugin.state = PluginState.UNLOADED
            self.loaded_plugins.pop(name, None)

            module = importlib.reload(sys.modules[mod_name])
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    inspect.isclass(attr)
                    and issubclass(attr, BasePlugin)
                    and attr is not BasePlugin
                ):
                    new_instance: BasePlugin = attr()
                    if new_instance.name == name:
                        await asyncio.wait_for(
                            new_instance.load(self.bot, self.dp),
                            timeout=self.load_timeout_seconds,
                        )
                        new_instance.state = PluginState.ACTIVE
                        await new_instance.on_startup(self.bot, self.dp)
                        self.loaded_plugins[name] = new_instance
                        logger.info("Successfully hot-reloaded plugin: %s", name)
                        return True
        except Exception as exc:
            self.failed_plugins[name] = str(exc)
            logger.error("Failed to hot-reload plugin '%s': %s", name, exc, exc_info=True)
        return False

    async def dispatch_pre_message(self, message: Any) -> Optional[Dict[str, Any]]:
        """Dispatch pre-message hook across active plugins in priority order."""
        for plugin in list(self.loaded_plugins.values()):
            if plugin.state != PluginState.ACTIVE:
                continue
            try:
                res = await plugin.on_pre_message(message)
                if isinstance(res, dict) and res.get("action"):
                    telemetry.increment("plugin_events_total", plugin=plugin.name, hook="pre_message")
                    return res
            except Exception as exc:
                telemetry.increment("plugin_hook_errors_total", plugin=plugin.name, hook="pre_message")
                logger.error("Plugin '%s' on_pre_message failed: %s", plugin.name, exc)
        return None

    async def dispatch_post_moderation(
        self, chat_id: int, user_id: int, action: str, reason: str
    ) -> None:
        """Notify all active plugins after a moderation action occurs."""
        for plugin in list(self.loaded_plugins.values()):
            if plugin.state != PluginState.ACTIVE:
                continue
            try:
                await plugin.on_post_moderation(chat_id, user_id, action, reason)
                telemetry.increment("plugin_events_total", plugin=plugin.name, hook="post_moderation")
            except Exception as exc:
                telemetry.increment(
                    "plugin_hook_errors_total", plugin=plugin.name, hook="post_moderation"
                )
                logger.error("Plugin '%s' on_post_moderation failed: %s", plugin.name, exc)

    async def unload_all_plugins(self) -> None:
        """Call on_shutdown and unload on all loaded plugins in reverse load order."""
        for name, plugin in reversed(list(self.loaded_plugins.items())):
            try:
                await asyncio.wait_for(
                    plugin.on_shutdown(self.bot, self.dp),
                    timeout=self.load_timeout_seconds,
                )
            except Exception as exc:
                logger.error("Plugin '%s' on_shutdown hook failed: %s", name, exc)
            try:
                await asyncio.wait_for(
                    plugin.unload(self.bot, self.dp),
                    timeout=self.load_timeout_seconds,
                )
                plugin.state = PluginState.UNLOADED
                logger.info("Unloaded plugin: %s", name)
            except Exception as exc:
                logger.error("Failed to unload plugin %s cleanly: %s", name, exc)
        self.loaded_plugins.clear()
        telemetry.set_gauge("active_plugins", 0)

    async def get_status_report(self) -> Dict[str, Any]:
        """Return health status of all loaded and failed plugins."""
        active_reports = {}
        for name, plugin in self.loaded_plugins.items():
            active_reports[name] = await plugin.health_check()
        return {
            "loaded_count": len(self.loaded_plugins),
            "failed_count": len(self.failed_plugins),
            "loaded_plugins": active_reports,
            "failed_plugins": dict(self.failed_plugins),
        }
