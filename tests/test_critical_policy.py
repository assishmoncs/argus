from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.handlers.message_handlers import automatic_ai_action, on_message
from services.ai_service import HeuristicFallbackProvider


@pytest.mark.parametrize("text", [
    "That was stupid", "WHAT A GREAT DAY THIS IS FOR EVERYONE",
    "We discussed how to prevent scams and crypto fraud",
    "The article mentioned doxxing and suicide prevention",
])
async def test_fallback_does_not_punish_ambiguous_text(text):
    assert (await HeuristicFallbackProvider().analyze_message(text, ""))["action"] == "none"


@pytest.mark.parametrize("action,severity,expected", [
    ("warn", 2, "none"), ("delete", 4, "none"), ("ban", 4, "none"),
    ("warn", 5, "warn"), ("ban", 5, "warn"), ("none", 5, "none"),
    ("warn", None, "none"),
])
def test_ai_decisions_are_bounded(action, severity, expected):
    assert automatic_ai_action({"action": action, "severity": severity}) == expected


async def test_private_messages_are_not_moderated():
    message = MagicMock()
    message.chat.type = "private"
    message.chat.id = 123
    message.delete = AsyncMock()
    await on_message(message, MagicMock())
    message.delete.assert_not_awaited()
