"""
Spam protection service checking message rates, repetitions, attachments, and raid join rates.
"""

import time
import logging
from typing import Optional, Any
from utils.text_utils import remove_emojis

logger = logging.getLogger("argus.spam")


class SpamService:
    """Protects groups from flood, mention spam, emoji spam, stickers, forwards, and joins raid."""

    def __init__(self) -> None:
        # History dict: {(chat_id, user_id): [message_records]}
        # where message_record is: {'timestamp': float, 'content_hash': int/str, 'msg_type': str}
        self.history: dict[tuple[int, int], list[dict[str, Any]]] = {}
        
        # Recent joins dict: {chat_id: [timestamps]}
        self.joins: dict[int, list[float]] = {}

        # Default thresholds (can be customized per group in database if needed)
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
        if key not in self.history:
            return
        # Keep logs within last 20 seconds, since our largest threshold window is 10.0s
        self.history[key] = [r for r in self.history[key] if now - r["timestamp"] < 20.0]
        if not self.history[key]:
            del self.history[key]

    def record_join(self, chat_id: int, anti_raid_enabled: bool) -> bool:
        """
        Record a join event for a group.
        If anti-raid is enabled and join rate is exceeded, returns True (Raid detected!).
        """
        if not anti_raid_enabled:
            return False

        now = time.time()
        if chat_id not in self.joins:
            self.joins[chat_id] = []

        # Clean joins older than window
        window = self.defaults["raid_window"]
        self.joins[chat_id] = [t for t in self.joins[chat_id] if now - t < window]
        self.joins[chat_id].append(now)

        if len(self.joins[chat_id]) >= self.defaults["raid_limit"]:
            logger.warning("Anti-raid triggered in chat %d: %d joins in %fs", 
                           chat_id, len(self.joins[chat_id]), window)
            return True
        return False

    async def check_spam(self, chat_id: int, user_id: int, message: Any) -> Optional[str]:
        """
        Analyze a message and user history for spam.
        Returns a string reason if spam is detected, or None if clean.
        """
        now = time.time()
        key = (chat_id, user_id)
        self._clean_history(key, now)

        text = (message.text or message.caption or "").strip()
        
        # 1. Check Mention Spam
        # Count '@' symbols (ignoring email addresses roughly)
        mentions_count = len([w for w in text.split() if w.startswith("@")])
        if mentions_count > self.defaults["mention_limit"]:
            return f"Mention spam ({mentions_count} mentions, limit: {self.defaults['mention_limit']})"

        # 2. Check Emoji Spam
        # Emojis count is length diff between text and text-without-emojis
        no_emoji_text = remove_emojis(text)
        emoji_count = len(text) - len(no_emoji_text)
        if emoji_count > self.defaults["emoji_limit"]:
            return f"Emoji spam ({emoji_count} emojis, limit: {self.defaults['emoji_limit']})"

        # Determine message type
        msg_type = "text"
        if message.sticker:
            msg_type = "sticker"
        elif message.animation:  # GIFs are sent as animations in aiogram
            msg_type = "gif"
        elif message.document or message.photo or message.video or message.audio or message.voice:
            msg_type = "file"
            
        if message.forward_date:
            msg_type = "forward"

        # Check if text contains URL link
        has_link = False
        if text and ("http://" in text.lower() or "https://" in text.lower() or "t.me/" in text.lower()):
            has_link = True

        # Append current message to user's history
        record = {
            "timestamp": now,
            "content_hash": hash(text) if text else msg_type,
            "msg_type": msg_type,
            "has_link": has_link,
        }
        if key not in self.history:
            self.history[key] = []
        self.history[key].append(record)

        user_history = self.history[key]

        # 3. Check Message Flood (any message type)
        flood_window = self.defaults["flood_window"]
        recent_msgs = [r for r in user_history if now - r["timestamp"] < flood_window]
        if len(recent_msgs) > self.defaults["flood_limit"]:
            return f"Message flood ({len(recent_msgs)} msgs in {flood_window}s, limit: {self.defaults['flood_limit']})"

        # 4. Check Repeated Messages
        repeat_window = self.defaults["repeat_window"]
        recent_repeats = [r for r in user_history if now - r["timestamp"] < repeat_window]
        # Count frequencies of message content hashes
        hashes = [r["content_hash"] for r in recent_repeats if r["msg_type"] == "text"]
        if hashes:
            for h in set(hashes):
                if hashes.count(h) >= self.defaults["repeat_limit"]:
                    return "Repeated messages (sending duplicates)"

        # 5. Check Attachment rate-limits (Stickers, GIFs, Forwards, Files, Links)
        # Stickers
        stickers_in_window = [r for r in user_history if r["msg_type"] == "sticker" and now - r["timestamp"] < self.defaults["sticker_window"]]
        if len(stickers_in_window) > self.defaults["sticker_limit"]:
            return "Sticker spam"

        # GIFs
        gifs_in_window = [r for r in user_history if r["msg_type"] == "gif" and now - r["timestamp"] < self.defaults["gif_window"]]
        if len(gifs_in_window) > self.defaults["gif_limit"]:
            return "GIF spam"

        # Forwards
        forwards_in_window = [r for r in user_history if r["msg_type"] == "forward" and now - r["timestamp"] < self.defaults["forward_window"]]
        if len(forwards_in_window) > self.defaults["forward_limit"]:
            return "Forward spam"

        # Files
        files_in_window = [r for r in user_history if r["msg_type"] == "file" and now - r["timestamp"] < self.defaults["file_window"]]
        if len(files_in_window) > self.defaults["file_limit"]:
            return "File spam"

        # Links
        links_in_window = [r for r in user_history if r["has_link"] and now - r["timestamp"] < self.defaults["link_window"]]
        if len(links_in_window) > self.defaults["link_limit"]:
            return "Link spam"

        return None


# Singleton instance
spam_service = SpamService()
