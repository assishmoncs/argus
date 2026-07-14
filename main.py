"""
Argus — The All-Seeing AI Guardian.
Main entry point orchestrating database setup, middleware, handlers, plugins, and bot event polling.
"""

import asyncio
import logging
from utils.logging_config import setup_logging

# Setup application-wide logging first
logger = setup_logging()

from database import db
from bot.client import bot, dp
from bot.middleware.logging_middleware import LoggingMiddleware
from services import scheduler_service
from plugins import PluginLoader

# Import routers
from bot.handlers import (
    moderation_handlers,
    admin_handlers,
    general_handlers,
    message_handlers,
)


async def main():
    # 1. Run database tables setup and data migrations
    logger.info("Initializing database and running migrations...")
    await db.run_migrations()

    # 2. Attach global middlewares
    dp.message.outer_middleware(LoggingMiddleware())

    # 3. Include command and flow routers in exact sequence
    # General & help commands first
    dp.include_router(general_handlers.router)
    # Moderation commands
    dp.include_router(moderation_handlers.router)
    # Admin commands
    dp.include_router(admin_handlers.router)
    # Catch-all message pipelines (must be last)
    dp.include_router(message_handlers.router)

    # 4. Start background scheduler task (unmutes/unbans check)
    scheduler_service.start(bot)

    # 5. Discover and load dynamic plugins
    loader = PluginLoader(bot, dp)
    logger.info("Loading dynamic plugins...")
    await loader.load_plugins()

    # 6. Begin Telegram updates polling
    logger.info("🤖 Argus is now active and guarding groups!")
    try:
        await dp.start_polling(bot)
    finally:
        # Cleanup
        logger.info("Shutting down bot services...")
        await scheduler_service.stop()
        await loader.unload_all_plugins()


if __name__ == "__main__":
    asyncio.run(main())
