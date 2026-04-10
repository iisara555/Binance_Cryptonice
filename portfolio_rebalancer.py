"""
Portfolio Rebalancer Module for Crypto Trading Bot
====================================================
Manages portfolio rebalancing with multiple strategies:
- Threshold-based: rebalance when allocation drifts beyond X%
- Calendar-based: rebalance on schedule (daily/weekly)
- Risk-based: adjust allocations based on volatility

Data-Aware: Skips rebalancing for assets with insufficient candle data.

Author: Memo 🐕
"""

import logging
import json
import os
import sqlite3
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field
from enum import Enum
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Minimum candles needed before an asset can be traded/rebalanced by ML system
MIN_CANDLES_FOR_TRADING = 1000

# Database path (can be overridden via config)
DEFAULT_DB_PATH = "crypto_bot.db"


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_fraction(value: Any, default: float = 0.5) -> float:
    parsed = _coerce_float(value, default)
    if parsed > 1.0:
        parsed /= 100.0
    return max(0.0, min(1.0, parsed))


def _parse_target_allocation_string(raw: str) -> Dict[str, float]:
    parsed: Dict[str, float] = {}
    for chunk in str(raw or "").replace(";", ",").split(","):
        item = chunk.strip()
        if not item:
            continue
        separator = "=" if "=" in item else ":" if ":" in item else ""
        if not separator:
            continue
        symbol, value = item.split(separator, 1)
        asset = symbol.strip().upper()
        pct = _coerce_float(value.strip().rstrip("%"), 0.0)
        if asset and pct > 0:
            parsed[asset] = pct
    return parsed


def _normalize_target_allocation(targets: Optional[Dict[str, float]]) -> Dict[str, float]:
    normalized = {
        str(symbol or "").upper(): _coerce_float(value, 0.0)
        for symbol, value in (targets or {}).items()
        if str(symbol or "").strip() and _coerce_float(value, 0.0) > 0
    }
    total = sum(normalized.values())
    if total <= 0:
        return {}
    if abs(total - 100.0) < 0.0001:
        return normalized
    logger.info("Normalizing target allocation to 100%% (raw total: %.4f%%)", total)
    return {symbol: (value / total) * 100.0 for symbol, value in normalized.items()}


def _load_target_allocation(config: Dict[str, Any]) -> Dict[str, float]:
    env_raw = os.environ.get("REBALANCE_TARGET_ALLOCATION", "").strip()
    if env_raw:
        parsed = _parse_target_allocation_string(env_raw)
        if parsed:
            logger.info("Loaded rebalance target allocation from REBALANCE_TARGET_ALLOCATION")
            return _normalize_target_allocation(parsed)

    env_prefixed: Dict[str, float] = {}
    for key, value in os.environ.items():
        if not key.startswith("REBALANCE_TARGET_") or key == "REBALANCE_TARGET_ALLOCATION":
            continue
        asset = key[len("REBALANCE_TARGET_"):].strip().upper()
        pct = _coerce_float(str(value).strip().rstrip("%"), 0.0)
        if asset and pct > 0:
            env_prefixed[asset] = pct
    if env_prefixed:
        logger.info("Loaded rebalance target allocation from REBALANCE_TARGET_<ASSET> env vars")
        return _normalize_target_allocation(env_prefixed)

    return _normalize_target_allocation(config.get("target_allocation", {}))


def _parse_asset_list(raw: Any) -> List[str]:
    if isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        items = str(raw or "").replace(";", ",").split(",")
    values: List[str] = []
    for item in items:
        asset = str(item or "").strip().upper()
        if asset and asset not in values:
            values.append(asset)
    return values


def _apply_sideways_target_allocation(
    target_allocation: Dict[str, float],
    cash_assets: List[str],
    exposure_factor: float,
) -> Dict[str, float]:
    if not target_allocation:
        return {}

    factor = _coerce_fraction(exposure_factor, 0.5)
    if factor >= 0.9999:
        return dict(target_allocation)

    adjusted: Dict[str, float] = {}
    for symbol, target_pct in target_allocation.items():
        if symbol in cash_assets:
            adjusted[symbol] = target_pct
        else:
            adjusted[symbol] = target_pct * factor

    receiving_assets = [asset for asset in cash_assets if asset in target_allocation]
    if not receiving_assets:
        adjusted.setdefault("THB", 0.0)
        receiving_assets = ["THB"]

    freed_pct = max(0.0, 100.0 - sum(adjusted.values()))
    receiver_weight = sum(target_allocation.get(asset, 0.0) for asset in receiving_assets)
    if receiver_weight <= 0:
        split = freed_pct / len(receiving_assets)
        for asset in receiving_assets:
            adjusted[asset] = adjusted.get(asset, 0.0) + split
    else:
        for asset in receiving_assets:
            share = target_allocation.get(asset, 0.0) / receiver_weight
            adjusted[asset] = adjusted.get(asset, 0.0) + (freed_pct * share)

    return _normalize_target_allocation(adjusted)


# ─────────────────────────────────────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────────────────────────────────────

def get_candle_count(symbol: str, db_path: str = DEFAULT_DB_PATH) -> Tuple[int, bool, str]:
    """
    Check how many candles an asset has in the database.
    
    Args:
        symbol: Trading pair symbol (e.g. "THB_BTC" or "BTC")
        db_path: Path to SQLite database
    
    Returns:
        (candle_count, data_ready, status_message)
    """
    if not os.path.exists(db_path):
        return 0, False, "DB not found"
    
    try:
        # Normalize symbol for DB query
        # Config uses "BTC", DB uses "THB_BTC"
        db_symbol = symbol if "_" in symbol else f"THB_{symbol}"
        
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                'SELECT COUNT(*) FROM prices WHERE pair = ?',
                (db_symbol,)
            )
            count = cur.fetchone()[0]
        finally:
            conn.close()
        
        if count >= MIN_CANDLES_FOR_TRADING:
            return count, True, "ready"
        elif count > 0:
            pct = (count / MIN_CANDLES_FOR_TRADING) * 100
            return count, False, f"collecting ({pct:.0f}%)"
        else:
            return 0, False, "no data"
    
    except Exception as e:
        logger.warning(f"Error checking candle count for {symbol}: {e}")
        return 0, False, f"error: {e}"


def has_ever_held(symbol: str, db_path: str = DEFAULT_DB_PATH) -> bool:
    """Return True if the bot has ever bought the given symbol/pair."""
    if not os.path.exists(db_path):
        return False

    try:
        db_symbol = symbol.upper()
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                'SELECT 1 FROM held_coins_history WHERE UPPER(symbol) = ? LIMIT 1',
                (db_symbol,)
            )
            exists = cur.fetchone() is not None
        finally:
            conn.close()
        return exists
    except Exception as e:
        logger.debug(f"Error checking held coin history for {symbol}: {e}")
        return False


class RebalanceStrategy(Enum):
    THRESHOLD = "threshold"    # Rebalance when drift > threshold
    CALENDAR = "calendar"     # Rebalance on schedule
    RISK = "risk"             # Adjust based on volatility
    COMBINED = "combined"     # All of the above


class RebalanceTrigger(Enum):
    THRESHOLD_BREACH = "threshold_breach"
    SCHEDULE_DUE = "schedule_due"
    VOLATILITY_ADJUSTMENT = "volatility_adjustment"
    MANUAL = "manual"
    BALANCE_EVENT = "balance_event"
    TRADE_COUNT = "trade_count"
    TP_SL_HIT = "tp_sl_hit"
    SIDEWAYS_REGIME = "sideways_regime"


@dataclass
class AllocationTarget:
    """Target allocation for a single asset."""
    symbol: str
    target_pct: float          # Target percentage (0-100)
    current_price: float = 0.0
    current_quantity: float = 0.0
    current_value: float = 0.0
    current_pct: float = 0.0   # Current allocation %
    drift_pct: float = 0.0      # Difference from target %
    imbalance_value: float = 0.0  # Value to trade to rebalance
    # Data readiness fields (added in v1.2)
    candle_count: int = 0        # Number of candles in database
    data_ready: bool = True      # True if has enough data for ML trading
    data_status: str = "ready"   # "ready" | "collecting" | "unknown"

    def calculate(self, total_portfolio_value: float):
        """Calculate current allocation metrics."""
        if total_portfolio_value > 0:
            self.current_pct = (self.current_value / total_portfolio_value) * 100
        else:
            self.current_pct = 0.0
        self.drift_pct = self.current_pct - self.target_pct
        self.imbalance_value = (self.drift_pct / 100) * total_portfolio_value


def create_rebalance_order(
    alloc: "AllocationTarget",
    trade_value: float,
    side: str,
    reason: str,
    priority: int = 0,
) -> Optional["RebalanceOrder"]:
    """Create a rebalance order if trade conditions are valid."""
    if alloc.symbol.upper() == "THB":
        return None

    trade_value = abs(trade_value)
    if trade_value <= 0 or alloc.current_price <= 0:
        return None

    quantity = trade_value / alloc.current_price
    if quantity <= 0:
        return None

    return RebalanceOrder(
        symbol=alloc.symbol,
        side=side,
        quantity=round(quantity, 8),
        estimated_value=round(trade_value, 2),
        current_price=alloc.current_price,
        reason=reason,
        priority=priority,
    )


@dataclass
class RebalanceOrder:
    """A single rebalance trade order."""
    symbol: str
    side: str           # "buy" or "sell"
    quantity: float
    estimated_value: float
    current_price: float
    reason: str
    priority: int = 0   # Higher = more urgent


@dataclass
class RebalancePlan:
    """Complete rebalancing plan."""
    trigger: RebalanceTrigger
    strategy: RebalanceStrategy
    timestamp: datetime
    total_portfolio_value: float
    allocations: List[AllocationTarget]
    orders: List[RebalanceOrder]
    total_trades: int = 0
    estimated_cost: float = 0.0
    max_drift_pct: float = 0.0
    reasons: List[str] = field(default_factory=list)
    # Data-aware fields (v1.2)
    skipped_assets: List[Dict[str, Any]] = field(default_factory=list)  # Assets skipped due to insufficient data

    def should_execute(self, min_trade_value: float = 1.0) -> bool:
        """Check if plan has actionable orders."""
        return (
            len(self.orders) > 0
            and any(o.estimated_value >= min_trade_value for o in self.orders)
        )

    def get_data_status_summary(self) -> str:
        """Get a summary of data readiness across all assets."""
        ready = [a for a in self.allocations if a.data_ready]
        collecting = [a for a in self.allocations if not a.data_ready]
        return f"{len(ready)} ready, {len(collecting)} collecting"

    def to_dict(self) -> dict:
        return {
            "trigger": self.trigger.value,
            "strategy": self.strategy.value,
            "timestamp": self.timestamp.isoformat(),
            "total_portfolio_value": round(self.total_portfolio_value, 2),
            "total_trades": self.total_trades,
            "estimated_cost": round(self.estimated_cost, 2),
            "max_drift_pct": round(self.max_drift_pct, 2),
            "reasons": self.reasons,
            "data_status": self.get_data_status_summary(),
            "allocations": [
                {
                    "symbol": a.symbol,
                    "target_pct": round(a.target_pct, 2),
                    "current_pct": round(a.current_pct, 2),
                    "drift_pct": round(a.drift_pct, 2),
                    "imbalance_value": round(a.imbalance_value, 2),
                    "data_ready": a.data_ready,
                    "candle_count": a.candle_count,
                }
                for a in self.allocations
            ],
            "skipped_assets": [
                {
                    "symbol": s["symbol"],
                    "candles": s["candle_count"],
                    "reason": s["reason"],
                }
                for s in self.skipped_assets
            ],
            "orders": [
                {
                    "symbol": o.symbol,
                    "side": o.side,
                    "quantity": round(o.quantity, 8),
                    "estimated_value": round(o.estimated_value, 2),
                    "reason": o.reason,
                }
                for o in self.orders
            ],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Base Strategy
# ─────────────────────────────────────────────────────────────────────────────

class RebalanceStrategyBase(ABC):
    """Abstract base class for rebalancing strategies."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    @abstractmethod
    def should_rebalance(
        self,
        allocations: List[AllocationTarget],
        context: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """
        Determine if rebalancing is needed.
        Returns (should_rebalance, reason).
        """
        pass

    @abstractmethod
    def generate_orders(
        self,
        allocations: List[AllocationTarget],
        total_value: float,
        context: Dict[str, Any]
    ) -> List[RebalanceOrder]:
        """Generate rebalance orders."""
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Threshold-Based Strategy
# ─────────────────────────────────────────────────────────────────────────────

class ThresholdRebalanceStrategy(RebalanceStrategyBase):
    """
    Rebalance when any single asset drifts beyond threshold %.
    
    Config:
        threshold_pct: 10.0  # Rebalance if any asset drifts > 10%
        min_rebalance_pct: 1.0  # Minimum drift to trigger (avoid noise)
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.threshold_pct = config.get("threshold_pct", 10.0)
        self.min_rebalance_pct = config.get("min_rebalance_pct", 1.0)

    def should_rebalance(
        self,
        allocations: List[AllocationTarget],
        context: Dict[str, Any]
    ) -> Tuple[bool, str]:
        # Check if any asset exceeds threshold drift
        worst_drift = max(abs(a.drift_pct) for a in allocations)
        
        if worst_drift > self.threshold_pct:
            worst_asset = max(allocations, key=lambda a: abs(a.drift_pct))
            return True, (
                f"Threshold breach: {worst_asset.symbol} "
                f"drifted {worst_asset.drift_pct:+.2f}% "
                f"(threshold: {self.threshold_pct}%)"
            )
        
        if worst_drift > self.min_rebalance_pct:
            # Check if sum of all drifts suggests systematic imbalance
            total_positive = sum(a.drift_pct for a in allocations if a.drift_pct > 0)
            total_negative = sum(a.drift_pct for a in allocations if a.drift_pct < 0)
            net_drift = abs(total_positive + total_negative)
            
            if net_drift > self.threshold_pct:
                return True, (
                    f"Net drift {net_drift:.2f}% exceeds threshold {self.threshold_pct}%"
                )
        
        return False, (
            f"Within threshold: max drift {worst_drift:.2f}% "
            f"(threshold: {self.threshold_pct}%)"
        )

    def generate_orders(
        self,
        allocations: List[AllocationTarget],
        total_value: float,
        context: Dict[str, Any]
    ) -> List[RebalanceOrder]:
        orders = []
        
        # Sort by absolute drift (largest first) to prioritize most imbalanced
        sorted_allocations = sorted(
            allocations, key=lambda a: abs(a.drift_pct), reverse=True
        )
        
        for alloc in sorted_allocations:
            if abs(alloc.drift_pct) < self.min_rebalance_pct:
                continue
            
            # Calculate trade value needed
            trade_value = abs(alloc.imbalance_value)
            
            if trade_value < context.get("min_trade_value", 1.0):
                continue
            
            side = "sell" if alloc.drift_pct > 0 else "buy"
            reason = (
                f"{side.upper()} {alloc.symbol} to reach {alloc.target_pct:.2f}% allocation "
                f"(drift: {alloc.drift_pct:+.1f}%)"
            )
            order = create_rebalance_order(
                alloc,
                trade_value,
                side,
                reason,
                priority=int(abs(alloc.drift_pct)),
            )
            if order:
                orders.append(order)
        
        return orders


# ─────────────────────────────────────────────────────────────────────────────
# Calendar-Based Strategy
# ─────────────────────────────────────────────────────────────────────────────

class CalendarRebalanceStrategy(RebalanceStrategyBase):
    """
    Rebalance on a fixed schedule.
    
    Config:
        frequency: "daily" | "weekly" | "monthly"
        day_of_week: 0-6 (for weekly, 0=Monday)
        hour_of_day: 0-23 (UTC recommended)
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.frequency = config.get("frequency", "weekly")
        self.day_of_week = config.get("day_of_week", 0)  # Monday
        self.hour_of_day = config.get("hour_of_day", 0)
        self._last_rebalance: Optional[datetime] = None
        
        # Load last rebalance time from context if available
        if context := config.get("_last_rebalance"):
            try:
                self._last_rebalance = datetime.fromisoformat(context)
            except (ValueError, TypeError):
                pass

    def _is_due(self) -> Tuple[bool, str]:
        """Check if scheduled rebalance is due."""
        now = datetime.utcnow()
        today = now.date()
        
        if self.frequency == "daily":
            # Rebalance once per day at specified hour
            if self._last_rebalance:
                last_date = self._last_rebalance.date()
                if last_date == today:
                    return False, f"Daily rebalance already done today ({today})"
            return True, f"Daily rebalance due (hour: {self.hour_of_day} UTC)"
        
        elif self.frequency == "weekly":
            # Rebalance once per week on specified day/hour
            days_since_monday = today.weekday()
            target_date = today - timedelta(days=days_since_monday)
            
            if self._last_rebalance:
                last_date = self._last_rebalance.date()
                if last_date >= target_date:
                    days_until_next = 7 - days_since_monday
                    return False, f"Weekly rebalance next in {days_until_next} days"
            
            # Check if today is the target day and hour
            if today.weekday() == self.day_of_week and now.hour >= self.hour_of_day:
                return True, f"Weekly rebalance due today (day {self.day_of_week}, hour {self.hour_of_day} UTC)"
            
            return False, f"Weekly rebalance scheduled for day {self.day_of_week}"
        
        elif self.frequency == "monthly":
            if self._last_rebalance:
                last_month = self._last_rebalance.month
                if last_month == now.month and self._last_rebalance.day >= now.day:
                    return False, "Monthly rebalance already done this month"
            return True, "Monthly rebalance due"
        
        return False, "Unknown frequency"

    def should_rebalance(
        self,
        allocations: List[AllocationTarget],
        context: Dict[str, Any]
    ) -> Tuple[bool, str]:
        return self._is_due()

    def generate_orders(
        self,
        allocations: List[AllocationTarget],
        total_value: float,
        context: Dict[str, Any]
    ) -> List[RebalanceOrder]:
        """Generate orders to restore target allocation."""
        orders = []
        
        for alloc in allocations:
            trade_value = abs(alloc.imbalance_value)
            
            if trade_value < context.get("min_trade_value", 1.0):
                continue
            
            side = "sell" if alloc.drift_pct > 0 else "buy"
            reason = (
                f"[Calendar] {'Sell' if side == 'sell' else 'Buy'} {alloc.symbol} "
                f"to reach {alloc.target_pct:.2f}%"
            )
            order = create_rebalance_order(
                alloc,
                trade_value,
                side,
                reason,
                priority=5,
            )
            if order:
                orders.append(order)
        
        # Update last rebalance time
        self._last_rebalance = datetime.utcnow()
        
        return orders


# ─────────────────────────────────────────────────────────────────────────────
# Risk-Based Strategy
# ─────────────────────────────────────────────────────────────────────────────

class RiskRebalanceStrategy(RebalanceStrategyBase):
    """
    Adjust allocations based on asset volatility.
    High-volatility assets get smaller allocations.
    Low-volatility assets get larger allocations.
    
    Config:
        volatility_window: 20        # Candles to use for volatility calc
        risk_adjustment_factor: 0.5  # How much to adjust (0-1)
        min_allocation_pct: 5.0      # Minimum allocation floor
        max_allocation_pct: 50.0     # Maximum allocation ceiling
        rebalance_on_volatility_change: True
        volatility_threshold_pct: 30  # Rebalance if volatility changes > X%
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.volatility_window = config.get("volatility_window", 20)
        self.risk_adjustment_factor = config.get("risk_adjustment_factor", 0.5)
        self.min_allocation_pct = config.get("min_allocation_pct", 5.0)
        self.max_allocation_pct = config.get("max_allocation_pct", 50.0)
        self.volatility_threshold_pct = config.get("volatility_threshold_pct", 30)
        self.min_rebalance_pct = config.get("min_rebalance_pct", 1.0)
        self._last_volatility: Dict[str, float] = {}

    def _calculate_volatility(self, prices: List[float]) -> float:
        """Calculate volatility as coefficient of variation (std/mean * 100)."""
        if len(prices) < 2:
            return 0.0
        
        import statistics
        mean = statistics.mean(prices)
        if mean == 0:
            return 0.0
        
        std = statistics.stdev(prices)
        return (std / mean) * 100

    def _calculate_target_adjustment(
        self,
        symbol: str,
        current_volatility: float,
        prices: List[float],
        base_target: float,
    ) -> float:
        """
        Adjust target allocation based on volatility.
        High volatility = reduce allocation.
        """
        if current_volatility == 0:
            return base_target
        
        # Calculate volatility rank across all tracked assets
        avg_volatility = 30.0  # Baseline
        
        # Scale adjustment: if volatility is higher than avg, reduce allocation
        volatility_ratio = avg_volatility / max(current_volatility, 1)
        adjustment = 1 + (volatility_ratio - 1) * self.risk_adjustment_factor
        
        adjusted = base_target * adjustment
        
        # Apply floor and ceiling
        adjusted = max(self.min_allocation_pct, min(self.max_allocation_pct, adjusted))
        
        return adjusted

    def should_rebalance(
        self,
        allocations: List[AllocationTarget],
        context: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """
        Check if volatility-based rebalancing is needed.
        Compares current vs last known volatility.
        Requires historical price data (list). If only current price (float) is
        available, returns False since volatility cannot be calculated.
        """
        price_data = context.get("price_data", {})  # symbol -> list of prices
        threshold = self.volatility_threshold_pct
        
        for alloc in allocations:
            raw_prices = price_data.get(alloc.symbol, [])
            
            # If we got a single float (current price) instead of a list,
            # we can't calculate volatility - risk strategy can't trigger
            if isinstance(raw_prices, (int, float)):
                return False, ""
            
            if not isinstance(raw_prices, list) or len(raw_prices) < self.volatility_window:
                # Not enough history to calculate volatility
                continue
            
            prices = raw_prices[-self.volatility_window:]
            current_vol = self._calculate_volatility(prices)
            last_vol = self._last_volatility.get(alloc.symbol, 0)
            
            if last_vol > 0:
                vol_change = abs(current_vol - last_vol) / last_vol * 100
                if vol_change > threshold:
                    return True, (
                        f"Volatility change for {alloc.symbol}: "
                        f"{last_vol:.1f}% -> {current_vol:.1f}% "
                        f"(change: {vol_change:.1f}%, threshold: {threshold}%)"
                    )
        
        return False, ""

    def generate_orders(
        self,
        allocations: List[AllocationTarget],
        total_value: float,
        context: Dict[str, Any]
    ) -> List[RebalanceOrder]:
        """Generate orders to match risk-adjusted targets."""
        orders = []
        price_data = context.get("price_data", {})
        
        for alloc in allocations:
            # price_data can be list of historical prices (for volatility calc)
            # or a single float (current price only) - skip if not list
            raw_prices = price_data.get(alloc.symbol, [])
            
            # If we got a single float (current price), wrap in list or skip
            if isinstance(raw_prices, (int, float)):
                if raw_prices == 0.0:
                    continue
                raw_prices = [raw_prices]
            
            if not isinstance(raw_prices, list) or len(raw_prices) < self.volatility_window:
                # Cannot calculate volatility without enough history
                # Fall back to threshold-based order for this asset
                if abs(alloc.drift_pct) < self.min_rebalance_pct:
                    continue
                side = "sell" if alloc.drift_pct > 0 else "buy"
                reason = (
                    f"[Risk-NoHistory] {'Sell' if side == 'sell' else 'Buy'} {alloc.symbol} "
                    f"to reach {alloc.target_pct:.2f}%"
                )
                order = create_rebalance_order(
                    alloc,
                    abs(alloc.imbalance_value),
                    side,
                    reason,
                    priority=2,
                )
                if order and order.estimated_value >= context.get("min_trade_value", 1.0):
                    orders.append(order)
                continue
            
            prices = raw_prices[-self.volatility_window:]
            current_vol = self._calculate_volatility(prices)
            
            # Calculate risk-adjusted target
            adjusted_target = self._calculate_target_adjustment(
                alloc.symbol, current_vol, prices, alloc.target_pct
            )
            
            # Calculate difference
            drift = adjusted_target - alloc.target_pct
            drift_value = (drift / 100) * total_value
            
            if abs(drift_value) < context.get("min_trade_value", 1.0):
                continue

            side = "buy" if drift_value > 0 else "sell"
            reason = (
                f"[Risk] {'Increase' if side == 'buy' else 'Decrease'} {alloc.symbol} "
                f"due to volatility {current_vol:.1f}% "
                f"(target: {adjusted_target:.2f}%)"
            )
            order = create_rebalance_order(
                alloc,
                abs(drift_value),
                side,
                reason,
                priority=3,
            )
            if order:
                orders.append(order)

            # Update stored volatility
            self._last_volatility[alloc.symbol] = current_vol
        
        return orders


# ─────────────────────────────────────────────────────────────────────────────
# Main Portfolio Rebalancer
# ─────────────────────────────────────────────────────────────────────────────

class PortfolioRebalancer:
    """
    Main portfolio rebalancing engine.
    
    Usage:
        rebalancer = PortfolioRebalancer(config)
        
        # Check if rebalance needed
        should_rebalance, reason = rebalancer.check_rebalance_needed(
            portfolio_manager, price_data
        )
        
        # Generate and execute rebalance plan
        plan = rebalancer.create_rebalance_plan(
            portfolio_manager, price_data, strategy="threshold"
        )
        
        if plan.should_execute():
            for order in plan.orders:
                execute_trade(order)
    """

    def __init__(
        self,
        config: Dict[str, Any],
        persist_path: Optional[str] = None,
    ):
        self.config = dict(config.get("rebalance", {}))
        self.enabled = self.config.get("enabled", False)
        self.persist_path = persist_path
        self._base_target_allocation = _load_target_allocation(self.config)
        self.config["target_allocation"] = dict(self._base_target_allocation)
        self.allow_new_assets = bool(self.config.get("allow_new_assets", True))
        self.cash_assets = _parse_asset_list(
            os.environ.get("REBALANCE_CASH_ASSETS", self.config.get("cash_assets", ["THB", "USDT"]))
        ) or ["THB"]
        self.sideways_exposure_factor = _coerce_fraction(
            os.environ.get(
                "REBALANCE_SIDEWAYS_EXPOSURE_FACTOR",
                self.config.get("sideways_exposure_factor", 0.5),
            ),
            0.5,
        )
        
        # Strategy configuration
        self.strategy_type = RebalanceStrategy(
            self.config.get("strategy", "combined")
        )
        
        # Load strategies
        self.threshold_strategy = ThresholdRebalanceStrategy(
            self.config.get("threshold", {})
        )
        self.calendar_strategy = CalendarRebalanceStrategy(
            self.config.get("calendar", {})
        )
        self.risk_strategy = RiskRebalanceStrategy(
            self.config.get("risk", {})
        )
        
        # Global settings
        self.rebalance_threshold = self.config.get("threshold", {}).get(
            "threshold_pct", 10.0
        )
        self.min_trade_value = self.config.get("min_trade_value", 1.0)
        self.estimated_cost_pct = self.config.get("estimated_cost_pct", 0.5)  # 0.5% round-trip (Bitkub 0.25% x 2)
        self._cooldown_minutes = max(0, _coerce_float(self.config.get("cooldown_minutes", 60), 60))
        
        # State
        self._iteration_counter = 0
        self._check_interval = self.config.get("check_interval", 1)  # Check every N iterations
        self._last_rebalance_time: Optional[datetime] = None
        self._rebalance_count = 0
        
        # Load persisted state
        self._load_state()
        
        logger.info(
            f"PortfolioRebalancer initialized | "
            f"Enabled: {self.enabled} | "
            f"Strategy: {self.strategy_type.value} | "
            f"Threshold: {self.rebalance_threshold}%"
        )

    def get_target_allocation(self, market_condition: Optional[str] = None) -> Dict[str, float]:
        """Return the active target allocation, optionally reduced for sideways regimes."""
        targets = dict(self._base_target_allocation)
        condition = str(market_condition or "").upper()
        if condition in {"SIDEWAY", "SIDEWAYS", "RANGING"}:
            return _apply_sideways_target_allocation(
                targets,
                self.cash_assets,
                self.sideways_exposure_factor,
            )
        return dict(targets)

    def _load_state(self):
        """Load persisted rebalancer state."""
        if not self.persist_path:
            return
        try:
            path = self.persist_path.replace(".json", "_rebalancer_state.json")
            with open(path, "r") as f:
                data = json.load(f)
            self._last_rebalance_time = datetime.fromisoformat(
                data.get("last_rebalance_time", "2000-01-01T00:00:00")
            )
            self._rebalance_count = data.get("rebalance_count", 0)
            self._iteration_counter = data.get("iteration_counter", 0)
            logger.info(f"Rebalancer state loaded. Count: {self._rebalance_count}")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Could not load rebalancer state: {e}")

    def _save_state(self):
        """Persist rebalancer state."""
        if not self.persist_path:
            return
        try:
            path = self.persist_path.replace(".json", "_rebalancer_state.json")
            data = {
                "last_rebalance_time": (
                    self._last_rebalance_time.isoformat()
                    if self._last_rebalance_time else None
                ),
                "rebalance_count": self._rebalance_count,
                "iteration_counter": self._iteration_counter,
            }
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save rebalancer state: {e}")

    # ── Public API ───────────────────────────────────────────────────────────

    def increment_iteration(self):
        """Call this each trading loop iteration."""
        self._iteration_counter += 1

    def should_check(self) -> bool:
        """Check if rebalance should be evaluated this iteration."""
        if not self.enabled:
            return False
        return self._iteration_counter % self._check_interval == 0

    def check_rebalance_needed(
        self,
        portfolio_manager,
        price_data: Dict[str, float],
        context: Optional[Dict[str, Any]] = None,
        skip_cooldown: bool = False,
    ) -> Tuple[bool, str]:
        """
        Quick check if rebalancing is needed.
        
        Args:
            portfolio_manager: PortfolioManager instance
            price_data: dict of {symbol: current_price}
            skip_cooldown: bypass cooldown gate (e.g. manual trigger)
        
        Returns:
            (should_rebalance, reason)
        """
        if not self.enabled:
            return False, "Rebalancing disabled"

        # Anti-whipsaw cooldown: block if last rebalance was too recent
        if (
            not skip_cooldown
            and self._cooldown_minutes > 0
            and self._last_rebalance_time is not None
        ):
            elapsed = (datetime.now() - self._last_rebalance_time).total_seconds() / 60
            remaining = self._cooldown_minutes - elapsed
            if remaining > 0:
                return False, (
                    f"Cooldown active: {remaining:.0f} min remaining "
                    f"(last rebalance {elapsed:.0f} min ago)"
                )

        context = dict(context or {})
        
        # Get target allocation from config
        target_allocation = context.get("target_allocation") or self.get_target_allocation(
            context.get("market_condition")
        )
        if not target_allocation:
            return False, "No target allocation configured"
        
        # Get DB path for candle count checks
        db_path = self.config.get("db_path", DEFAULT_DB_PATH)
        
        # Build allocation objects (with data readiness check)
        allocations, skipped = self._build_allocations(
            portfolio_manager, price_data, target_allocation, db_path
        )
        
        if not allocations:
            return False, "No assets to rebalance"
        
        # Build context
        total_value = portfolio_manager.total_portfolio_value()
        context = {
            **context,
            "price_data": price_data,
            "min_trade_value": self.min_trade_value,
            "total_value": total_value,
            "skipped_assets": skipped,
            "target_allocation": target_allocation,
        }
        
        # Check each strategy
        if self.strategy_type == RebalanceStrategy.THRESHOLD:
            return self.threshold_strategy.should_rebalance(allocations, context)
        
        elif self.strategy_type == RebalanceStrategy.CALENDAR:
            return self.calendar_strategy.should_rebalance(allocations, context)
        
        elif self.strategy_type == RebalanceStrategy.RISK:
            return self.risk_strategy.should_rebalance(allocations, context)
        
        else:  # COMBINED - use any that triggers
            # Check threshold first
            should_thresh, reason_thresh = (
                self.threshold_strategy.should_rebalance(allocations, context)
            )
            if should_thresh:
                return True, reason_thresh
            
            # Check calendar
            should_cal, reason_cal = (
                self.calendar_strategy.should_rebalance(allocations, context)
            )
            if should_cal:
                return True, reason_cal
            
            # Check risk
            should_risk, reason_risk = (
                self.risk_strategy.should_rebalance(allocations, context)
            )
            if should_risk:
                return True, reason_risk
            
            return False, "No rebalance trigger activated"

    def create_rebalance_plan(
        self,
        portfolio_manager,
        price_data: Dict[str, float],
        strategy: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        trigger_override: Optional[RebalanceTrigger] = None,
        reason_override: Optional[str] = None,
    ) -> RebalancePlan:
        """
        Create a complete rebalancing plan.
        
        Args:
            portfolio_manager: PortfolioManager instance
            price_data: dict of {symbol: current_price}
            strategy: override strategy type
        
        Returns:
            RebalancePlan with orders ready for execution
        """
        context = dict(context or {})
        target_allocation = context.get("target_allocation") or self.get_target_allocation(
            context.get("market_condition")
        )
        total_value = portfolio_manager.total_portfolio_value()
        
        # Get DB path for candle count checks
        db_path = self.config.get("db_path", DEFAULT_DB_PATH)
        
        # Build allocations (with data readiness check & held-coins filter)
        # ✓ NOTE: Only processes coins that are currently held in the portfolio
        allocations, skipped = self._build_allocations(
            portfolio_manager, price_data, target_allocation, db_path
        )
        
        # Log rebalancing scope
        held_coins = [a.symbol for a in allocations if a.data_ready]
        not_held_coins = [s["symbol"] for s in skipped if s["reason"].startswith("Not currently held")]
        insufficient_data_coins = [s["symbol"] for s in skipped if not s["reason"].startswith("Not currently held")]
        
        if held_coins:
            logger.info(f"📊 Rebalancing held coins: {held_coins}")
        if not_held_coins:
            logger.info(f"💤 Skipping coins not in portfolio: {not_held_coins}")
        if insufficient_data_coins:
            logger.info(f"⏰ Skipping coins without sufficient data: {insufficient_data_coins}")
        
        # Determine strategy to use (needed before skip check for aborted plan)
        strat_type = RebalanceStrategy(strategy) if strategy else self.strategy_type
        
        # ABORT ENTIRE PLAN if ANY asset is skipped (v1.2 fix)
        # Don't try to rebalance partial assets while waiting for data
        if skipped:
            skipped_names = [s["symbol"] for s in skipped]
            logger.warning(
                f"⏳ REBALANCE ABORTED: Waiting for data. "
                f"Skipped: {skipped_names}. "
                f"Will rebalance when all assets have sufficient data."
            )
            plan = RebalancePlan(
                trigger=trigger_override or RebalanceTrigger.MANUAL,
                strategy=strat_type,
                timestamp=datetime.now(),
                total_portfolio_value=round(total_value, 2),
                allocations=allocations,
                orders=[],  # NO ORDERS - aborted!
                total_trades=0,
                estimated_cost=0.0,
                max_drift_pct=0.0,
                reasons=[reason_override or f"ABORTED: Waiting for data on {skipped_names}"],
                skipped_assets=skipped,
            )
            return plan
        
        context = {
            **context,
            "price_data": price_data,
            "min_trade_value": self.min_trade_value,
            "total_value": total_value,
            "skipped_assets": skipped,
            "target_allocation": target_allocation,
        }
        
        # Generate orders based on strategy (all assets have sufficient data!)
        if strat_type == RebalanceStrategy.THRESHOLD:
            should_rebal, reason = (
                self.threshold_strategy.should_rebalance(allocations, context)
            )
            orders = self.threshold_strategy.generate_orders(allocations, total_value, context)
            trigger = RebalanceTrigger.THRESHOLD_BREACH
        
        elif strat_type == RebalanceStrategy.CALENDAR:
            should_rebal, reason = (
                self.calendar_strategy.should_rebalance(allocations, context)
            )
            orders = self.calendar_strategy.generate_orders(allocations, total_value, context)
            trigger = RebalanceTrigger.SCHEDULE_DUE
        
        elif strat_type == RebalanceStrategy.RISK:
            should_rebal, reason = (
                self.risk_strategy.should_rebalance(allocations, context)
            )
            orders = self.risk_strategy.generate_orders(allocations, total_value, context)
            trigger = RebalanceTrigger.VOLATILITY_ADJUSTMENT
        
        else:  # COMBINED
            # Check each strategy in priority order
            should_thresh, reason_thresh = (
                self.threshold_strategy.should_rebalance(allocations, context)
            )
            should_cal, reason_cal = (
                self.calendar_strategy.should_rebalance(allocations, context)
            )
            should_risk, reason_risk = (
                self.risk_strategy.should_rebalance(allocations, context)
            )
            
            # Determine primary trigger and reason
            if should_thresh:
                should_rebal = True
                reason = reason_thresh
                trigger = RebalanceTrigger.THRESHOLD_BREACH
            elif should_cal:
                should_rebal = True
                reason = reason_cal
                trigger = RebalanceTrigger.SCHEDULE_DUE
            elif should_risk:
                should_rebal = True
                reason = reason_risk
                trigger = RebalanceTrigger.VOLATILITY_ADJUSTMENT
            else:
                should_rebal = False
                reason = "No rebalance trigger activated"
                trigger = RebalanceTrigger.MANUAL
            
            if not should_rebal:
                orders = []
            else:
                # Collect orders from all strategies
                orders = []
                orders.extend(
                    self.threshold_strategy.generate_orders(allocations, total_value, context)
                )
                orders.extend(
                    self.calendar_strategy.generate_orders(allocations, total_value, context)
                )
                orders.extend(
                    self.risk_strategy.generate_orders(allocations, total_value, context)
                )
                # Deduplicate by symbol+side, keep largest quantity
                seen = {}
                for o in orders:
                    key = (o.symbol, o.side)
                    if key not in seen or abs(o.quantity) > abs(seen[key].quantity):
                        seen[key] = o
                orders = list(seen.values())
                # FIX: Execute SELL orders BEFORE BUY orders to free up capital
                orders.sort(key=lambda x: (0 if x.side == "sell" else 1, -x.priority))

        if trigger_override is not None:
            trigger = trigger_override
        
        # Calculate max drift
        max_drift = max(abs(a.drift_pct) for a in allocations) if allocations else 0.0
        
        # Estimate cost
        total_trade_value = sum(o.estimated_value for o in orders)
        estimated_cost = total_trade_value * (self.estimated_cost_pct / 100)
        
        plan = RebalancePlan(
            trigger=trigger,
            strategy=strat_type,
            timestamp=datetime.now(),
            total_portfolio_value=round(total_value, 2),
            allocations=allocations,
            orders=orders,
            total_trades=len(orders),
            estimated_cost=round(estimated_cost, 2),
            max_drift_pct=round(max_drift, 2),
            reasons=[item for item in [reason_override, reason] if item],
            skipped_assets=skipped,  # Data-aware: assets skipped due to insufficient data
        )
        
        # Update state
        if plan.should_execute(self.min_trade_value):
            self._last_rebalance_time = datetime.now()
            self._rebalance_count += 1
            self._save_state()
        
        return plan

    def _build_allocations(
        self,
        portfolio_manager,
        price_data: Dict[str, float],
        target_allocation: Dict[str, float],
        db_path: str = DEFAULT_DB_PATH,
    ) -> Tuple[List[AllocationTarget], List[Dict[str, Any]]]:
        """Build allocation targets from portfolio and config.
        
        Args:
            portfolio_manager: PortfolioManager instance
            price_data: dict of {symbol: current_price}
            target_allocation: dict of {symbol: target_pct}
            db_path: path to SQLite database
        
        Returns:
            List of AllocationTarget with data readiness populated
        
        NOTE: THB is treated as the quote/cash asset with price = 1.
        """
        allocations = []
        skipped = []
        total_value = portfolio_manager.total_portfolio_value()
        
        # Process configured allocations
        for symbol, target_pct in target_allocation.items():
            asset = str(symbol or "").upper()
            pos = portfolio_manager.get_position(asset)
            is_cash_asset = asset == "THB"
            current_price = 1.0 if is_cash_asset else price_data.get(asset, 0.0)

            if not is_cash_asset and not self.allow_new_assets and not has_ever_held(asset, db_path):
                logger.debug(
                    f"💤 Skipping {asset} for rebalance: "
                    f"Coin has never been held by this bot. Only historically held coins are rebalanced."
                )
                skipped.append({
                    "symbol": asset,
                    "candle_count": 0,
                    "reason": "Coin has never been held (history guard)",
                })
                continue

            if not is_cash_asset and current_price <= 0:
                logger.debug(
                    f"💤 Skipping {asset} for rebalance: "
                    f"Missing or invalid market price"
                )
                skipped.append({
                    "symbol": asset,
                    "candle_count": 0,
                    "reason": "Missing or invalid market price",
                })
                continue

            quantity = _coerce_float(getattr(pos, "quantity", 0.0), 0.0) if pos else 0.0
            
            current_value = current_price * quantity
            
            # Check data readiness
            if is_cash_asset:
                candle_count, data_ready, status_msg = MIN_CANDLES_FOR_TRADING, True, "cash"
            else:
                candle_count, data_ready, status_msg = get_candle_count(asset, db_path)
            
            alloc = AllocationTarget(
                symbol=asset,
                target_pct=target_pct,
                current_price=current_price,
                current_quantity=quantity,
                current_value=current_value,
                candle_count=candle_count,
                data_ready=data_ready,
                data_status=status_msg,
            )
            alloc.calculate(total_value)
            allocations.append(alloc)
            
            if not data_ready:
                logger.info(
                    f"⏳ Skipping {asset} for rebalance: "
                    f"Insufficient data ({candle_count}/{MIN_CANDLES_FOR_TRADING} candles). "
                    f"Status: {status_msg}"
                )
                skipped.append({
                    "symbol": asset,
                    "candle_count": candle_count,
                    "reason": f"Insufficient data ({candle_count}/{MIN_CANDLES_FOR_TRADING}). {status_msg}",
                })
        
        return allocations, skipped

    # ── Simulation / Backtest ────────────────────────────────────────────────

    def simulate_rebalance(
        self,
        initial_portfolio: Dict[str, float],  # symbol -> value
        target_allocation: Dict[str, float],  # symbol -> target %
        price_data: Dict[str, float],           # symbol -> price
        trade_cost_pct: float = 0.1,
    ) -> Dict[str, Any]:
        """
        Simulate a rebalance operation (no real trades).
        
        Returns simulation results with before/after comparison.
        """
        initial_value = sum(initial_portfolio.values())
        
        # Calculate current allocation
        current_alloc = {}
        for symbol, value in initial_portfolio.items():
            pct = (value / initial_value * 100) if initial_value > 0 else 0
            current_alloc[symbol] = pct
        
        # Calculate target values
        target_values = {}
        for symbol, target_pct in target_allocation.items():
            target_values[symbol] = (target_pct / 100) * initial_value
        
        # Calculate trades
        trades = []
        total_cost = 0.0
        
        for symbol, target_val in target_values.items():
            current_val = initial_portfolio.get(symbol, 0.0)
            diff = target_val - current_val
            
            if abs(diff) < self.min_trade_value:
                continue
            
            price = price_data.get(symbol, 0.0)
            if price <= 0:
                continue
            
            quantity = abs(diff) / price
            side = "buy" if diff > 0 else "sell"
            cost = abs(diff) * (trade_cost_pct / 100)
            total_cost += cost
            
            trades.append({
                "symbol": symbol,
                "side": side,
                "quantity": round(quantity, 8),
                "value": round(abs(diff), 2),
                "cost": round(cost, 2),
            })
        
        # Final portfolio
        final_portfolio = {}
        for symbol, current_val in initial_portfolio.items():
            target_val = target_values.get(symbol, 0.0)
            final_portfolio[symbol] = target_val
        
        # Final allocation
        final_alloc = {}
        final_value = initial_value - total_cost
        for symbol, value in final_portfolio.items():
            pct = (value / final_value * 100) if final_value > 0 else 0
            final_alloc[symbol] = pct
        
        return {
            "initial_value": round(initial_value, 2),
            "final_value": round(final_value, 2),
            "total_cost": round(total_cost, 2),
            "num_trades": len(trades),
            "trades": trades,
            "before": {
                "allocation": {k: round(v, 2) for k, v in current_alloc.items()},
                "portfolio": {k: round(v, 2) for k, v in initial_portfolio.items()},
            },
            "after": {
                "allocation": {k: round(v, 2) for k, v in final_alloc.items()},
                "portfolio": {k: round(v, 2) for k, v in final_portfolio.items()},
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# Quick Test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO)
    
    # Test configuration
    config = {
        "rebalance": {
            "enabled": True,
            "strategy": "combined",
            "threshold": {
                "threshold_pct": 10.0,
                "min_rebalance_pct": 1.0,
            },
            "calendar": {
                "frequency": "weekly",
                "day_of_week": 0,
                "hour_of_day": 0,
            },
            "risk": {
                "volatility_window": 20,
                "risk_adjustment_factor": 0.5,
                "min_allocation_pct": 5.0,
                "max_allocation_pct": 50.0,
                "volatility_threshold_pct": 30,
            },
            "target_allocation": {
                "BTC": 40.0,
                "ETH": 30.0,
                "BNB": 20.0,
                "SOL": 10.0,
            },
            "min_trade_value": 5.0,
            "estimated_cost_pct": 0.5,
        }
    }
    
    rebalancer = PortfolioRebalancer(config)
    
    # Mock portfolio manager
    class MockPortfolioManager:
        def __init__(self):
            self._balance = 500.0
            self._positions = {
                "BTC": {"quantity": 0.01, "price": 45000.0},
                "ETH": {"quantity": 0.1, "price": 3000.0},
                "BNB": {"quantity": 0.5, "price": 350.0},
                "SOL": {"quantity": 1.0, "price": 100.0},
            }
        
        def total_portfolio_value(self):
            positions_value = sum(p["quantity"] * p["price"] for p in self._positions.values())
            return self._balance + positions_value
        
        def get_position(self, symbol):
            if symbol in self._positions:
                class Pos:
                    quantity = self._positions[symbol]["quantity"]
                    current_price = self._positions[symbol]["price"]
                return Pos()
            return None
    
    pm = MockPortfolioManager()
    
    # Simulated prices
    price_data = {
        "BTC": 45000.0,
        "ETH": 3000.0,
        "BNB": 350.0,
        "SOL": 100.0,
    }
    
    print("\n=== Portfolio Rebalancer Test ===")
    print(f"Total portfolio value: {pm.total_portfolio_value():.2f} THB")
    
    should_rebal, reason = rebalancer.check_rebalance_needed(pm, price_data)
    print(f"Should rebalance: {should_rebal}")
    print(f"Reason: {reason}")
    
    if should_rebal:
        plan = rebalancer.create_rebalance_plan(pm, price_data)
        print(f"\nRebalance Plan:")
        print(f"  Trigger: {plan.trigger.value}")
        print(f"  Strategy: {plan.strategy.value}")
        print(f"  Total trades: {plan.total_trades}")
        print(f"  Max drift: {plan.max_drift_pct}%")
        print(f"  Estimated cost: {plan.estimated_cost} THB")
        
        for order in plan.orders:
            print(f"  - {order.side.upper()} {order.symbol}: {order.quantity:.8f} "
                  f"(~{order.estimated_value:.2f} THB) | {order.reason}")
    
    # Sim