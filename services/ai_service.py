"""
AI Service layer providing modular provider interface and automatic fallback mechanisms.
"""

import abc
import json
import logging
import asyncio
from typing import Any, Optional
from datetime import datetime, timezone
from google import genai
from google.genai import types as genai_types

from config.settings import settings

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


class AIProvider(abc.ABC):
    """Abstract Base Class defining AI provider interfaces."""

    @abc.abstractmethod
    async def analyze_message(self, text: str, context: str) -> dict[str, Any]:
        """Analyze a message for moderation actions."""
        pass

    @abc.abstractmethod
    async def explain_message(self, text: str) -> str:
        """Provide an explanation for moderation/content questions."""
        pass

    @abc.abstractmethod
    async def generate_summary(self, text: str) -> str:
        """Summarize recent conversation history."""
        pass

    @abc.abstractmethod
    async def answer_question(self, question: str, rules_context: str) -> str:
        """Answer a question based on group rules."""
        pass


class GeminiProvider(AIProvider):
    """Google Gemini AI integration."""

    def __init__(self, api_key: str, model_name: str = "gemini-2.0-flash") -> None:
        self.api_key = api_key
        self.model_name = model_name
        self.client = genai.Client(api_key=self.api_key)

    async def analyze_message(self, text: str, context: str) -> dict[str, Any]:
        system_prompt = (
            "You are Argus, an intelligent and fair AI moderator for Telegram groups.\n"
            "Protect the group from spam, toxicity, and inappropriate content. Act quickly but fairly.\n"
            "Strict Rules:\n"
            "- Delete marketing, spam, crypto, affiliate links immediately.\n"
            "- Delete adult, NSFW, gore, or disturbing media/content.\n"
            "- Warn for toxicity, harassment, swearing, or off-topic spam.\n"
            "- Calm down heated arguments before they escalate.\n"
            "Always reply with valid JSON only in this exact format:\n"
            "{\n"
            '  "action": "none" | "warn" | "delete" | "ban",\n'
            '  "reason": "brief reason for your decision",\n'
            '  "severity": 1-5,\n'
            '  "user_message": "short polite message to the user if warning"\n'
            "}"
        )
        prompt = (
            f"{system_prompt}\n\n"
            f"Recent chat context:\n{context}\n\n"
            f"New message to analyze: {text}"
        )

        def _generate():
            return self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=300,
                ),
            )

        response = await asyncio.to_thread(_generate)
        parsed = _parse_json_response(response.text)
        if parsed is None:
            return get_default_response()
        return validate_moderation_response(parsed)

    async def explain_message(self, text: str) -> str:
        prompt = f"Explain briefly and politely why this message is toxic, inappropriate or violates standard group chat guidelines:\n\n{text}"
        def _generate():
            return self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=genai_types.GenerateContentConfig(max_output_tokens=200),
            )
        response = await asyncio.to_thread(_generate)
        return response.text.strip()

    async def generate_summary(self, text: str) -> str:
        prompt = (
            "Summarize the following chat conversation history into bullet points highlight key topics, "
            "arguments, or decisions. Keep it structured and easy to read:\n\n"
            f"{text}"
        )
        def _generate():
            return self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=genai_types.GenerateContentConfig(max_output_tokens=400),
            )
        response = await asyncio.to_thread(_generate)
        return response.text.strip()

    async def answer_question(self, question: str, rules_context: str) -> str:
        prompt = (
            f"You are the group moderator. Based on the following group rules, answer the user's question. "
            f"If the rules don't cover it, respond politely with standard etiquette.\n\n"
            f"Rules:\n{rules_context}\n\n"
            f"Question: {question}"
        )
        def _generate():
            return self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=genai_types.GenerateContentConfig(max_output_tokens=300),
            )
        response = await asyncio.to_thread(_generate)
        return response.text.strip()


class FallbackMockProvider(AIProvider):
    """Local fallback / stub provider used when primary model fails."""

    async def analyze_message(self, text: str, context: str) -> dict[str, Any]:
        logger.warning("Fallback provider used for analysis.")
        bad_words = {"spam", "crypto", "bitcoin", "porn", "adult", "scam"}
        cleaned = text.lower()
        if any(word in cleaned for word in bad_words):
            return {
                "action": "delete",
                "reason": "Local heuristic check flagged content",
                "severity": 2,
                "user_message": "Please respect the rules. No spam/scams.",
            }
        return {"action": "none", "reason": "Local check passed", "severity": 1, "user_message": ""}

    async def explain_message(self, text: str) -> str:
        return "Explanation is currently unavailable. Placed under automatic heuristic guidelines."

    async def generate_summary(self, text: str) -> str:
        return "Conversation summary is currently unavailable (API Cooldown/Quota exceeded)."

    async def answer_question(self, question: str, rules_context: str) -> str:
        return "I am unable to access the AI service to answer questions at the moment. Please refer directly to the pinned rules."


class AIService:
    """Orchestrates AI tasks with automatic fallback and rate limiting."""

    def __init__(self) -> None:
        self.providers: list[AIProvider] = []
        
        if settings.GEMINI_API_KEY:
            self.providers.append(GeminiProvider(settings.GEMINI_API_KEY))

        self.providers.append(FallbackMockProvider())
        
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
