"""
Strategy base class and all trading strategies.
"""

from .base import Signal, StrategyBase, StrategyConfig
from .breakout import BreakoutStrategy
from .machete_v8b_lite import MacheteV8bLite
from .mean_reversion import MeanReversionStrategy
from .momentum import MomentumStrategy
from .scalping import ScalpingStrategy
from .simple_scalp_plus import SimpleScalpPlus
from .sniper import SniperStrategy
from .trend_following import TrendFollowingStrategy

__all__ = [
    "StrategyBase",
    "Signal",
    "StrategyConfig",
    "TrendFollowingStrategy",
    "MeanReversionStrategy",
    "BreakoutStrategy",
    "MomentumStrategy",
    "ScalpingStrategy",
    "SniperStrategy",
    "MacheteV8bLite",
    "SimpleScalpPlus",
]
