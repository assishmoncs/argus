"""
Argus — AI-powered message moderation via Google Gemini.
"""

import google.generativeai as genai
import json
import logging
import re
from config import GEMINI_API_KEY

logger = logging.getLogger("argus.moderation")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

SYSTEM_PROMPT = """
You are **Argus**, an intelligent and fair AI moderator for Telegram groups.

Your purpose:
- Protect the group from spam, toxicity, and inappropriate content.
- Maintain respectful and on-topic discussions.
- Act quickly but fairly.

Strict Rules:
- Delete marketing, spam, crypto, affiliate links immediately.
- Delete adult, NSFW, gore, or disturbing media/content.
- Warn for toxicity, harassment, swearing, or off-topic spam.
- Calm down heated arguments before they escalate.

Always reply with **valid JSON only** in this exact format:
{
  "action": "none" | "warn" | "delete" | "ban",
  "reason": "brief reason for your decision",
  "severity": 1-5,
  "user_message": "short polite message to the user if warning"
}
"""

_VALID_ACTIONS = {"none", "warn", "delete", "ban"}


def _extract_json_object(text: str) -> str | None:
    """
    Find and return the first complete JSON object in *text* using balanced-brace
    counting. This correctly handles:
      - Nested objects  ({"a": {"b": 1}})
      - Escaped quotes  ({"a": "say \"hi\""})
      - Surrounding prose before/after the object
    Returns the raw JSON substring, or None if no balanced object is found.
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


def _parse_json_response(text: str) -> dict | None:
    """
    Robustly extract a JSON object from a Gemini response string.

    Strategy:
      1. Try to parse the full text as JSON (ideal case — no surrounding prose).
      2. Use balanced-brace extraction to find the first complete JSON object,
         then parse it (handles nested objects and escaped quotes correctly).
      3. Return None if neither attempt succeeds.
    """
    text = text.strip()

    # Attempt 1 — direct parse (fastest path, works when Gemini follows instructions)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Attempt 2 — extract first balanced {...} block
    candidate = _extract_json_object(text)
    if candidate:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Failed to parse extracted JSON block. Error: %s | Raw (first 200 chars): %.200s",
                exc,
                text,
            )

    logger.error(
        "Could not extract valid JSON from Gemini response. Raw (first 200 chars): %.200s",
        text,
    )
    return None


def validate_moderation_response(response: dict) -> dict:
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


def get_default_response() -> dict:
    """Safe no-op response used whenever AI analysis is unavailable."""
    return {"action": "none", "reason": "AI analysis unavailable", "severity": 1, "user_message": ""}


async def analyze_message(message_text: str, chat_history: str = "") -> dict:
    """
    Send a message to Gemini for a moderation decision.

    Returns a validated dict with keys: action, reason, severity, user_message.
    Falls back to a safe no-op dict on any error so the bot never crashes.
    """
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Recent chat context:\n{chat_history}\n\n"
        f"New message: {message_text}"
    )

    try:
        response = model.generate_content(
            prompt,
            generation_config={"temperature": 0.1, "max_output_tokens": 300},
        )
        text = response.text.strip()
        parsed = _parse_json_response(text)

        if parsed is None:
            return get_default_response()

        result = validate_moderation_response(parsed)
        logger.debug(
            "Moderation result — action=%s severity=%s reason=%.80s",
            result["action"],
            result["severity"],
            result["reason"],
        )
        return result

    except Exception as exc:
        logger.error("Gemini API error: %s", exc, exc_info=True)
        return get_default_response()
