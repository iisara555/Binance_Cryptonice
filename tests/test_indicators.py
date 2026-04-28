import numpy as np
import pandas as pd
import pytest

from indicators import TechnicalIndicators


def _sample_ohlc(length: int = 40) -> tuple[pd.Series, pd.Series, pd.Series]:
    base = np.linspace(100.0, 120.0, length)
    noise = np.sin(np.linspace(0.0, 6.0, length))
    close = pd.Series(base + noise)
    high = close + 1.5
    low = close - 1.2
    return high, low, close


def test_calculate_cci_matches_legacy_formula():
    high, low, close = _sample_ohlc(length=35)

    result = TechnicalIndicators.calculate_cci(high, low, close, period=20)

    tp = (high + low + close) / 3.0
    sma_tp = tp.rolling(window=20).mean()
    legacy_mad = tp.rolling(window=20).apply(lambda values: np.abs(values - values.mean()).mean())
    expected = (tp - sma_tp) / (0.015 * legacy_mad + 1e-10)

    np.testing.assert_allclose(result.dropna().to_numpy(), expected.dropna().to_numpy())


def test_calculate_rsi_uses_wilder_smoothing():
    _, _, close = _sample_ohlc(length=30)

    result = TechnicalIndicators.calculate_rsi(close, period=14)

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / 14.0, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1.0 / 14.0, adjust=False, min_periods=14).mean()
    safe_loss = avg_loss.replace(0.0, np.nan)
    expected = 100 - (100 / (1 + (avg_gain / safe_loss)))
    expected = expected.mask((avg_gain == 0.0) & (avg_loss == 0.0), 50.0)
    expected = expected.mask((avg_loss == 0.0) & (avg_gain > 0.0), 100.0)
    expected = expected.clip(0, 100)

    np.testing.assert_allclose(result.dropna().to_numpy(), expected.dropna().to_numpy())


def test_calculate_atr_uses_wilder_smoothing():
    high, low, close = _sample_ohlc(length=30)

    result = TechnicalIndicators.calculate_atr(high, low, close, period=14)

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    expected = tr.ewm(alpha=1.0 / 14.0, adjust=False, min_periods=14).mean()

    np.testing.assert_allclose(result.dropna().to_numpy(), expected.dropna().to_numpy())


def test_calculate_adx_uses_wilder_smoothing():
    high, low, close = _sample_ohlc(length=40)

    result = TechnicalIndicators.calculate_adx(high, low, close, period=14)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / 14.0, adjust=False, min_periods=14).mean()
    plus_dm_smoothed = plus_dm.ewm(alpha=1.0 / 14.0, adjust=False, min_periods=14).mean().to_numpy(dtype=float)
    minus_dm_smoothed = minus_dm.ewm(alpha=1.0 / 14.0, adjust=False, min_periods=14).mean().to_numpy(dtype=float)
    atr_values = atr.to_numpy(dtype=float)
    valid_atr = np.isfinite(atr_values) & (atr_values > 0.0)
    plus_di = np.divide(100.0 * plus_dm_smoothed, atr_values, out=np.zeros_like(atr_values), where=valid_atr)
    minus_di = np.divide(100.0 * minus_dm_smoothed, atr_values, out=np.zeros_like(atr_values), where=valid_atr)
    di_sum = plus_di + minus_di
    dx = np.divide(
        100.0 * np.abs(plus_di - minus_di),
        di_sum,
        out=np.zeros_like(di_sum),
        where=np.isfinite(di_sum) & (di_sum > 0.0),
    )
    expected = pd.Series(dx).ewm(alpha=1.0 / 14.0, adjust=False, min_periods=14).mean().fillna(0.0).clip(0.0, 100.0)

    np.testing.assert_allclose(result.to_numpy(), expected.to_numpy())
    assert result.between(0.0, 100.0).all()


def test_calculate_adx_handles_flat_market_without_nan_or_inf():
    close = pd.Series([100.0] * 40)
    high = pd.Series([100.0] * 40)
    low = pd.Series([100.0] * 40)

    result = TechnicalIndicators.calculate_adx(high, low, close, period=14)

    assert np.isfinite(result.to_numpy()).all()
    assert (result == 0.0).all()
