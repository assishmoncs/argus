"""
Tests for P0-2: Async-Safe Warning Counter.

Verifies that concurrent message handlers increment the warning counter
atomically so no warnings are lost under load.
"""

import asyncio
import sqlite3
import sys
import os
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_user(user_id: int = 42, first_name: str = "TestUser", username: str = "testuser"):
    user = MagicMock()
    user.id = user_id
    user.first_name = first_name
    user.username = username
    return user


def make_message(user, text: str = "bad message", chat_id: int = -100):
    msg = MagicMock()
    msg.from_user = user
    msg.text = text
    msg.caption = None
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.answer = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
    msg.delete = AsyncMock()
    return msg


class TestWarningCounterConcurrency:

    @pytest.mark.asyncio
    async def test_sequential_warnings_accumulate(self):
        """Baseline: sequential warnings increment correctly."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        test_conn = sqlite3.connect(db_path)
        test_conn.execute(
            "CREATE TABLE IF NOT EXISTS warnings "
            "(user_id INTEGER PRIMARY KEY, username TEXT, count INTEGER DEFAULT 0)"
        )
        test_conn.commit()

        user = make_user()

        def close_coro(coro):
            coro.close()

        with patch("main.conn", test_conn), \
             patch("main.analyze_message", new_callable=AsyncMock) as mock_analyze, \
             patch("main.ban_user", new_callable=AsyncMock), \
             patch("main.asyncio.create_task", side_effect=close_coro):

            mock_analyze.return_value = {
                "action": "warn", "reason": "test", "severity": 2, "user_message": "stop"
            }

            from main import moderate_message
            for _ in range(3):
                msg = make_message(user)
                await moderate_message(msg)

        cur = test_conn.cursor()
        cur.execute("SELECT count FROM warnings WHERE user_id=?", (user.id,))
        row = cur.fetchone()
        test_conn.close()
        os.unlink(db_path)

        assert row is not None
        assert row[0] == 3

    @pytest.mark.asyncio
    async def test_concurrent_warnings_no_count_lost(self):
        """
        Core race condition test: 10 handlers fire concurrently for the same
        user. Every increment must be counted — final total must equal 10.
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        test_conn = sqlite3.connect(db_path)
        test_conn.execute(
            "CREATE TABLE IF NOT EXISTS warnings "
            "(user_id INTEGER PRIMARY KEY, username TEXT, count INTEGER DEFAULT 0)"
        )
        test_conn.commit()

        user = make_user(user_id=99)
        n_concurrent = 10

        def close_coro(coro):
            coro.close()

        with patch("main.conn", test_conn), \
             patch("main.analyze_message", new_callable=AsyncMock) as mock_analyze, \
             patch("main.ban_user", new_callable=AsyncMock), \
             patch("main.asyncio.create_task", side_effect=close_coro):

            mock_analyze.return_value = {
                "action": "warn", "reason": "spam", "severity": 2, "user_message": "stop"
            }

            from main import moderate_message

            messages = [make_message(user, text=f"spam {i}") for i in range(n_concurrent)]
            await asyncio.gather(*[moderate_message(msg) for msg in messages])

        cur = test_conn.cursor()
        cur.execute("SELECT count FROM warnings WHERE user_id=?", (user.id,))
        row = cur.fetchone()
        test_conn.close()
        os.unlink(db_path)

        assert row is not None, "No warning row found for user"
        assert row[0] == n_concurrent, (
            f"Expected {n_concurrent} warnings, got {row[0]} — "
            "race condition detected: some increments were lost"
        )
