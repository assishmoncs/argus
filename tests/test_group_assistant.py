from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatMemberStatus

from bot.handlers import message_handlers as handlers


@pytest.fixture
def group_message(monkeypatch):
    msg = MagicMock()
    msg.chat.id = -123
    msg.chat.type = "supergroup"
    msg.from_user.id = 42
    msg.from_user.is_bot = False
    msg.from_user.first_name = "Member"
    msg.caption = None
    msg.reply_to_message = None
    msg.reply = AsyncMock()
    msg.delete = AsyncMock()
    bot = MagicMock(id=99)
    monkeypatch.setattr(handlers.UserRepository, "get_or_create_user", AsyncMock())
    monkeypatch.setattr(handlers, "get_cached_chat_member_status", AsyncMock(return_value=ChatMemberStatus.MEMBER))
    monkeypatch.setattr(handlers.filter_service, "check_message", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers.spam_service, "check_spam", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers.GroupRepository, "get_group", AsyncMock(return_value={"ai_enabled": 1}))
    monkeypatch.setattr(handlers.ai_service, "analyze_message", AsyncMock(return_value={"action": "none"}))
    handlers._reminder_times.clear()
    return msg, bot


async def test_explicit_language_gets_reply_without_strike(group_message, monkeypatch):
    msg, bot = group_message
    msg.text = "That is bullshit and shit"
    warn = AsyncMock()
    monkeypatch.setattr(handlers.ModerationActions, "warn", warn)
    await handlers.on_message(msg, bot)
    msg.reply.assert_awaited_once()
    warn.assert_not_awaited()
    msg.delete.assert_not_awaited()


async def test_wake_and_followup_answer(group_message, monkeypatch):
    msg, bot = group_message
    answer = AsyncMock(return_value="An answer")
    monkeypatch.setattr(handlers.ai_service, "answer_question", answer)
    msg.text = "hey Argus"
    await handlers.on_message(msg, bot)
    assert "listening" in msg.reply.await_args.args[0]
    msg.reply.reset_mock()
    msg.text = "What is Python?"
    msg.reply_to_message = MagicMock(from_user=MagicMock(id=99))
    await handlers.on_message(msg, bot)
    answer.assert_awaited_once_with("What is Python?", "", -123)
    msg.reply.assert_awaited_once_with("An answer")


async def test_fighting_gets_gentle_reply(group_message, monkeypatch):
    msg, bot = group_message
    msg.text = "You never listen to anyone"
    handlers.ai_service.analyze_message.return_value = {"action": "none", "fighting": True}
    warn = AsyncMock()
    monkeypatch.setattr(handlers.ModerationActions, "warn", warn)
    await handlers.on_message(msg, bot)
    assert "respectful" in msg.reply.await_args.args[0]
    warn.assert_not_awaited()
    msg.delete.assert_not_awaited()
