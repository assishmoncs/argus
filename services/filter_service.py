"""
Word Filtering service detecting banned words, wildcards, regexes, obfuscations, emojis, and links.
Features compiled pattern caching and ReDoS guardrails for production scale.
"""

import functools
import logging
import re
from typing import Any, Optional

from database.models import CustomFilterRepository, WhitelistedWordsRepository
from utils.telemetry import telemetry
from utils.text_utils import (
    clean_obfuscation,
    collapse_duplicates,
    normalize_unicode,
    remove_emojis,
)

logger = logging.getLogger("argus.filter")

# Regex to detect links and telegram invite links
INVITE_LINK_REGEX = re.compile(
    r"(t\.me|telegram\.me|telegram\.dog)/(joinchat|\+[\w-]{6,})",
    re.IGNORECASE,
)
URL_REGEX = re.compile(
    r"\b((https?://)?([a-z0-9]+(-[a-z0-9]+)*\.)+[a-z]{2,}(/[^\s]*)?)\b",
    re.IGNORECASE,
)

MAX_REGEX_PATTERN_LENGTH = 512


@functools.lru_cache(maxsize=1024)
def _compile_wildcard(pattern: str) -> Optional[re.Pattern[str]]:
    escaped = re.escape(pattern)
    regex_str = "^" + escaped.replace(r"\*", ".*") + "$"
    try:
        return re.compile(regex_str, re.IGNORECASE)
    except re.error:
        return None


@functools.lru_cache(maxsize=1024)
def _compile_regex(pattern: str) -> Optional[re.Pattern[str]]:
    if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
        logger.warning("Refusing overly long regex pattern (%d chars)", len(pattern))
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        logger.error("Invalid filter regex pattern '%s': %s", pattern, exc)
        return None


class FilterService:
    """Banned words and link scanning engine with compiled regex caching."""

    @staticmethod
    def match_pattern(word: str, pattern: str, match_type: str) -> bool:
        """Helper to match a word against a pattern (exact, wildcard, regex)."""
        word = word.lower().strip()
        pattern = pattern.lower().strip()

        if match_type == "exact":
            return word == pattern
        elif match_type == "wildcard":
            compiled = _compile_wildcard(pattern)
            return bool(compiled and compiled.match(word))
        elif match_type == "regex":
            compiled = _compile_regex(pattern)
            return bool(compiled and compiled.search(word))
        return False

    async def check_message(self, chat_id: int, text: str) -> Optional[dict[str, Any]]:
        """
        Scan a message text for banned custom filters and invite links.
        Returns a dict describing the match and the configured action, or None if safe.
        """
        if not text:
            return None

        async with telemetry.async_timer("filter_check_latency"):
            filters = await CustomFilterRepository.get_filters(chat_id)
            whitelisted_words = await WhitelistedWordsRepository.get_whitelist(chat_id)

            raw_words = re.findall(r"\b\w+\b", text.lower())

            no_emoji = remove_emojis(text)
            normalized = normalize_unicode(no_emoji)
            normalized_words = re.findall(r"\b\w+\b", normalized.lower())
            deobfuscated = clean_obfuscation(normalized).lower()
            deobfuscated_collapsed = collapse_duplicates(deobfuscated).lower()

            for f in filters:
                pattern = f["pattern"].lower()
                match_type = f["match_type"]
                action = f["action"]

                if pattern in whitelisted_words:
                    continue

                # Level A: Match against raw split words
                for w in raw_words:
                    if w in whitelisted_words:
                        continue
                    if self.match_pattern(w, pattern, match_type):
                        telemetry.increment("filter_triggers_total", match_type=match_type)
                        return {
                            "action": action,
                            "pattern": pattern,
                            "match_type": match_type,
                            "reason": f"Banned word matched: {pattern}",
                        }

                # Level B: Match against normalized words
                for w in normalized_words:
                    if w in whitelisted_words:
                        continue
                    if self.match_pattern(w, pattern, match_type):
                        telemetry.increment("filter_triggers_total", match_type="normalized")
                        return {
                            "action": action,
                            "pattern": pattern,
                            "match_type": match_type,
                            "reason": f"Banned word matched (normalized): {pattern}",
                        }

                # Level C: Obfuscation bypass check (e.g. c.r.y.p.t.o)
                cleaned_pattern = clean_obfuscation(normalize_unicode(pattern)).lower()
                if cleaned_pattern and len(cleaned_pattern) > 2:
                    if (
                        cleaned_pattern in deobfuscated_collapsed
                        or collapse_duplicates(cleaned_pattern) in deobfuscated_collapsed
                    ):
                        is_whitelisted = any(ww in text.lower() for ww in whitelisted_words)
                        if not is_whitelisted:
                            telemetry.increment("filter_triggers_total", match_type="obfuscated")
                            return {
                                "action": action,
                                "pattern": pattern,
                                "match_type": "obfuscated",
                                "reason": f"Banned word matched (obfuscation): {pattern}",
                            }

            # 3. Scan for Telegram Invite Links
            if INVITE_LINK_REGEX.search(text):
                telemetry.increment("filter_triggers_total", match_type="invite_link")
                return {
                    "action": "delete",
                    "pattern": "invite_link",
                    "match_type": "invite_link",
                    "reason": "Telegram invite link detected",
                }

            # 4. Scan for Generic Links if restricted
            if URL_REGEX.search(text):
                for f in filters:
                    if f["pattern"] == "__links__":
                        telemetry.increment("filter_triggers_total", match_type="url")
                        return {
                            "action": f["action"],
                            "pattern": "links",
                            "match_type": "url",
                            "reason": "URLs are restricted in this chat",
                        }

            return None


# Singleton instance
filter_service = FilterService()
