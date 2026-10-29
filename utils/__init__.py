"""
Utility package for Argus.
"""

from utils.logging_config import (
    setup_logging,
    redact_secrets,
    CorrelationAndRedactionFilter,
    StructuredJsonFormatter,
)
from utils.telemetry import (
    telemetry,
    TelemetryCollector,
    get_correlation_id,
    set_correlation_id,
    correlation_scope,
)
from utils.text_utils import (
    strip_zero_width,
    remove_emojis,
    normalize_unicode,
    clean_obfuscation,
    collapse_duplicates,
    normalize_for_cache,
)

__all__ = [
    "setup_logging",
    "redact_secrets",
    "CorrelationAndRedactionFilter",
    "StructuredJsonFormatter",
    "telemetry",
    "TelemetryCollector",
    "get_correlation_id",
    "set_correlation_id",
    "correlation_scope",
    "strip_zero_width",
    "remove_emojis",
    "normalize_unicode",
    "clean_obfuscation",
    "collapse_duplicates",
    "normalize_for_cache",
]
