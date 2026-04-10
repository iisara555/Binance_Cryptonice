"""
Strategy base module - exports from strategies package.
"""
from strategies.base import StrategyBase, Signal, StrategyConfig
from strategies import (
    TrendFollowingStrategy,
    MeanReversionStrategy,
    BreakoutStrategy,
    MomentumStrategy,
    ScalpingStrategy
)
from dataclasses import dataclass
from typing import Dict, Any, Optional
from datetime import datetime
from enum import Enum

# Re-export for compatibility
__all__ = [
    'StrategyBase', 'Signal', 'StrategyConfig',
    'TrendFollowingStrategy', 'MeanReversionStrategy',
    'BreakoutStrategy', 'MomentumStrategy', 'ScalpingStrategy',
    'TradingSignal', 'SignalType', 'SignalConfidence', 'MarketCondition', 
    'detect_market_condition'
]

class SignalType(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

class SignalConfidence(Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

class MarketCondition(Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    SIDEWAY = "SIDEWAY"
    RANGING = "RANGING"  # Alias for SIDEWAY
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    VOLATILE = "VOLATILE"
    LOW_VOLUME = "LOW_VOLUME"

@dataclass
class TradingSignal:
    strategy_name: str
    symbol: str
    signal_type: SignalType
    confidence: float
    price: float
    timestamp: datetime = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_reward_ratio: Optional[float] = None
    metadata: Dict[str, Any] = None


def detect_market_condition(prices: list) -> MarketCondition:
    """Detect current market condition.
    
    Args:
        prices: List of numeric price values (not timestamps!)
    """
    if len(prices) < 50:
        return MarketCondition.SIDEWAY
    
    import numpy as np
    
    # Filter out non-numeric values (timestamps, strings, etc)
    numeric_prices = []
    for p in prices:
        try:
            numeric_prices.append(float(p))
        except (ValueError, TypeError):
            continue  # Skip timestamps or invalid values
    
    if len(numeric_prices) < 50:
        return MarketCondition.SIDEWAY
    
    prices_arr = np.array(numeric_prices, dtype=float)
    sma = np.mean(prices_arr[-50:])
    
    if prices_arr[-1] > sma * 1.02:
        return MarketCondition.BULL
    elif prices_arr[-1] < sma * 0.98:
        return MarketCondition.BEAR
    return MarketCondition.SIDEWAY
