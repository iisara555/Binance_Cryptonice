"""
Breakout Strategy.
"""
import pandas as pd
from .base import StrategyBase, Signal


class BreakoutStrategy(StrategyBase):
    """Breakout detection strategy."""

    @staticmethod
    def _scale_confidence(distance_pct: float) -> float:
        # 0.55 base conviction at breakout threshold, ramps with breakout distance.
        return max(0.55, min(0.95, 0.55 + (max(distance_pct, 0.0) * 40.0)))

    def analyze(self, data: pd.DataFrame) -> Signal:
        if len(data) < 20:
            return Signal(action='HOLD', confidence=0.0)

        # Look for breakouts
        high_20 = data['high'].rolling(20).max()
        low_20 = data['low'].rolling(20).min()

        price = data['close'].iloc[-1]
        prev_high = float(high_20.iloc[-2])
        prev_low = float(low_20.iloc[-2])

        if price > prev_high:
            breakout_pct = (float(price) - prev_high) / max(prev_high, 1e-9)
            return Signal(action='BUY', confidence=self._scale_confidence(breakout_pct))
        elif price < prev_low:
            breakout_pct = (prev_low - float(price)) / max(prev_low, 1e-9)
            return Signal(action='SELL', confidence=self._scale_confidence(breakout_pct))
        return Signal(action='HOLD', confidence=0.0)
