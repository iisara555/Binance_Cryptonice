"""
Position Manager
===============
Manages open positions, stop loss, take profit, and trailing stops.
Extracted from monolithic trading_bot.py.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Dict, Any, List, Optional
from dataclasses import dataclass, field
from enum import Enum

if TYPE_CHECKING:
    from api_client import BitkubClient
    from trade_executor import TradeExecutor

logger = logging.getLogger(__name__)

class PositionStatus(Enum):
    """Position lifecycle status"""
    PENDING = "pending"
    OPEN = "open"
    PARTIAL_FILL = "partial_fill"
    CLOSING = "closing"
    CLOSED = "closed"
    CANCELLED = "cancelled"

class ExitTrigger(Enum):
    """Reason for position exit"""
    MANUAL = "manual"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    EXPIRED = "expired"
    SIGNAL = "signal"

@dataclass
class Position:
    """Represents an open trading position"""
    order_id: str
    symbol: str
    side: str
    entry_price: float
    amount: float
    remaining_amount: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    status: PositionStatus = PositionStatus.OPEN
    created_at: datetime = field(default_factory=datetime.now)
    total_entry_cost: float = 0.0
    trailing_stop_activated: bool = False
    trailing_stop_distance: float = 0.0
    
    @property
    def filled_amount(self) -> float:
        return self.amount - self.remaining_amount
    
    @property
    def is_open(self) -> bool:
        return self.status in (PositionStatus.OPEN, PositionStatus.PARTIAL_FILL)
    
    def update_price_levels(self, current_price: float, atr_value: float) -> None:
        """Update stop loss and take profit based on current price"""
        if self.trailing_stop_activated and self.side == 'buy':
            new_sl = current_price - self.trailing_stop_distance
            if new_sl > self.stop_loss:
                logger.info(f"Trailing stop ratcheted: {self.stop_loss:.2f} → {new_sl:.2f}")
                self.stop_loss = new_sl

class PositionManager:
    """
    Manages open positions, monitors for SL/TP triggers,
    handles trailing stops, and position lifecycle.
    """
    
    def __init__(
        self,
        api_client: BitkubClient,
        trade_executor: TradeExecutor,
        config: Dict[str, Any]
    ):
        self.api_client = api_client
        self.executor = trade_executor
        self.config = config
        
        # Position storage
        self._positions: Dict[str, Position] = {}
        self._lock = threading.RLock()
        
        # Configuration
        self.enable_trailing_stop = config.get('trailing_stop', {}).get('enabled', True)
        self.trailing_stop_activation_pct = config.get('trailing_stop', {}).get('activation_pct', 0.015)
        self.trailing_stop_distance_pct = config.get('trailing_stop', {}).get('distance_pct', 0.01)
        
        # Price cache
        self._price_cache: Dict[str, float] = {}
        
        logger.info(
            f"PositionManager initialized | "
            f"Trailing stop: {'enabled' if self.enable_trailing_stop else 'disabled'}"
        )
    
    def add_position(self, position: Position) -> None:
        """Add new position to manager"""
        with self._lock:
            self._positions[position.order_id] = position
            logger.info(f"Added position {position.order_id} | {position.symbol} {position.side} @ {position.entry_price:.2f}")
    
    def remove_position(self, order_id: str) -> Optional[Position]:
        """Remove position from manager"""
        with self._lock:
            return self._positions.pop(order_id, None)
    
    def get_position(self, order_id: str) -> Optional[Position]:
        """Get position by order ID"""
        return self._positions.get(order_id)
    
    def get_open_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """Get all open positions, optionally filtered by symbol"""
        with self._lock:
            positions = [
                pos for pos in self._positions.values()
                if pos.is_open
            ]
            
            if symbol:
                positions = [p for p in positions if p.symbol == symbol]
            
            return positions
    
    def update_price(self, symbol: str, price: float) -> None:
        """Update latest price for symbol"""
        self._price_cache[symbol] = price
        self._check_positions_for_symbol(symbol, price)
    
    def _check_positions_for_symbol(self, symbol: str, current_price: float) -> None:
        """Check all positions for a symbol against current price"""
        positions = self.get_open_positions(symbol)
        
        for position in positions:
            self._check_position_exit(position, current_price)
            self._update_trailing_stop(position, current_price)
    
    def _check_position_exit(self, position: Position, current_price: float) -> None:
        """Check if position should be closed via SL/TP"""
        if position.side == 'buy':
            if position.stop_loss is not None and current_price <= position.stop_loss:
                self._trigger_exit(position, ExitTrigger.STOP_LOSS, current_price)
            elif position.take_profit is not None and current_price >= position.take_profit:
                self._trigger_exit(position, ExitTrigger.TAKE_PROFIT, current_price)
        else:  # sell position
            if position.stop_loss is not None and current_price >= position.stop_loss:
                self._trigger_exit(position, ExitTrigger.STOP_LOSS, current_price)
            elif position.take_profit is not None and current_price <= position.take_profit:
                self._trigger_exit(position, ExitTrigger.TAKE_PROFIT, current_price)
    
    def _update_trailing_stop(self, position: Position, current_price: float) -> None:
        """Update trailing stop if activated"""
        if not self.enable_trailing_stop:
            return
        
        if not position.trailing_stop_activated and position.side == 'buy':
            if position.entry_price <= 0:
                return
            profit_pct = (current_price - position.entry_price) / position.entry_price
            if profit_pct >= self.trailing_stop_activation_pct:
                position.trailing_stop_activated = True
                position.trailing_stop_distance = current_price * self.trailing_stop_distance_pct
                position.stop_loss = current_price - position.trailing_stop_distance
                logger.info(f"Trailing stop activated for {position.order_id} | SL={position.stop_loss:.2f}")
        
        if position.trailing_stop_activated:
            position.update_price_levels(current_price, 0.0)
    
    def _trigger_exit(self, position: Position, trigger: ExitTrigger, current_price: float) -> None:
        """Trigger position exit"""
        logger.info(
            f"{trigger.value.upper()} triggered for position {position.order_id} | "
            f"Entry: {position.entry_price:.2f} Current: {current_price:.2f}"
        )
        
        position.status = PositionStatus.CLOSING
        
        # Execute exit order
        try:
            exit_side = 'sell' if position.side == 'buy' else 'buy'
            result = self.executor.execute_exit(
                position_id=position.order_id,
                order_id=position.order_id,
                side=exit_side,
                amount=position.remaining_amount,
                price=current_price
            )
            
            if result.success:
                position.status = PositionStatus.CLOSED
                self.remove_position(position.order_id)
                logger.info(f"Position {position.order_id} closed successfully via {trigger.value}")
            else:
                position.status = PositionStatus.OPEN
                logger.error(f"Failed to close position {position.order_id}: {result.message}")
                
        except Exception as e:
            position.status = PositionStatus.OPEN
            logger.error(f"Error closing position {position.order_id}: {e}", exc_info=True)
    
    def check_all_positions(self) -> None:
        """Check all open positions against current market prices"""
        symbols = {pos.symbol for pos in self.get_open_positions()}
        
        for symbol in symbols:
            try:
                ticker = self.api_client.get_ticker(symbol)
                if ticker and 'last' in ticker:
                    self.update_price(symbol, float(ticker['last']))
            except Exception as e:
                logger.error(f"Error checking positions for {symbol}: {e}")
    
    def get_position_summary(self) -> Dict[str, Any]:
        """Get summary of all positions"""
        with self._lock:
            open_positions = self.get_open_positions()
            
            total_exposure = sum(
                pos.entry_price * pos.remaining_amount
                for pos in open_positions
            )
            
            return {
                'open_positions_count': len(open_positions),
                'total_exposure_thb': total_exposure,
                'positions': [
                    {
                        'order_id': pos.order_id,
                        'symbol': pos.symbol,
                        'side': pos.side,
                        'entry_price': pos.entry_price,
                        'amount': pos.amount,
                        'remaining_amount': pos.remaining_amount,
                        'stop_loss': pos.stop_loss,
                        'take_profit': pos.take_profit,
                        'trailing_activated': pos.trailing_stop_activated,
                        'created_at': pos.created_at.isoformat()
                    }
                    for pos in open_positions
                ]
            }
    
    def sync_from_database(self) -> None:
        """Sync positions from database"""
        # Implementation will sync open positions from SQLite
        pass
    
    def reconcile_with_exchange(self) -> None:
        """Reconcile positions with exchange state"""
        # Implementation will verify positions against exchange open orders
        pass