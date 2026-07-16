"""
Tests for the reusable AI provider seam and Groq-backed provider behavior.
"""

import json

import pytest

from services.ai_providers import AIProvider, FallbackMockProvider, GroqProvider, build_default_providers
from services.ai_service import AIService


class StubProvider(AIProvider):
    async def analyze_message(self, text: str, context: str) -> dict[str, object]:
        return {"action": "warn", "reason": "stub", "severity": 2, "user_message": "be careful"}

    async def explain_message(self, text: str) -> str:
        return "stub explanation"

    async def generate_summary(self, text: str) -> str:
        return "stub summary"

    async def answer_question(self, question: str, rules_context: str) -> str:
        return "stub answer"


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


@pytest.mark.asyncio
async def test_ai_service_uses_injected_provider_chain():
    service = AIService(providers=[StubProvider()])

    result = await service.analyze_message("hello", "context", chat_id=1)

    assert result["action"] == "warn"
    assert await service.explain_message("hello", chat_id=1) == "stub explanation"
    assert await service.generate_summary("hello", chat_id=1) == "stub summary"
    assert await service.answer_question("hello?", "rules", chat_id=1) == "stub answer"


def test_default_provider_chain_prefers_groq():
    providers = build_default_providers("test-key", "groq")

    assert isinstance(providers[0], GroqProvider)
    assert isinstance(providers[-1], FallbackMockProvider)


@pytest.mark.asyncio
async def test_groq_provider_parses_moderation_json(monkeypatch):
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "action": "delete",
                            "reason": "spam",
                            "severity": 4,
                            "user_message": "please stop",
                        }
                    )
                }
            }
        ]
    }

    def fake_urlopen(request, timeout=30):
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr("services.ai_providers.urllib.request.urlopen", fake_urlopen)

    provider = GroqProvider("test-key")
    result = await provider.analyze_message("spam message", "context")

    assert result["action"] == "delete"
    assert result["severity"] == 4