"""
Reusable LLM provider implementations for Argus.
"""

import abc
import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any


logger = logging.getLogger("argus.ai")


def _extract_text_content(response_payload: dict[str, Any]) -> str:
    """Extract the first assistant message content from a chat completion payload."""
    choices = response_payload.get("choices") or []
    if not choices:
        raise RuntimeError("Groq API returned no choices")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        fragments: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    fragments.append(str(text))
        content = "".join(fragments)

    if not content:
        raise RuntimeError("Groq API returned an empty response")

    return str(content).strip()


class AIProvider(abc.ABC):
    """Abstract base class for all AI providers."""

    @abc.abstractmethod
    async def analyze_message(self, text: str, context: str) -> dict[str, Any]:
        """Analyze a message for moderation actions."""

    @abc.abstractmethod
    async def explain_message(self, text: str) -> str:
        """Provide an explanation for moderation/content questions."""

    @abc.abstractmethod
    async def generate_summary(self, text: str) -> str:
        """Summarize recent conversation history."""

    @abc.abstractmethod
    async def answer_question(self, question: str, rules_context: str) -> str:
        """Answer a question based on group rules."""


class GroqProvider(AIProvider):
    """Groq-backed chat completion provider using the OpenAI-compatible API."""

    def __init__(self, api_key: str, model_name: str = "llama-3.1-8b-instant") -> None:
        self.api_key = api_key
        self.model_name = model_name
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"

    def _chat_completion(self, messages: list[dict[str, str]], max_tokens: int, temperature: float | None = None) -> str:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature

        request = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Groq API request failed with HTTP {exc.code}: {details[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Groq API request failed: {exc.reason}") from exc

        return _extract_text_content(response_payload)

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
        user_prompt = (
            f"Recent chat context:\n{context}\n\n"
            f"New message to analyze: {text}"
        )
        response_text = await asyncio.to_thread(
            self._chat_completion,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            300,
            0.1,
        )

        from services.ai_service import _parse_json_response, get_default_response, validate_moderation_response

        parsed = _parse_json_response(response_text)
        if parsed is None:
            return get_default_response()
        return validate_moderation_response(parsed)

    async def explain_message(self, text: str) -> str:
        prompt = (
            "Explain briefly and politely why this message is toxic, inappropriate or violates standard group chat guidelines:\n\n"
            f"{text}"
        )
        return await asyncio.to_thread(
            self._chat_completion,
            [{"role": "user", "content": prompt}],
            200,
            None,
        )

    async def generate_summary(self, text: str) -> str:
        prompt = (
            "Summarize the following chat conversation history into bullet points highlight key topics, "
            "arguments, or decisions. Keep it structured and easy to read:\n\n"
            f"{text}"
        )
        return await asyncio.to_thread(
            self._chat_completion,
            [{"role": "user", "content": prompt}],
            400,
            None,
        )

    async def answer_question(self, question: str, rules_context: str) -> str:
        prompt = (
            "You are the group moderator. Based on the following group rules, answer the user's question. "
            "If the rules don't cover it, respond politely with standard etiquette.\n\n"
            f"Rules:\n{rules_context}\n\n"
            f"Question: {question}"
        )
        return await asyncio.to_thread(
            self._chat_completion,
            [{"role": "user", "content": prompt}],
            300,
            None,
        )


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


def build_default_providers(api_key: str, default_provider: str = "groq") -> list[AIProvider]:
    """Build the provider chain used by the application at runtime."""
    providers: list[AIProvider] = []

    provider_name = (default_provider or "groq").strip().lower()
    if provider_name != "groq":
        logger.warning("Unsupported AI provider '%s'; falling back to Groq.", provider_name)

    if api_key:
        providers.append(GroqProvider(api_key))

    providers.append(FallbackMockProvider())
    return providers