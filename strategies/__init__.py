"""
Strategy base class and all trading strategies.
"""
from .base import StrategyBase, Signal, StrategyConfig
from .trend_following import TrendFollowingStrategy
from .mean_reversion import MeanReversionStrategy
from .breakout import BreakoutStrategy
from .momentum import MomentumStrategy
from .scalping import ScalpingStrategy
from .sniper import SniperStrategy
from .machete_v8b_lite import MacheteV8bLite
from .simple_scalp_plus import SimpleScalpPlus

__all__ = [
    'StrategyBase', 'Signal', 'StrategyConfig',
    'TrendFollowingStrategy', 'MeanReversionStrategy', 
    'BreakoutStrategy', 'MomentumStrategy', 'ScalpingStrategy',
    'SniperStrategy', 'MacheteV8bLite', 'SimpleScalpPlus'
]
