"""
Argus — centralised logging configuration.

Call setup_logging() once at startup (main.py). Every other module should
obtain a child logger via logging.getLogger(__name__) — no handler setup
needed in those modules.
"""

import logging
import logging.handlers
import os
from datetime import datetime, timezone


def setup_logging(log_dir: str = "logs") -> logging.Logger:
    """
    Configure application-wide logging with rotating file + console handlers.

    Returns the root 'argus' logger. Child loggers (argus.main, argus.moderation,
    etc.) inherit all handlers automatically.

    File rotation: daily at midnight, keeping 7 days of backups.
    Console: INFO and above only.
    """
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger("argus")
    # Avoid adding duplicate handlers if called more than once (e.g. in tests)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Time-based rotating file handler — rotates daily at midnight, keeps 7 days
    log_file = os.path.join(log_dir, "argus.log")
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=7, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Console handler — INFO and above only
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    logger.info("=" * 60)
    logger.info("🤖 Argus Telegram Moderator starting up")
    logger.info("Timestamp: %s", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    logger.info("=" * 60)

    return logger
