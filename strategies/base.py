"""
Base class for trading strategies.

Provides SL/TP calculation helpers aligned with risk_management conventions:
- SL/TP distances use ATR-based calculations (not fixed percentages)
- Default risk:reward ratio uses config.MIN_RISK_REWARD_RATIO (1.3)
- Strategies should call calculate_sl_tp_from_atr() for consistent SL/TP values
"""
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple
import pandas as pd


@dataclass
class Signal:
    """Trading signal from a strategy."""
    action: str  # BUY, SELL, HOLD
    confidence: float  # 0.0 to 1.0
    metadata: Dict[str, Any] = None


@dataclass
class StrategyConfig:
    """Configuration for a strategy."""
    name: str
    enabled: bool = True


class StrategyBase:
    """Base class for all trading strategies."""
    
    def __init__(self, config: StrategyConfig = None):
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
        
        sl_distance = atr_value * atr_multiplier
        tp_distance = sl_distance * risk_reward_ratio
        
        if direction == "long":
            sl = round(entry_price - sl_distance, 6)
            tp = round(entry_price + tp_distance, 6)
        else:  # short
            sl = round(entry_price + sl_distance, 6)
            tp = round(entry_price - tp_distance, 6)
        
        return sl, tp
    
    def generate_signal(self, data: pd.DataFrame, symbol: str = "Unknown") -> Any:
        """
        Generate a complete TradingSignal object for the Orchestrator.
        Calls the analyze() method internally.
        
        SL/TP values are calculated using ATR-based method for consistency
        with risk_management. If ATR is unavailable, returns None SL/TP.
        """
        # We import locally to avoid circular dependencies
        from strategy_base import TradingSignal, SignalType
        from config import MIN_RISK_REWARD_RATIO
        
        signal_action = self.analyze(data)
        
        # Map simple string action to SignalType enum
        sig_type = SignalType.HOLD
        if signal_action.action.upper() == 'BUY':
            sig_type = SignalType.BUY
        elif signal_action.action.upper() == 'SELL':
            sig_type = SignalType.SELL
            
        current_price = data['close'].iloc[-1] if not data.empty and 'close' in data else 0.0
        
        # Calculate ATR-based SL/TP (aligned with risk_management)
        stop_loss = 0.0
        take_profit = 0.0
        rr_ratio = max(MIN_RISK_REWARD_RATIO, 2.0)  # Consistent minimum R:R
        
        if sig_type in (SignalType.BUY, SignalType.SELL) and current_price > 0:
            # Calculate ATR from OHLCV data if available
            if all(k in data.columns for k in ['high', 'low', 'close']) and len(data) >= 15:
                from risk_management import calculate_atr
                atr_values = calculate_atr(
                    data['high'].tolist(),
                    data['low'].tolist(),
                    data['close'].tolist(),
                    period=14
                )
                atr = atr_values[-1] if atr_values else 0.0
                
                if atr > 0:
                    direction = "long" if sig_type == SignalType.BUY else "short"
                    stop_loss, take_profit = self.calculate_sl_tp_from_atr(
                        entry_price=current_price,
                        atr_value=atr,
                        direction=direction,
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
            metadata=signal_action.metadata or {}
        )
    
    def validate_signal(self, signal: Any, data: pd.DataFrame) -> bool:
        """
        Validate the generated signal against data or indicators.
        Returns True by default.
        """
        if not signal or signal.signal_type.value == "HOLD":
            return False
        return True

    def get_indicators(self, data: pd.DataFrame) -> Dict[str, Any]:
        """Calculate and return strategy-specific indicators."""
        return {}
