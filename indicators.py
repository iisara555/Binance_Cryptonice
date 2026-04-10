"""
Shared Technical Indicators Module
Provides common technical analysis calculations used across the codebase.

This module centralizes indicator calculations to avoid duplication
across the codebase.
"""
import numpy as np
import pandas as pd
from typing import Tuple


class TechnicalIndicators:
    """Calculate technical indicators for trading signals."""
    
    @staticmethod
    def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
        """Relative Strength Index (RSI)"""
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        # Proper RSI: when loss=0 -> RSI=100, when gain=0 -> RSI=0, when both=0 -> RSI=50
        rs = pd.Series(np.where(
            (gain == 0) & (loss == 0), 1.0,       # flat market -> RS=1 -> RSI=50
            np.where(loss == 0, np.inf, gain / loss)
        ), index=gain.index)
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
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window=period).mean()
    
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
        plus_dm = high.diff()
        minus_dm = -low.diff()
        plus_dm[plus_dm < 0] = 0
        minus_dm[minus_dm < 0] = 0
        
        tr = TechnicalIndicators.calculate_atr(high, low, close, period)
        plus_di = 100 * (plus_dm.rolling(window=period).mean() / tr)
        minus_di = 100 * (minus_dm.rolling(window=period).mean() / tr)
        
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        adx = dx.rolling(window=period).mean()
        return adx
    
    @staticmethod
    def calculate_cci(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int = 20
    ) -> pd.Series:
        """Commodity Channel Index (CCI)"""
        tp = (high + low + close) / 3
        sma_tp = tp.rolling(window=period).mean()
        mad = tp.rolling(window=period).apply(lambda x: np.abs(x - x.mean()).mean())
        cci = (tp - sma_tp) / (0.015 * mad + 1e-10)
        return cci
    
    @staticmethod
    def calculate_williams_r(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int = 14
    ) -> pd.Series:
        """Williams %R"""
        highest_high = high.rolling(window=period).max()
        lowest_low = low.rolling(window=period).min()
        williams_r = -100 * (highest_high - close) / (highest_high - lowest_low + 1e-10)
        return williams_r
    
    @staticmethod
    def calculate_mfi(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        volume: pd.Series,
        period: int = 14
    ) -> pd.Series:
        """Money Flow Index (MFI)"""
        tp = (high + low + close) / 3
        raw_money_flow = tp * volume
        positive_flow = raw_money_flow.where(tp > tp.shift(1), 0)
        negative_flow = raw_money_flow.where(tp < tp.shift(1), 0)
        
        positive_mf = positive_flow.rolling(window=period).sum()
        negative_mf = negative_flow.rolling(window=period).sum()
        
        mfi = 100 - (100 / (1 + positive_mf / (negative_mf + 1e-10)))
        return mfi
    
    @staticmethod
    def calculate_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
        """On-Balance Volume (OBV)"""
        direction = pd.Series(np.sign(close.diff()), index=close.index, dtype=float).fillna(0.0)
        obv = (direction * volume).cumsum()
        return obv
    
    @staticmethod
    def calculate_vwap(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        volume: pd.Series
    ) -> pd.Series:
        """Volume Weighted Average Price (VWAP)"""
        typical_price = (high + low + close) / 3
        return (typical_price * volume).cumsum() / volume.cumsum()


# Convenience functions for direct import
calculate_rsi = TechnicalIndicators.calculate_rsi
calculate_macd = TechnicalIndicators.calculate_macd
calculate_bollinger_bands = TechnicalIndicators.calculate_bollinger_bands
calculate_atr = TechnicalIndicators.calculate_atr
calculate_stochastic = TechnicalIndicators.calculate_stochastic
calculate_adx = TechnicalIndicators.calculate_adx
calculate_cci = TechnicalIndicators.calculate_cci
calculate_williams_r = TechnicalIndicators.calculate_williams_r
calculate_mfi = TechnicalIndicators.calculate_mfi
calculate_obv = TechnicalIndicators.calculate_obv
calculate_vwap = TechnicalIndicators.calculate_vwap
