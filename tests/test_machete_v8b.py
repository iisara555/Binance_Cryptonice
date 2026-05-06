"""
Tests for MacheteV8b strategy.

Monkeypatches individual _check_<group> methods to inject known True/False
so tests remain isolated from real indicator calculations.
"""
import pytest
import pandas as pd
import numpy as np

from strategies.machete_v8b import MacheteV8b


@pytest.fixture
def strategy():
    return MacheteV8b()


def _make_df(n=210):
    """Minimal OHLCV DataFrame with enough rows (min_bars=205)."""
    close = np.linspace(100, 120, n)
    return pd.DataFrame({
        "open": close - 0.5,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": np.ones(n) * 1000,
    })


def _patch_all_false(strategy, monkeypatch, except_group=None):
    """Silence every group check; optionally force one group True."""
    for g in strategy._ALL_GROUPS:
        val = (g == except_group)
        monkeypatch.setattr(strategy, f"_check_{g}", lambda data, v=val: v)


# ── Insufficient data ─────────────────────────────────────────────────────────

def test_insufficient_candles_returns_none(strategy):
    df = _make_df(n=50)
    assert strategy.generate_signal(df, "BTCUSDT") is None


# ── Each group fires independently ───────────────────────────────────────────

@pytest.mark.parametrize("group", MacheteV8b._ALL_GROUPS)
def test_single_group_fires(strategy, monkeypatch, group):
    df = _make_df()
    _patch_all_false(strategy, monkeypatch, except_group=group)
    sig = strategy.generate_signal(df, "BTCUSDT")
    assert sig is not None, f"group '{group}' should produce a BUY"
    assert sig.signal_type.name == "BUY"
    assert group in sig.metadata["groups_fired"]


# ── No groups → no signal ─────────────────────────────────────────────────────

def test_no_groups_fired_returns_none(strategy, monkeypatch):
    df = _make_df()
    _patch_all_false(strategy, monkeypatch, except_group=None)
    assert strategy.generate_signal(df, "BTCUSDT") is None


# ── Confidence = groups_fired / total ────────────────────────────────────────

def test_confidence_one_of_six(strategy, monkeypatch):
    df = _make_df()
    _patch_all_false(strategy, monkeypatch, except_group="scalp")
    sig = strategy.generate_signal(df, "BTCUSDT")
    expected = 1 / len(strategy._enabled_groups)
    assert abs(sig.confidence - expected) < 1e-9


def test_confidence_all_six(strategy, monkeypatch):
    df = _make_df()
    for g in strategy._ALL_GROUPS:
        monkeypatch.setattr(strategy, f"_check_{g}", lambda data: True)
    sig = strategy.generate_signal(df, "BTCUSDT")
    assert sig.confidence == pytest.approx(1.0)


def test_confidence_three_of_six(strategy, monkeypatch):
    df = _make_df()
    fire_set = {"quickie", "adx_smas", "hlhb"}
    for g in strategy._ALL_GROUPS:
        monkeypatch.setattr(strategy, f"_check_{g}", lambda data, v=(g in fire_set): v)
    sig = strategy.generate_signal(df, "BTCUSDT")
    expected = 3 / len(strategy._enabled_groups)
    assert abs(sig.confidence - expected) < 1e-9


# ── Stop-loss is -10% ─────────────────────────────────────────────────────────

def test_stop_loss_ten_pct(strategy, monkeypatch):
    df = _make_df()
    _patch_all_false(strategy, monkeypatch, except_group="scalp")
    sig = strategy.generate_signal(df, "BTCUSDT")
    price = sig.price
    assert abs(sig.stop_loss - price * 0.90) < 1e-4


# ── Take-profit is None (dynamic ROI) ────────────────────────────────────────

def test_take_profit_is_none(strategy, monkeypatch):
    df = _make_df()
    _patch_all_false(strategy, monkeypatch, except_group="scalp")
    sig = strategy.generate_signal(df, "BTCUSDT")
    assert sig.take_profit is None


# ── Dynamic ROI values ────────────────────────────────────────────────────────

def test_minimal_roi_default_values(strategy):
    roi = strategy.get_minimal_roi()
    assert roi["0"] == pytest.approx(0.279)
    assert roi["92"] == pytest.approx(0.109)
    assert roi["245"] == pytest.approx(0.059)
    assert roi["561"] == pytest.approx(0.0)


def test_minimal_roi_in_signal_metadata(strategy, monkeypatch):
    df = _make_df()
    _patch_all_false(strategy, monkeypatch, except_group="scalp")
    sig = strategy.generate_signal(df, "BTCUSDT")
    assert "minimal_roi" in sig.metadata
    assert sig.metadata["minimal_roi"]["0"] == pytest.approx(0.279)


def test_minimal_roi_override_from_config():
    cfg = {
        "minimal_roi": {"0": 0.10, "100": 0.05, "200": 0.0},
    }
    s = MacheteV8b(config=cfg)
    roi = s.get_minimal_roi()
    assert roi["0"] == pytest.approx(0.10)
    assert roi["100"] == pytest.approx(0.05)


# ── Disabled group is excluded from denominator ──────────────────────────────

def test_disabled_group_excluded(monkeypatch):
    cfg = {"signals": {"quickie": False, "scalp": True, "adx_smas": True,
                        "awesome_macd": True, "gettin_moist": True, "hlhb": True}}
    s = MacheteV8b(config=cfg)
    assert "quickie" not in s._enabled_groups
    assert len(s._enabled_groups) == 5

    df = _make_df()
    for g in s._enabled_groups:
        monkeypatch.setattr(s, f"_check_{g}", lambda data: True)
    sig = s.generate_signal(df, "BTCUSDT")
    assert sig.confidence == pytest.approx(1.0)
