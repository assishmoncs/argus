"""
Tests for moderation.py JSON parsing and response validation.
Covers P1-1: Fix Unsafe JSON Parsing in Moderation.
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# conftest.py stubs 'moderation' with an AsyncMock for use by main.py tests.
# Here we need the *real* moderation module, so pop the stub and import fresh.
# 'config' remains stubbed (safe — moderation only reads the AI key from it).
sys.modules.pop("moderation", None)
from moderation import (
    _extract_json_object,
    _parse_json_response,
    validate_moderation_response,
    get_default_response,
)


# ---------------------------------------------------------------------------
# _extract_json_object — balanced-brace extractor
# ---------------------------------------------------------------------------

class TestExtractJsonObject:

    def test_simple_object(self):
        text = '{"action": "none"}'
        assert _extract_json_object(text) == '{"action": "none"}'

    def test_nested_object(self):
        """Greedy regex would over-capture here; balanced-brace must not."""
        text = '{"action": "warn", "meta": {"score": 3}}'
        result = _extract_json_object(text)
        assert result == text

    def test_object_with_prose_before(self):
        text = 'Here is the JSON: {"action": "delete", "reason": "spam"}'
        result = _extract_json_object(text)
        assert result == '{"action": "delete", "reason": "spam"}'

    def test_object_with_prose_after(self):
        text = '{"action": "none"} (end of response)'
        result = _extract_json_object(text)
        assert result == '{"action": "none"}'

    def test_escaped_quotes_in_string(self):
        text = '{"reason": "user said \\"hello\\"", "action": "none"}'
        result = _extract_json_object(text)
        assert result == text

    def test_no_json_returns_none(self):
        assert _extract_json_object("no json here") is None

    def test_unclosed_brace_returns_none(self):
        assert _extract_json_object('{"action": "none"') is None

    def test_multiple_objects_returns_first(self):
        text = '{"action": "none"} {"action": "warn"}'
        result = _extract_json_object(text)
        assert result == '{"action": "none"}'


# ---------------------------------------------------------------------------
# _parse_json_response — full pipeline
# ---------------------------------------------------------------------------

class TestParseJsonResponse:

    def test_clean_json_string(self):
        text = '{"action": "warn", "reason": "spam", "severity": 2, "user_message": "stop"}'
        result = _parse_json_response(text)
        assert result is not None
        assert result["action"] == "warn"

    def test_json_embedded_in_prose(self):
        text = 'My decision: {"action": "delete", "reason": "nsfw", "severity": 4, "user_message": ""} Thank you.'
        result = _parse_json_response(text)
        assert result is not None
        assert result["action"] == "delete"

    def test_nested_objects_parse_correctly(self):
        text = '{"action": "none", "meta": {"foo": "bar"}, "reason": "ok", "severity": 1, "user_message": ""}'
        result = _parse_json_response(text)
        assert result is not None
        assert result["action"] == "none"

    def test_malformed_json_returns_none(self):
        result = _parse_json_response("{not valid json}")
        assert result is None

    def test_empty_string_returns_none(self):
        result = _parse_json_response("")
        assert result is None

    def test_plain_text_returns_none(self):
        result = _parse_json_response("I cannot help with that.")
        assert result is None


# ---------------------------------------------------------------------------
# validate_moderation_response
# ---------------------------------------------------------------------------

class TestValidateModerationResponse:

    def test_complete_valid_response_unchanged(self):
        resp = {"action": "warn", "reason": "toxic", "severity": 3, "user_message": "be kind"}
        result = validate_moderation_response(resp)
        assert result == resp

    def test_missing_fields_filled_with_defaults(self):
        result = validate_moderation_response({"action": "delete"})
        assert result["reason"] == ""
        assert result["severity"] == 1
        assert result["user_message"] == ""

    def test_invalid_action_defaults_to_none(self):
        result = validate_moderation_response({"action": "explode"})
        assert result["action"] == "none"

    def test_severity_clamped_above_five(self):
        result = validate_moderation_response({"action": "none", "severity": 99})
        assert result["severity"] == 5

    def test_severity_clamped_below_one(self):
        result = validate_moderation_response({"action": "none", "severity": -3})
        assert result["severity"] == 1

    def test_non_numeric_severity_defaults_to_one(self):
        result = validate_moderation_response({"action": "none", "severity": "high"})
        assert result["severity"] == 1

    def test_all_valid_actions_accepted(self):
        for action in ("none", "warn", "delete", "ban"):
            result = validate_moderation_response({"action": action})
            assert result["action"] == action


# ---------------------------------------------------------------------------
# get_default_response
# ---------------------------------------------------------------------------

class TestGetDefaultResponse:

    def test_default_response_structure(self):
        resp = get_default_response()
        assert resp["action"] == "none"
        assert resp["severity"] == 1
        assert "reason" in resp
        assert "user_message" in resp
