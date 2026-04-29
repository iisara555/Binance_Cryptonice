"""Tests for signal_pipeline strategy collection + diagnostics."""

from __future__ import annotations

import pandas as pd
import pytest

from signal_pipeline import collect_raw_trading_signals


def _noop_diag(*_a, **_k) -> None:
    return None


def test_collect_raw_trading_signals_logs_strategy_reject_snapshot(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG", logger="crypto-bot.signal")

    class FakeStrat:
        def __init__(self) -> None:
            self._last = "SR_GUARD_BLOCKED"

        def generate_signal(self, _data, _symbol):
            return None

        def get_last_reject_reason(self):
            return self._last

    strategies = {"machete_v8b_lite": FakeStrat()}
    df = pd.DataFrame(
        {
            "close": [1.0, 1.1, 1.2, 1.3, 1.4] * 20,
            "high": [1.1, 1.2, 1.3, 1.4, 1.5] * 20,
            "low": [0.9, 1.0, 1.1, 1.2, 1.3] * 20,
            "volume": [100.0] * 100,
        }
    )
    raw, reasons = collect_raw_trading_signals(
        strategies=strategies,
        strategy_names=["machete_v8b_lite"],
        data=df,
        symbol="BTCUSDT",
        diag_fn=_noop_diag,
        emit_sniper_diagnostics=lambda *_a, **_k: None,
    )
    assert raw == []
    assert reasons.get("machete_v8b_lite") == "SR_GUARD_BLOCKED"
    assert "[StrategyRejectSnapshot]" in caplog.text
    assert "pair=BTCUSDT" in caplog.text
    assert "machete_v8b_lite=SR_GUARD_BLOCKED" in caplog.text
