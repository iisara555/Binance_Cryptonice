"""
Performance Logging Configuration
=================================
Centralized control for performance-related debug logging.
Toggle to reduce log noise in production.

Usage:
    from observability.performance_config import (
        is_performance_logging_enabled,
        log_performance,
    )
    
    if is_performance_logging_enabled():
        logger.debug("[Performance] Some expensive operation took %dms", elapsed_ms)
"""

import os
import threading
from typing import Optional


# Global toggle — can be set via environment variable or config
_performance_logging_enabled: bool = True
_lock = threading.Lock()


def set_performance_logging(enabled: bool) -> None:
    """Enable or disable performance logging globally."""
    global _performance_logging_enabled
    with _lock:
        _performance_logging_enabled = bool(enabled)


def is_performance_logging_enabled() -> bool:
    """Check if performance logging is enabled.
    
    Checks:
    1. Environment variable PERFORMANCE_LOGGING
    2. Global toggle (set via set_performance_logging)
    """
    # Environment variable takes precedence
    env_val = os.environ.get("PERFORMANCE_LOGGING", "").strip().lower()
    if env_val in ("0", "false", "off", "no"):
        return False
    if env_val in ("1", "true", "on", "yes"):
        return True
    
    # Fall back to global toggle
    with _lock:
        return _performance_logging_enabled


def log_performance(
    logger,
    operation: str,
    elapsed_ms: float,
    threshold_ms: float = 100.0,
    **kwargs,
) -> None:
    """Log performance metrics if enabled and threshold exceeded.
    
    Args:
        logger: Logger instance to use
        operation: Name of the operation (e.g., "get_portfolio_state")
        elapsed_ms: Time elapsed in milliseconds
        threshold_ms: Only log if elapsed_ms exceeds this threshold
        **kwargs: Additional context to log
    """
    if not is_performance_logging_enabled():
        return
    
    if elapsed_ms < threshold_ms:
        return
    
    # Build context string
    context_parts = [f"{k}={v}" for k, v in kwargs.items()]
    context_str = f" | {' | '.join(context_parts)}" if context_parts else ""
    
    # Log with appropriate level based on severity
    if elapsed_ms > 1000:
        logger.warning(
            "[Performance] SLOW: %s took %.1fms (threshold: %.1fms)%s",
            operation,
            elapsed_ms,
            threshold_ms,
            context_str,
        )
    elif elapsed_ms > 500:
        logger.info(
            "[Performance] ELEVATED: %s took %.1fms (threshold: %.1fms)%s",
            operation,
            elapsed_ms,
            threshold_ms,
            context_str,
        )
    else:
        logger.debug(
            "[Performance] %s took %.1fms (threshold: %.1fms)%s",
            operation,
            elapsed_ms,
            threshold_ms,
            context_str,
        )


class PerformanceTimer:
    """Context manager for timing operations."""
    
    def __init__(
        self,
        operation: str,
        logger=None,
        threshold_ms: float = 100.0,
        **context,
    ):
        self.operation = operation
        self.logger = logger
        self.threshold_ms = threshold_ms
        self.context = context
        self._start_time: Optional[float] = None
    
    def __enter__(self):
        import time
        self._start_time = time.perf_counter()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        import time
        if self._start_time is None:
            return
        
        elapsed_ms = (time.perf_counter() - self._start_time) * 1000
        
        if self.logger and is_performance_logging_enabled():
            if exc_type is None:
                log_performance(
                    self.logger,
                    self.operation,
                    elapsed_ms,
                    self.threshold_ms,
                    **self.context,
                )
            else:
                # Log error but don't interfere with exception propagation
                self.logger.debug(
                    "[Performance] %s raised %s after %.1fms",
                    self.operation,
                    exc_type.__name__,
                    elapsed_ms,
                )
        
        return False  # Don't suppress exceptions
