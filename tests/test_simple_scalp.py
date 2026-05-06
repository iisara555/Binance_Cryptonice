"""
Tests for SimpleScalp strategy.

Monkeypatches _compute_indicators so each test exercises pure signal logic
without needing real indicator calculations.
"""
import pytest
import pandas as pd
import numpy as np

from strategies.simple_scalp import SimpleScalp


@pytest.fixture
def strategy():
    return SimpleScalp()


def _make_df(n=60):
    """Minimal OHLCV DataFrame with enough rows."""
    close = np.linspace(100, 110, n)
    return pd.DataFrame({
        "open": close - 0.5,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": np.ones(n) * 1000,
    })


# ── Not enough candles ────────────────────────────────────────────────────────

def test_insufficient_candles_returns_none(strategy):
    df = _make_df(n=10)
    assert strategy.generate_signal(df, "BTCUSDT") is None


# ── Entry fires (all conditions met) ─────────────────────────────────────────

def test_entry_all_conditions(strategy, monkeypatch):
    df = _make_df()
    monkeypatch.setattr(
        strategy,
        "_compute_indicators",
        lambda data: {
            "current_price": 98.0,   # below ema21=100
            "ema21": 100.0,
            "stochrsi_k": 15.0,      # < 30
            "stochrsi_d": 20.0,
            "adx": 25.0,             # > 20
        },
    )
    sig = strategy.generate_signal(df, "BTCUSDT")
    assert sig is not None
    assert sig.signal_type.name == "BUY"
    assert sig.stop_loss < sig.price
    assert sig.take_profit > sig.price
    assert 0.50 <= sig.confidence <= 0.95


# ── Entry blocked by EMA condition ───────────────────────────────────────────

def test_entry_blocked_ema(strategy, monkeypatch):
    df = _make_df()
    monkeypatch.setattr(
        strategy,
        "_compute_indicators",
        lambda data: {
            "current_price": 102.0,  # above ema21 → blocks entry AND triggers exit
            "ema21": 100.0,
            "stochrsi_k": 15.0,
            "stochrsi_d": 20.0,
            "adx": 25.0,
        },
    )
    sig = strategy.generate_signal(df, "BTCUSDT")
    # price > ema21 triggers SELL (exit), not BUY
    assert sig is not None
    assert sig.signal_type.name == "SELL"


# ── Entry blocked by StochRSI ─────────────────────────────────────────────────

def test_entry_blocked_stochrsi(strategy, monkeypatch):
    df = _make_df()
    monkeypatch.setattr(
        strategy,
        "_compute_indicators",
        lambda data: {
            "current_price": 98.0,
            "ema21": 100.0,
            "stochrsi_k": 50.0,     # not < 30, not > 70 → no signal
            "stochrsi_d": 50.0,
            "adx": 25.0,
        },
    )
    sig = strategy.generate_signal(df, "BTCUSDT")
    assert sig is None


# ── Entry blocked by ADX ──────────────────────────────────────────────────────

def test_entry_blocked_adx(strategy, monkeypatch):
    df = _make_df()
    monkeypatch.setattr(
        strategy,
        "_compute_indicators",
        lambda data: {
            "current_price": 98.0,
            "ema21": 100.0,
            "stochrsi_k": 15.0,
            "stochrsi_d": 20.0,
            "adx": 10.0,            # < 20 → blocks entry
        },
    )
    sig = strategy.generate_signal(df, "BTCUSDT")
    assert sig is None


# ── Exit fires: StochRSI overbought ──────────────────────────────────────────

def test_exit_stochrsi_overbought(strategy, monkeypatch):
    df = _make_df()
    monkeypatch.setattr(
        strategy,
        "_compute_indicators",
        lambda data: {
            "current_price": 99.0,  # below ema — would be entry, but exit wins
            "ema21": 100.0,
            "stochrsi_k": 80.0,     # > 70 → SELL
            "stochrsi_d": 75.0,
            "adx": 25.0,
        },
    )
    sig = strategy.generate_signal(df, "BTCUSDT")
    assert sig is not None
    assert sig.signal_type.name == "SELL"


# ── Exit fires: price above EMA ───────────────────────────────────────────────

def test_exit_price_above_ema(strategy, monkeypatch):
    df = _make_df()
    monkeypatch.setattr(
        strategy,
        "_compute_indicators",
        lambda data: {
            "current_price": 105.0,  # > ema21=100 → SELL
            "ema21": 100.0,
            "stochrsi_k": 50.0,
            "stochrsi_d": 50.0,
            "adx": 25.0,
        },
    )
    sig = strategy.generate_signal(df, "BTCUSDT")
    assert sig is not None
    assert sig.signal_type.name == "SELL"


# ── Confidence scaling ────────────────────────────────────────────────────────

def test_confidence_scales_with_stochrsi_depth(strategy, monkeypatch):
    df = _make_df()

    def _ind_deep(data):
        return {"current_price": 98.0, "ema21": 100.0, "stochrsi_k": 0.0, "stochrsi_d": 0.0, "adx": 25.0}

    def _ind_shallow(data):
        return {"current_price": 98.0, "ema21": 100.0, "stochrsi_k": 29.0, "stochrsi_d": 28.0, "adx": 25.0}

    monkeypatch.setattr(strategy, "_compute_indicators", _ind_deep)
    sig_deep = strategy.generate_signal(df, "BTCUSDT")

    monkeypatch.setattr(strategy, "_compute_indicators", _ind_shallow)
    sig_shallow = strategy.generate_signal(df, "BTCUSDT")

    assert sig_deep.confidence > sig_shallow.confidence
