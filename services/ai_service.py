"""
Enterprise AI Service layer providing Google Gemini integration with LRU+TTL caching,
circuit breaker resilience, exponential backoff retries, model fallback cascades,
and a multi-signal heuristic fallback engine.
"""

import abc
import asyncio
import copy
import hashlib
import json
import logging
import random
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from google import genai
from google.genai import types as genai_types

from config.settings import settings
from utils.telemetry import telemetry
from utils.text_utils import (
    clean_obfuscation,
    collapse_duplicates,
    normalize_for_cache,
    normalize_unicode,
    remove_emojis,
)

logger = logging.getLogger("argus.ai")

_VALID_ACTIONS = {"none", "warn", "delete", "ban"}


def _extract_json_object(text: str) -> Optional[str]:
    """
    Find and return the first complete JSON object in *text* using balanced-brace counting.
    """
    if not text:
        return None
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
    if not text:
        logger.error("Could not extract valid JSON from response: ")
        return None
    text = text.strip()
    if not text:
        logger.error("Could not extract valid JSON from response: ")
        return None

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    candidate = _extract_json_object(text)
    if candidate:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse extracted JSON block: %s", exc)

    logger.error("Could not extract valid JSON from response: %.200s", text)
    return None


def validate_moderation_response(response: dict[str, Any]) -> dict[str, Any]:
    """Validate and back-fill missing fields in a moderation response dict."""
    defaults = {"action": "none", "reason": "", "severity": 1, "user_message": ""}
    for field, default in defaults.items():
        if field not in response or response[field] is None:
            response[field] = default

    if response["action"] not in _VALID_ACTIONS:
        logger.warning(
            "Invalid action '%s' in moderation response — defaulting to 'none'",
            response["action"],
        )
        response["action"] = "none"

    try:
        response["severity"] = max(1, min(5, int(response["severity"])))
    except (ValueError, TypeError):
        response["severity"] = 1

    response["reason"] = str(response.get("reason", ""))
    response["user_message"] = str(response.get("user_message", ""))
    return response


def get_default_response() -> dict[str, Any]:
    """Safe no-op response used whenever AI analysis is unavailable."""
    return {
        "action": "none",
        "reason": "AI analysis unavailable",
        "severity": 1,
        "user_message": "",
    }


class AIResponseCache:
    """Thread-safe LRU + TTL cache for AI moderation and Q&A responses."""

    def __init__(self, max_size: int = 1024, ttl_seconds: float = 300.0) -> None:
        self.max_size = max(1, max_size)
        self.ttl_seconds = max(0.0, ttl_seconds)
        self._lock = threading.RLock()
        self._store: OrderedDict[str, Tuple[float, Any]] = OrderedDict()
        self.hits: int = 0
        self.misses: int = 0

    @staticmethod
    def build_key(operation: str, text: str, extra: str = "") -> str:
        norm_text = normalize_for_cache(text)
        norm_extra = normalize_for_cache(extra)
        digest = hashlib.sha256(f"{operation}|{norm_text}|{norm_extra}".encode("utf-8")).hexdigest()
        return f"{operation}:{digest[:32]}"

    def get(self, key: str) -> Optional[Any]:
        if self.ttl_seconds <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                telemetry.increment("ai_cache_misses_total")
                return None
            expires_at, val = entry
            if now >= expires_at:
                del self._store[key]
                self.misses += 1
                telemetry.increment("ai_cache_misses_total")
                return None
            self._store.move_to_end(key)
            self.hits += 1
            telemetry.increment("ai_cache_hits_total")
            return copy.deepcopy(val)

    def set(self, key: str, value: Any) -> None:
        if self.ttl_seconds <= 0:
            return
        now = time.monotonic()
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (now + self.ttl_seconds, copy.deepcopy(value))
            while len(self._store) > self.max_size:
                self._store.popitem(last=False)
            telemetry.set_gauge("ai_cache_size", len(self._store))

    def evict_expired(self) -> int:
        now = time.monotonic()
        removed = 0
        with self._lock:
            expired_keys = [k for k, (exp, _) in self._store.items() if now >= exp]
            for k in expired_keys:
                del self._store[k]
                removed += 1
            telemetry.set_gauge("ai_cache_size", len(self._store))
        return removed

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0
            telemetry.set_gauge("ai_cache_size", 0)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            hit_rate = round((self.hits / total) * 100.0, 2) if total > 0 else 0.0
            return {
                "size": len(self._store),
                "max_size": self.max_size,
                "ttl_seconds": self.ttl_seconds,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate_pct": hit_rate,
            }


class CircuitBreaker:
    """
    Three-state circuit breaker (closed -> open -> half_open) protecting upstream AI providers
    from cascading timeouts and quota exhaustion.
    """

    def __init__(self, failure_threshold: int = 5, recovery_seconds: float = 30.0) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_seconds = max(1.0, recovery_seconds)
        self._lock = threading.RLock()
        self._state: str = "closed"
        self._failure_count: int = 0
        self._opened_at: float = 0.0

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "open":
                if time.monotonic() - self._opened_at >= self.recovery_seconds:
                    self._state = "half_open"
                    telemetry.set_gauge("ai_circuit_breaker_state", 1.0)
            return self._state

    def allow_request(self) -> bool:
        current = self.state
        return current in ("closed", "half_open")

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._state = "closed"
            telemetry.set_gauge("ai_circuit_breaker_state", 0.0)

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            if self._failure_count >= self.failure_threshold or self._state == "half_open":
                self._state = "open"
                self._opened_at = time.monotonic()
                telemetry.set_gauge("ai_circuit_breaker_state", 2.0)
                telemetry.increment("ai_circuit_breaker_trips_total")
                logger.warning(
                    "AI CircuitBreaker tripped to OPEN after %d consecutive failures (cooldown %.1fs)",
                    self._failure_count,
                    self.recovery_seconds,
                )

    def reset(self) -> None:
        with self._lock:
            self._state = "closed"
            self._failure_count = 0
            self._opened_at = 0.0
            telemetry.set_gauge("ai_circuit_breaker_state", 0.0)


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
    """
    Production Google Gemini AI integration with timeout enforcement,
    exponential backoff retry, and automatic model fallback cascade.
    """

    def __init__(
        self,
        api_key: str,
        model_name: Optional[str] = None,
        fallback_models: Optional[List[str]] = None,
        timeout_seconds: Optional[float] = None,
        max_retries: Optional[int] = None,
        backoff_base: Optional[float] = None,
    ) -> None:
        self.api_key = api_key
        self.model_name = model_name or settings.GEMINI_MODEL
        self.fallback_models = (
            fallback_models if fallback_models is not None else list(settings.GEMINI_FALLBACK_MODELS)
        )
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.AI_TIMEOUT_SECONDS
        )
        self.max_retries = max_retries if max_retries is not None else settings.AI_MAX_RETRIES
        self.backoff_base = (
            backoff_base if backoff_base is not None else settings.AI_RETRY_BACKOFF_BASE
        )
        self.client = genai.Client(api_key=self.api_key)

    def _candidate_models(self) -> List[str]:
        models = [self.model_name]
        for m in self.fallback_models:
            if m and m not in models:
                models.append(m)
        return models

    async def _call_gemini(
        self,
        prompt: str,
        max_output_tokens: int = 300,
        temperature: float = 0.1,
    ) -> str:
        """Execute a Gemini generation call with retries, timeout, and model fallback."""
        last_exc: Optional[Exception] = None
        for model in self._candidate_models():
            for attempt in range(self.max_retries + 1):
                start = time.perf_counter()
                try:
                    def _generate() -> Any:
                        return self.client.models.generate_content(
                            model=model,
                            contents=prompt,
                            config=genai_types.GenerateContentConfig(
                                temperature=temperature,
                                max_output_tokens=max_output_tokens,
                            ),
                        )

                    response = await asyncio.wait_for(
                        asyncio.to_thread(_generate),
                        timeout=self.timeout_seconds,
                    )
                    telemetry.increment("ai_gemini_calls_total", model=model, status="ok")
                    telemetry.observe_latency(
                        "ai_gemini_latency", time.perf_counter() - start, model=model
                    )
                    return getattr(response, "text", "") or ""
                except Exception as exc:
                    last_exc = exc
                    telemetry.increment("ai_gemini_calls_total", model=model, status="error")
                    if attempt < self.max_retries:
                        delay = self.backoff_base * (2 ** attempt) + random.uniform(0.0, 0.1)
                        logger.warning(
                            "Gemini model '%s' call failed (attempt %d/%d): %s — retrying in %.2fs",
                            model,
                            attempt + 1,
                            self.max_retries + 1,
                            exc,
                            delay,
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.warning(
                            "Gemini model '%s' exhausted retries: %s", model, exc
                        )
        if last_exc:
            raise last_exc
        raise RuntimeError("No Gemini models configured")

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

        raw_text = await self._call_gemini(prompt, max_output_tokens=300, temperature=0.1)
        parsed = _parse_json_response(raw_text)
        if parsed is None:
            return get_default_response()
        return validate_moderation_response(parsed)

    async def explain_message(self, text: str) -> str:
        prompt = (
            "Explain briefly and politely why this message is toxic, inappropriate or violates "
            f"standard group chat guidelines:\n\n{text}"
        )
        raw_text = await self._call_gemini(prompt, max_output_tokens=200, temperature=0.2)
        return raw_text.strip()

    async def generate_summary(self, text: str) -> str:
        prompt = (
            "Summarize the following chat conversation history into bullet points highlight key topics, "
            "arguments, or decisions. Keep it structured and easy to read:\n\n"
            f"{text}"
        )
        raw_text = await self._call_gemini(prompt, max_output_tokens=400, temperature=0.3)
        return raw_text.strip()

    async def answer_question(self, question: str, rules_context: str) -> str:
        prompt = (
            "You are the group moderator. Based on the following group rules, answer the user's question. "
            "If the rules don't cover it, respond politely with standard etiquette.\n\n"
            f"Rules:\n{rules_context}\n\n"
            f"Question: {question}"
        )
        raw_text = await self._call_gemini(prompt, max_output_tokens=300, temperature=0.2)
        return raw_text.strip()


class HeuristicFallbackProvider(AIProvider):
    """
    Production multi-signal heuristic moderation and offline assistant engine.
    Evaluates normalized homoglyphs, obfuscation, scam phrases, phishing links,
    severe harm, toxicity, and excessive caps/flooding when primary AI is unreachable.
    """

    SEVERE_BAN_PATTERNS = (
        re.compile(r"\b(kill\s+yourself|kys|doxx(?:ing)?|swat(?:ting)?|terroris[mt]|child\s*porn|csam)\b", re.I),
    )

    SCAM_DELETE_KEYWORDS = frozenset({
        "spam",
        "crypto",
        "bitcoin",
        "porn",
        "adult",
        "scam",
        "nsfw",
        "airdrop",
        "giveaway",
        "forex",
        "pumpanddump",
        "seedphrase",
    })

    SCAM_PHRASE_PATTERNS = (
        re.compile(r"\b(double\s+your\s+(?:btc|eth|crypto|money)|guaranteed\s+profit|dm\s+me\s+for\s+(?:signals|vip)|free\s+usdt|claim\s+your\s+reward|whatsapp\s+\+?\d{7,})\b", re.I),
        re.compile(r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)[\w-]+", re.I),
        re.compile(r"\b(?:bit\.ly|tinyurl\.com|cutt\.ly|is\.gd|rb\.gy)/[\w-]+", re.I),
    )

    TOXIC_WARN_KEYWORDS = frozenset({
        "idiot",
        "moron",
        "stupid",
        "stfu",
        "loser",
        "scumbag",
        "bastard",
        "asshole",
        "bitch",
        "fuck",
        "shit",
    })

    async def analyze_message(self, text: str, context: str) -> dict[str, Any]:
        logger.warning("Fallback heuristic provider used for message analysis.")
        telemetry.increment("ai_fallback_invocations_total", operation="analyze_message")

        if not text or not text.strip():
            return {"action": "none", "reason": "Empty message", "severity": 1, "user_message": ""}

        raw_lower = text.lower()
        normalized = normalize_unicode(remove_emojis(text)).lower()
        deobfuscated = clean_obfuscation(normalized)
        collapsed = collapse_duplicates(deobfuscated)
        tokens = set(re.findall(r"\b\w+\b", normalized))

        # 1. Severe violations -> Ban (severity 5)
        for pattern in self.SEVERE_BAN_PATTERNS:
            if pattern.search(raw_lower) or pattern.search(normalized):
                return {
                    "action": "ban",
                    "reason": "Local heuristic check flagged severe safety violation",
                    "severity": 5,
                    "user_message": "Severe violation of community safety policy.",
                }

        # 2. Scam / Phishing / NSFW / Spam -> Delete (severity 2-4)
        for pattern in self.SCAM_PHRASE_PATTERNS:
            if pattern.search(raw_lower) or pattern.search(normalized):
                return {
                    "action": "delete",
                    "reason": "Local heuristic check flagged scam/promotional pattern",
                    "severity": 3,
                    "user_message": "Please respect the rules. Promotional links and scams are not allowed.",
                }

        if any(word in raw_lower or word in normalized or word in collapsed for word in self.SCAM_DELETE_KEYWORDS):
            return {
                "action": "delete",
                "reason": "Local heuristic check flagged content",
                "severity": 2,
                "user_message": "Please respect the rules. No spam/scams.",
            }

        # 3. Toxicity / Harassment -> Warn (severity 2)
        if tokens & self.TOXIC_WARN_KEYWORDS:
            return {
                "action": "warn",
                "reason": "Local heuristic check flagged toxic or disrespectful language",
                "severity": 2,
                "user_message": "Please keep the conversation civil and respectful.",
            }

        # 4. Excessive ALL-CAPS shouting -> Warn (severity 2)
        alpha_chars = [c for c in text if c.isalpha()]
        if len(alpha_chars) >= 25:
            upper_ratio = sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars)
            if upper_ratio >= 0.85:
                return {
                    "action": "warn",
                    "reason": "Local heuristic check flagged excessive caps shouting",
                    "severity": 2,
                    "user_message": "Please avoid shouting in all caps.",
                }

        return {"action": "none", "reason": "Local check passed", "severity": 1, "user_message": ""}

    async def explain_message(self, text: str) -> str:
        telemetry.increment("ai_fallback_invocations_total", operation="explain_message")
        analysis = await self.analyze_message(text, "")
        if analysis["action"] != "none":
            return (
                f"Heuristic analysis flagged this message ({analysis['action'].upper()}, "
                f"severity {analysis['severity']}/5): {analysis['reason']}."
            )
        return "Explanation is currently unavailable. Placed under automatic heuristic guidelines."

    async def generate_summary(self, text: str) -> str:
        telemetry.increment("ai_fallback_invocations_total", operation="generate_summary")
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        if not lines:
            return "Conversation summary is currently unavailable (API Cooldown/Quota exceeded)."
        preview = "\n".join(f"• {ln[:120]}" for ln in lines[-5:])
        return (
            "Conversation summary is currently running in offline heuristic mode "
            "(API Cooldown/Quota exceeded). Recent messages:\n"
            f"{preview}"
        )

    async def answer_question(self, question: str, rules_context: str) -> str:
        telemetry.increment("ai_fallback_invocations_total", operation="answer_question")
        if rules_context and question:
            q_words = {
                w for w in re.findall(r"\b\w{3,}\b", question.lower())
                if w not in {"what", "when", "where", "which", "who", "how", "are", "the", "can", "you", "for"}
            }
            matching_lines = [
                line.strip()
                for line in rules_context.splitlines()
                if line.strip() and any(w in line.lower() for w in q_words)
            ]
            if matching_lines:
                joined = "\n".join(f"• {m}" for m in matching_lines[:3])
                return f"Based on the group rules:\n{joined}"
        return (
            "I am unable to access the AI service to answer questions at the moment. "
            "Please refer directly to the pinned rules."
        )


# Preserve legacy class name for backward compatibility
FallbackMockProvider = HeuristicFallbackProvider


class AIService:
    """
    Orchestrates AI tasks with LRU+TTL caching, circuit breaker protection,
    per-chat and global rate limiting, and automatic fallback heuristics.
    """

    def __init__(self) -> None:
        self.providers: list[AIProvider] = []
        self.fallback_provider = HeuristicFallbackProvider()

        if settings.GEMINI_API_KEY:
            self.providers.append(GeminiProvider(settings.GEMINI_API_KEY))

        self.providers.append(self.fallback_provider)

        self.cache = AIResponseCache(
            max_size=settings.AI_CACHE_MAX_SIZE,
            ttl_seconds=settings.AI_CACHE_TTL_SECONDS,
        )
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=settings.AI_CB_FAILURE_THRESHOLD,
            recovery_seconds=settings.AI_CB_RECOVERY_SECONDS,
        )

        self._rate_limits: dict[int, list[float]] = {}
        self._global_requests: list[float] = []
        self._cooldown_window = settings.AI_RATE_LIMIT_WINDOW_SECONDS
        self._max_requests = settings.AI_RATE_LIMIT_MAX_REQUESTS
        self._global_max_per_min = settings.AI_GLOBAL_RATE_LIMIT_PER_MIN
        self._lock = threading.RLock()

    def _check_rate_limit(self, chat_id: int) -> bool:
        """Return True if request is allowed, False if rate limited (per-chat or global)."""
        now = datetime.now(timezone.utc).timestamp()
        with self._lock:
            # Periodic cleanup of inactive chat buckets
            if len(self._rate_limits) > 500:
                stale_chats = [
                    cid
                    for cid, ts_list in self._rate_limits.items()
                    if not ts_list or (now - ts_list[-1] >= self._cooldown_window)
                ]
                for cid in stale_chats:
                    self._rate_limits.pop(cid, None)

            # Global sliding window check (60s)
            self._global_requests = [t for t in self._global_requests if now - t < 60.0]
            if len(self._global_requests) >= self._global_max_per_min:
                logger.warning("AI Service global rate limit hit (%d/min)", self._global_max_per_min)
                telemetry.increment("ai_rate_limited_total", scope="global")
                return False

            # Per-chat sliding window check
            if chat_id not in self._rate_limits:
                self._rate_limits[chat_id] = []

            self._rate_limits[chat_id] = [
                t for t in self._rate_limits[chat_id] if now - t < self._cooldown_window
            ]

            if len(self._rate_limits[chat_id]) >= self._max_requests:
                logger.warning("AI Service rate limit hit for chat %d", chat_id)
                telemetry.increment("ai_rate_limited_total", scope="chat")
                return False

            self._rate_limits[chat_id].append(now)
            self._global_requests.append(now)
            return True

    async def analyze_message(
        self, text: str, context: str, chat_id: int = 0
    ) -> dict[str, Any]:
        """
        Analyze message using cache -> primary AI provider (guarded by circuit breaker)
        -> heuristic fallback provider.
        """
        if not text or not text.strip():
            return {"action": "none", "reason": "Empty text", "severity": 1, "user_message": ""}

        # 1. Check LRU+TTL cache first (does not consume rate limit quota!)
        cache_key = self.cache.build_key("analyze", text)
        cached_resp = self.cache.get(cache_key)
        if cached_resp is not None:
            return cached_resp

        # 2. Check rate limits; if rate limited, run local zero-cost heuristics for safety
        if not self._check_rate_limit(chat_id):
            heuristic_res = await self.fallback_provider.analyze_message(text, context)
            if heuristic_res.get("action") != "none":
                return heuristic_res
            return {"action": "none", "reason": "Rate limited", "severity": 1, "user_message": ""}

        # 3. Iterate through providers with circuit breaker awareness
        for provider in self.providers:
            is_fallback = isinstance(provider, HeuristicFallbackProvider)
            if not is_fallback and not self.circuit_breaker.allow_request():
                logger.debug(
                    "Skipping primary provider %s because circuit breaker is OPEN",
                    provider.__class__.__name__,
                )
                continue

            try:
                res = await provider.analyze_message(text, context)
                if res:
                    validated = validate_moderation_response(res)
                    if not is_fallback:
                        self.circuit_breaker.record_success()
                    # Cache successful responses if valid
                    if validated.get("reason") != "AI analysis unavailable":
                        self.cache.set(cache_key, validated)
                    return validated
            except Exception as exc:
                if not is_fallback:
                    self.circuit_breaker.record_failure()
                logger.error("AI Provider %s failed: %s", provider.__class__.__name__, exc)
                continue

        return {"action": "none", "reason": "All providers failed", "severity": 1, "user_message": ""}

    async def explain_message(self, text: str, chat_id: int = 0) -> str:
        """Explain toxicity of a message with caching and fallback."""
        cache_key = self.cache.build_key("explain", text)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        if not self._check_rate_limit(chat_id):
            return "Too many requests. Please try again later."

        for provider in self.providers:
            is_fallback = isinstance(provider, HeuristicFallbackProvider)
            if not is_fallback and not self.circuit_breaker.allow_request():
                continue
            try:
                result = await provider.explain_message(text)
                if not is_fallback:
                    self.circuit_breaker.record_success()
                if result:
                    self.cache.set(cache_key, result)
                return result
            except Exception as exc:
                if not is_fallback:
                    self.circuit_breaker.record_failure()
                logger.error("AI Provider %s failed: %s", provider.__class__.__name__, exc)
                continue
        return "Error explaining message contents."

    async def generate_summary(self, text: str, chat_id: int = 0) -> str:
        """Generate summary of conversation text with caching and fallback."""
        cache_key = self.cache.build_key("summary", text)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        if not self._check_rate_limit(chat_id):
            return "Too many requests. Please try again later."

        for provider in self.providers:
            is_fallback = isinstance(provider, HeuristicFallbackProvider)
            if not is_fallback and not self.circuit_breaker.allow_request():
                continue
            try:
                result = await provider.generate_summary(text)
                if not is_fallback:
                    self.circuit_breaker.record_success()
                if result:
                    self.cache.set(cache_key, result)
                return result
            except Exception as exc:
                if not is_fallback:
                    self.circuit_breaker.record_failure()
                logger.error("AI Provider %s failed: %s", provider.__class__.__name__, exc)
                continue
        return "Error generating conversation summary."

    async def answer_question(
        self, question: str, rules_context: str, chat_id: int = 0
    ) -> str:
        """Answer rule questions with caching and fallback."""
        cache_key = self.cache.build_key("qa", question, rules_context)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        if not self._check_rate_limit(chat_id):
            return "Too many requests. Please try again later."

        for provider in self.providers:
            is_fallback = isinstance(provider, HeuristicFallbackProvider)
            if not is_fallback and not self.circuit_breaker.allow_request():
                continue
            try:
                result = await provider.answer_question(question, rules_context)
                if not is_fallback:
                    self.circuit_breaker.record_success()
                if result:
                    self.cache.set(cache_key, result)
                return result
            except Exception as exc:
                if not is_fallback:
                    self.circuit_breaker.record_failure()
                logger.error("AI Provider %s failed: %s", provider.__class__.__name__, exc)
                continue
        return "Error answering rule question."

    def get_health_status(self) -> Dict[str, Any]:
        """Return health and caching telemetry for the AI subsystem."""
        return {
            "circuit_breaker_state": self.circuit_breaker.state,
            "cache": self.cache.stats(),
            "providers": [p.__class__.__name__ for p in self.providers],
        }


ai_service = AIService()
