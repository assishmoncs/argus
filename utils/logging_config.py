"""
Centralised logging configuration for Argus with correlation ID injection,
automatic secret redaction, and optional structured JSON output.
"""

import json
import logging
import logging.handlers
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from utils.telemetry import get_correlation_id

# Patterns that look like Telegram Bot tokens or Google API keys
_BOT_TOKEN_PATTERN = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,50}\b")
_GOOGLE_KEY_PATTERN = re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")


def redact_secrets(text: str) -> str:
    """Redact credentials, bot tokens, and API keys from log strings."""
    if not text:
        return text
    redacted = _BOT_TOKEN_PATTERN.sub("***REDACTED_BOT_TOKEN***", text)
    redacted = _GOOGLE_KEY_PATTERN.sub("***REDACTED_API_KEY***", redacted)
    for env_key in ("TELEGRAM_TOKEN", "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        secret_val = os.environ.get(env_key, "").strip()
        if secret_val and len(secret_val) >= 8 and secret_val in redacted:
            redacted = redacted.replace(secret_val, f"***REDACTED_{env_key}***")
    return redacted


class CorrelationAndRedactionFilter(logging.Filter):
    """Injects correlation_id into LogRecord and scrubs secrets from rendered messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id()
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(a) if isinstance(a, str) else a
                    for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: redact_secrets(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
        return True


class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects for log aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        correlation_id = getattr(record, "correlation_id", get_correlation_id())
        message = redact_secrets(record.getMessage())
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": correlation_id,
            "message": message,
        }
        if record.exc_info:
            payload["exception"] = redact_secrets(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_dir: str = "logs", json_format: Optional[bool] = None) -> logging.Logger:
    """
    Configure application-wide logging with rotating file + console handlers.

    File rotation: daily at midnight, keeping 7 days of backups.
    Console: INFO and above only.
    """
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger("argus")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    use_json = json_format if json_format is not None else (
        os.getenv("LOG_FORMAT", "text").strip().lower() == "json"
    )

    if use_json:
        formatter: logging.Formatter = StructuredJsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)s  [cid=%(correlation_id)s]  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    security_filter = CorrelationAndRedactionFilter()

    # Time-based rotating file handler (daily at midnight, keeping 7 days)
    log_file = os.path.join(log_dir, "argus.log")
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when="midnight", interval=1, backupCount=7, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(security_filter)
    logger.addHandler(file_handler)

    # Console handler (INFO and above)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(security_filter)
    logger.addHandler(console_handler)

    logger.info("=" * 60)
    logger.info("🤖 Argus Telegram Moderator starting up")
    logger.info("Timestamp: %s", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    logger.info("=" * 60)

    return logger
