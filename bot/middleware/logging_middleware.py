"""
Logging and performance monitoring middleware for incoming updates.
"""

import time
import logging
from typing import Any, Callable, Dict, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message

logger = logging.getLogger("argus.bot.middleware")


class LoggingMiddleware(BaseMiddleware):
    """Logs message details and counts execution time for performance metrics."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # Check if the event is a message
        if not isinstance(event, Message):
            return await handler(event, data)

        start_time = time.perf_counter()
        user = event.from_user
        chat = event.chat
        
        user_info = f"user={user.id} ({user.username or user.first_name})" if user else "user=unknown"
        chat_info = f"chat={chat.id} ({chat.type})"
        
        logger.debug("Incoming message: %s %s text='%.100s'", user_info, chat_info, event.text or "")

        try:
            result = await handler(event, data)
            duration = time.perf_counter() - start_time
            logger.debug("Processed message in %.4f seconds", duration)
            return result
        except Exception as exc:
            duration = time.perf_counter() - start_time
            logger.error("Failed to process message in %.4f seconds: %s", duration, exc, exc_info=True)
            raise exc
