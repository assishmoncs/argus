"""
Enterprise production readiness test suite covering:
1. Database connection pooling, WAL pragmas, atomic transactions, rollback, SQL injection guardrails, and TTL repo caching
2. Gemini AI LRU+TTL caching, circuit breaker state machine, model fallback cascade, and multi-signal heuristic engine
3. Structured JSON logging, correlation IDs, secret scrubbing, and telemetry histograms
4. Middleware concurrency throttling, error boundaries, and background task lifecycle management
5. Extensible plugin architecture (topological dependency sorting, priority ordering, hooks, and error isolation)
"""

import asyncio
import json
import logging
import os
import tempfile
import time
from unittest.mock import MagicMock

import pytest

from bot.middleware.logging_middleware import ErrorBoundaryMiddleware, LoggingMiddleware
from database.connection import Database
from database.models import (
    AuditLogRepository,
    GroupRepository,
    repo_cache,
)
from moderation.actions import BackgroundTaskManager
from plugins.base import BasePlugin
from plugins.loader import PluginLoader
from services.ai_service import (
    AIResponseCache,
    CircuitBreaker,
    GeminiProvider,
    HeuristicFallbackProvider,
)
from utils.logging_config import StructuredJsonFormatter, redact_secrets
from utils.telemetry import TelemetryCollector, correlation_scope, get_correlation_id


class TestDatabaseProductionScale:

    @pytest.mark.asyncio
    async def test_connection_pool_and_health_check(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        pool_db = Database(db_path=db_path, pool_size=3, enable_wal=True)
        try:
            await pool_db.run_migrations()
            health = await pool_db.health_check()
            assert health["healthy"] is True
            assert health["pool_size"] == 3
            assert health["journal_mode"].lower() == "wal"
        finally:
            await pool_db.close()
            try:
                os.unlink(db_path)
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_atomic_transaction_rollback_on_error(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        test_db = Database(db_path=db_path, pool_size=2)
        await test_db.run_migrations()
        try:
            with pytest.raises(RuntimeError):
                async with test_db.transaction() as tx:
                    await tx.execute(
                        "INSERT INTO groups (chat_id, title) VALUES (?, ?);",
                        (777, "Rollback Group"),
                    )
                    raise RuntimeError("Simulated mid-transaction failure")

            rows = await test_db.execute("SELECT * FROM groups WHERE chat_id = ?;", (777,))
            assert len(rows) == 0
        finally:
            await test_db.close()
            try:
                os.unlink(db_path)
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_sql_injection_column_whitelist_enforced(self):
        with pytest.raises(ValueError):
            await GroupRepository.update_group(100, malicious_column="1; DROP TABLE groups;--")

    @pytest.mark.asyncio
    async def test_repository_ttl_cache_and_invalidation(self):
        from database import models
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        test_db = Database(db_path=db_path)
        await test_db.run_migrations()
        orig_db = models.db
        models.db = test_db
        repo_cache.clear()
        try:
            grp1 = await GroupRepository.get_group(555)
            assert grp1["warning_limit"] == 3

            # Update invalidates cache automatically
            await GroupRepository.update_group(555, warning_limit=5)
            grp2 = await GroupRepository.get_group(555)
            assert grp2["warning_limit"] == 5

            # Audit log check
            await AuditLogRepository.log_event(555, 1, 2, "warn", "Test audit")
            logs = await AuditLogRepository.get_recent_logs(555)
            assert len(logs) == 1
            assert logs[0]["action"] == "warn"
        finally:
            models.db = orig_db
            await test_db.close()
            try:
                os.unlink(db_path)
            except Exception:
                pass


class TestGeminiOptimizationAndFallbacks:

    def test_lru_ttl_cache_hit_miss_and_eviction(self):
        cache = AIResponseCache(max_size=2, ttl_seconds=10.0)
        k1 = cache.build_key("analyze", "Hello   World! ")
        k1_dup = cache.build_key("analyze", "hello world!")
        assert k1 == k1_dup

        assert cache.get(k1) is None
        cache.set(k1, {"action": "none"})
        assert cache.get(k1) == {"action": "none"}

        k2 = cache.build_key("analyze", "Second message")
        k3 = cache.build_key("analyze", "Third message")
        cache.set(k2, {"action": "warn"})
        cache.set(k3, {"action": "delete"})

        # k1 should be evicted due to max_size=2
        assert cache.get(k1) is None
        assert cache.get(k2) == {"action": "warn"}
        assert cache.stats()["size"] == 2

    def test_circuit_breaker_state_transitions(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_seconds=1.0)
        assert cb.state == "closed"
        assert cb.allow_request() is True

        cb.record_failure()
        assert cb.state == "closed"
        cb.record_failure()
        assert cb.state == "open"
        assert cb.allow_request() is False

        # Simulate cooldown elapsed
        cb._opened_at = time.monotonic() - 2.0
        assert cb.state == "half_open"
        assert cb.allow_request() is True

        cb.record_success()
        assert cb.state == "closed"

    @pytest.mark.asyncio
    async def test_gemini_provider_model_fallback_cascade(self):
        provider = GeminiProvider(
            api_key="test-key",
            model_name="gemini-3.8-flash",
            fallback_models=["gemini-3.5-flash-lite"],
            timeout_seconds=2.0,
            max_retries=0,
        )
        calls = []

        def fake_generate_content(model, contents, config=None):
            calls.append(model)
            if model == "gemini-3.8-flash":
                raise RuntimeError("Primary model overloaded")
            resp = MagicMock()
            resp.text = '{"action": "warn", "reason": "fallback model worked", "severity": 2, "user_message": "careful"}'
            return resp

        provider.client.models.generate_content = fake_generate_content
        res = await provider.analyze_message("test message", "no context")
        assert calls == ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
        assert res["action"] == "warn"
        assert res["reason"] == "fallback model worked"

    @pytest.mark.asyncio
    async def test_heuristic_fallback_multi_signal_engine(self):
        fb = HeuristicFallbackProvider()

        # 1. Severe safety violation -> ban
        res_ban = await fb.analyze_message("You should kill yourself right now", "")
        assert res_ban["action"] == "ban"
        assert res_ban["severity"] == 5

        # 2. Scam phrase -> delete
        res_scam = await fb.analyze_message("Double your BTC today with guaranteed profit!", "")
        assert res_scam["action"] == "delete"
        assert res_scam["severity"] >= 2

        # 3. Toxic insult -> warn
        res_toxic = await fb.analyze_message("You are a complete idiot and moron", "")
        assert res_toxic["action"] == "warn"

        # 4. Excessive caps shouting -> warn
        res_caps = await fb.analyze_message("THIS IS AN EXTREMELY LOUD ALL CAPS SHOUTING MESSAGE IN CHAT", "")
        assert res_caps["action"] == "warn"

        # 5. Clean message -> none
        res_clean = await fb.analyze_message("Good morning everyone, how is the project going?", "")
        assert res_clean["action"] == "none"

        # 6. Rule-aware Q&A fallback
        qa = await fb.answer_question(
            "Are promotional links allowed?",
            "1. Be respectful.\n2. Promotional links and affiliate spam are strictly forbidden.",
        )
        assert "Promotional links" in qa


class TestTelemetryAndLogging:

    def test_telemetry_counters_gauges_and_percentiles(self):
        tc = TelemetryCollector()
        tc.increment("requests_total", status="ok")
        tc.increment("requests_total", value=2, status="ok")
        assert tc.get_counter("requests_total", status="ok") == 3

        tc.set_gauge("workers", 4.0)
        tc.adjust_gauge("workers", -1.0)
        assert tc.get_gauge("workers") == 3.0

        for dur in (0.01, 0.02, 0.03, 0.04, 0.10):
            tc.observe_latency("op_latency", dur)

        snap = tc.snapshot()
        lat = snap["latencies"]["op_latency"]
        assert lat["count"] == 5
        assert lat["min_ms"] >= 10.0
        assert lat["max_ms"] >= 100.0
        assert lat["p95_ms"] >= 40.0

    def test_correlation_scope_and_secret_redaction(self):
        with correlation_scope("trace-abc-123"):
            assert get_correlation_id() == "trace-abc-123"
        assert get_correlation_id() == "-"

        raw = "Failed with token 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi in request"
        scrubbed = redact_secrets(raw)
        assert "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi" not in scrubbed
        assert "REDACTED" in scrubbed

        formatter = StructuredJsonFormatter()
        record = logging.LogRecord(
            name="argus.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="Test log with token 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            args=(),
            exc_info=None,
        )
        formatted = json.loads(formatter.format(record))
        assert formatted["level"] == "INFO"
        assert "REDACTED" in formatted["message"]


class TestMiddlewareAndPlugins:

    @pytest.mark.asyncio
    async def test_logging_and_error_boundary_middleware(self):
        from aiogram.types import Chat, Message, User

        log_mw = LoggingMiddleware(max_concurrent_updates=4, per_chat_limit=2)
        err_mw = ErrorBoundaryMiddleware(suppress_exceptions=True)

        msg = Message(
            message_id=10,
            from_user=User(id=1, is_bot=False, first_name="Tester"),
            chat=Chat(id=-1001, type="supergroup"),
            text="Hello middleware",
        )

        captured_cid = None

        async def ok_handler(event, data):
            nonlocal captured_cid
            captured_cid = data.get("correlation_id")
            return "handled"

        res = await log_mw(ok_handler, msg, {})
        assert res == "handled"
        assert captured_cid is not None and captured_cid != "-"

        async def failing_handler(event, data):
            raise ValueError("Boom inside handler")

        # ErrorBoundaryMiddleware suppresses unhandled exceptions safely
        isolated_res = await err_mw(failing_handler, msg, {})
        assert isolated_res is None

    @pytest.mark.asyncio
    async def test_plugin_loader_dependency_sorting_and_lifecycle(self):
        load_order = []

        class PluginA(BasePlugin):
            def __init__(self):
                super().__init__(name="PluginA", priority=50, dependencies=["PluginB"])

            async def load(self, bot, dp):
                load_order.append("PluginA")

        class PluginB(BasePlugin):
            def __init__(self):
                super().__init__(name="PluginB", priority=100)

            async def load(self, bot, dp):
                load_order.append("PluginB")

        class PluginBrokenDep(BasePlugin):
            def __init__(self):
                super().__init__(name="PluginBrokenDep", dependencies=["NonExistentPlugin"])

            async def load(self, bot, dp):
                load_order.append("PluginBrokenDep")

        ordered, failures = PluginLoader._sort_plugins_by_dependencies(
            [PluginA(), PluginB(), PluginBrokenDep()]
        )
        assert [p.name for p in ordered] == ["PluginB", "PluginA"]
        assert "PluginBrokenDep" in failures

    @pytest.mark.asyncio
    async def test_background_task_manager_cleanup(self):
        mgr = BackgroundTaskManager()

        async def sleeper():
            await asyncio.sleep(10)

        t = mgr.create_task(sleeper(), name="test_sleeper")
        assert len(mgr._tasks) == 1
        await mgr.cancel_all(timeout=1.0)
        assert len(mgr._tasks) == 0
        assert t.cancelled() or t.done()
