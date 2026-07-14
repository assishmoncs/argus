"""
Shared pytest fixtures that stub out external dependencies so tests can
import main.py without real Telegram/Gemini credentials or a live network.
"""

import sys
import types
import logging
from unittest.mock import MagicMock, AsyncMock
import pytest


def _stub_modules():
    """
    Pre-populate sys.modules with lightweight stubs for modules that require
    real credentials or heavy network dependencies at import time.
    Must run before any test module imports 'main'.
    """

    # --- Stub: config ---
    config_stub = types.ModuleType("config")
    config_stub.TELEGRAM_TOKEN = "0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    config_stub.GEMINI_API_KEY = "test-gemini-key"
    config_stub.GROUP_CHAT_ID = 0
    sys.modules["config"] = config_stub

    # --- Stub: moderation ---
    moderation_stub = types.ModuleType("moderation")
    moderation_stub.analyze_message = AsyncMock(
        return_value={"action": "none", "reason": "", "severity": 1, "user_message": ""}
    )
    sys.modules["moderation"] = moderation_stub

    # --- Stub: logging_config ---
    # Return a real (no-op) logger so main.py's setup_logging() call succeeds
    # without creating log files or console output during test runs.
    logging_config_stub = types.ModuleType("logging_config")

    def _noop_setup_logging(log_dir: str = "logs") -> logging.Logger:
        return logging.getLogger("argus")

    logging_config_stub.setup_logging = _noop_setup_logging
    sys.modules["logging_config"] = logging_config_stub


# Run once when conftest is loaded (before any test collection)
_stub_modules()
