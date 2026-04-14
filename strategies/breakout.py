"""
Breakout Strategy.
"""
import pandas as pd
from .base import StrategyBase, Signal


class BreakoutStrategy(StrategyBase):
    """Breakout detection strategy."""

    def analyze(self, data: pd.DataFrame) -> Signal:
        if len(data) < 20:
            return Signal(action='HOLD', confidence=0.0)

        # Look for breakouts
        high_20 = data['high'].rolling(20).max()
        low_20 = data['low'].rolling(20).min()

        price = data['close'].iloc[-1]

        if price > high_20.iloc[-2]:
            return Signal(action='BUY', confidence=0.7)
        elif price < low_20.iloc[-2]:
            return Signal(action='SELL', confidence=0.7)
        return Signal(action='HOLD', confidence=0.5)
