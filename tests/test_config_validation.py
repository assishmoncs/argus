"""
Tests for P0-3: Input Validation for Environment Variables.

Tests validate_config() directly against the real config.py, bypassing the
conftest stub by temporarily removing the 'config' entry from sys.modules.
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def real_config_module(monkeypatch):
    """
    Temporarily remove the conftest stub so that `import config` in each test
    re-executes the real config.py. Restores the stub after the test.
    """
    stub = sys.modules.pop("config", None)
    yield
    # Restore stub (or remove if it wasn't there originally)
    sys.modules.pop("config", None)
    if stub is not None:
        sys.modules["config"] = stub


class TestValidateConfig:

    def test_valid_config_does_not_exit(self, real_config_module, monkeypatch):
        """All vars present → validate_config() returns a dict, no SystemExit."""
        monkeypatch.setenv("TELEGRAM_TOKEN", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        monkeypatch.setenv("GROUP_CHAT_ID", "0")

        import config
        assert config.TELEGRAM_TOKEN == "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        assert config.GEMINI_API_KEY == "test-gemini-key"
        assert config.GROUP_CHAT_ID == 0

    def test_missing_telegram_token_exits(self, real_config_module, monkeypatch):
        """Missing TELEGRAM_TOKEN → sys.exit(1)."""
        monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")

        with pytest.raises(SystemExit) as exc:
            import config
        assert exc.value.code == 1

    def test_missing_gemini_key_exits(self, real_config_module, monkeypatch):
        """Missing GEMINI_API_KEY → sys.exit(1)."""
        monkeypatch.setenv("TELEGRAM_TOKEN", "123:token")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        with pytest.raises(SystemExit) as exc:
            import config
        assert exc.value.code == 1

    def test_both_missing_exits_once(self, real_config_module, monkeypatch):
        """Both vars missing → single sys.exit(1) (not two separate exits)."""
        monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        with pytest.raises(SystemExit) as exc:
            import config
        assert exc.value.code == 1

    def test_invalid_group_chat_id_exits(self, real_config_module, monkeypatch):
        """Non-integer GROUP_CHAT_ID → sys.exit(1)."""
        monkeypatch.setenv("TELEGRAM_TOKEN", "123:token")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("GROUP_CHAT_ID", "not-a-number")

        with pytest.raises(SystemExit) as exc:
            import config
        assert exc.value.code == 1

    def test_group_chat_id_defaults_to_zero(self, real_config_module, monkeypatch):
        """GROUP_CHAT_ID defaults to 0 when not set."""
        monkeypatch.setenv("TELEGRAM_TOKEN", "123:token")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.delenv("GROUP_CHAT_ID", raising=False)

        import config
        assert config.GROUP_CHAT_ID == 0

    def test_whitespace_only_token_is_rejected(self, real_config_module, monkeypatch):
        """Whitespace-only TELEGRAM_TOKEN should be treated as missing."""
        monkeypatch.setenv("TELEGRAM_TOKEN", "   ")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        with pytest.raises(SystemExit) as exc:
            import config
        assert exc.value.code == 1
