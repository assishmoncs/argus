"""
Word Filtering service detecting banned words, wildcards, regexes, obfuscations, emojis, and links.
"""

import re
import logging
from typing import Optional, Any
from database.models import CustomFilterRepository, WhitelistedWordsRepository
from utils.text_utils import (
    remove_emojis,
    normalize_unicode,
    clean_obfuscation,
    collapse_duplicates,
)

logger = logging.getLogger("argus.filter")

# Regex to detect links and telegram invite links
INVITE_LINK_REGEX = re.compile(
    r"(t\.me|telegram\.me|telegram\.dog)/(joinchat|\+[\w-]{10,})"
)
URL_REGEX = re.compile(
    r"\b((https?://)?([a-z0-9]+(-[a-z0-9]+)*\.)+[a-z]{2,}(/[^\s]*)?)\b",
    re.IGNORECASE,
)


class FilterService:
    """Banned words and link scanning engine."""

    @staticmethod
    def match_pattern(word: str, pattern: str, match_type: str) -> bool:
        """Helper to match a word against a pattern (exact, wildcard, regex)."""
        word = word.lower().strip()
        pattern = pattern.lower().strip()

        if match_type == "exact":
            return word == pattern
        elif match_type == "wildcard":
            # Convert glob wildcard to regex (e.g. *bad* -> .*bad.*, bad* -> bad.*)
            # Escape pattern characters first, then swap escaped star with .*
            escaped = re.escape(pattern)
            regex_str = "^" + escaped.replace(r"\*", ".*") + "$"
            try:
                return bool(re.match(regex_str, word))
            except re.error:
                return False
        elif match_type == "regex":
            try:
                return bool(re.search(pattern, word))
            except re.error as exc:
                logger.error("Invalid filter regex pattern '%s': %s", pattern, exc)
                return False
        return False

    async def check_message(self, chat_id: int, text: str) -> Optional[dict[str, Any]]:
        """
        Scan a message text for banned custom filters and invite links.
        Returns a dict describing the match and the configured action, or None if safe.
        """
        if not text:
            return None

        # Fetch settings/filters for this group
        filters = await CustomFilterRepository.get_filters(chat_id)
        whitelisted_words = await WhitelistedWordsRepository.get_whitelist(chat_id)

        # 1. Split raw words
        raw_words = re.findall(r"\b\w+\b", text.lower())

        # 2. Extract clean/normalized texts
        no_emoji = remove_emojis(text)
        normalized = normalize_unicode(no_emoji)
        collapsed = collapse_duplicates(normalized)
        deobfuscated = clean_obfuscation(normalized)
        deobfuscated_collapsed = collapse_duplicates(deobfuscated)

        # Perform checking for custom filters
        for f in filters:
            pattern = f["pattern"].lower()
            match_type = f["match_type"]
            action = f["action"]

            # Ignore checking if pattern is a whitelisted word
            if pattern in whitelisted_words:
                continue

            # Level A: Match against raw split words
            for w in raw_words:
                if w in whitelisted_words:
                    continue
                if self.match_pattern(w, pattern, match_type):
                    return {"action": action, "pattern": pattern, "match_type": match_type, "reason": f"Banned word matched: {pattern}"}

            # Level B: Match against normalized/collapsed words
            normalized_words = re.findall(r"\b\w+\b", normalized.lower())
            for w in normalized_words:
                if w in whitelisted_words:
                    continue
                if self.match_pattern(w, pattern, match_type):
                    return {"action": action, "pattern": pattern, "match_type": match_type, "reason": f"Banned word matched (normalized): {pattern}"}

            # Level C: Obfuscation bypass check (e.g. c.r.y.p.t.o)
            # For exact/wildcard filters, check if the collapsed/deobfuscated pattern is found inside the text
            cleaned_pattern = clean_obfuscation(normalize_unicode(pattern))
            if cleaned_pattern and len(cleaned_pattern) > 2:  # Prevent matching short tokens
                # If the deobfuscated text contains the pattern, or collapsed contains the collapsed pattern
                if cleaned_pattern in deobfuscated_collapsed or collapse_duplicates(cleaned_pattern) in deobfuscated_collapsed:
                    # Double check if any word in the raw text was whitelisted
                    is_whitelisted = False
                    for ww in whitelisted_words:
                        if ww in text.lower():
                            is_whitelisted = True
                            break
                    if not is_whitelisted:
                        return {"action": action, "pattern": pattern, "match_type": "obfuscated", "reason": f"Banned word matched (obfuscation): {pattern}"}

        # 3. Scan for Telegram Invite Links
        if INVITE_LINK_REGEX.search(text):
            # Check whitelist: if the invite link is explicitly whitelisted (not common but possible)
            return {
                "action": "delete",
                "pattern": "invite_link",
                "match_type": "invite_link",
                "reason": "Telegram invite link detected",
            }

        # 4. Scan for Generic Links
        if URL_REGEX.search(text):
            # If the user sets a filter for "links" or "urls", we trigger it, else we let other services (e.g., spam) handle links
            for f in filters:
                if f["pattern"] == "__links__":
                    return {
                        "action": f["action"],
                        "pattern": "links",
                        "match_type": "url",
                        "reason": "URLs are restricted in this chat",
                    }

        return None


# Singleton instance
filter_service = FilterService()
