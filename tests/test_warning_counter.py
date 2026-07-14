"""
Tests for Async-Safe Warning Counter.
Verifies that concurrent database increments are atomic and do not lose warning counts under load.
"""

import asyncio
import os
import tempfile
import pytest

from database.connection import Database
from database.models import WarningRepository


class TestWarningCounterConcurrency:

    @pytest.mark.asyncio
    async def test_sequential_warnings_accumulate(self):
        """Sequential warnings increment correctly."""
        # Create a temp DB
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        # Initialize Database connection
        test_db = Database(db_path=db_path)
        await test_db.run_migrations()

        # Target user ID
        user_id = 42

        # Patch models' DB to use our test DB
        from database import models
        original_db = models.db
        models.db = test_db

        try:
            # Add warnings sequentially
            for i in range(3):
                count = await WarningRepository.add_warning(
                    chat_id=100,
                    user_id=user_id,
                    reason=f"Reason {i}",
                    moderator_id=999
                )
                assert count == i + 1

            # Fetch final count
            final_count = await WarningRepository.get_warnings_count(chat_id=100, user_id=user_id)
            assert final_count == 3
        finally:
            models.db = original_db
            # Cleanup DB file
            try:
                os.unlink(db_path)
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_concurrent_warnings_no_count_lost(self):
        """
        10 tasks concurrently log warnings for the same user.
        Every increment must be registered — final total must be 10.
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        test_db = Database(db_path=db_path)
        await test_db.run_migrations()

        user_id = 99
        n_concurrent = 10

        from database import models
        original_db = models.db
        models.db = test_db

        try:
            # Fire concurrent database writes
            tasks = [
                WarningRepository.add_warning(
                    chat_id=100,
                    user_id=user_id,
                    reason=f"Concurrent {i}",
                    moderator_id=999
                )
                for i in range(n_concurrent)
            ]
            await asyncio.gather(*tasks)

            # Assert final count equals concurrency level
            final_count = await WarningRepository.get_warnings_count(chat_id=100, user_id=user_id)
            assert final_count == n_concurrent

        finally:
            models.db = original_db
            try:
                os.unlink(db_path)
            except Exception:
                pass
