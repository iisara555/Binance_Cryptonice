"""
Shared Technical Indicators Module
Provides common technical analysis calculations used across the codebase.

This module centralizes indicator calculations to avoid duplication
across the codebase.
"""

from typing import Optional, Tuple

import numpy as np
import pandas as pd
import pandas_ta as ta


class TechnicalIndicators:
    """Calculate technical indicators for trading signals."""

    @staticmethod
    def _wilder_smoothing(values: pd.Series, period: int) -> pd.Series:
        return values.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    @staticmethod
    def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
        """Relative Strength Index (RSI)"""
        delta = prices.diff()
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)
        avg_gain = TechnicalIndicators._wilder_smoothing(gain, period)
        avg_loss = TechnicalIndicators._wilder_smoothing(loss, period)
        # Proper RSI: when loss=0 -> RSI=100, when gain=0 -> RSI=0, when both=0 -> RSI=50
        avg_gain_values = avg_gain.to_numpy(dtype=float)
        avg_loss_values = avg_loss.to_numpy(dtype=float)
        safe_loss_values = np.where(avg_loss_values == 0.0, 1.0, avg_loss_values)
        rs = pd.Series(
            np.where(
                (avg_gain_values == 0.0) & (avg_loss_values == 0.0),
                1.0,  # flat market -> RS=1 -> RSI=50
                np.where(avg_loss_values == 0.0, np.inf, avg_gain_values / safe_loss_values),
            ),
            index=avg_gain.index,
        )
        rsi = 100 - (100 / (1 + rs))
        return rsi.clip(0, 100)

    @staticmethod
    def calculate_macd(
        prices: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """MACD indicator - returns (macd, signal, histogram)"""
        exp1 = prices.ewm(span=fast, adjust=False).mean()
        exp2 = prices.ewm(span=slow, adjust=False).mean()
        macd = exp1 - exp2
        macd_signal = macd.ewm(span=signal, adjust=False).mean()
        macd_hist = macd - macd_signal
        return macd, macd_signal, macd_hist

    @staticmethod
    def calculate_bollinger_bands(
        prices: pd.Series, period: int = 20, std_dev: float = 2.0
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """Bollinger Bands - returns (upper, middle, lower)"""
        bb = ta.bbands(prices, length=period, std=std_dev)
        upper = bb.filter(like="BBU").iloc[:, 0]
        middle = bb.filter(like="BBM").iloc[:, 0]
        lower = bb.filter(like="BBL").iloc[:, 0]
        return upper, middle, lower

    @staticmethod
    def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        """Average True Range (ATR)"""
        return ta.atr(high, low, close, length=period)

    @staticmethod
    def calculate_stochastic(
        high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
    ) -> Tuple[pd.Series, pd.Series]:
        """Stochastic Oscillator - returns (%K, %D)"""
        stoch = ta.stoch(high, low, close, k=period, d=3, smooth_k=1)
        k = stoch.filter(like="STOCHk").iloc[:, 0]
        d = stoch.filter(like="STOCHd").iloc[:, 0]
        return k, d

    @staticmethod
    def calculate_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        """Average Directional Index (ADX) - Trend strength"""
        adx_df = ta.adx(high, low, close, length=period)
        adx = adx_df.filter(like="ADX_").iloc[:, 0]
        return adx.fillna(0.0).clip(0.0, 100.0)

    @staticmethod
    def calculate_cci(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20) -> pd.Series:
        """Commodity Channel Index (CCI)"""
        return ta.cci(high, low, close, length=period)


# Convenience functions for direct import
calculate_rsi = TechnicalIndicators.calculate_rsi
calculate_macd = TechnicalIndicators.calculate_macd
calculate_bollinger_bands = TechnicalIndicators.calculate_bollinger_bands
calculate_atr = TechnicalIndicators.calculate_atr
calculate_stochastic = TechnicalIndicators.calculate_stochastic
calculate_adx = TechnicalIndicators.calculate_adx
calculate_cci = TechnicalIndicators.calculate_cci


# --- NEW: Community Strategy Indicators (SPEC_10) ---
# Inspired by MacheteV8b, Simple Scalp, and BB_RPB_TSL community strategies.
# Module-level helpers — the existing TechnicalIndicators class is untouched.


def fisher_transform(
    high: pd.Series,
    low: pd.Series,
    period: int = 10,
) -> Tuple[pd.Series, pd.Series]:
    """
    Fisher Transform — maps price into a (near) Gaussian normal distribution
    so that extremes generate sharper turning-point signals than RSI.

    Returns:
        (fisher, signal) — both pd.Series. ``signal`` is ``fisher.shift(1)``.
        fisher > 0 and crosses above signal -> bullish reversal.
        fisher < 0 and crosses below signal -> bearish reversal.

    Typical values: roughly -3 to +3 (readings beyond +/-2 are rare extremes).
    Source: MacheteV8b community strategy.
    """
    hl2 = (high + low) / 2
    highest_high = hl2.rolling(window=period).max()
    lowest_low = hl2.rolling(window=period).min()

    hl_range = (highest_high - lowest_low).replace(0, 0.001)
    value = 2 * ((hl2 - lowest_low) / hl_range) - 1
    value = value.clip(-0.999, 0.999)

    fisher_raw = 0.5 * np.log((1 + value) / (1 - value))
    fisher = fisher_raw.ewm(span=period, adjust=False).mean()
    signal = fisher.shift(1)
    return fisher, signal


def fisher_signal(
    high: pd.Series,
    low: pd.Series,
    period: int = 10,
) -> pd.Series:
    """
    Simplified Fisher Transform signal.
    +1 = fisher crosses above signal AND fisher > 0 (bullish reversal).
    -1 = fisher crosses below signal AND fisher < 0 (bearish reversal).
     0 = no cross / neutral.
    """
    fisher, sig = fisher_transform(high, low, period)
    cross_up = (fisher > sig) & (fisher.shift(1) <= sig.shift(1))
    cross_down = (fisher < sig) & (fisher.shift(1) >= sig.shift(1))

    result = pd.Series(0, index=high.index, dtype=int)
    result[cross_up & (fisher > 0)] = 1
    result[cross_down & (fisher < 0)] = -1
    return result


def tema(series: pd.Series, period: int = 21) -> pd.Series:
    """
    Triple Exponential Moving Average — reduces lag vs a plain EMA.
    TEMA = 3*EMA1 - 3*EMA2 + EMA3   (EMA2 = EMA(EMA1), EMA3 = EMA(EMA2))

    - More responsive than EMA on fast moves.
    - Smoother than SMA, with less whipsaw than a single EMA.

    Usage:
        df['tema21'] = tema(df['close'], 21)
        df['tema55'] = tema(df['close'], 55)
        bull_trend = df['tema21'] > df['tema55']

    Source: MacheteV8b community strategy.
    """
    ema1 = series.ewm(span=period, adjust=False).mean()
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    ema3 = ema2.ewm(span=period, adjust=False).mean()
    return (3 * ema1) - (3 * ema2) + ema3


def tema_signal(
    series: pd.Series,
    fast_period: int = 9,
    slow_period: int = 21,
) -> pd.Series:
    """
    TEMA crossover signal.
    +1 = fast TEMA crosses above slow TEMA (bullish).
    -1 = fast TEMA crosses below slow TEMA (bearish).
     0 = no cross.
    """
    fast = tema(series, fast_period)
    slow = tema(series, slow_period)

    cross_up = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    cross_down = (fast < slow) & (fast.shift(1) >= slow.shift(1))

    result = pd.Series(0, index=series.index, dtype=int)
    result[cross_up] = 1
    result[cross_down] = -1
    return result


def awesome_oscillator(
    high: pd.Series,
    low: pd.Series,
    fast: int = 5,
    slow: int = 34,
) -> pd.Series:
    """
    Awesome Oscillator (Bill Williams) — momentum indicator.
    AO = SMA(median_price, fast) - SMA(median_price, slow)
    where median_price = (high + low) / 2.

    Interpretation:
        AO > 0  -> bullish momentum.
        AO < 0  -> bearish momentum.
        AO crossing zero -> momentum regime change.

    Saucer signal (3 consecutive bars):
        AO positive + 2 red bars + 1 green bar -> BUY.

    Source: MacheteV8b community strategy.
    """
    median = (high + low) / 2
    return median.rolling(window=fast).mean() - median.rolling(window=slow).mean()


def ao_signal(
    high: pd.Series,
    low: pd.Series,
    fast: int = 5,
    slow: int = 34,
) -> pd.Series:
    """
    Simplified Awesome Oscillator signal.
    +1 = AO crosses above 0 (bullish momentum starts).
    -1 = AO crosses below 0 (bearish momentum starts).
     0 = no zero-cross.
    """
    ao = awesome_oscillator(high, low, fast, slow)
    cross_up = (ao > 0) & (ao.shift(1) <= 0)
    cross_down = (ao < 0) & (ao.shift(1) >= 0)

    result = pd.Series(0, index=high.index, dtype=int)
    result[cross_up] = 1
    result[cross_down] = -1
    return result


def vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: Optional[int] = None,
) -> pd.Series:
    """
    Volume Weighted Average Price.
    VWAP = sum(typical_price * volume) / sum(volume)
    where typical_price = (high + low + close) / 3.

    Args:
        period: Optional rolling window length. When given, returns a
            rolling VWAP over the last ``period`` bars (useful for intraday
            scalping where a fresh per-bar reference is preferable to a
            slow cumulative one). When None, returns the conventional
            cumulative session VWAP.

    Acts as:
        - Dynamic support / resistance reference.
        - Entry filter (long only when price > VWAP).
        - Institutional benchmark price.

    Source: Simple Scalp strategy concept.
    """
    typical_price = (high + low + close) / 3
    tp_vol = typical_price * volume
    if period is not None and int(period) > 0:
        window = int(period)
        rolling_tp_vol = tp_vol.rolling(window=window, min_periods=1).sum()
        rolling_vol = volume.rolling(window=window, min_periods=1).sum().replace(0, np.nan)
        return rolling_tp_vol / rolling_vol
    cumulative_tp_vol = tp_vol.cumsum()
    cumulative_vol = volume.cumsum().replace(0, np.nan)
    return cumulative_tp_vol / cumulative_vol


def volume_confirmation(
    volume: pd.Series,
    period: int = 20,
    threshold: float = 1.2,
) -> pd.Series:
    """
    Volume confirmation filter.
    Returns a boolean pd.Series — True when current volume exceeds
    ``threshold * rolling_mean(volume, period)``.

    threshold = 1.2 -> current volume must be >= 120% of the recent average.

    Combine with another signal:
        if signal and volume_confirmation: enter
        if signal and not volume_confirmation: skip (likely false signal)
    """
    avg_vol = volume.rolling(window=period).mean()
    return volume > (avg_vol * threshold)


def volume_profile_score(
    close: pd.Series,
    volume: pd.Series,
    vwap_series: pd.Series,
    vol_period: int = 20,
    vol_threshold: float = 1.2,
) -> pd.Series:
    """
    Composite volume score (0.0 - 1.0) for entry filtering.

    Score breakdown:
        1.0 = price above VWAP AND high volume (strong entry).
        0.5 = price above VWAP OR high volume.
        0.0 = price below VWAP AND low volume (weak entry).
    """
    above_vwap = (close > vwap_series).astype(float)
    high_vol = volume_confirmation(volume, vol_period, vol_threshold).astype(float)
    return (above_vwap + high_vol) / 2


def hull_ma(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Hull Moving Average — minimal-lag moving average.
    HMA = WMA(2 * WMA(period/2) - WMA(period), sqrt(period))

    Faster response than EMA, useful in scalping where lag must be low.
    Direction:
        rising slope -> bullish trend.
        falling slope -> bearish trend.

    Source: MacheteV8b community strategy.
    """
    half = max(int(period / 2), 1)
    sqrt_period = max(int(np.sqrt(period)), 1)

    def _wma(window: np.ndarray) -> float:
        return float(np.average(window, weights=np.arange(1, len(window) + 1)))

    wma_half = series.rolling(window=half).apply(_wma, raw=True)
    wma_full = series.rolling(window=period).apply(_wma, raw=True)
    raw_hull = 2 * wma_half - wma_full
    return raw_hull.rolling(window=sqrt_period).apply(_wma, raw=True)


def hull_signal(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Hull MA slope signal.
    +1 = HMA slope rising (bull) — current value > value 2 bars ago.
    -1 = HMA slope falling (bear) — current value < value 2 bars ago.
     0 = flat / undefined (warmup).
    """
    h = hull_ma(series, period)
    prev = h.shift(2)
    return pd.Series(
        np.where(h > prev, 1, np.where(h < prev, -1, 0)),
        index=series.index,
        dtype=int,
    )
