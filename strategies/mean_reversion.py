"""
Mean Reversion Strategy.
"""
import pandas as pd
from .base import StrategyBase, Signal


class MeanReversionStrategy(StrategyBase):
    """Mean reversion using Bollinger Bands."""

    @staticmethod
    def _scale_confidence(z_distance: float) -> float:
        # 0.55 base confidence beyond the band, rising with z-distance.
        return max(0.55, min(0.95, 0.55 + (max(z_distance, 0.0) * 0.12)))

    def analyze(self, data: pd.DataFrame) -> Signal:
        if len(data) < 20:
            return Signal(action='HOLD', confidence=0.0)

        sma = data['close'].rolling(20).mean()
        std = data['close'].rolling(20).std()
        upper = sma + (2 * std)
        lower = sma - (2 * std)

        price = float(data['close'].iloc[-1])
        std_now = float(std.iloc[-1]) if len(std) else 0.0
        if std_now <= 0.0:
            return Signal(action='HOLD', confidence=0.0)

        upper_now = float(upper.iloc[-1])
        lower_now = float(lower.iloc[-1])

        if price < lower_now:
            z_distance = (lower_now - price) / std_now
            return Signal(action='BUY', confidence=self._scale_confidence(z_distance))
        elif price > upper_now:
            z_distance = (price - upper_now) / std_now
            return Signal(action='SELL', confidence=self._scale_confidence(z_distance))
        return Signal(action='HOLD', confidence=0.0)
