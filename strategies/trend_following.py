"""
Trend Following Strategy.
"""
import pandas as pd
from .base import StrategyBase, Signal


class TrendFollowingStrategy(StrategyBase):
    """Trend following using moving averages."""
    
    def analyze(self, data: pd.DataFrame) -> Signal:
        if len(data) < 50:
            return Signal(action='HOLD', confidence=0.0)
        
        # Simple trend detection
        sma20 = data['close'].rolling(20).mean()
        sma50 = data['close'].rolling(50).mean()
        
        if sma20.iloc[-1] > sma50.iloc[-1]:
            return Signal(action='BUY', confidence=0.6)
        elif sma20.iloc[-1] < sma50.iloc[-1]:
            return Signal(action='SELL', confidence=0.6)
        return Signal(action='HOLD', confidence=0.5)
        
    # NOTE: generate_signal is inherited from StrategyBase
    # which provides ATR-based SL/TP calculation automatically
    pass
