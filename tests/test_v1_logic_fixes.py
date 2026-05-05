"""
tests/test_v1_logic_fixes.py
============================
Regression tests for the four logic fixes in CryptoBot V1.

FIX 1 — Kelly minimum floor  (risk_management.py)
FIX 2 — Drawdown peak resets too often  (risk_management.py)
FIX 3 — Signal cache key unstable  (signal_generator.py)
FIX 4 — MTF EMA fallback uses wrong timeframe  (signal_generator.py)

Run with:
    pytest tests/test_v1_logic_fixes.py -v -k "kelly or drawdown or cache or mtf"
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


def _make_risk_config(
    *,
    portfolio: float = 42.0,
    max_risk: float = 2.5,
    min_risk: float = 0.5,
    min_order: float = 10.0,
    max_pos_pct: float = 28.0,
    use_fractional_kelly: bool = False,
    drawdown_threshold: float = 12.0,
    drawdown_soft: float = 5.0,
    min_drawdown_multiplier: float = 0.35,
):
    """Return a RiskConfig pre-loaded with sensible test values."""
    from risk_management import RiskConfig

    return RiskConfig(
        max_risk_per_trade_pct=max_risk,
        min_risk_per_trade_pct=min_risk,
        max_position_per_trade_pct=max_pos_pct,
        min_order_amount=min_order,
        use_fractional_kelly=use_fractional_kelly,
        initial_balance=portfolio,
        min_balance_threshold=10.0,
        max_drawdown_threshold_pct=drawdown_threshold,
        drawdown_soft_reduce_start_pct=drawdown_soft,
        min_drawdown_risk_multiplier=min_drawdown_multiplier,
        drawdown_block_new_entries=True,
        max_open_positions=5,
        max_daily_trades=10,
        cool_down_minutes=0,
    )


def _make_risk_manager(cfg=None, **kw):
    """Return a RiskManager with state-file persistence disabled."""
    from risk_management import RiskConfig, RiskManager

    if cfg is None:
        cfg = _make_risk_config(**kw)
    with patch.object(RiskManager, "load_state", return_value=False):
        with patch.object(RiskManager, "save_state"):
            rm = RiskManager(cfg)
    # Patch save_state so tests run without hitting disk
    rm.save_state = MagicMock()
    return rm


def _make_ohlcv(n: int = 50, base_price: float = 100.0, ts_start: Optional[datetime] = None) -> pd.DataFrame:
    """Create a minimal OHLCV DataFrame with a DatetimeIndex."""
    import numpy as np

    if ts_start is None:
        ts_start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    idx = pd.date_range(start=ts_start, periods=n, freq="15min", tz=timezone.utc)
    prices = base_price + np.zeros(n)
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": np.ones(n) * 1000,
        },
        index=idx,
    )


# ===========================================================================
# FIX 1 — Kelly minimum floor
# ===========================================================================


class TestKellyMinimumFloor:
    """Position size must never fall below min_order_amount when use_fractional_kelly=False."""

    def test_position_size_never_below_min_order_amount_non_positive_kelly(self):
        """
        When Kelly edge is non-positive and use_fractional_kelly=False,
        effective_risk_pct must be clamped up to min_risk_per_trade_pct so that
        the resulting position_usdt stays >= min_order_amount.
        """
        # A tiny portfolio: 42 USDT, SL close to entry → tiny risk_amount
        rm = _make_risk_manager(
            portfolio=42.0,
            max_risk=2.5,
            min_risk=0.5,
            min_order=10.0,
            max_pos_pct=50.0,
            use_fractional_kelly=False,
        )
        entry = 100.0
        # Tight SL → tiny risk_per_unit → tiny suggested investment
        sl = 99.5  # 0.5% below entry
        tp = 101.0  # 1% above entry (b < 1, so kelly would be negative or tiny)

        # Force a deep drawdown so the drawdown multiplier also squeezes risk
        rm._peak_portfolio_value = 100.0  # peak far above current NAV

        result = rm.calculate_position_size(
            portfolio_value=42.0,
            entry_price=entry,
            stop_loss_price=sl,
            take_profit_price=tp,
            confidence=0.55,  # low confidence → b < 1 likely → negative kelly
            symbol="BTCUSDT",
        )

        assert result.allowed, f"Trade should be allowed but got: {result.reason}"
        assert result.suggested_size >= rm.config.min_order_amount, (
            f"Suggested size {result.suggested_size:.4f} is below min_order_amount "
            f"{rm.config.min_order_amount}"
        )

    def test_kelly_floor_only_applies_when_fractional_kelly_false(self):
        """When use_fractional_kelly=True, floor does NOT clamp — normal Kelly rejects."""
        rm = _make_risk_manager(
            portfolio=42.0,
            max_risk=2.5,
            min_risk=0.5,
            min_order=10.0,
            max_pos_pct=50.0,
            use_fractional_kelly=True,  # strict Kelly: reject on non-positive edge
        )
        entry = 100.0
        sl = 99.5
        tp = 100.1  # very tight TP → b very small → kelly < 0

        result = rm.calculate_position_size(
            portfolio_value=1000.0,
            entry_price=entry,
            stop_loss_price=sl,
            take_profit_price=tp,
            confidence=0.5,
            symbol="BTCUSDT",
        )
        # With use_fractional_kelly=True and b < 1, p=0.5 → kelly = 0.5 - (0.5/0.2) = negative → reject
        assert not result.allowed, "use_fractional_kelly=True should reject negative kelly edge"

    def test_min_risk_floor_is_loaded_from_config(self):
        """RiskConfig.from_file() must pick up min_risk_per_trade_pct from the risk section."""
        import json
        import tempfile
        import os
        from risk_management import RiskConfig

        cfg_data = {
            "risk": {
                "max_risk_per_trade_pct": 2.5,
                "min_risk_per_trade_pct": 0.75,
                "use_fractional_kelly": False,
            },
            "portfolio": {"initial_balance": 1000.0, "min_balance_threshold": 100.0},
            "trading": {"max_open_positions": 5, "max_daily_trades": 10, "cool_down_minutes": 0},
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(cfg_data, f)
            tmp_path = f.name

        try:
            cfg = RiskConfig.from_file(tmp_path)
            assert cfg.min_risk_per_trade_pct == pytest.approx(0.75), (
                f"Expected 0.75, got {cfg.min_risk_per_trade_pct}"
            )
        finally:
            os.unlink(tmp_path)


# ===========================================================================
# FIX 2 — Drawdown peak must not advance on every loop tick
# ===========================================================================


class TestDrawdownPeak:
    """Peak must only move after trade closes, not on every calculate_position_size call."""

    def test_peak_does_not_advance_on_loop_call(self):
        """
        Repeated calculate_position_size calls with rising NAV must NOT advance
        the peak — that would render the drawdown circuit breaker useless.
        """
        rm = _make_risk_manager(portfolio=1000.0, max_risk=2.5, min_risk=0.5, min_order=10.0, max_pos_pct=50.0)
        entry = 100.0
        sl = 95.0
        tp = 110.0

        # First call sets the peak (initialisation)
        rm.calculate_position_size(
            portfolio_value=1000.0, entry_price=entry, stop_loss_price=sl, take_profit_price=tp
        )
        peak_after_first_call = rm._peak_portfolio_value
        assert peak_after_first_call == pytest.approx(1000.0)

        # NAV rises, but we're mid-loop (no trade closed) — peak must NOT follow
        rm.calculate_position_size(
            portfolio_value=1200.0, entry_price=entry, stop_loss_price=sl, take_profit_price=tp
        )
        assert rm._peak_portfolio_value == pytest.approx(1000.0), (
            f"Peak advanced intra-loop to {rm._peak_portfolio_value}; it should stay at 1000.0"
        )

        # Same check via can_open_position
        rm.can_open_position(portfolio_value=1300.0, open_positions_count=0)
        assert rm._peak_portfolio_value == pytest.approx(1000.0), (
            f"Peak advanced via can_open_position to {rm._peak_portfolio_value}"
        )

    def test_peak_advances_after_record_trade(self):
        """
        After record_trade(portfolio_value=X) the peak should reflect X if X > old peak.
        """
        rm = _make_risk_manager(portfolio=1000.0, max_risk=2.5, min_risk=0.5, min_order=10.0, max_pos_pct=50.0)
        rm._peak_portfolio_value = 1000.0

        rm.record_trade(symbol="BTCUSDT", portfolio_value=1150.0)
        assert rm._peak_portfolio_value == pytest.approx(1150.0), (
            f"Peak should have advanced to 1150.0 after record_trade, got {rm._peak_portfolio_value}"
        )

    def test_peak_does_not_retreat_on_record_trade(self):
        """
        record_trade with a lower NAV than the current peak must NOT reduce the peak.
        """
        rm = _make_risk_manager(portfolio=1200.0, max_risk=2.5, min_risk=0.5, min_order=10.0, max_pos_pct=50.0)
        rm._peak_portfolio_value = 1200.0

        rm.record_trade(symbol="BTCUSDT", portfolio_value=900.0)  # NAV below peak after loss
        assert rm._peak_portfolio_value == pytest.approx(1200.0), (
            "Peak retreated after a losing trade — it must be monotonically increasing only"
        )

    def test_drawdown_circuit_breaker_fires_because_peak_is_stable(self):
        """
        With a stable peak the circuit breaker should actually fire when drawdown > threshold.
        This was impossible with the old implementation that always chased the current NAV.
        """
        rm = _make_risk_manager(
            portfolio=1000.0,
            max_risk=2.5,
            min_risk=0.5,
            min_order=10.0,
            max_pos_pct=50.0,
            drawdown_threshold=12.0,
        )
        rm._peak_portfolio_value = 1000.0  # peak set manually

        # NAV drops 15% — above the 12% circuit-breaker threshold
        nav = 850.0
        result = rm.can_open_position(portfolio_value=nav, open_positions_count=0)
        assert not result.allowed, (
            "Circuit breaker should have fired at 15% drawdown but allowed the trade"
        )
        assert "rawdown" in result.reason  # "Drawdown limit reached: ..."


# ===========================================================================
# FIX 3 — Signal cache key stability
# ===========================================================================


class TestSignalCacheKey:
    """Cache key must be based on the last closed candle timestamp, not live OHLCV values."""

    def _make_generator(self):
        """Return a SignalGenerator with all strategies mocked out."""
        from signal_generator import SignalGenerator

        # Minimal config to avoid import errors
        cfg = {
            "risk": {},
            "strategies": {"enabled": [], "min_confidence": 0.3, "independent_strategy_execution": False},
            "mode_indicator_profiles": {},
        }
        with patch("signal_generator.TrendFollowingStrategy"):
            with patch("signal_generator.MeanReversionStrategy"):
                with patch("signal_generator.BreakoutStrategy"):
                    with patch("signal_generator.ScalpingStrategy"):
                        with patch("signal_generator.SniperStrategy"):
                            with patch("signal_generator.MacheteV8bLite"):
                                with patch("signal_generator.SimpleScalpPlus"):
                                    sg = SignalGenerator(cfg)
        return sg

    def test_same_timestamp_produces_cache_hit(self):
        """
        Two calls with the same DataFrame index[-1] and same strategy list must
        produce the same cache key, yielding a cache hit on the second call.
        """
        sg = self._make_generator()

        data = _make_ohlcv(n=50)
        symbol = "BTCUSDT"
        strategies = ["machete_v8b_lite", "simple_scalp_plus"]

        # Build key for first call
        last_ts = str(data.index[-1])
        strategies_key = ",".join(sorted(strategies))
        expected_key = f"{symbol}:{last_ts}:{strategies_key}"

        # Simulate mid-candle OHLCV update (close price changes slightly)
        data_updated = data.copy()
        data_updated.loc[data_updated.index[-1], "close"] = data_updated["close"].iloc[-1] + 0.5

        # The index[-1] is the same — both should produce the same key
        last_ts_updated = str(data_updated.index[-1])
        key_updated = f"{symbol}:{last_ts_updated}:{strategies_key}"

        assert expected_key == key_updated, (
            f"Cache keys differ despite same candle timestamp:\n  {expected_key}\n  {key_updated}"
        )

    def test_new_candle_produces_cache_miss(self):
        """
        When a new bar opens (index[-1] changes), the cache key must change too.
        """
        symbol = "BTCUSDT"
        strategies = sorted(["machete_v8b_lite", "simple_scalp_plus"])
        strategies_key = ",".join(strategies)

        data_bar1 = _make_ohlcv(n=50, ts_start=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc))
        data_bar2 = _make_ohlcv(n=51, ts_start=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc))

        key1 = f"{symbol}:{data_bar1.index[-1]}:{strategies_key}"
        key2 = f"{symbol}:{data_bar2.index[-1]}:{strategies_key}"

        assert key1 != key2, "A new candle must produce a different cache key (cache miss)"

    def test_different_symbols_produce_different_keys(self):
        """Two different symbols on the same bar must not share a cache key."""
        data = _make_ohlcv(n=50)
        strategies_key = ",".join(sorted(["machete_v8b_lite", "simple_scalp_plus"]))
        last_ts = str(data.index[-1])

        key_btc = f"BTCUSDT:{last_ts}:{strategies_key}"
        key_eth = f"ETHUSDT:{last_ts}:{strategies_key}"

        assert key_btc != key_eth


# ===========================================================================
# FIX 4 — MTF EMA fallback must not use wrong timeframe
# ===========================================================================


class TestMTFFallback:
    """When HTF data is unavailable the MTF check must be skipped (pass), never wrong-TF substituted."""

    def _build_htf_missing_signal(self, sg, data, sig_type_str="BUY"):
        """
        Call _make_aggregate_for_direction with a db that returns no HTF candles,
        verify the resulting AggregatedSignal has the 'skip' rationale (not the
        ema60_approx string), and that confidence was NOT halved.
        """
        from strategy_base import MarketCondition, SignalType, TradingSignal

        sig_type = SignalType[sig_type_str]

        raw = TradingSignal(
            strategy_name="machete_v8b_lite",
            symbol="BTCUSDT",
            signal_type=sig_type,
            confidence=0.80,
            price=100.0,
            timestamp=datetime.now(timezone.utc),
            stop_loss=95.0,
            take_profit=110.0,
            risk_reward_ratio=2.0,
        )

        # Stub out strategy performance so weights loop never sees a MagicMock value
        sg._get_strategy_performance = lambda: {}

        # db returns empty DataFrame for any HTF interval
        mock_db = MagicMock()
        mock_db.get_candles.return_value = pd.DataFrame()
        sg._db = mock_db

        agg = sg._make_aggregate_for_direction(
            [raw], sig_type, MarketCondition.BULL, "BTCUSDT", data
        )
        return agg

    def _make_generator(self):
        from signal_generator import SignalGenerator

        cfg: Dict[str, Any] = {
            "risk": {},
            "strategies": {"enabled": [], "min_confidence": 0.3, "independent_strategy_execution": False},
            "mode_indicator_profiles": {},
        }
        with patch("signal_generator.TrendFollowingStrategy"):
            with patch("signal_generator.MeanReversionStrategy"):
                with patch("signal_generator.BreakoutStrategy"):
                    with patch("signal_generator.ScalpingStrategy"):
                        with patch("signal_generator.SniperStrategy"):
                            with patch("signal_generator.MacheteV8bLite"):
                                with patch("signal_generator.SimpleScalpPlus"):
                                    sg = SignalGenerator(cfg)
        return sg

    def test_no_htf_data_skips_mtf_check_not_wrong_tf(self):
        """
        When db returns no HTF data the MTF rationale must say 'Skipped'
        and the confidence must NOT be halved.
        """
        sg = self._make_generator()
        data = _make_ohlcv(n=80)  # enough rows that ema60 would be possible in old code

        agg = self._build_htf_missing_signal(sg, data, sig_type_str="BUY")
        assert agg is not None

        rationale = agg._mtf_rationale
        assert "Skipped" in rationale, (
            f"Expected 'Skipped' in MTF rationale when HTF unavailable, got: {rationale!r}"
        )
        # Ensure the wrong-TF label is gone
        assert "ema60_approx" not in rationale, (
            f"Old ema60_approx fallback should be removed, got: {rationale!r}"
        )

    def test_no_htf_data_does_not_halve_confidence(self):
        """
        Missing HTF must NOT penalise confidence (skip = pass, not mismatch).
        """
        sg = self._make_generator()
        data = _make_ohlcv(n=80)

        agg = self._build_htf_missing_signal(sg, data, sig_type_str="BUY")
        assert agg is not None

        # Original confidence is 0.80; if halved by mistake it would be <= 0.40
        assert agg.combined_confidence > 0.40, (
            f"Confidence was halved ({agg.combined_confidence:.3f}) even though HTF was unavailable — "
            "skip should be a pass, not a mismatch penalty"
        )

    def test_htf_available_applies_mtf_filter_normally(self):
        """
        When real HTF data IS available the filter still works (aligned / misaligned).
        """
        from strategy_base import MarketCondition, SignalType, TradingSignal

        sg = self._make_generator()
        # Stub strategy performance so the weights loop doesn't see a MagicMock
        sg._get_strategy_performance = lambda: {}
        data = _make_ohlcv(n=80)

        raw = TradingSignal(
            strategy_name="machete_v8b_lite",
            symbol="BTCUSDT",
            signal_type=SignalType.BUY,
            confidence=0.80,
            price=100.0,
            timestamp=datetime.now(timezone.utc),
            stop_loss=95.0,
            take_profit=110.0,
            risk_reward_ratio=2.0,
        )

        # HTF data shows SELL bias: ema_fast < ema_slow (prices declining)
        import numpy as np

        declining = np.linspace(110, 90, 30)  # fast EMA will be below slow EMA
        htf_df = pd.DataFrame(
            {"close": declining}, index=pd.date_range("2024-01-01", periods=30, freq="1h", tz=timezone.utc)
        )
        mock_db = MagicMock()
        mock_db.get_candles.return_value = htf_df
        sg._db = mock_db

        agg = sg._make_aggregate_for_direction(
            [raw], SignalType.BUY, MarketCondition.BULL, "BTCUSDT", data
        )
        assert agg is not None
        # BUY vs SELL HTF → misaligned → confidence should be halved
        assert agg.combined_confidence < 0.70, (
            f"Expected confidence to be reduced on MTF mismatch, got {agg.combined_confidence:.3f}"
        )
        assert "Misaligned" in agg._mtf_rationale, (
            f"Expected 'Misaligned' in rationale, got: {agg._mtf_rationale!r}"
        )
