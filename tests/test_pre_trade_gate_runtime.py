"""Tests for pre-trade gate BUY sizing preview (ExecutionPlan.amount === 0 path)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, Mock, patch

import pytest

from risk_management import (
    PRETRADE_GATE_POSITION_PCT_EPSILON,
    GateCheckResult,
    PreTradeGate,
    RiskCheckResult,
)
from trade_executor import ExecutionPlan, OrderSide
from trading.orchestrator import TradeDecision
from trading.bot_runtime.pre_trade_gate_runtime import _estimate_buy_quote_for_gate, check_pre_trade_gate


def test_estimate_buy_quote_matches_risk_manager_sizing() -> None:
    bot = MagicMock()
    bot._get_risk_portfolio_value = Mock(return_value=10_000.0)
    rm = MagicMock()
    rm.calculate_position_size.return_value = RiskCheckResult(True, "Position size OK", 123.45)
    bot.risk_manager = rm
    plan = ExecutionPlan(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        amount=0.0,
        entry_price=42_000.0,
        stop_loss=41_000.0,
        take_profit=43_000.0,
        confidence=0.75,
    )
    portfolio: dict = {"balance": 10_000.0}
    assert _estimate_buy_quote_for_gate(bot, plan, portfolio) == pytest.approx(123.45)
    rm.calculate_position_size.assert_called_once_with(
        portfolio_value=10_000.0,
        entry_price=42_000.0,
        stop_loss_price=41_000.0,
        take_profit_price=43_000.0,
        confidence=0.75,
        symbol="BTCUSDT",
    )


def test_estimate_buy_quote_respects_preset_amount() -> None:
    bot = MagicMock()
    plan = ExecutionPlan(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        amount=500.0,
        entry_price=42_000.0,
    )
    assert _estimate_buy_quote_for_gate(bot, plan, {}) == 500.0
    bot.risk_manager.calculate_position_size.assert_not_called()


def test_check_pre_trade_gate_passes_sized_quote_to_check_all() -> None:
    captured: dict = {}

    def capture_check_all(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return GateCheckResult(passed=True, checks=[], failed_checks=[])

    rm = MagicMock()
    rm.calculate_position_size.return_value = RiskCheckResult(True, "OK", 88.0)

    bot = MagicMock()
    bot._pre_trade_gate_enabled = True
    bot._get_risk_portfolio_value = Mock(return_value=5_000.0)
    bot.risk_manager = rm
    bot._pre_trade_gate = MagicMock()
    bot._pre_trade_gate.check_all.side_effect = capture_check_all
    bot._state_machine_enabled = False
    bot.executor.get_open_orders.return_value = []
    bot._executed_today = []
    bot.send_alerts = False
    bot.api_client.get_ticker.return_value = {"last": 100.0}
    bot.config = {"portfolio": {"min_balance_threshold": 10}, "risk": {}, "trading": {}}
    bot._active_strategy_mode = "scalping"
    bot._pair_loss_guard = None
    bot._balance_monitor = None  # bypass free-USDT pre-check (not under test here)

    plan = ExecutionPlan(
        symbol="ADAUSDT",
        side=OrderSide.BUY,
        amount=0.0,
        entry_price=0.5,
        stop_loss=0.48,
        take_profit=0.54,
        confidence=0.6,
        signal_timestamp=datetime.now(),
    )
    signal = MagicMock()
    decision = TradeDecision(plan=plan, signal=signal, risk_check=MagicMock(passed=True))

    with patch(
        "trading.bot_runtime.pre_trade_gate_runtime.parse_ticker_last",
        return_value=0.5,
    ):
        assert check_pre_trade_gate(bot, decision, {"balance": 5000.0}) is True

    assert captured.get("proposed_amount_usdt") == pytest.approx(88.0)
    rm.calculate_position_size.assert_called_once()


def test_blocked_log_includes_failed_checks_csv(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING")
    rm = MagicMock()
    rm.calculate_position_size.return_value = RiskCheckResult(True, "OK", 100.0)

    bot = MagicMock()
    bot._pre_trade_gate_enabled = True
    bot._get_risk_portfolio_value = Mock(return_value=500.0)
    bot.risk_manager = rm
    bot._pre_trade_gate = MagicMock()
    bot._pre_trade_gate.check_all.return_value = GateCheckResult(
        passed=False,
        checks=[],
        failed_checks=["Portfolio above minimum", "Order quote >= min_order_amount"],
    )
    bot._state_machine_enabled = False
    bot.executor.get_open_orders.return_value = []
    bot._executed_today = []
    bot.send_alerts = False
    bot.api_client.get_ticker.return_value = {"last": 1.0}
    bot.config = {"portfolio": {}, "risk": {}, "trading": {}}
    bot._active_strategy_mode = "standard"
    bot._pair_loss_guard = None
    bot._balance_monitor = None  # bypass free-USDT pre-check (not under test here)

    plan = ExecutionPlan(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        amount=0.0,
        entry_price=42_000.0,
        stop_loss=41_000.0,
        take_profit=43_000.0,
        signal_timestamp=datetime.now(),
    )
    decision = TradeDecision(plan=plan, signal=MagicMock(), risk_check=MagicMock(passed=True))

    with patch(
        "trading.bot_runtime.pre_trade_gate_runtime.parse_ticker_last",
        return_value=42000.0,
    ):
        assert check_pre_trade_gate(bot, decision, {}) is False

    assert "failed_checks=" in caplog.text
    assert "Portfolio above minimum,Order quote >= min_order_amount" in caplog.text


def _minimal_pre_trade_gate_cfg(max_position_pct: float = 28.0):
    """Config so check_all clears every gate except the ones under test."""
    return {
        "portfolio": {"min_balance_threshold": 10.0},
        "risk": {
            "max_open_positions": 6,
            "max_daily_trades": 99,
            "max_position_per_trade_pct": max_position_pct,
            "max_drawdown_threshold_pct": 99.0,
            "max_slippage_pct": {"scalping": 999.0},
        },
        "trading": {"min_order_amount": 1.0},
        "mode_indicator_profiles": {"scalping": {"min_confidence": 0.1}},
    }


def test_position_size_within_limit_exact_cap_passes():
    """At exactly max cap (28% of NAV), Position size gate must pass."""
    gate = PreTradeGate()
    rm = MagicMock()
    rm.check_daily_loss_limit.return_value = RiskCheckResult(True, "OK")
    rm.check_cooldown.return_value = False
    rm._get_current_drawdown_pct.return_value = 0.0
    cfg = _minimal_pre_trade_gate_cfg(28.0)
    pv = 1000.0
    proposed = pv * (28.0 / 100.0)
    result = gate.check_all(
        symbol="TSTUSDT",
        side="BUY",
        proposed_amount_usdt=proposed,
        portfolio_value=pv,
        open_positions_count=0,
        daily_trades_today=0,
        current_price=100.0,
        signal_price=100.0,
        signal_confidence=0.5,
        mode="scalping",
        config=cfg,
        risk_manager=rm,
        pair_loss_guard=None,
    )
    pos = next(c for c in result.checks if c["name"] == "Position size within limit")
    assert pos["passed"] is True


def test_position_size_within_limit_fp_jitter_below_epsilon_passes():
    """Float noise just above nominal cap must not block (same .1f display)."""
    gate = PreTradeGate()
    rm = MagicMock()
    rm.check_daily_loss_limit.return_value = RiskCheckResult(True, "OK")
    rm.check_cooldown.return_value = False
    rm._get_current_drawdown_pct.return_value = 0.0
    cfg = _minimal_pre_trade_gate_cfg(28.0)
    pv = 100.0
    proposed = 28.0 + min(PRETRADE_GATE_POSITION_PCT_EPSILON, 1e-12) * 10
    result = gate.check_all(
        symbol="TSTUSDT",
        side="BUY",
        proposed_amount_usdt=proposed,
        portfolio_value=pv,
        open_positions_count=0,
        daily_trades_today=0,
        current_price=100.0,
        signal_price=100.0,
        signal_confidence=0.5,
        mode="scalping",
        config=cfg,
        risk_manager=rm,
        pair_loss_guard=None,
    )
    pos = next(c for c in result.checks if c["name"] == "Position size within limit")
    assert pos["passed"] is True


def test_position_size_gate_matches_min_order_buffer_ceiling_vs_nominal_pct():
    """Nominal YAML cap 28% → gate compares to 28×MIN_ORDER_BUFFER (~30.8%) for headroom."""
    gate = PreTradeGate()
    rm = MagicMock()
    rm.check_daily_loss_limit.return_value = RiskCheckResult(True, "OK")
    rm.check_cooldown.return_value = False
    rm._get_current_drawdown_pct.return_value = 0.0
    cfg = _minimal_pre_trade_gate_cfg(28.0)
    pv = 100.0
    proposed = 29.5  # 29.5% of NAV — passes ceiling 30.8%, would fail nominal 28%
    result = gate.check_all(
        symbol="TSTUSDT",
        side="BUY",
        proposed_amount_usdt=proposed,
        portfolio_value=pv,
        open_positions_count=0,
        daily_trades_today=0,
        current_price=100.0,
        signal_price=100.0,
        signal_confidence=0.5,
        mode="scalping",
        config=cfg,
        risk_manager=rm,
        pair_loss_guard=None,
    )
    pos = next(c for c in result.checks if c["name"] == "Position size within limit")
    assert pos["passed"] is True
    assert "cfg_max=28.0%" in pos["reason"]
    assert "gate≤" in pos["reason"]