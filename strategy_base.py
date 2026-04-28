"""
Strategy base module — re-exports the strategies package and local enums/helpers.
"""

from __future__ import annotations

from enum import Enum

from strategies import (
    BreakoutStrategy,
    MacheteV8bLite,
    MeanReversionStrategy,
    MomentumStrategy,
    ScalpingStrategy,
    SimpleScalpPlus,
    SniperStrategy,
    TrendFollowingStrategy,
)
from strategies.base import Signal, SignalType, StrategyBase, StrategyConfig, TradingSignal

# Re-export for compatibility
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
    "TradingSignal",
    "SignalType",
    "SignalConfidence",
    "MarketCondition",
    "detect_market_condition",
]


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


def _coerce_numeric_prices(prices: list) -> list[float]:
    """Keep only finite float closing prices; drop timestamps and invalid rows."""
    out: list[float] = []
    for p in prices or []:
        try:
            v = float(p)
        except (TypeError, ValueError):
            continue
        if not (v == v):  # NaN
            continue
        out.append(v)
    return out


def detect_market_condition(prices: list) -> MarketCondition:
    """Rough regime from the last 50 closes (numeric values only)."""
    numeric_prices = _coerce_numeric_prices(list(prices))
    if len(numeric_prices) < 50:
        return MarketCondition.SIDEWAY

    import numpy as np

    prices_arr = np.asarray(numeric_prices, dtype=float)
    sma = float(np.mean(prices_arr[-50:]))
    last = float(prices_arr[-1])
    if last > sma * 1.02:
        return MarketCondition.BULL
    if last < sma * 0.98:
        return MarketCondition.BEAR
    return MarketCondition.SIDEWAY
