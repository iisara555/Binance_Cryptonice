"""
Shared Technical Indicators Module
Provides common technical analysis calculations used across the codebase.

This module centralizes indicator calculations to avoid duplication
across the codebase.
"""
import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from typing import Tuple


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
        rs = pd.Series(np.where(
            (avg_gain_values == 0.0) & (avg_loss_values == 0.0), 1.0,  # flat market -> RS=1 -> RSI=50
            np.where(avg_loss_values == 0.0, np.inf, avg_gain_values / safe_loss_values)
        ), index=avg_gain.index)
        rsi = 100 - (100 / (1 + rs))
        return rsi.clip(0, 100)
    
    @staticmethod
    def calculate_macd(
        prices: pd.Series,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9
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
        prices: pd.Series,
        period: int = 20,
        std_dev: float = 2.0
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """Bollinger Bands - returns (upper, middle, lower)"""
        middle = prices.rolling(window=period).mean()
        std = prices.rolling(window=period).std()
        upper = middle + (std * std_dev)
        lower = middle - (std * std_dev)
        return upper, middle, lower
    
    @staticmethod
    def calculate_atr(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int = 14
    ) -> pd.Series:
        """Average True Range (ATR)"""
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return TechnicalIndicators._wilder_smoothing(tr, period)
    
    @staticmethod
    def calculate_stochastic(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int = 14
    ) -> Tuple[pd.Series, pd.Series]:
        """Stochastic Oscillator - returns (%K, %D)"""
        lowest_low = low.rolling(window=period).min()
        highest_high = high.rolling(window=period).max()
        k = 100 * ((close - lowest_low) / (highest_high - lowest_low))
        d = k.rolling(window=3).mean()
        return k, d
    
    @staticmethod
    def calculate_adx(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int = 14
    ) -> pd.Series:
        """Average Directional Index (ADX) - Trend strength"""
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

        atr = TechnicalIndicators.calculate_atr(high, low, close, period)
        plus_dm_smoothed = TechnicalIndicators._wilder_smoothing(plus_dm, period)
        minus_dm_smoothed = TechnicalIndicators._wilder_smoothing(minus_dm, period)

        atr_values = atr.to_numpy(dtype=float)
        plus_dm_values = plus_dm_smoothed.to_numpy(dtype=float)
        minus_dm_values = minus_dm_smoothed.to_numpy(dtype=float)
        valid_atr = np.isfinite(atr_values) & (atr_values > 0.0)

        plus_di_values = np.divide(
            100.0 * plus_dm_values,
            atr_values,
            out=np.zeros_like(atr_values, dtype=float),
            where=valid_atr,
        )
        minus_di_values = np.divide(
            100.0 * minus_dm_values,
            atr_values,
            out=np.zeros_like(atr_values, dtype=float),
            where=valid_atr,
        )

        di_sum_values = plus_di_values + minus_di_values
        valid_di_sum = np.isfinite(di_sum_values) & (di_sum_values > 0.0)
        dx_values = np.divide(
            100.0 * np.abs(plus_di_values - minus_di_values),
            di_sum_values,
            out=np.zeros_like(di_sum_values, dtype=float),
            where=valid_di_sum,
        )

        dx = pd.Series(dx_values, index=high.index)
        adx = TechnicalIndicators._wilder_smoothing(dx, period).fillna(0.0)
        return adx.clip(0.0, 100.0)
    
    @staticmethod
    def calculate_cci(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int = 20
    ) -> pd.Series:
        """Commodity Channel Index (CCI)"""
        tp = ((high + low + close) / 3).astype(float)
        sma_tp = tp.rolling(window=period).mean()
        tp_values = tp.to_numpy(dtype=float)
        mad_values = np.full(tp_values.shape, np.nan, dtype=float)
        if period > 0 and len(tp_values) >= period:
            windows = sliding_window_view(tp_values, window_shape=period)
            window_means = windows.mean(axis=1)
            mad_values[period - 1:] = np.abs(windows - window_means[:, None]).mean(axis=1)
        mad = pd.Series(mad_values, index=tp.index)
        cci = (tp - sma_tp) / (0.015 * mad + 1e-10)
        return cci
    
# Convenience functions for direct import
calculate_rsi = TechnicalIndicators.calculate_rsi
calculate_macd = TechnicalIndicators.calculate_macd
calculate_bollinger_bands = TechnicalIndicators.calculate_bollinger_bands
calculate_atr = TechnicalIndicators.calculate_atr
calculate_stochastic = TechnicalIndicators.calculate_stochastic
calculate_adx = TechnicalIndicators.calculate_adx
calculate_cci = TechnicalIndicators.calculate_cci
