"""Tests for YAML-driven max slippage % overrides (PreTradeGate)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from risk_management import PreTradeGate, RiskCheckResult, resolve_max_slippage_pct


def test_resolve_uses_hardcoded_when_no_config() -> None:
    assert resolve_max_slippage_pct("scalping", {}) == pytest.approx(0.15)
    assert resolve_max_slippage_pct("scalping", None) == pytest.approx(0.15)
    assert resolve_max_slippage_pct("trend_only", None) == pytest.approx(0.30)


def test_resolve_risk_section_override() -> None:
    cfg = {"risk": {"max_slippage_pct": {"scalping": 0.36}}, "trading": {}}
    assert resolve_max_slippage_pct("scalping", cfg) == pytest.approx(0.36)


def test_resolve_trading_wins_duplicate_mode() -> None:
    cfg = {
        "risk": {"max_slippage_pct": {"scalping": 0.25}},
        "trading": {"max_slippage_pct": {"scalping": 0.42}},
    }
    assert resolve_max_slippage_pct("scalping", cfg) == pytest.approx(0.42)


def test_pre_trade_gate_slippage_check_respects_yaml() -> None:
    gate = PreTradeGate()
    rm = MagicMock()
    rm.check_daily_loss_limit.return_value = RiskCheckResult(True, "OK")
    rm.check_cooldown.return_value = False
    rm._get_current_drawdown_pct.return_value = 0.0
    cfg = {
        "portfolio": {"min_balance_threshold": 10.0},
        "risk": {
            "max_open_positions": 6,
            "max_daily_trades": 99,
            "max_position_per_trade_pct": 50.0,
            "max_drawdown_threshold_pct": 99.0,
            "max_slippage_pct": {"scalping": 1.0},
        },
        "trading": {"min_order_amount": 1.0},
        "mode_indicator_profiles": {"scalping": {"min_confidence": 0.1}},
    }
    result = gate.check_all(
        symbol="BTCUSDT",
        side="BUY",
        proposed_amount_usdt=50.0,
        portfolio_value=1000.0,
        open_positions_count=0,
        daily_trades_today=0,
        current_price=101.0,
        signal_price=100.0,
        signal_confidence=0.9,
        mode="scalping",
        config=cfg,
        risk_manager=rm,
        pair_loss_guard=None,
    )
    slip = next(c for c in result.checks if c["name"] == "Slippage within limit")
    assert slip["passed"] is True
    assert "max=1.000%" in slip["reason"]
