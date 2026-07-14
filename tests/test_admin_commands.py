"""
Unit tests for admin-only commands — specifically the /reset permission check.

These tests mock the aiogram Bot and Message objects so no live Telegram
connection is required.
"""

import asyncio
import sqlite3
import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so we can import main.py helpers
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Helpers to build fake aiogram objects
# ---------------------------------------------------------------------------

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
    """Return a fake ChatMember whose .status matches aiogram's ChatMemberStatus values."""
    member = MagicMock()
    member.status = status
    return member


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestResetAdminCheck:
    """Verify /reset enforces admin-only access."""

    @pytest.mark.asyncio
    async def test_non_admin_is_denied(self):
        """Regular member cannot reset warnings."""
        from aiogram.enums import ChatMemberStatus

        user = make_user(user_id=1, first_name="Bob")
        message = make_message(user)

        with patch("main.bot") as mock_bot, \
             patch("main.conn") as mock_conn:

            mock_bot.get_chat_member = AsyncMock(
                return_value=make_chat_member(ChatMemberStatus.MEMBER)
            )
            # Provide a fake cursor so the DB path is not reached
            mock_conn.cursor.return_value = MagicMock()

            from main import cmd_reset
            await cmd_reset(message)

        # Should have replied with permission-denied message
        message.answer.assert_awaited_once()
        call_args = message.answer.call_args
        text = call_args.args[0] if call_args.args else ""
        assert "Permission Denied" in text or "denied" in text.lower()

    @pytest.mark.asyncio
    async def test_administrator_can_reset(self):
        """Group administrator can reset their own warnings."""
        from aiogram.enums import ChatMemberStatus

        user = make_user(user_id=2, first_name="Carol")
        message = make_message(user)

        with patch("main.bot") as mock_bot, \
             patch("main.conn") as mock_conn:

            mock_bot.get_chat_member = AsyncMock(
                return_value=make_chat_member(ChatMemberStatus.ADMINISTRATOR)
            )
            cursor_mock = MagicMock()
            mock_conn.cursor.return_value = cursor_mock

            from main import cmd_reset
            await cmd_reset(message)

        # DB update should have been called
        cursor_mock.execute.assert_called_once()
        sql = cursor_mock.execute.call_args.args[0]
        assert "UPDATE warnings" in sql

        # Success reply should have been sent
        message.answer.assert_awaited_once()
        success_text = message.answer.call_args.args[0]
        assert "✅" in success_text

    @pytest.mark.asyncio
    async def test_creator_can_reset(self):
        """Group creator can reset their own warnings."""
        from aiogram.enums import ChatMemberStatus

        user = make_user(user_id=3, first_name="Dan")
        message = make_message(user)

        with patch("main.bot") as mock_bot, \
             patch("main.conn") as mock_conn:

            mock_bot.get_chat_member = AsyncMock(
                return_value=make_chat_member(ChatMemberStatus.CREATOR)
            )
            cursor_mock = MagicMock()
            mock_conn.cursor.return_value = cursor_mock

            from main import cmd_reset
            await cmd_reset(message)

        message.answer.assert_awaited_once()
        success_text = message.answer.call_args.args[0]
        assert "✅" in success_text

    @pytest.mark.asyncio
    async def test_api_failure_returns_error(self):
        """If get_chat_member raises, the user gets a helpful error instead of a crash."""
        user = make_user(user_id=4, first_name="Eve")
        message = make_message(user)

        with patch("main.bot") as mock_bot:
            mock_bot.get_chat_member = AsyncMock(
                side_effect=Exception("Telegram API error")
            )

            from main import cmd_reset
            await cmd_reset(message)

        message.answer.assert_awaited_once()
        error_text = message.answer.call_args.args[0]
        assert "verify" in error_text.lower() or "❌" in error_text
