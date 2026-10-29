"""
Spam protection service checking message rates, repetitions, attachments, and raid join rates.
Includes thread/async concurrency safety and bounded memory eviction.
"""

import logging
import threading
import time
from typing import Any, Optional

from utils.telemetry import telemetry
from utils.text_utils import remove_emojis

logger = logging.getLogger("argus.spam")


class SpamService:
    """Protects groups from flood, mention spam, emoji spam, stickers, forwards, and joins raid."""

    def __init__(self, max_tracked_keys: int = 5000) -> None:
        self._lock = threading.RLock()
        self.max_tracked_keys = max_tracked_keys

        # History dict: {(chat_id, user_id): [message_records]}
        self.history: dict[tuple[int, int], list[dict[str, Any]]] = {}

        # Recent joins dict: {chat_id: [timestamps]}
        self.joins: dict[int, list[float]] = {}

        # Default thresholds
        self.defaults = {
            "flood_limit": 5,          # msgs
            "flood_window": 5.0,       # seconds
            "repeat_limit": 3,         # duplicate msgs
            "repeat_window": 10.0,     # seconds
            "mention_limit": 5,        # mentions per msg
            "emoji_limit": 10,         # emojis per msg
            "sticker_limit": 3,        # stickers in window
            "sticker_window": 10.0,
            "gif_limit": 3,            # GIFs in window
            "gif_window": 10.0,
            "forward_limit": 3,        # forwards in window
            "forward_window": 10.0,
            "file_limit": 3,           # files in window
            "file_window": 10.0,
            "link_limit": 3,           # links in window
            "link_window": 10.0,
            "raid_limit": 8,           # joins in window
            "raid_window": 20.0,       # seconds
        }

    def _clean_history(self, key: tuple[int, int], now: float) -> None:
        """Remove message logs older than 20 seconds to conserve memory."""
        with self._lock:
            if key not in self.history:
                return
            self.history[key] = [r for r in self.history[key] if now - r["timestamp"] < 20.0]
            if not self.history[key]:
                del self.history[key]

    def cleanup_stale(self, now: Optional[float] = None) -> int:
        """Global garbage collection for expired user history and join windows."""
        current = now if now is not None else time.time()
        evicted = 0
        with self._lock:
            stale_keys = [
                k
                for k, records in self.history.items()
                if not records or (current - records[-1]["timestamp"] >= 20.0)
            ]
            for k in stale_keys:
                self.history.pop(k, None)
                evicted += 1

            # Enforce upper bound on tracked keys
            if len(self.history) > self.max_tracked_keys:
                overflow = len(self.history) - self.max_tracked_keys
                for k in list(self.history.keys())[:overflow]:
                    self.history.pop(k, None)
                    evicted += 1

            raid_window = self.defaults["raid_window"]
            for chat_id in list(self.joins.keys()):
                self.joins[chat_id] = [
                    t for t in self.joins[chat_id] if current - t < raid_window
                ]
                if not self.joins[chat_id]:
                    self.joins.pop(chat_id, None)

            telemetry.set_gauge("spam_tracked_users", len(self.history))
        return evicted

    def record_join(self, chat_id: int, anti_raid_enabled: bool) -> bool:
        """
        Record a join event for a group.
        If anti-raid is enabled and join rate is exceeded, returns True (Raid detected!).
        """
        if not anti_raid_enabled:
            return False

        now = time.time()
        window = self.defaults["raid_window"]
        with self._lock:
            if chat_id not in self.joins:
                self.joins[chat_id] = []

            self.joins[chat_id] = [t for t in self.joins[chat_id] if now - t < window]
            self.joins[chat_id].append(now)

            if len(self.joins[chat_id]) >= self.defaults["raid_limit"]:
                logger.warning(
                    "Anti-raid triggered in chat %d: %d joins in %.1fs",
                    chat_id,
                    len(self.joins[chat_id]),
                    window,
                )
                telemetry.increment("spam_triggers_total", category="raid")
                return True
        return False

    async def check_spam(self, chat_id: int, user_id: int, message: Any) -> Optional[str]:
        """
        Analyze a message and user history for spam.
        Returns a string reason if spam is detected, or None if clean.
        """
        now = time.time()
        key = (chat_id, user_id)

        text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()

        # 1. Check Mention Spam
        mentions_count = len([w for w in text.split() if w.startswith("@") and len(w) > 1])
        if mentions_count > self.defaults["mention_limit"]:
            telemetry.increment("spam_triggers_total", category="mention")
            return f"Mention spam ({mentions_count} mentions, limit: {self.defaults['mention_limit']})"

        # 2. Check Emoji Spam
        no_emoji_text = remove_emojis(text)
        emoji_count = len(text) - len(no_emoji_text)
        if emoji_count > self.defaults["emoji_limit"]:
            telemetry.increment("spam_triggers_total", category="emoji")
            return f"Emoji spam ({emoji_count} emojis, limit: {self.defaults['emoji_limit']})"

        # Determine message type
        msg_type = "text"
        if getattr(message, "sticker", None):
            msg_type = "sticker"
        elif getattr(message, "animation", None):
            msg_type = "gif"
        elif any(
            getattr(message, attr, None)
            for attr in ("document", "photo", "video", "audio", "voice")
        ):
            msg_type = "file"

        if getattr(message, "forward_date", None):
            msg_type = "forward"

        has_link = bool(
            text
            and (
                "http://" in text.lower()
                or "https://" in text.lower()
                or "t.me/" in text.lower()
            )
        )

        record = {
            "timestamp": now,
            "content_hash": hash(text) if text else msg_type,
            "msg_type": msg_type,
            "has_link": has_link,
        }

        with self._lock:
            self._clean_history(key, now)
            if len(self.history) >= self.max_tracked_keys:
                self.cleanup_stale(now)

            if key not in self.history:
                self.history[key] = []
            self.history[key].append(record)
            user_history = list(self.history[key])

        # 3. Check Message Flood
        flood_window = self.defaults["flood_window"]
        recent_msgs = [r for r in user_history if now - r["timestamp"] < flood_window]
        if len(recent_msgs) > self.defaults["flood_limit"]:
            telemetry.increment("spam_triggers_total", category="flood")
            return f"Message flood ({len(recent_msgs)} msgs in {flood_window}s, limit: {self.defaults['flood_limit']})"

        # 4. Check Repeated Messages
        repeat_window = self.defaults["repeat_window"]
        recent_repeats = [r for r in user_history if now - r["timestamp"] < repeat_window]
        hashes = [r["content_hash"] for r in recent_repeats if r["msg_type"] == "text"]
        if hashes:
            for h in set(hashes):
                if hashes.count(h) >= self.defaults["repeat_limit"]:
                    telemetry.increment("spam_triggers_total", category="repeat")
                    return "Repeated messages (sending duplicates)"

        # 5. Check Attachment rate-limits
        stickers_in_window = [
            r
            for r in user_history
            if r["msg_type"] == "sticker" and now - r["timestamp"] < self.defaults["sticker_window"]
        ]
        if len(stickers_in_window) > self.defaults["sticker_limit"]:
            telemetry.increment("spam_triggers_total", category="sticker")
            return "Sticker spam"

        gifs_in_window = [
            r
            for r in user_history
            if r["msg_type"] == "gif" and now - r["timestamp"] < self.defaults["gif_window"]
        ]
        if len(gifs_in_window) > self.defaults["gif_limit"]:
            telemetry.increment("spam_triggers_total", category="gif")
            return "GIF spam"

        forwards_in_window = [
            r
            for r in user_history
            if r["msg_type"] == "forward" and now - r["timestamp"] < self.defaults["forward_window"]
        ]
        if len(forwards_in_window) > self.defaults["forward_limit"]:
            telemetry.increment("spam_triggers_total", category="forward")
            return "Forward spam"

        files_in_window = [
            r
            for r in user_history
            if r["msg_type"] == "file" and now - r["timestamp"] < self.defaults["file_window"]
        ]
        if len(files_in_window) > self.defaults["file_limit"]:
            telemetry.increment("spam_triggers_total", category="file")
            return "File spam"

        links_in_window = [
            r
            for r in user_history
            if r["has_link"] and now - r["timestamp"] < self.defaults["link_window"]
        ]
        if len(links_in_window) > self.defaults["link_limit"]:
            telemetry.increment("spam_triggers_total", category="link")
            return "Link spam"

        return None


# Singleton instance
spam_service = SpamService()
