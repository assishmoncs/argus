"""
Middleware package for Argus bot.
"""

from bot.middleware.logging_middleware import ErrorBoundaryMiddleware, LoggingMiddleware

__all__ = ["LoggingMiddleware", "ErrorBoundaryMiddleware"]
