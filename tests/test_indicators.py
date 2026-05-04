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


def test_calculate_cci_returns_series_with_values():
    high, low, close = _sample_ohlc(length=35)

    result = TechnicalIndicators.calculate_cci(high, low, close, period=20)

    assert isinstance(result, pd.Series)
    assert len(result) == len(close)
    # First period-1 values are NaN (warmup); rest should be finite
    assert np.isfinite(result.dropna().to_numpy()).all()


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


def test_calculate_adx_range_and_finite():
    high, low, close = _sample_ohlc(length=40)

    result = TechnicalIndicators.calculate_adx(high, low, close, period=14)

    assert isinstance(result, pd.Series)
    assert len(result) == len(close)
    assert np.isfinite(result.to_numpy()).all()
    assert result.between(0.0, 100.0).all()


def test_calculate_adx_handles_flat_market_without_nan_or_inf():
    close = pd.Series([100.0] * 40)
    high = pd.Series([100.0] * 40)
    low = pd.Series([100.0] * 40)

    result = TechnicalIndicators.calculate_adx(high, low, close, period=14)

    assert np.isfinite(result.to_numpy()).all()
    assert (result == 0.0).all()
