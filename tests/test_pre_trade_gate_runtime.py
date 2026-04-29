"""Tests for pre-trade gate BUY sizing preview (ExecutionPlan.amount === 0 path)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, Mock, patch

import pytest

from risk_management import GateCheckResult, RiskCheckResult
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
