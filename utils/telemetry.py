"""
Comprehensive telemetry, metrics collection, latency histograms, and correlation ID tracking for Argus.
"""

import contextlib
import contextvars
import functools
import math
import threading
import time
import uuid
from collections import deque
from typing import Any, AsyncIterator, Callable, Deque, Dict, Iterator, Optional

# Context variable for per-update correlation/trace ID
_correlation_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "argus_correlation_id", default="-"
)


def get_correlation_id() -> str:
    """Return the current async context correlation ID."""
    return _correlation_id_ctx.get()


def set_correlation_id(cid: Optional[str] = None) -> str:
    """Set and return a correlation ID for the current execution context."""
    val = cid or uuid.uuid4().hex[:12]
    _correlation_id_ctx.set(val)
    return val


@contextlib.contextmanager
def correlation_scope(cid: Optional[str] = None) -> Iterator[str]:
    """Context manager binding a correlation ID for the duration of a block."""
    val = cid or uuid.uuid4().hex[:12]
    token = _correlation_id_ctx.set(val)
    try:
        yield val
    finally:
        _correlation_id_ctx.reset(token)


class _LatencyReservoir:
    """Bounded sliding-window reservoir for computing latency percentiles."""

    def __init__(self, max_samples: int = 1000) -> None:
        self.samples: Deque[float] = deque(maxlen=max_samples)
        self.total_count: int = 0
        self.total_sum_ms: float = 0.0
        self.min_ms: float = float("inf")
        self.max_ms: float = 0.0

    def record(self, duration_seconds: float) -> None:
        ms = max(0.0, duration_seconds * 1000.0)
        self.samples.append(ms)
        self.total_count += 1
        self.total_sum_ms += ms
        if ms < self.min_ms:
            self.min_ms = ms
        if ms > self.max_ms:
            self.max_ms = ms

    def summary(self) -> Dict[str, float]:
        if self.total_count == 0 or not self.samples:
            return {
                "count": 0,
                "avg_ms": 0.0,
                "min_ms": 0.0,
                "max_ms": 0.0,
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "p99_ms": 0.0,
            }
        sorted_samples = sorted(self.samples)
        n = len(sorted_samples)

        def _pct(p: float) -> float:
            idx = min(n - 1, max(0, math.ceil((p / 100.0) * n) - 1))
            return round(sorted_samples[idx], 3)

        return {
            "count": self.total_count,
            "avg_ms": round(self.total_sum_ms / self.total_count, 3),
            "min_ms": round(self.min_ms if self.min_ms != float("inf") else 0.0, 3),
            "max_ms": round(self.max_ms, 3),
            "p50_ms": _pct(50.0),
            "p95_ms": _pct(95.0),
            "p99_ms": _pct(99.0),
        }


class TelemetryCollector:
    """Thread-safe and async-safe metrics collector for counters, gauges, and latencies."""

    def __init__(self, max_samples_per_metric: int = 1000) -> None:
        self._lock = threading.RLock()
        self._max_samples = max_samples_per_metric
        self._started_at = time.time()
        self._counters: Dict[str, int] = {}
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, _LatencyReservoir] = {}

    @staticmethod
    def _format_key(name: str, labels: Dict[str, Any]) -> str:
        if not labels:
            return name
        label_str = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
        return f"{name}{{{label_str}}}"

    def increment(self, name: str, value: int = 1, **labels: Any) -> int:
        """Increment a monotonic counter metric."""
        key = self._format_key(name, labels)
        with self._lock:
            new_val = self._counters.get(key, 0) + value
            self._counters[key] = new_val
            return new_val

    def get_counter(self, name: str, **labels: Any) -> int:
        """Read current value of a counter metric."""
        key = self._format_key(name, labels)
        with self._lock:
            return self._counters.get(key, 0)

    def set_gauge(self, name: str, value: float, **labels: Any) -> None:
        """Set a point-in-time gauge metric."""
        key = self._format_key(name, labels)
        with self._lock:
            self._gauges[key] = float(value)

    def adjust_gauge(self, name: str, delta: float = 1.0, **labels: Any) -> float:
        """Add delta to a gauge metric and return the updated value."""
        key = self._format_key(name, labels)
        with self._lock:
            new_val = self._gauges.get(key, 0.0) + delta
            self._gauges[key] = new_val
            return new_val

    def get_gauge(self, name: str, **labels: Any) -> float:
        """Read current value of a gauge metric."""
        key = self._format_key(name, labels)
        with self._lock:
            return self._gauges.get(key, 0.0)

    def observe_latency(self, name: str, duration_seconds: float, **labels: Any) -> None:
        """Record a duration measurement (in seconds) into a latency histogram."""
        key = self._format_key(name, labels)
        with self._lock:
            reservoir = self._histograms.get(key)
            if reservoir is None:
                reservoir = _LatencyReservoir(max_samples=self._max_samples)
                self._histograms[key] = reservoir
            reservoir.record(duration_seconds)

    @contextlib.contextmanager
    def timer(self, name: str, **labels: Any) -> Iterator[None]:
        """Synchronous context manager recording execution time."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe_latency(name, time.perf_counter() - start, **labels)

    @contextlib.asynccontextmanager
    async def async_timer(self, name: str, **labels: Any) -> AsyncIterator[None]:
        """Asynchronous context manager recording execution time."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe_latency(name, time.perf_counter() - start, **labels)

    def track_async(self, metric_name: str) -> Callable[..., Any]:
        """Decorator measuring latency and success/error counts for an async function."""
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                    self.increment(f"{metric_name}_calls_total", status="ok")
                    return result
                except Exception:
                    self.increment(f"{metric_name}_calls_total", status="error")
                    raise
                finally:
                    self.observe_latency(f"{metric_name}_latency", time.perf_counter() - start)
            return wrapper
        return decorator

    def snapshot(self) -> Dict[str, Any]:
        """Return a point-in-time snapshot of all telemetry metrics."""
        with self._lock:
            uptime = max(0.0, time.time() - self._started_at)
            return {
                "uptime_seconds": round(uptime, 2),
                "counters": dict(sorted(self._counters.items())),
                "gauges": dict(sorted(self._gauges.items())),
                "latencies": {
                    k: v.summary() for k, v in sorted(self._histograms.items())
                },
            }

    def reset(self) -> None:
        """Clear all recorded metrics (useful in unit tests)."""
        with self._lock:
            self._started_at = time.time()
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


# Global singleton telemetry collector
telemetry = TelemetryCollector()
