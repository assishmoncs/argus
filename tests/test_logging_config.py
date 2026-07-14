"""
Tests for Proper Logging System.
Verifies that setup_logging() creates the expected handlers, levels, and rotation configuration.
"""

import logging
import logging.handlers
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def clean_argus_logger():
    """Remove all handlers from the 'argus' logger before each test."""
    logger = logging.getLogger("argus")
    original_handlers = logger.handlers[:]
    logger.handlers.clear()
    yield
    logger.handlers.clear()
    for h in original_handlers:
        logger.addHandler(h)


class TestSetupLogging:

    def test_returns_argus_logger(self, tmp_path):
        from utils.logging_config import setup_logging
        logger = setup_logging(log_dir=str(tmp_path))
        assert logger.name == "argus"

    def test_logger_level_is_debug(self, tmp_path):
        from utils.logging_config import setup_logging
        logger = setup_logging(log_dir=str(tmp_path))
        assert logger.level == logging.DEBUG

    def test_two_handlers_created(self, tmp_path):
        from utils.logging_config import setup_logging
        logger = setup_logging(log_dir=str(tmp_path))
        assert len(logger.handlers) == 2

    def test_file_handler_is_timed_rotating(self, tmp_path):
        from utils.logging_config import setup_logging
        logger = setup_logging(log_dir=str(tmp_path))
        file_handlers = [
            h for h in logger.handlers
            if isinstance(h, logging.handlers.TimedRotatingFileHandler)
        ]
        assert len(file_handlers) == 1, "Expected exactly one TimedRotatingFileHandler"

    def test_file_handler_rotates_daily(self, tmp_path):
        from utils.logging_config import setup_logging
        logger = setup_logging(log_dir=str(tmp_path))
        file_handler = next(
            h for h in logger.handlers
            if isinstance(h, logging.handlers.TimedRotatingFileHandler)
        )
        assert file_handler.when.upper() == "MIDNIGHT"

    def test_file_handler_keeps_7_backups(self, tmp_path):
        from utils.logging_config import setup_logging
        logger = setup_logging(log_dir=str(tmp_path))
        file_handler = next(
            h for h in logger.handlers
            if isinstance(h, logging.handlers.TimedRotatingFileHandler)
        )
        assert file_handler.backupCount == 7

    def test_file_handler_level_is_debug(self, tmp_path):
        from utils.logging_config import setup_logging
        logger = setup_logging(log_dir=str(tmp_path))
        file_handler = next(
            h for h in logger.handlers
            if isinstance(h, logging.handlers.TimedRotatingFileHandler)
        )
        assert file_handler.level == logging.DEBUG

    def test_console_handler_level_is_info(self, tmp_path):
        from utils.logging_config import setup_logging
        logger = setup_logging(log_dir=str(tmp_path))
        stream_handlers = [
            h for h in logger.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.handlers.TimedRotatingFileHandler)
        ]
        assert len(stream_handlers) == 1
        assert stream_handlers[0].level == logging.INFO

    def test_log_directory_created(self, tmp_path):
        from utils.logging_config import setup_logging
        log_dir = tmp_path / "sublogs"
        setup_logging(log_dir=str(log_dir))
        assert log_dir.exists()

    def test_duplicate_setup_calls_do_not_add_handlers(self, tmp_path):
        """Calling setup_logging() twice must not double-register handlers."""
        from utils.logging_config import setup_logging
        setup_logging(log_dir=str(tmp_path))
        setup_logging(log_dir=str(tmp_path))
        logger = logging.getLogger("argus")
        assert len(logger.handlers) == 2
