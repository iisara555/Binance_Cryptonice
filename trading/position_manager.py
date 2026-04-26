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
    from api_client import BinanceThClient
    from trade_executor import TradeExecutor

logger = logging.getLogger(__name__)


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return datetime.now()
    return datetime.now()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

class PositionStatus(Enum):
    """Position lifecycle status"""
    PENDING = "pending"
    OPEN = "open"
    PARTIAL_FILL = "partial_fill"
    CLOSED = "closed"
    CANCELLED = "cancelled"

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
    
    @property
    def is_open(self) -> bool:
        return self.status in (PositionStatus.OPEN, PositionStatus.PARTIAL_FILL)

class PositionManager:
    """
    Manages open positions, monitors for SL/TP triggers,
    handles trailing stops, and position lifecycle.
    """
    
    def __init__(
        self,
        api_client: BinanceThClient,
        trade_executor: TradeExecutor,
        config: Dict[str, Any]
    ):
        self.api_client = api_client
        self.executor = trade_executor
        self.config = config
        
        # Position storage
        self._positions: Dict[str, Position] = {}
        self._lock = threading.RLock()
        
        logger.info("PositionManager initialized")

    @staticmethod
    def _normalize_side(side: Any) -> str:
        if isinstance(side, Enum):
            return str(side.value).lower()
        return str(side or "buy").lower()

    @staticmethod
    def _position_from_row(row: Dict[str, Any]) -> Optional[Position]:
        order_id = str(row.get('order_id') or row.get('id') or '').strip()
        symbol = str(row.get('symbol') or '').strip().upper()
        if not order_id or not symbol:
            return None

        amount = float(row.get('amount') or 0.0)
        raw_remaining = row.get('remaining_amount')
        remaining_amount = float(raw_remaining or 0.0)
        if raw_remaining is None and amount > 0.0:
            remaining_amount = amount

        is_partial_fill = bool(row.get('is_partial_fill'))
        if remaining_amount <= 0.0:
            status = PositionStatus.CLOSED
        else:
            status = PositionStatus.PARTIAL_FILL if is_partial_fill else PositionStatus.OPEN
        return Position(
            order_id=order_id,
            symbol=symbol,
            side=PositionManager._normalize_side(row.get('side')),
            entry_price=float(row.get('entry_price') or 0.0),
            amount=amount,
            remaining_amount=remaining_amount,
            stop_loss=row.get('stop_loss'),
            take_profit=row.get('take_profit'),
            status=status,
            created_at=_coerce_datetime(row.get('timestamp') or row.get('opened_at') or row.get('updated_at')),
            total_entry_cost=float(row.get('total_entry_cost') or 0.0),
        )

    def _get_position_store_rows(self) -> List[Dict[str, Any]]:
        db = getattr(self.executor, '_db', None)
        if db is None:
            logger.warning('PositionManager sync skipped: executor has no database handle')
            return []
        return list(db.load_all_positions() or [])

    def _get_exchange_balances(self) -> tuple[Dict[str, Any], bool]:
        try:
            try:
                balances = self.api_client.get_balances(force_refresh=True, allow_stale=False)
            except TypeError:
                balances = self.api_client.get_balances()
        except Exception as exc:
            logger.warning('PositionManager reconcile could not fetch exchange balances: %s', exc)
            return {}, False
        return balances if isinstance(balances, dict) else {}, True

    def _extract_total_balance(self, balances: Dict[str, Any], asset: str) -> float:
        payload = balances.get(asset.upper(), {}) if isinstance(balances, dict) else {}
        if isinstance(payload, dict):
            available = _safe_float(payload.get('available'), 0.0)
            reserved = _safe_float(payload.get('reserved'), 0.0)
            total = payload.get('total')
            if total is not None:
                return _safe_float(total, available + reserved)
            return available + reserved
        return _safe_float(payload, 0.0)

    def _get_exchange_open_order_rows(self, symbols: List[str]) -> tuple[List[Dict[str, Any]], set[str]]:
        rows: List[Dict[str, Any]] = []
        failed_symbols: set[str] = set()
        if not hasattr(self.api_client, 'get_open_orders'):
            return rows, failed_symbols

        for symbol in symbols:
            try:
                open_orders = self.api_client.get_open_orders(symbol)
            except Exception as exc:
                logger.warning('PositionManager reconcile could not fetch open orders for %s: %s', symbol, exc)
                failed_symbols.add(symbol)
                continue

            for row in list(open_orders or []):
                if not isinstance(row, dict):
                    continue
                normalized = dict(row)
                normalized['order_id'] = str(row.get('id') or row.get('order_id') or '').strip()
                normalized['symbol'] = str(row.get('_checked_symbol') or row.get('symbol') or symbol).strip().upper()
                normalized['side'] = self._normalize_side(row.get('side'))
                normalized['entry_price'] = _safe_float(row.get('rat') or row.get('rate') or row.get('entry_price'), 0.0)
                amount = _safe_float(row.get('amt') or row.get('amount'), 0.0)
                remaining = _safe_float(row.get('rec') or row.get('remaining_amount'), amount)
                normalized['amount'] = amount
                normalized['remaining_amount'] = remaining if remaining > 0.0 else amount
                rows.append(normalized)

        return rows, failed_symbols
    
    def add_position(self, position: Position) -> None:
        """Add new position to manager"""
        with self._lock:
            self._positions[position.order_id] = position
            logger.info(f"Added position {position.order_id} | {position.symbol} {position.side} @ {position.entry_price:.2f}")
    
    def remove_position(self, order_id: str) -> Optional[Position]:
        """Remove position from manager"""
        with self._lock:
            return self._positions.pop(order_id, None)
    
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
    
    def sync_from_database(self) -> None:
        """Sync positions from database"""
        rows = self._get_position_store_rows()
        loaded: Dict[str, Position] = {}
        for row in rows:
            position = self._position_from_row(row)
            if position is None or not position.is_open:
                continue
            loaded[position.order_id] = position
        with self._lock:
            self._positions = loaded
        logger.info('PositionManager synced %d positions from database', len(loaded))
    
    def reconcile_with_exchange(self) -> None:
        """Reconcile positions with exchange state"""
        with self._lock:
            current_positions = list(self._positions.values())

        symbols = sorted({str(position.symbol or '').upper() for position in current_positions if position.symbol})
        if not symbols:
            logger.info('PositionManager reconcile skipped: no tracked symbols')
            return

        exchange_rows, failed_order_symbols = self._get_exchange_open_order_rows(symbols)
        balances, balances_ok = self._get_exchange_balances()

        exchange_positions: Dict[str, Position] = {}
        for row in exchange_rows:
            position = self._position_from_row(row)
            if position is None or not position.is_open:
                continue
            exchange_positions[position.order_id] = position

        reconciled: Dict[str, Position] = dict(exchange_positions)
        for position in current_positions:
            if position.order_id in exchange_positions:
                continue

            symbol = str(position.symbol or '').upper()
            if symbol in failed_order_symbols and not balances_ok:
                reconciled[position.order_id] = position
                continue

            base_asset = symbol.split('_', 1)[1].upper() if '_' in symbol else symbol
            balance_total = self._extract_total_balance(balances, base_asset)
            tracked_amount = max(position.remaining_amount, position.amount, 0.0)
            dust_threshold = min(max(tracked_amount * 0.01, 1e-8), 1e-6)

            if balance_total > dust_threshold:
                reconciled[position.order_id] = position
            elif symbol in failed_order_symbols:
                reconciled[position.order_id] = position

        with self._lock:
            removed = sorted(set(self._positions) - set(reconciled))
            added = sorted(set(reconciled) - set(self._positions))
            self._positions = reconciled

        logger.info(
            'PositionManager reconciled against exchange | added=%d removed=%d symbols=%d',
            len(added),
            len(removed),
            len(symbols),
        )