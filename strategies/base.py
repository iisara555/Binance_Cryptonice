"""
Base class for trading strategies.

Provides SL/TP calculation helpers aligned with risk_management conventions:
- SL/TP distances use ATR-based calculations (not fixed percentages)
- Default risk:reward ratio uses config.MIN_RISK_REWARD_RATIO (1.3)
- Strategies should call calculate_sl_tp_from_atr() for consistent SL/TP values
"""

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional, Tuple

import pandas as pd


@dataclass
class Signal:
    """Trading signal from a strategy."""

    action: str  # BUY, SELL, HOLD
    confidence: float  # 0.0 to 1.0
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class StrategyConfig:
    """Configuration for a strategy."""

    name: str
    enabled: bool = True


class SignalType(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class TradingSignal:
    strategy_name: str
    symbol: str
    signal_type: SignalType
    confidence: float
    price: float
    timestamp: Optional[datetime] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_reward_ratio: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None


class StrategyBase:
    """Base class for all trading strategies."""

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig(name=self.__class__.__name__)
        self.name = self.config.name

    def analyze(self, data: pd.DataFrame) -> Signal:
        """
        Analyze data and return a trading signal.

        Args:
            data: DataFrame with OHLCV data

        Returns:
            Signal with action and confidence
        """
        raise NotImplementedError("Strategy must implement analyze()")

    @staticmethod
    def calculate_sl_tp_from_atr(
        entry_price: float,
        atr_value: float,
        direction: str = "long",
        risk_reward_ratio: float = 2.0,
        atr_multiplier: float = 1.5,
    ) -> Tuple[float, float]:
        """
        Calculate SL and TP prices using ATR-based calculation.

        This method is aligned with risk_management.RiskManager.calc_sl_tp_from_atr().

        Args:
            entry_price: Position entry price
            atr_value: Current ATR value (from 14-period Wilder's smoothing)
            direction: 'long' or 'short'
            risk_reward_ratio: TP distance = SL distance * this ratio
            atr_multiplier: SL distance = ATR * this (default 1.5)

        Returns:
            (stop_loss_price, take_profit_price) tuple.
            Returns (0.0, 0.0) if ATR is unavailable (<= 0).
        """
        if not atr_value or atr_value <= 0 or entry_price <= 0:
            return 0.0, 0.0

        # Spot bot: only long-side protective levels are supported.
        if str(direction or "long").lower() != "long":
            return 0.0, 0.0

        sl_distance = atr_value * atr_multiplier
        tp_distance = sl_distance * risk_reward_ratio

        sl = round(entry_price - sl_distance, 6)
        tp = round(entry_price + tp_distance, 6)

        return sl, tp

    def generate_signal(self, data: pd.DataFrame, symbol: str = "Unknown") -> Any:
        """
        Generate a complete TradingSignal object for the Orchestrator.
        Calls the analyze() method internally.

        SL/TP values are calculated using ATR-based method for consistency
        with risk_management. If ATR is unavailable, returns None SL/TP.
        """
        # We import locally to avoid circular dependencies
        from config import MIN_RISK_REWARD_RATIO

        signal_action = self.analyze(data)

        # Map simple string action to SignalType enum
        sig_type = SignalType.HOLD
        if signal_action.action.upper() == "BUY":
            sig_type = SignalType.BUY
        elif signal_action.action.upper() == "SELL":
            sig_type = SignalType.SELL

        current_price = data["close"].iloc[-1] if not data.empty and "close" in data else 0.0

        # Calculate ATR-based SL/TP (aligned with risk_management)
        stop_loss = 0.0
        take_profit = 0.0
        rr_ratio = max(MIN_RISK_REWARD_RATIO, 2.0)  # Consistent minimum R:R

        if sig_type is SignalType.BUY and current_price > 0:
            # Calculate ATR from OHLCV data if available
            if all(k in data.columns for k in ["high", "low", "close"]) and len(data) >= 15:
                from risk_management import calculate_atr

                atr_values = calculate_atr(
                    data["high"].tolist(), data["low"].tolist(), data["close"].tolist(), period=14
                )
                atr = atr_values[-1] if atr_values else 0.0

                if atr > 0:
                    stop_loss, take_profit = self.calculate_sl_tp_from_atr(
                        entry_price=current_price,
                        atr_value=atr,
                        direction="long",
                        risk_reward_ratio=rr_ratio,
                    )

        # Calculate actual R:R ratio
        actual_rr = 0.0
        if stop_loss > 0 and take_profit > 0 and current_price > 0:
            risk = abs(current_price - stop_loss)
            reward = abs(take_profit - current_price)
            actual_rr = reward / risk if risk > 0 else rr_ratio

        return TradingSignal(
            strategy_name=self.name,
            symbol=symbol,
            signal_type=sig_type,
            confidence=signal_action.confidence,
            price=current_price,
            risk_reward_ratio=actual_rr if actual_rr > 0 else rr_ratio,
            stop_loss=stop_loss if stop_loss > 0 else None,
            take_profit=take_profit if take_profit > 0 else None,
            metadata=signal_action.metadata or {},
        )

    def validate_signal(self, signal: Any, data: pd.DataFrame) -> bool:
        """
        Validate generated signals with basic structural and risk sanity checks.
        """
        if not signal or data is None or data.empty:
            return False

        signal_type = getattr(signal, "signal_type", None)
        confidence = getattr(signal, "confidence", None)
        price = getattr(signal, "price", None)

        if signal_type is None or confidence is None or price is None:
            return False
        if signal_type == SignalType.HOLD:
            return False

        try:
            conf_val = float(confidence)
            price_val = float(price)
        except (TypeError, ValueError):
            return False

        if not math.isfinite(conf_val) or not math.isfinite(price_val):
            return False
        if conf_val <= 0.0 or conf_val > 1.0 or price_val <= 0.0:
            return False

        if "close" in data.columns and len(data["close"]) > 0:
            try:
                last_close = float(data["close"].iloc[-1])
            except (TypeError, ValueError):
                return False
            if last_close <= 0:
                return False

        # Spot BUY sanity: SL must be below entry and TP above entry when provided.
        if signal_type == SignalType.BUY:
            sl = getattr(signal, "stop_loss", None)
            tp = getattr(signal, "take_profit", None)
            if sl is not None and tp is not None:
                try:
                    sl_val = float(sl)
                    tp_val = float(tp)
                except (TypeError, ValueError):
                    return False
                if not (sl_val < price_val < tp_val):
                    return False

            rr = getattr(signal, "risk_reward_ratio", None)
            if rr is not None:
                try:
                    rr_val = float(rr)
                except (TypeError, ValueError):
                    return False
                if rr_val <= 0:
                    return False

        return True

    def get_indicators(self, data: pd.DataFrame) -> Dict[str, Any]:
        """Calculate and return strategy-specific indicators."""
        return {}
