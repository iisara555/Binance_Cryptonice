"""
Mean Reversion Strategy.
"""
import pandas as pd
from .base import StrategyBase, Signal


class MeanReversionStrategy(StrategyBase):
    """Mean reversion using Bollinger Bands."""

    def analyze(self, data: pd.DataFrame) -> Signal:
        if len(data) < 20:
            return Signal(action='HOLD', confidence=0.0)

        sma = data['close'].rolling(20).mean()
        std = data['close'].rolling(20).std()
        upper = sma + (2 * std)
        lower = sma - (2 * std)

        price = data['close'].iloc[-1]

        if price < lower.iloc[-1]:
            return Signal(action='BUY', confidence=0.7)
        elif price > upper.iloc[-1]:
            return Signal(action='SELL', confidence=0.7)
        return Signal(action='HOLD', confidence=0.5)
