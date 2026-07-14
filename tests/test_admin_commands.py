"""
Unit tests for admin-only commands — specifically the /reset permission check.
Mocks the aiogram Bot and the WarningRepository to run without Telegram or database connections.
"""

import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_user(user_id: int = 123, first_name: str = "Alice", username: str = "alice"):
    user = MagicMock()
    user.id = user_id
    user.first_name = first_name
    user.username = username
    return user


def make_message(user, chat_id: int = -100):
    msg = MagicMock()
    msg.from_user = user
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.answer = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def make_chat_member(status: str):
    member = MagicMock()
    member.status = status
    return member


class TestResetAdminCheck:
    """Verify /reset enforces admin-only access."""

    @pytest.mark.asyncio
    async def test_non_admin_is_denied(self):
        """Regular member cannot reset warnings."""
        from aiogram.enums import ChatMemberStatus

        user = make_user(user_id=1, first_name="Bob")
        message = make_message(user)
        bot_mock = MagicMock()
        bot_mock.get_chat_member = AsyncMock(
            return_value=make_chat_member(ChatMemberStatus.MEMBER)
        )

        with patch("bot.handlers.moderation_handlers.WarningRepository.clear_warnings", new_callable=AsyncMock) as mock_clear:
            from bot.handlers.moderation_handlers import cmd_reset
            await cmd_reset(message, bot_mock)

        # Clear warnings should not have been called
        mock_clear.assert_not_called()
        
        # Should have replied with permission-denied message
        message.answer.assert_awaited_once()
        text = message.answer.call_args.args[0]
        assert "Permission Denied" in text

    @pytest.mark.asyncio
    async def test_administrator_can_reset(self):
        """Group administrator can reset their warnings."""
        from aiogram.enums import ChatMemberStatus

        user = make_user(user_id=2, first_name="Carol")
        message = make_message(user)
        bot_mock = MagicMock()
        bot_mock.get_chat_member = AsyncMock(
            return_value=make_chat_member(ChatMemberStatus.ADMINISTRATOR)
        )

        with patch("bot.handlers.moderation_handlers.WarningRepository.clear_warnings", new_callable=AsyncMock) as mock_clear:
            from bot.handlers.moderation_handlers import cmd_reset
            await cmd_reset(message, bot_mock)

        # DB update should have been called
        mock_clear.assert_awaited_once_with(message.chat.id, user.id)

        # Success reply should have been sent
        message.answer.assert_awaited_once()
        success_text = message.answer.call_args.args[0]
        assert "✅" in success_text

    @pytest.mark.asyncio
    async def test_creator_can_reset(self):
        """Group creator can reset their warnings."""
        from aiogram.enums import ChatMemberStatus

        user = make_user(user_id=3, first_name="Dan")
        message = make_message(user)
        bot_mock = MagicMock()
        bot_mock.get_chat_member = AsyncMock(
            return_value=make_chat_member(ChatMemberStatus.CREATOR)
        )

        with patch("bot.handlers.moderation_handlers.WarningRepository.clear_warnings", new_callable=AsyncMock) as mock_clear:
            from bot.handlers.moderation_handlers import cmd_reset
            await cmd_reset(message, bot_mock)

        mock_clear.assert_awaited_once_with(message.chat.id, user.id)
        message.answer.assert_awaited_once()
        success_text = message.answer.call_args.args[0]
        assert "✅" in success_text

    @pytest.mark.asyncio
    async def test_api_failure_returns_error(self):
        """If get_chat_member raises, the user gets a helpful error instead of a crash."""
        user = make_user(user_id=4, first_name="Eve")
        message = make_message(user)
        bot_mock = MagicMock()
        bot_mock.get_chat_member = AsyncMock(
            side_effect=Exception("Telegram API error")
        )

        with patch("bot.handlers.moderation_handlers.WarningRepository.clear_warnings", new_callable=AsyncMock) as mock_clear:
            from bot.handlers.moderation_handlers import cmd_reset
            await cmd_reset(message, bot_mock)

        mock_clear.assert_not_called()
        message.answer.assert_awaited_once()
        error_text = message.answer.call_args.args[0]
        assert "verify" in error_text.lower()
