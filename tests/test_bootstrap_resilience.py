"""Startup reconcile budget, collector health, executor bounded sets, min-notional merge."""

from __future__ import annotations

import threading
import time
from decimal import Decimal
from unittest.mock import Mock

from typing import Optional

import pytest

from trading.bot_runtime.main_loop_runtime import maybe_check_collector_health
from trading.startup_runtime import (
    DEFAULT_STARTUP_RECONCILE_TIMEOUT_S,
    StartupRuntimeHelper,
    _startup_reconcile_budget_seconds,
)
from trade_executor import TradeExecutor


def test_startup_reconcile_budget_default():
    bot = Mock(config={"trading": {}})
    assert _startup_reconcile_budget_seconds(bot) == DEFAULT_STARTUP_RECONCILE_TIMEOUT_S


def test_startup_reconcile_budget_from_yaml_style_config():
    bot = Mock(config={"trading": {"startup_reconcile_timeout_seconds": 90}})
    assert _startup_reconcile_budget_seconds(bot) == 90.0


def test_startup_reconcile_budget_zero_means_disabled():
    bot = Mock(config={"trading": {"startup_reconcile_timeout_seconds": 0}})
    assert _startup_reconcile_budget_seconds(bot) == 0.0


@pytest.mark.parametrize(
    "deadline,expect",
    [
        (None, False),
        (time.monotonic() - 1.0, True),
        (time.monotonic() + 3600.0, False),
    ],
)
def test_reconcile_deadline_passed(deadline: Optional[float], expect: bool):
    assert StartupRuntimeHelper._reconcile_deadline_passed(deadline) == expect


def test_collector_health_resets_fail_streak_when_recent_success():
    bot = Mock()
    collector = Mock(
        running=True,
        interval=60,
        _thread=Mock(is_alive=Mock(return_value=True)),
    )
    collector._last_collect_loop_success_at = time.time()
    setattr(bot, "collector", collector)
    setattr(bot, "_collector_health_fail_streak", 4)

    maybe_check_collector_health(bot)

    assert getattr(bot, "_collector_health_fail_streak") == 0


def test_collector_health_dead_thread_streak():
    bot = Mock(alert_system=None)
    collector = Mock(running=True, _thread=Mock(is_alive=Mock(return_value=False)))
    setattr(bot, "collector", collector)

    maybe_check_collector_health(bot)

    assert getattr(bot, "_collector_health_fail_streak") == 1


def test_collector_health_stale_success_increments_streak():
    bot = Mock(alert_system=None)
    collector = Mock(
        running=True,
        interval=60,
        _thread=Mock(is_alive=Mock(return_value=True)),
        _collector_thread_started_at=time.time() - 10_000.0,
        _collect_consecutive_errors_exposed=0,
    )
    collector._last_collect_loop_success_at = time.time() - 10_000.0
    setattr(bot, "collector", collector)
    setattr(bot, "_collector_health_fail_streak", 0)

    maybe_check_collector_health(bot)

    assert getattr(bot, "_collector_health_fail_streak") >= 1


def test_mark_oms_processing_ignores_empty():
    ex = TradeExecutor.__new__(TradeExecutor)
    ex.api_client = Mock()
    ex._oms_processing_lock = threading.Lock()
    ex._oms_processing = set()

    TradeExecutor._mark_oms_processing(ex, "")
    TradeExecutor._mark_oms_processing(ex, "   ")

    assert len(ex._oms_processing) == 0


def test_bounded_set_inplace_hard_prune():
    ex = TradeExecutor.__new__(TradeExecutor)
    ex._executor_processing_set_warn_limit = 0
    ex._executor_processing_set_max_hard = 5
    tgt = {"0", "1", "2", "3", "4", "5", "6", "7"}

    TradeExecutor._bounded_set_audit_inplace(ex, "pytest", tgt)

    assert len(tgt) == 5


def test_effective_min_order_quote_uses_exchange_when_higher():
    ex = TradeExecutor.__new__(TradeExecutor)
    ex.api_client = Mock(get_symbol_min_notional=Mock(return_value=42.5))
    ex._min_order_quote = 10.0

    got = TradeExecutor._effective_min_order_quote(ex, "ALTUSDT")
    assert got == Decimal("42.5")


def test_effective_min_order_quote_fallback_when_exchange_zero():
    ex = TradeExecutor.__new__(TradeExecutor)
    ex.api_client = Mock(get_symbol_min_notional=Mock(return_value=0.0))
    ex._min_order_quote = 12.34

    got = TradeExecutor._effective_min_order_quote(ex, "BTCUSDT")
    assert got == Decimal("12.34")
