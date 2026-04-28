"""
Mean Reversion Strategy.

Edge: Bollinger Band re-entry — price closed *back inside* the bands after
spending the prior bar(s) outside, indicating mean reversion. The legacy
version emitted BUY for every bar where price was below the lower band, which
meant catching falling knives during trends. The new logic waits for the
actual reversion bar.
"""

import pandas as pd

from .base import Signal, StrategyBase


class MeanReversionStrategy(StrategyBase):
    """Mean reversion using Bollinger Band re-entry signals."""

    PERIOD = 20
    BAND_STD = 2.0

    @staticmethod
    def _scale_confidence(z_distance: float) -> float:
        # 0.55 base confidence beyond the band, rising with how stretched it was.
        return max(0.55, min(0.95, 0.55 + (max(z_distance, 0.0) * 0.12)))

    def analyze(self, data: pd.DataFrame) -> Signal:
        if len(data) < self.PERIOD + 2 or "close" not in data.columns:
            return Signal(action="HOLD", confidence=0.0)

        sma = data["close"].rolling(self.PERIOD).mean()
        std = data["close"].rolling(self.PERIOD).std()
        if pd.isna(sma.iloc[-1]) or pd.isna(std.iloc[-1]) or float(std.iloc[-1]) <= 0.0:
            return Signal(action="HOLD", confidence=0.0)

        upper = sma + (self.BAND_STD * std)
        lower = sma - (self.BAND_STD * std)

        prev_close = float(data["close"].iloc[-2])
        cur_close = float(data["close"].iloc[-1])
        upper_prev = float(upper.iloc[-2])
        lower_prev = float(lower.iloc[-2])
        upper_now = float(upper.iloc[-1])
        lower_now = float(lower.iloc[-1])
        std_now = float(std.iloc[-1])

        # Bullish reversion: previous bar closed below lower band, now closed back inside.
        if prev_close < lower_prev and cur_close >= lower_now:
            z_distance = (lower_prev - prev_close) / max(std_now, 1e-9)
            return Signal(
                action="BUY",
                confidence=self._scale_confidence(z_distance),
                metadata={
                    "lower_band": lower_now,
                    "sma": float(sma.iloc[-1]),
                    "z_distance": float(z_distance),
                    "entry_reason": "bb_lower_reversion",
                },
            )

        # Bearish reversion: previous bar closed above upper band, now back inside.
        if prev_close > upper_prev and cur_close <= upper_now:
            z_distance = (prev_close - upper_prev) / max(std_now, 1e-9)
            return Signal(
                action="SELL",
                confidence=self._scale_confidence(z_distance),
                metadata={
                    "upper_band": upper_now,
                    "sma": float(sma.iloc[-1]),
                    "z_distance": float(z_distance),
                    "entry_reason": "bb_upper_reversion",
                },
            )

        return Signal(action="HOLD", confidence=0.0)
