"""
Shared pytest fixtures and test setup.
Sets up environment variables for settings validation before any code imports configuration.
"""

import os
import pytest

# Configure mock environment variables for settings loading during tests
os.environ["PYTEST_CURRENT_TEST"] = "true"
os.environ["TELEGRAM_TOKEN"] = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
os.environ["GROQ_API_KEY"] = "test-groq-key"
os.environ["GROUP_CHAT_ID"] = "0"
os.environ["DATABASE_PATH"] = ":memory:"  # Use in-memory SQLite for testing
