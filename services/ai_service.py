"""
AI Service layer providing modular provider interface and automatic fallback mechanisms.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from config.settings import settings
from services.ai_providers import AIProvider, build_default_providers


logger = logging.getLogger("argus.ai")

_VALID_ACTIONS = {"none", "warn", "delete", "ban"}


def _extract_json_object(text: str) -> Optional[str]:
    """
    Find and return the first complete JSON object in *text* using balanced-brace counting.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i, ch in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return None


def _parse_json_response(text: str) -> Optional[dict[str, Any]]:
    """
    Robustly extract a JSON object from a response string.
    """
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    candidate = _extract_json_object(text)
    if candidate:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse extracted JSON block: %s", exc)

    logger.error("Could not extract valid JSON from response: %.200s", text)
    return None


def validate_moderation_response(response: dict[str, Any]) -> dict[str, Any]:
    """Validate and back-fill missing fields in a moderation response dict."""
    defaults = {"action": "none", "reason": "", "severity": 1, "user_message": ""}
    for field, default in defaults.items():
        if field not in response:
            response[field] = default

    if response["action"] not in _VALID_ACTIONS:
        logger.warning("Invalid action '%s' in moderation response — defaulting to 'none'", response["action"])
        response["action"] = "none"

    try:
        response["severity"] = max(1, min(5, int(response["severity"])))
    except (ValueError, TypeError):
        response["severity"] = 1

    return response


def get_default_response() -> dict[str, Any]:
    """Safe no-op response used whenever AI analysis is unavailable."""
    return {"action": "none", "reason": "AI analysis unavailable", "severity": 1, "user_message": ""}


class AIService:
    """Orchestrates AI tasks with automatic fallback and rate limiting."""

    def __init__(self, providers: Optional[list[AIProvider]] = None) -> None:
        self.providers = providers if providers is not None else build_default_providers(
            settings.GROQ_API_KEY,
            settings.DEFAULT_AI_PROVIDER,
        )

        self._rate_limits: dict[int, list[float]] = {}
        self._cooldown_window = 60.0
        self._max_requests = 10

    def _check_rate_limit(self, chat_id: int) -> bool:
        """Return True if request is allowed, False if rate limited."""
        now = datetime.now(timezone.utc).timestamp()
        if chat_id not in self._rate_limits:
            self._rate_limits[chat_id] = []
        
        self._rate_limits[chat_id] = [t for t in self._rate_limits[chat_id] if now - t < self._cooldown_window]
        
        if len(self._rate_limits[chat_id]) >= self._max_requests:
            logger.warning("AI Service rate limit hit for chat %d", chat_id)
            return False
            
        self._rate_limits[chat_id].append(now)
        return True

    async def analyze_message(self, text: str, context: str, chat_id: int = 0) -> dict[str, Any]:
        """Analyze message, falling back to next provider if errors arise."""
        if not self._check_rate_limit(chat_id):
            return {"action": "none", "reason": "Rate limited", "severity": 1, "user_message": ""}

        for provider in self.providers:
            try:
                res = await provider.analyze_message(text, context)
                if res:
                    return res
            except Exception as exc:
                logger.error("AI Provider %s failed: %s", provider.__class__.__name__, exc)
                continue
        return {"action": "none", "reason": "All providers failed", "severity": 1, "user_message": ""}

    async def explain_message(self, text: str, chat_id: int = 0) -> str:
        """Explain toxicity of a message."""
        if not self._check_rate_limit(chat_id):
            return "Too many requests. Please try again later."

        for provider in self.providers:
            try:
                return await provider.explain_message(text)
            except Exception as exc:
                logger.error("AI Provider %s failed: %s", provider.__class__.__name__, exc)
                continue
        return "Error explaining message contents."

    async def generate_summary(self, text: str, chat_id: int = 0) -> str:
        """Generate summary of text."""
        if not self._check_rate_limit(chat_id):
            return "Too many requests. Please try again later."

        for provider in self.providers:
            try:
                return await provider.generate_summary(text)
            except Exception as exc:
                logger.error("AI Provider %s failed: %s", provider.__class__.__name__, exc)
                continue
        return "Error generating conversation summary."

    async def answer_question(self, question: str, rules_context: str, chat_id: int = 0) -> str:
        """Answer rule questions."""
        if not self._check_rate_limit(chat_id):
            return "Too many requests. Please try again later."

        for provider in self.providers:
            try:
                return await provider.answer_question(question, rules_context)
            except Exception as exc:
                logger.error("AI Provider %s failed: %s", provider.__class__.__name__, exc)
                continue
        return "Error answering rule question."


ai_service = AIService()
