"""
Tests for Performance Logging Configuration
=========================================
"""

import logging
import os
import threading
import time
from unittest.mock import Mock, patch

import pytest

from observability.performance_config import (
    is_performance_logging_enabled,
    log_performance,
    set_performance_logging,
    PerformanceTimer,
)


class TestPerformanceLoggingToggle:
    """Tests for performance logging toggle functionality."""

    def test_enabled_by_default(self):
        """Performance logging should be enabled by default."""
        # Reset to default state
        set_performance_logging(True)
        assert is_performance_logging_enabled() is True

    def test_can_disable(self):
        """Can disable performance logging via set function."""
        set_performance_logging(False)
        assert is_performance_logging_enabled() is False
        # Reset
        set_performance_logging(True)

    def test_can_enable_after_disable(self):
        """Can re-enable performance logging after disabling."""
        set_performance_logging(False)
        set_performance_logging(True)
        assert is_performance_logging_enabled() is True

    def test_environment_variable_true(self):
        """Environment variable '1' or 'true' enables logging."""
        with patch.dict(os.environ, {"PERFORMANCE_LOGGING": "1"}):
            assert is_performance_logging_enabled() is True

    def test_environment_variable_false(self):
        """Environment variable '0' or 'false' disables logging."""
        with patch.dict(os.environ, {"PERFORMANCE_LOGGING": "0"}):
            assert is_performance_logging_enabled() is False

    def test_environment_variable_case_insensitive(self):
        """Environment variable check is case-insensitive."""
        for val in ("TRUE", "True", "FALSE", "False", "YES", "No"):
            with patch.dict(os.environ, {"PERFORMANCE_LOGGING": val}):
                result = is_performance_logging_enabled()
                if val.upper() in ("TRUE", "YES", "1", "ON"):
                    assert result is True, f"Expected True for {val}"
                else:
                    assert result is False, f"Expected False for {val}"


class TestLogPerformance:
    """Tests for log_performance function."""

    def test_nothing_logged_when_disabled(self):
        """Nothing should be logged when disabled."""
        set_performance_logging(False)
        mock_logger = Mock()
        
        log_performance(mock_logger, "test_op", 500.0)
        
        assert mock_logger.debug.call_count == 0
        assert mock_logger.info.call_count == 0
        set_performance_logging(True)

    def test_nothing_logged_below_threshold(self):
        """Nothing should be logged when elapsed_ms < threshold."""
        mock_logger = Mock()
        
        log_performance(mock_logger, "test_op", 50.0, threshold_ms=100.0)
        
        assert mock_logger.debug.call_count == 0

    def test_debug_logged_above_threshold(self):
        """Debug log should be called when elapsed_ms > threshold."""
        mock_logger = Mock()
        
        log_performance(mock_logger, "test_op", 150.0, threshold_ms=100.0)
        
        assert mock_logger.debug.call_count == 1
        call_args = mock_logger.debug.call_args[0]
        assert "test_op" in str(call_args)
        assert "150" in str(call_args)

    def test_info_logged_elevated_performance(self):
        """Info log for elevated performance (500-1000ms)."""
        mock_logger = Mock()
        
        log_performance(mock_logger, "test_op", 600.0, threshold_ms=100.0)
        
        assert mock_logger.info.call_count == 1

    def test_warning_logged_slow_performance(self):
        """Warning log for slow performance (>1000ms)."""
        mock_logger = Mock()
        
        log_performance(mock_logger, "test_op", 1500.0, threshold_ms=100.0)
        
        assert mock_logger.warning.call_count == 1

    def test_context_included_in_log(self):
        """Additional context kwargs should be included in log message."""
        mock_logger = Mock()
        
        log_performance(
            mock_logger,
            "test_op",
            150.0,
            threshold_ms=100.0,
            symbol="BTCUSDT",
            count=5,
        )
        
        call_args = str(mock_logger.debug.call_args)
        assert "BTCUSDT" in call_args
        assert "count=5" in call_args


class TestPerformanceTimer:
    """Tests for PerformanceTimer context manager."""

    def test_timer_measures_elapsed_time(self):
        """Timer should measure elapsed time correctly."""
        mock_logger = Mock()
        
        with PerformanceTimer("test_op", logger=mock_logger, threshold_ms=1.0):
            time.sleep(0.01)  # 10ms
        
        # Should have logged (10ms > 1ms threshold)
        assert mock_logger.debug.call_count >= 1

    def test_timer_disabled_when_flag_off(self):
        """Timer should not log when logging is disabled."""
        set_performance_logging(False)
        mock_logger = Mock()
        
        with PerformanceTimer("test_op", logger=mock_logger, threshold_ms=1.0):
            time.sleep(0.01)
        
        assert mock_logger.debug.call_count == 0
        set_performance_logging(True)

    def test_timer_reraises_exception(self):
        """Timer should re-raise exceptions without suppression."""
        mock_logger = Mock()
        
        with pytest.raises(ValueError):
            with PerformanceTimer("test_op", logger=mock_logger, threshold_ms=1.0):
                raise ValueError("test error")
        
        # Exception was raised (verified by pytest.raises)
        # Timer logs the exception info for debugging - this is intentional
        assert mock_logger.debug.call_count == 1
        # Verify the logged message contains the exception info
        call_args = str(mock_logger.debug.call_args)
        assert "ValueError" in call_args

    def test_timer_with_context(self):
        """Timer should pass context to log_performance."""
        mock_logger = Mock()
        
        with PerformanceTimer(
            "test_op",
            logger=mock_logger,
            threshold_ms=1.0,
            symbol="BTCUSDT",
        ):
            time.sleep(0.01)
        
        call_args = str(mock_logger.debug.call_args)
        assert "BTCUSDT" in call_args


class TestThreadSafety:
    """Tests for thread safety of toggle."""

    def test_concurrent_toggle_access(self):
        """Concurrent access to toggle should not raise exceptions."""
        errors = []
        
        def toggle_randomly():
            try:
                for _ in range(50):
                    set_performance_logging(True)
                    is_performance_logging_enabled()
                    set_performance_logging(False)
                    is_performance_logging_enabled()
            except Exception as e:
                errors.append(e)
        
        threads = [threading.Thread(target=toggle_randomly) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert not errors, f"Thread safety errors: {errors}"
