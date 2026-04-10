"""
Scalping Strategy.
"""
import pandas as pd
from .base import StrategyBase, Signal


class ScalpingStrategy(StrategyBase):
    """Scalping strategy for short-term trades."""
    
    def analyze(self, data: pd.DataFrame) -> Signal:
        if len(data) < 5:
            return Signal(action='HOLD', confidence=0.0)
        
        # Simple EMA crossover
        ema5 = data['close'].ewm(span=5).mean()
        ema20 = data['close'].ewm(span=20).mean()
        
        if ema5.iloc[-1] > ema20.iloc[-1]:
            return Signal(action='BUY', confidence=0.5)
        elif ema5.iloc[-1] < ema20.iloc[-1]:
            return Signal(action='SELL', confidence=0.5)
        return Signal(action='HOLD', confidence=0.5)
        
    # NOTE: generate_signal is inherited from StrategyBase
    # which provides ATR-based SL/TP calculation automatically
    pass
