"""
Argus — The All-Seeing AI Guardian.
Enterprise main entry point orchestrating database pool initialization, middleware stack,
handlers, dynamic plugins, background tasks, and graceful shutdown.
"""

import asyncio
import logging
from utils.logging_config import setup_logging

# Setup application-wide logging first
logger = setup_logging()

from config.settings import settings
from database import db
from bot.client import bot, dp
from bot.middleware.logging_middleware import ErrorBoundaryMiddleware, LoggingMiddleware
from moderation.actions import task_manager
from services import scheduler_service
from plugins import PluginLoader

# Import routers
from bot.handlers import (
    moderation_handlers,
    admin_handlers,
    general_handlers,
    message_handlers,
)


async def main() -> None:
    # 1. Run database tables setup, indexes, and data migrations
    logger.info("Initializing database connection pool and running migrations...")
    await db.run_migrations()
    db_health = await db.health_check()
    logger.info("Database health check: %s", db_health)

    # 2. Attach global middlewares (ErrorBoundary outer -> Logging/Concurrency inner)
    dp.message.outer_middleware(ErrorBoundaryMiddleware(suppress_exceptions=True))
    dp.message.outer_middleware(LoggingMiddleware())

    # 3. Include command and flow routers in exact sequence
    dp.include_router(general_handlers.router)
    dp.include_router(moderation_handlers.router)
    dp.include_router(admin_handlers.router)
    # Catch-all message pipelines (must be last)
    dp.include_router(message_handlers.router)

    # 4. Start background scheduler task (unmutes/unbans check & cache maintenance)
    scheduler_service.start(bot)

    # 5. Discover and load dynamic plugins
    loader = PluginLoader(bot, dp)
    logger.info("Loading dynamic plugins from '%s'...", settings.PLUGINS_DIR)
    await loader.load_plugins(settings.PLUGINS_DIR)

    # 6. Begin Telegram updates polling with guaranteed graceful shutdown
    logger.info("🤖 Argus is now active and guarding groups!")
    try:
        await dp.start_polling(bot)
    finally:
        logger.info("Shutting down bot services gracefully...")
        await scheduler_service.stop()
        await loader.unload_all_plugins()
        await task_manager.cancel_all(timeout=settings.SHUTDOWN_TIMEOUT_SECONDS)
        await db.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
