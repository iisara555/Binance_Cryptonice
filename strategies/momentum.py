"""
Momentum Strategy.
"""
import pandas as pd
from .base import StrategyBase, Signal


class MomentumStrategy(StrategyBase):
    """Momentum trading strategy using RSI."""
    
    def analyze(self, data: pd.DataFrame) -> Signal:
        if len(data) < 14:
            return Signal(action='HOLD', confidence=0.0)
        
        # Calculate RSI
        delta = data['close'].diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        rsi_val = rsi.iloc[-1]
        
        if rsi_val < 30:
            return Signal(action='BUY', confidence=0.7)
        elif rsi_val > 70:
            return Signal(action='SELL', confidence=0.7)
        return Signal(action='HOLD', confidence=0.5)

    # NOTE: generate_signal is inherited from StrategyBase
    # which provides ATR-based SL/TP calculation automatically
    pass
