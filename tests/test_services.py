"""
Unit and integration tests for FilterService, SpamService, and ModerationActions.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock

from services.filter_service import filter_service, CustomFilterRepository, WhitelistedWordsRepository
from services.spam_service import spam_service
from moderation.actions import can_moderate


def make_text_message(text: str) -> MagicMock:
    """Helper to build a mock text message with all attachments set to None."""
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.sticker = None
    msg.animation = None
    msg.document = None
    msg.photo = None
    msg.video = None
    msg.audio = None
    msg.voice = None
    msg.forward_date = None
    return msg


class TestFilterService:

    @pytest.mark.asyncio
    async def test_exact_filter_matches(self, monkeypatch):
        """Checks exact term filters match successfully."""
        # Mock CustomFilterRepository to return a list of filters
        async def mock_get_filters(chat_id):
            return [{"pattern": "scam", "match_type": "exact", "action": "delete"}]
            
        async def mock_get_whitelist(chat_id):
            return set()

        monkeypatch.setattr(CustomFilterRepository, "get_filters", mock_get_filters)
        monkeypatch.setattr(WhitelistedWordsRepository, "get_whitelist", mock_get_whitelist)

        # Matched
        res = await filter_service.check_message(chat_id=1, text="This is a scam!")
        assert res is not None
        assert res["action"] == "delete"
        assert res["pattern"] == "scam"

        # Not matched
        res_safe = await filter_service.check_message(chat_id=1, text="This is safe content.")
        assert res_safe is None

    @pytest.mark.asyncio
    async def test_wildcard_filter_matches(self, monkeypatch):
        """Checks wildcard term filters match successfully."""
        async def mock_get_filters(chat_id):
            return [{"pattern": "bad*", "match_type": "wildcard", "action": "warn"}]
            
        async def mock_get_whitelist(chat_id):
            return set()

        monkeypatch.setattr(CustomFilterRepository, "get_filters", mock_get_filters)
        monkeypatch.setattr(WhitelistedWordsRepository, "get_whitelist", mock_get_whitelist)

        res = await filter_service.check_message(chat_id=1, text="You are badguy.")
        assert res is not None
        assert res["action"] == "warn"

    @pytest.mark.asyncio
    async def test_regex_filter_matches(self, monkeypatch):
        """Checks regex pattern filters match successfully."""
        async def mock_get_filters(chat_id):
            return [{"pattern": r"cr.pto", "match_type": "regex", "action": "delete"}]
            
        async def mock_get_whitelist(chat_id):
            return set()

        monkeypatch.setattr(CustomFilterRepository, "get_filters", mock_get_filters)
        monkeypatch.setattr(WhitelistedWordsRepository, "get_whitelist", mock_get_whitelist)

        res = await filter_service.check_message(chat_id=1, text="Buy crypto now!")
        assert res is not None

    @pytest.mark.asyncio
    async def test_unicode_homoglyphs_and_obfuscation(self, monkeypatch):
        """Checks homoglyph and obfuscation bypass detection."""
        async def mock_get_filters(chat_id):
            return [{"pattern": "crypto", "match_type": "exact", "action": "delete"}]
            
        async def mock_get_whitelist(chat_id):
            return set()

        monkeypatch.setattr(CustomFilterRepository, "get_filters", mock_get_filters)
        monkeypatch.setattr(WhitelistedWordsRepository, "get_whitelist", mock_get_whitelist)

        # Homoglyphs: 𝖈𝖗𝖞𝖕𝖙𝖔
        res = await filter_service.check_message(chat_id=1, text="Get free \U0001d588\U0001d597\U0001d59e\U0001d595\U0001d599\U0001d594 today!")
        assert res is not None

        # Obfuscation: c.r.y-p_t.o
        res2 = await filter_service.check_message(chat_id=1, text="Check out c.r.y-p_t.o!")
        assert res2 is not None

        # Duplicates collapsing: cryyyyptoooo
        res3 = await filter_service.check_message(chat_id=1, text="Check out cryyyyptoooo!")
        assert res3 is not None

    @pytest.mark.asyncio
    async def test_invite_link_detection(self, monkeypatch):
        """Checks invite links are detected regardless of custom filters."""
        monkeypatch.setattr(CustomFilterRepository, "get_filters", AsyncMock(return_value=[]))
        monkeypatch.setattr(WhitelistedWordsRepository, "get_whitelist", AsyncMock(return_value=set()))

        res = await filter_service.check_message(chat_id=1, text="Join here: t.me/+ABC123xyz_")
        assert res is not None
        assert res["action"] == "delete"
        assert res["match_type"] == "invite_link"


class TestSpamService:

    @pytest.mark.asyncio
    async def test_flood_protection(self):
        """Checks sliding window flood protection works."""
        chat_id = 1
        user_id = 42

        # Reset history
        spam_service.history.clear()

        # Send 5 unique messages quickly (limit is 5)
        for i in range(5):
            msg = make_text_message(f"Hello {i}!")
            res = await spam_service.check_spam(chat_id, user_id, msg)
            assert res is None

        # 6th message should trigger flood detection
        msg = make_text_message("Hello 5!")
        res = await spam_service.check_spam(chat_id, user_id, msg)
        assert res is not None
        assert "flood" in res.lower()

    @pytest.mark.asyncio
    async def test_repeated_messages_protection(self):
        """Checks identical repeated message detection works."""
        chat_id = 2
        user_id = 42

        msg = make_text_message("Same message!")
        spam_service.history.clear()

        # Send 2 identical messages (limit is 3)
        for _ in range(2):
            res = await spam_service.check_spam(chat_id, user_id, msg)
            assert res is None

        # 3rd identical message should trigger repeated messages warning
        res = await spam_service.check_spam(chat_id, user_id, msg)
        assert res is not None
        assert "repeated" in res.lower()

    @pytest.mark.asyncio
    async def test_mention_limit_protection(self):
        """Checks mention spam detection."""
        chat_id = 3
        user_id = 42

        msg = make_text_message("Hey @user1 @user2 @user3 @user4 @user5 @user6 @user7")
        res = await spam_service.check_spam(chat_id, user_id, msg)
        assert res is not None
        assert "mention" in res.lower()


class TestModerationRoleHierarchy:

    def test_role_comparisons(self):
        """Checks permission privilege hierarchy checks."""
        # Owner can moderate Admin
        assert can_moderate("owner", "admin") is True
        
        # Admin can moderate Moderator
        assert can_moderate("admin", "moderator") is True

        # Moderator can moderate Member
        assert can_moderate("moderator", "member") is True

        # Moderator CANNOT moderate Admin
        assert can_moderate("moderator", "admin") is False

        # Admin CANNOT moderate Owner
        assert can_moderate("admin", "owner") is False

        # Same roles cannot moderate each other
        assert can_moderate("admin", "admin") is False
        assert can_moderate("moderator", "moderator") is False
