"""
Logging, telemetry, concurrency throttling, and error boundary middlewares for incoming updates.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from config.settings import settings
from utils.telemetry import correlation_scope, telemetry

logger = logging.getLogger("argus.bot.middleware")


class LoggingMiddleware(BaseMiddleware):
    """
    Assigns a correlation ID per update, enforces global and per-chat concurrency bounds,
    logs message details, and records execution latency and error metrics.
    """

    def __init__(
        self,
        max_concurrent_updates: int = settings.MAX_CONCURRENT_UPDATES,
        per_chat_limit: int = settings.PER_CHAT_CONCURRENCY_LIMIT,
    ) -> None:
        super().__init__()
        self._global_sem = asyncio.Semaphore(max(1, max_concurrent_updates))
        self._per_chat_limit = max(1, per_chat_limit)
        self._chat_sems: OrderedDict[int, asyncio.Semaphore] = OrderedDict()
        self._max_tracked_chats = 1024

    def _get_chat_semaphore(self, chat_id: int) -> asyncio.Semaphore:
        sem = self._chat_sems.get(chat_id)
        if sem is not None:
            self._chat_sems.move_to_end(chat_id)
            return sem
        sem = asyncio.Semaphore(self._per_chat_limit)
        self._chat_sems[chat_id] = sem
        while len(self._chat_sems) > self._max_tracked_chats:
            self._chat_sems.popitem(last=False)
        return sem

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        chat = event.chat
        user = event.from_user
        chat_id = chat.id if chat else 0
        chat_sem = self._get_chat_semaphore(chat_id)

        with correlation_scope() as cid:
            data["correlation_id"] = cid
            start_time = time.perf_counter()
            user_info = (
                f"user={user.id} ({user.username or user.first_name})"
                if user
                else "user=unknown"
            )
            chat_info = f"chat={chat.id} ({chat.type})" if chat else "chat=unknown"

            logger.debug(
                "Incoming message: %s %s text='%.100s'",
                user_info,
                chat_info,
                event.text or "",
            )

            telemetry.increment("messages_received_total")
            telemetry.adjust_gauge("inflight_updates", 1.0)
            try:
                async with self._global_sem, chat_sem:
                    result = await handler(event, data)
                duration = time.perf_counter() - start_time
                telemetry.increment("messages_processed_total", status="ok")
                telemetry.observe_latency("message_processing_latency", duration)
                logger.debug("Processed message in %.4f seconds", duration)
                return result
            except Exception as exc:
                duration = time.perf_counter() - start_time
                telemetry.increment("messages_processed_total", status="error")
                telemetry.increment("errors_total", component="middleware")
                telemetry.observe_latency("message_processing_latency", duration)
                logger.error(
                    "Failed to process message in %.4f seconds: %s",
                    duration,
                    exc,
                    exc_info=True,
                )
                raise
            finally:
                telemetry.adjust_gauge("inflight_updates", -1.0)


class ErrorBoundaryMiddleware(BaseMiddleware):
    """
    Outer fault-isolation middleware catching unexpected handler exceptions in production
    so a single malformed update never crashes the polling loop.
    """

    def __init__(self, suppress_exceptions: bool = True) -> None:
        super().__init__()
        self.suppress_exceptions = suppress_exceptions

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            telemetry.increment("unhandled_handler_exceptions_total", type=exc.__class__.__name__)
            logger.error("Unhandled exception caught by ErrorBoundaryMiddleware: %s", exc, exc_info=True)
            if not self.suppress_exceptions:
                raise
            return None
