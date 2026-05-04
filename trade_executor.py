"""
Trade Executor Module
=====================
Handles order execution with retry logic, timeout handling,
partial fill handling, and error recovery.
"""

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from execution import (
    BINANCE_TH_FEE_PCT,
    BINANCE_TH_ROUND_TRIP_FEE,
    quantize_decimal,
    to_decimal,
)
from financial_precision import precise_add, precise_divide, precise_multiply
from risk_management import DEFAULT_MIN_ORDER_QUOTE
from state_management import normalize_buy_quantity

logger = logging.getLogger(__name__)


def _coerce_utc(dt: datetime) -> datetime:
    """Normalize datetimes for arithmetic (naive treated as UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Late-bound WS import (avoids circular dep at module load)
_ws_mod = None
_ws_mod_lock = threading.Lock()


def _ws_ticker(symbol: str):
    """Get latest WS ticker, lazy-loading the module on first call."""
    global _ws_mod
    if _ws_mod is None:
        with _ws_mod_lock:
            if _ws_mod is None:  # double-checked locking
                try:
                    import binance_websocket as _bws

                    _ws_mod = _bws
                except ImportError:
                    try:
                        import bitkub_websocket as _bws

                        _ws_mod = _bws
                    except ImportError:
                        return None
    return _ws_mod.get_latest_ticker(symbol)


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    ERROR = "error"


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class PartialFillInfo:
    """Tracks partial fill state for an order"""

    order_id: str
    symbol: str
    side: OrderSide
    original_amount: float
    filled_amount: float = 0.0
    avg_fill_price: float = 0.0
    last_update: datetime = field(default_factory=datetime.now)
    is_complete: bool = False


class PartialFillTracker:
    """Tracks partial fills for orders."""

    def __init__(self, max_wait_seconds: float = 60.0):
        self.max_wait = timedelta(seconds=max_wait_seconds)
        self._fills: Dict[str, PartialFillInfo] = {}
        self._lock = threading.Lock()

    def start_tracking(self, order_id: str, info: PartialFillInfo):
        with self._lock:
            self._fills[order_id] = info
            logger.info(
                "[PartialFill] Tracking started: %s | filled=%.6f/%.6f",
                order_id,
                info.filled_amount,
                info.original_amount,
            )

    def update_fill(self, order_id: str, filled: float, price: float) -> PartialFillInfo:
        """Update fill amount for a tracked order."""
        with self._lock:
            if order_id not in self._fills:
                logger.warning("[PartialFill] Unknown order_id: %s", order_id)
                raise KeyError(order_id)
            info = self._fills[order_id]
            prev = info.filled_amount
            total_value = precise_add(
                precise_multiply(info.avg_fill_price, prev),
                precise_multiply(price, filled),
            )
            info.filled_amount = precise_add(info.filled_amount, filled)
            info.avg_fill_price = precise_divide(total_value, info.filled_amount) if info.filled_amount else 0
            info.last_update = datetime.now()
            if info.filled_amount >= precise_multiply(info.original_amount, 0.9999):
                info.is_complete = True
            logger.info(
                "[PartialFill] %s: %.6f -> %.6f (@ %.4f)",
                order_id,
                prev,
                info.filled_amount,
                info.avg_fill_price,
            )
            return info

    def get_actual_position(self, order_id: str) -> Optional[Dict[str, float]]:
        """Return actual position size and avg price after partial fill."""
        with self._lock:
            if order_id not in self._fills:
                return None
            info = self._fills[order_id]
            return {
                "filled_amount": info.filled_amount,
                "avg_price": info.avg_fill_price,
                "remaining": info.original_amount - info.filled_amount,
                "is_complete": info.is_complete,
                "elapsed_seconds": (datetime.now() - info.last_update).total_seconds(),
            }

    def is_expired(self, order_id: str) -> bool:
        """Return True if partial fill wait period has expired."""
        with self._lock:
            if order_id not in self._fills:
                return True
            return (datetime.now() - self._fills[order_id].last_update) > self.max_wait

    def recalculate_sl_tp(
        self,
        order_id: str,
        original_sl: Optional[float],
        original_tp: Optional[float],
        entry_price: float,
    ) -> Dict[str, float]:
        """Recalculate SL/TP for the actual filled amount."""
        pos = self.get_actual_position(order_id)
        if not pos:
            return {}
        filled = pos["filled_amount"]
        sl_dist = abs(entry_price - original_sl) if original_sl else entry_price * 0.02
        tp_dist = abs(original_tp - entry_price) if original_tp else entry_price * 0.04
        new_sl = entry_price - sl_dist if entry_price > 0 else entry_price + sl_dist
        new_tp = entry_price + tp_dist if entry_price > 0 else entry_price - tp_dist
        return {
            "stop_loss": new_sl,
            "take_profit": new_tp,
            "position_value": filled * entry_price,
            "filled_amount": filled,
            "avg_price": pos["avg_price"],
        }

    def stop_tracking(self, order_id: str):
        with self._lock:
            self._fills.pop(order_id, None)

    def get_all_pending(self) -> List[PartialFillInfo]:
        with self._lock:
            return [v for v in self._fills.values() if not v.is_complete]


def _is_bootstrap_order_id(order_id: Optional[str]) -> bool:
    return str(order_id or "").startswith("bootstrap_")


@dataclass
class OrderRequest:
    """Order request object"""

    symbol: str
    side: OrderSide
    amount: float
    price: Optional[float] = None
    order_type: str = "limit"
    client_order_id: Optional[str] = None


@dataclass
class OrderResult:
    """Result of an order execution attempt"""

    success: bool
    status: OrderStatus
    order_id: Optional[str] = None
    filled_amount: float = 0.0
    filled_price: Optional[float] = None
    ordered_amount: float = 0.0
    remaining_amount: float = 0.0
    message: str = ""
    attempts: int = 0
    execution_time_ms: float = 0.0
    error_code: Optional[int] = None
    partial_fill_info: Optional[PartialFillInfo] = None


@dataclass
class ExecutionPlan:
    """Execution plan for a trade signal"""

    symbol: str
    side: OrderSide
    amount: float
    entry_price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_reward_ratio: float = 0.0
    confidence: float = 0.0
    order_type: str = "limit"
    strategy_votes: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    signal_timestamp: Optional[datetime] = None
    signal_id: Optional[str] = None
    max_price_drift_pct: float = 1.5
    close_position: bool = False


class TradeExecutor:
    """Manages order execution with retry logic and timeout handling."""

    # Decimal places per asset — shared from portfolio_manager for consistency.
    # For dynamic decimals from exchange filters, use get_asset_decimals_with_fallback()
    ASSET_DECIMALS: Dict[str, int] = {
        "BTC": 8,
        "ETH": 8,
        "XAUT": 8,
        "BNB": 8,
        "SOL": 8,
        "XRP": 0,
        "ADA": 0,
        "DOGE": 0,
        "THB": 2,
        "SHIB": 0,
        "USDT": 2,
    }

    def get_asset_decimals_with_fallback(self, symbol: str) -> int:
        """Get asset decimal places with dynamic fallback to exchange filters.

        Priority:
        1. Exchange filter LOT_SIZE stepSize (most accurate)
        2. API client cached filters (if available)
        3. Hardcoded ASSET_DECIMALS (fallback)

        Args:
            symbol: Asset symbol (e.g., 'BTC', 'USDT')

        Returns:
            Number of decimal places for the asset
        """
        upper_sym = symbol.upper()

        # Try API client cached filters first
        if hasattr(self, "api_client") and self.api_client:
            try:
                filters = self.api_client._get_symbol_filters(upper_sym)
                step = filters.get("stepSize")
                if step and float(step) > 0:
                    # Derive decimals from stepSize
                    step_str = str(step)
                    if "." in step_str:
                        decimals = len(step_str.split(".")[1].rstrip("0"))
                        return max(0, min(decimals, 16))
            except Exception:
                pass

        # Fall back to hardcoded values
        return self.ASSET_DECIMALS.get(upper_sym, 8)

    def _format_balance_for_display(self, symbol: str, amount: Any) -> str:
        """Format balance with 8 decimal places for crypto, 2 for THB.
        Keep all decimals to avoid losing value opportunities.

        Args:
            symbol: Asset symbol (e.g., 'BTC', 'THB')
            amount: The balance amount to format

        Returns:
            Formatted string with full decimal places
        """
        amount_dec = to_decimal(amount)
        if symbol.upper() in ("THB", "USDT"):
            return f"{amount_dec:.2f}"
        return f"{amount_dec:.8f}".rstrip("0").rstrip(".")

    def _get_decimal_balance(self, balances: Dict[str, Dict[str, Any]], asset: str) -> Decimal:
        upper_asset = asset.upper()
        lower_asset = asset.lower()
        asset_data = balances.get(upper_asset) or balances.get(lower_asset) or {}
        if not isinstance(asset_data, dict):
            return to_decimal(asset_data)
        return to_decimal(asset_data.get("available", 0))

    def _get_balance(self, balances: Dict[str, Dict[str, float]], asset: str) -> float:
        """Get balance for an asset, trying both uppercase and lowercase keys.

        Bitkub API may return keys in either case, so we try both.

        Args:
            balances: The balances dict from get_balances()
            asset: The asset symbol (e.g., 'BTC', 'btc')

        Returns:
            The available balance for the asset, or 0.0 if not found
        """
        return float(self._get_decimal_balance(balances, asset))

    def _invalid_api_response(self, context: str, detail: str) -> OrderResult:
        logger.error("[%s] Invalid API response: %s", context, detail)
        return OrderResult(
            success=False,
            status=OrderStatus.ERROR,
            message=f"{context}: {detail}",
        )

    def _normalize_side(self, side: Any, default: str = "") -> str:
        """Normalize side to lowercase string ('buy'/'sell') when possible."""
        if isinstance(side, Enum):
            side = side.value
        if isinstance(side, str):
            return side.lower()
        return str(side or default).lower()

    def _to_order_side(self, side: Any, default: OrderSide = OrderSide.SELL) -> OrderSide:
        """Convert side-like values to OrderSide enum with a deterministic fallback."""
        return OrderSide.BUY if self._normalize_side(side) == "buy" else default

    @staticmethod
    def _display_strategy_name(strategy_key: str) -> str:
        key = str(strategy_key or "").strip().lower()
        mapping = {
            "machete_v8b_lite": "MacheteV8bLite",
            "simple_scalp_plus": "SimpleScalpPlus",
        }
        if key in mapping:
            return mapping[key]
        if not key:
            return "-"
        return "".join(part.capitalize() for part in key.split("_"))

    def _resolve_strategy_source(self, strategy_votes: Any) -> str:
        votes = strategy_votes if isinstance(strategy_votes, dict) else {}
        if not votes:
            return "-"
        winner = max(
            ((str(name), int(votes.get(name, 0) or 0)) for name in votes.keys()),
            key=lambda item: (item[1], item[0]),
        )[0]
        return self._display_strategy_name(winner)

    def _resolve_strategy_key(self, strategy_votes: Any) -> str:
        """Return raw strategy key (e.g. 'machete_v8b_lite') for the winning vote."""
        votes = strategy_votes if isinstance(strategy_votes, dict) else {}
        if not votes:
            return "-"
        return max(
            ((str(name), int(votes.get(name, 0) or 0)) for name in votes.keys()),
            key=lambda item: (item[1], item[0]),
        )[0]

    def _split_symbol(self, symbol: Optional[str]) -> Tuple[str, str]:
        """Return (base_asset, quote_asset) for THB_BTC, BTC_THB, or BTCUSDT."""
        sym = str(symbol or "").strip().upper()
        if not sym:
            return "", "USDT"

        known_quotes = ("USDT", "THB", "BUSD", "USD")
        if "_" in sym:
            left, right = sym.split("_", 1)
            if right in known_quotes:
                return left, right
            if left in known_quotes:
                return right, left
            return right, left

        for quote in known_quotes:
            if sym.endswith(quote) and len(sym) > len(quote):
                return sym[: -len(quote)], quote

        return sym, "USDT"

    def _extract_base_asset(self, symbol: Optional[str]) -> str:
        base_asset, _ = self._split_symbol(symbol)
        return base_asset

    def _extract_quote_asset(self, symbol: Optional[str]) -> str:
        _, quote_asset = self._split_symbol(symbol)
        return quote_asset

    def _reject_nonpositive_amount(self, amount_dec: Decimal, side_label: str, symbol: str) -> Optional[OrderResult]:
        """Reject orders with non-positive amount using a consistent response shape."""
        if amount_dec > 0:
            return None
        logger.warning(
            "[Pre-flight] %s order rejected: amount=%.6f <= 0 for %s",
            side_label,
            float(amount_dec),
            symbol,
        )
        return OrderResult(False, OrderStatus.REJECTED, message="Amount <= 0", error_code=-1)

    def _extract_payload_dict(
        self,
        response: Any,
        context: str,
        missing_payload_detail: str,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[OrderResult]]:
        """Extract dict payload from API response while preserving context-specific errors."""
        if not isinstance(response, dict):
            return None, self._invalid_api_response(context, "non-dict response")
        data = response.get("result", response)
        if not isinstance(data, dict):
            return None, self._invalid_api_response(context, missing_payload_detail)
        return data, None

    def _snap_limit_order_to_exchange_grid(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        amount: float,
        order_type: str,
    ) -> Tuple[float, float]:
        """Snap limit price and quantity to Binance PRICE_FILTER / LOT_SIZE using cached exchangeInfo."""
        ot = (order_type or "limit").lower()
        if ot != "limit" or price <= 0:
            return price, amount
        try:
            from api_client import BinanceThClient, round_step
        except ImportError:
            return price, amount
        if not isinstance(self.api_client, BinanceThClient):
            return price, amount
        filt = self.api_client.get_symbol_filter_strings(symbol)
        if filt.get("_fallback"):
            return price, amount
        tick = str(filt.get("tick_size") or "").strip()
        step = str(filt.get("step_size") or "").strip()
        if not tick or not step:
            return price, amount
        # Sanity-check maxPrice: Binance TH sometimes returns stale/wrong maxPrice
        # (e.g. BTCUSDT maxPrice=1000 while BTC trades at ~77,000). If the
        # live price is more than 2× the maxPrice, treat maxPrice as bad data
        # and skip snapping so the order is not silently zeroed out.
        max_price = float(filt.get("max_price") or 0.0)
        if max_price > 0 and price > max_price * 2:
            logger.warning(
                "[PriceFilter] %s maxPrice=%.2f looks stale (order price=%.2f) — skipping grid snap",
                symbol, max_price, price,
            )
            return price, amount
        rp = round_step(price, tick)
        if rp <= 0 and price > 0:
            return price, amount
        if side == OrderSide.BUY:
            quote = float(amount)
            base = quote / rp if rp > 0 else 0.0
            base = round_step(base, step)
            quote_adj = base * rp
            return rp, quote_adj
        qty = round_step(float(amount), step)
        return rp, qty

    def __init__(
        self,
        api_client,
        config: Dict[str, Any],
        risk_manager=None,
        db=None,
        on_trailing_stop=None,
        notifier=None,
    ):
        self.api_client = api_client
        self.config = config
        self.risk_manager = risk_manager
        self._db = db
        self._on_trailing_stop = on_trailing_stop
        self._notifier = notifier
        self._last_cancel_error: Optional[str] = None
        self._oms_cancel_was_error_21: bool = False

        self.retry_attempts = config.get("retry_attempts", 3)
        self.retry_delay = config.get("retry_delay_seconds", 5)
        self.order_timeout = config.get("order_timeout_seconds", 30)
        self.order_type = config.get("order_type", "limit")
        try:
            trading_cfg = config.get("trading", {}) if isinstance(config, dict) else {}
            # Minimum quote-side notional (Binance Spot: commonly USDT for *USDT pairs).
            self._min_order_quote = float(
                trading_cfg.get(
                    "min_order_amount",
                    config.get("min_order_amount", float(DEFAULT_MIN_ORDER_QUOTE)),
                )
                or float(DEFAULT_MIN_ORDER_QUOTE)
            )
        except (TypeError, ValueError):
            self._min_order_quote = float(DEFAULT_MIN_ORDER_QUOTE)

        self._fill_tracker = PartialFillTracker(max_wait_seconds=config.get("partial_fill_max_wait", 60.0))
        self._open_orders: Dict[str, Dict] = {}
        self._order_history: List[OrderResult] = []
        self._orders_lock = threading.Lock()
        self._oms_processing_lock = threading.Lock()
        self._oms_processing: set = set()  # Orders currently being replaced
        self._exit_in_progress: set = set()
        self._exit_in_progress_lock = threading.Lock()

        # ── H5/H6: In-flight entry guard ────────────────────────────────────
        # Stores idempotency keys of entry orders currently being placed.
        # Prevents two threads (e.g. two symbol iteration threads, or a fast
        # retry path) from placing the same order concurrently.
        # Key: idempotency_key string; Value: True (present = in-flight).
        self._in_flight_entries: set = set()
        self._in_flight_lock = threading.Lock()

        # ── Balance-monitor race condition guard ─────────────────────────────
        # Populated before execute_order(); cleared after _persist_position().
        # BalanceMonitor checks this to skip bootstrap / deposit alerts for
        # trades the bot just placed.
        self._pending_entry_symbols: set = set()
        self._pending_entry_symbols_lock = threading.Lock()

        self._trailing_stop_pct: float = config.get("trailing_stop_pct", 1.0)
        self._trailing_activation_pct: float = config.get("trailing_activation_pct", 0.5)
        self._allow_trailing_stop: bool = bool(config.get("allow_trailing_stop", True))

        # Always start with empty OMS state, then load only valid rows from SQLite
        with self._orders_lock:
            self._open_orders.clear()
        self.sync_open_orders_from_db()

        # ── H3/H4: Reconciliation gate ─────────────────────────────────
        # The OMS thread starts immediately so it can warm up, but it MUST
        # NOT cancel, reprice or otherwise mutate order state until the bot
        # has completed its full startup reconciliation with Bitkub.
        # trading_bot.start() calls set_reconcile_complete() after
        # _reconcile_on_startup() and sync_open_orders_from_db() finish.
        self._reconcile_done = threading.Event()

        self._oms_running = True
        self._oms_stop_event = threading.Event()
        self._oms_thread = threading.Thread(target=self._oms_monitor_loop, daemon=True)
        self._oms_thread.start()
        logger.info("[OMS] Smart Execution Order Management System running in background.")

    def _persist_position(self, order_id: str, pos_data: Dict):
        """Persist a position to the database (fire-and-forget)."""
        if not self._db:
            return
        try:
            data = dict(pos_data)
            data["order_id"] = order_id
            self._db.save_position(data)
        except Exception as e:
            logger.error("[OMS] Failed to persist position %s: %s", order_id, e, exc_info=True)

    def _remove_persisted_position(self, order_id: str):
        """Remove a position from the database."""
        if not self._db:
            return
        try:
            self._db.delete_position(order_id)
        except Exception as e:
            logger.error("[OMS] Failed to remove persisted position %s: %s", order_id, e, exc_info=True)

    def _cleanup_completed_order(self, order_id: str) -> None:
        """Remove an order from tracking and persisted state after a terminal outcome."""
        with self._orders_lock:
            self._open_orders.pop(order_id, None)
        self._remove_persisted_position(order_id)

    def _is_oms_processing(self, order_id: str) -> bool:
        with self._oms_processing_lock:
            return order_id in self._oms_processing

    def _mark_oms_processing(self, order_id: str) -> None:
        with self._oms_processing_lock:
            self._oms_processing.add(order_id)

    def _clear_oms_processing(self, order_id: Optional[str]) -> None:
        if not order_id:
            return
        with self._oms_processing_lock:
            self._oms_processing.discard(order_id)

    def is_entry_in_flight(self, symbol: str) -> bool:
        """Return True while a BUY for this symbol is in-flight (placed but not yet persisted)."""
        with self._pending_entry_symbols_lock:
            return symbol in self._pending_entry_symbols

    def _start_oms_replacement(self, order_id: str, new_order: OrderRequest, old_pos_data: Dict[str, Any]) -> None:
        self._mark_oms_processing(order_id)
        try:
            replacement_thread = threading.Thread(
                target=self._replace_order_async,
                args=(new_order, old_pos_data),
                daemon=True,
            )
            replacement_thread.start()
        except Exception:
            self._clear_oms_processing(order_id)
            raise

    def remove_tracked_position(self, order_id: str) -> None:
        """Drop a position from in-memory tracking and DB (invalid/ghost cleanup)."""
        if not order_id:
            return
        with self._orders_lock:
            self._open_orders.pop(order_id, None)
        self._remove_persisted_position(order_id)

    def register_tracked_position(self, position_id: str, pos_data: Dict[str, Any]) -> None:
        """Insert or restore a tracked position and persist it.

        M2 fix: DB-first ordering.  The persistent store is updated *before*
        the in-memory dictionary.  If the DB write fails the in-memory state is
        left unchanged, preventing divergent partial state during rebalancing.
        """
        if not position_id:
            return
        data = dict(pos_data)
        data["order_id"] = position_id
        # Persist to DB first; skip memory update on failure (M2 atomicity fix).
        if self._db:
            try:
                self._db.save_position(data)
            except Exception as exc:
                logger.error(
                    "[OMS] DB write failed for position %s — skipping memory update " "to prevent divergent state: %s",
                    position_id,
                    exc,
                    exc_info=True,
                )
                return  # Do NOT touch in-memory dict if DB write failed
        with self._orders_lock:
            self._open_orders[position_id] = data

    def _position_row_valid_for_sync(self, pos: Dict[str, Any]) -> bool:
        """Keep rows with open size; only SELL BTC quantities must pass the BTC sanity cap."""
        sym = (pos.get("symbol") or "").upper()
        side = self._normalize_side(pos.get("side"))
        amt = float(pos.get("amount") or 0)
        rem = float(pos.get("remaining_amount") or 0)
        partial = bool(pos.get("is_partial_fill"))
        if rem <= 0 and not partial and amt > 0:
            rem = amt
        if rem <= 0:
            return False
        if sym in ("THB_BTC", "BTC_THB") and side == "sell" and amt > 1.0:
            return False
        return True

    def sync_open_orders_from_db(self) -> None:
        """Clear in-memory tracking and reload only valid open positions from SQLite."""
        if not self._db:
            return
        with self._orders_lock:
            self._open_orders.clear()
        try:
            rows = self._db.load_all_positions()
        except Exception as e:
            logger.error("[Sync] Failed to load positions from DB: %s", e, exc_info=True)
            return
        n = 0
        for pos in rows:
            if not self._position_row_valid_for_sync(pos):
                continue
            oid = pos.get("order_id", "")
            if not oid:
                continue
            side_str = pos.get("side", "buy")
            if isinstance(side_str, str):
                pos["side"] = self._to_order_side(side_str)
            if pos.get("side") == OrderSide.BUY:
                normalized_amount = normalize_buy_quantity(
                    float(pos.get("amount") or 0.0),
                    float(pos.get("entry_price") or 0.0),
                    float(pos.get("total_entry_cost") or 0.0),
                )
                if normalized_amount > 0:
                    pos["amount"] = normalized_amount
            with self._orders_lock:
                self._open_orders[oid] = pos
            n += 1
        logger.info("[Sync] Loaded %d valid open orders from DB", n)

    def stop(self):
        """Stop the OMS background thread gracefully."""
        self._oms_running = False
        stop_event = getattr(self, "_oms_stop_event", None)
        if stop_event is not None:
            stop_event.set()
        # Unblock _oms_monitor_loop if it is waiting for reconciliation.
        self._reconcile_done.set()
        if self._oms_thread and self._oms_thread.is_alive():
            self._oms_thread.join(timeout=2.0)

    def set_reconcile_complete(self) -> None:
        """Signal that startup reconciliation is done.

        Must be called by ``trading_bot.start()`` (or equivalent) after
        ``_reconcile_on_startup()`` **and** ``sync_open_orders_from_db()``
        have both finished.  Until this is called the OMS monitor loop
        silently skips every cycle so it cannot act on pre-reconciliation
        stale state.
        """
        self._reconcile_done.set()
        logger.info("[OMS] Reconciliation complete — OMS order processing enabled.")

    def _oms_monitor_loop(self):
        """Background Order Management System loop."""
        while getattr(self, "_oms_running", True):
            try:
                stop_event = getattr(self, "_oms_stop_event", None)
                # Poll in short slices so stop() can terminate quickly.
                slept = 0.0
                while slept < getattr(self, "_oms_poll_interval", 5.0) and getattr(self, "_oms_running", True):
                    if stop_event is not None:
                        if stop_event.wait(timeout=0.1):
                            break
                    else:
                        time.sleep(0.1)
                    slept += 0.1
                if not getattr(self, "_oms_running", True):
                    break
                if stop_event is not None and stop_event.is_set():
                    break

                # ── H3/H4: Block until reconciliation is complete ─────────────
                # The bot may still be running _reconcile_on_startup() while
                # this loop ticks.  Acting on pre-reconciliation state risks
                # cancelling/repricing orders that reconciliation is about to
                # re-classify.  We wait (non-blocking poll) and skip this cycle
                # if reconciliation hasn't finished yet.
                if not self._reconcile_done.is_set():
                    logger.debug("[OMS] Waiting for startup reconciliation before processing orders.")
                    continue

                if self.api_client.is_circuit_open():
                    logger.debug("Circuit breaker is OPEN. Skipping OMS cleanup routine.")
                    continue

                with self._orders_lock:
                    if not self._open_orders:
                        continue
                    orders_to_check = list(self._open_orders.values())

                for order_info in orders_to_check:
                    order_id = order_info.get("order_id")
                    if not order_id:
                        continue

                    if _is_bootstrap_order_id(order_id):
                        if order_info.get("filled", False):
                            self._apply_trailing_stop(order_info)
                        else:
                            logger.debug(
                                "[OMS] Skipping exchange timeout/status checks for synthetic bootstrap position %s",
                                order_id,
                            )
                        continue

                    # Skip orders already being processed/replaced
                    if self._is_oms_processing(order_id):
                        continue

                    order_time = order_info.get("timestamp") or datetime.now(timezone.utc)
                    order_time = _coerce_utc(order_time)
                    elapsed = (datetime.now(timezone.utc) - order_time).total_seconds()

                    if order_info.get("filled", False):
                        self._apply_trailing_stop(order_info)
                        continue

                    # ── FIX HIGH-01: Periodic Order Fill Verification ──────────────
                    # Periodically check order fill status regardless of timeout.
                    # This ensures we detect fills even when network delays cause
                    # timeout checks to be missed.
                    if elapsed > 30:  # Check every 30 seconds for active orders
                        self._verify_order_fill(order_info)

                    MAX_ORDER_AGE = 86400  # 24 hours
                    if elapsed > MAX_ORDER_AGE:
                        # M1 fix: attempt exchange cancellation before discarding
                        # local tracking.  Dropping the order without cancelling
                        # it first leaves an open order on the exchange (orphan).
                        logger.warning(
                            "[OMS] Order %s stale (%.0fs > 24h) — attempting cancel before removing tracking",
                            order_id,
                            elapsed,
                        )
                        _aged_side = self._normalize_side(order_info.get("side"))
                        if not self.cancel_order(
                            order_id,
                            symbol=order_info.get("symbol"),
                            side=_aged_side,
                            retry=False,
                        ):
                            # Could not confirm cancellation — keep tracking so
                            # the next cycle can retry rather than orphan the order.
                            logger.warning(
                                "[OMS] Could not cancel aged order %s — retaining tracking to avoid orphan",
                                order_id,
                            )
                        # cancel_order's success path already removes from
                        # _open_orders and DB via _remove_persisted_position.
                        continue

                    if elapsed > self.order_timeout:
                        logger.warning(
                            "[OMS] Order %s exceeded timeout (%.1fs) -> cancelling for repricing",
                            order_id,
                            elapsed,
                        )
                        side_val = self._normalize_side(order_info.get("side"))

                        status_result = self.check_order_status(
                            order_id,
                            symbol=order_info.get("symbol"),
                            side=side_val,
                        )
                        if status_result.status == OrderStatus.FILLED:
                            logger.info("[OMS] Order %s actually filled. Ignoring timeout.", order_id)
                            self._cleanup_completed_order(order_id)
                            continue

                        if not self.cancel_order(
                            order_id,
                            symbol=order_info.get("symbol"),
                            side=side_val,
                            retry=False,
                        ):
                            err_detail = self._last_cancel_error or "Unknown"
                            logger.warning(
                                "[OMS] Failed to cancel order %s | reason: %s | retrying next loop",
                                order_id,
                                err_detail,
                            )
                            continue

                        # BALANCE SYNC DELAY - prevents Error 18 on reprice
                        time.sleep(0.5)
                        if getattr(self, "_oms_cancel_was_error_21", False):
                            self._oms_cancel_was_error_21 = False
                            continue

                        symbol = order_info["symbol"]
                        side = order_info["side"]
                        amount = order_info["amount"]
                        if order_info.get("is_partial_fill", False):
                            amount = order_info.get("remaining_amount", amount)

                        # Balance check after cancel - use _get_balance for case-insensitivity
                        side_str = self._normalize_side(side)
                        if side_str == "sell" and amount > 0:
                            try:
                                _base = self._extract_base_asset(symbol)
                                _bals = self.api_client.get_balances()
                                _avail = self._get_balance(_bals, _base)
                                if _avail <= 0:
                                    logger.info(
                                        "[OMS] Order %s — %s balance is 0. Position closed. Cleaning up.",
                                        order_id,
                                        _base,
                                    )
                                    with self._orders_lock:
                                        self._open_orders.pop(order_id, None)
                                    self._remove_persisted_position(order_id)
                                    continue
                            except Exception as bal_err:
                                logger.warning("[OMS] Balance check failed: %s — proceeding", bal_err, exc_info=True)

                        try:
                            try:
                                tick = _ws_ticker(symbol)
                                if tick and tick.last > 0:
                                    new_price = float(tick.last)
                                else:
                                    raise ValueError("Empty WS cache")
                            except Exception:
                                try:
                                    from helpers import parse_ticker_last

                                    ticker = self.api_client.get_ticker(symbol)
                                    parsed = parse_ticker_last(ticker)
                                    new_price = parsed if parsed is not None else order_info["entry_price"]
                                except Exception as tick_e:
                                    logger.error("[OMS] API ticker error %s. Using old price.", tick_e, exc_info=True)
                                    new_price = order_info["entry_price"]

                            if amount <= 0:
                                logger.warning(
                                    "[OMS] Order %s has amount=%.6f <= 0 — cleaning up local state.",
                                    order_id,
                                    amount,
                                )
                                with self._orders_lock:
                                    self._open_orders.pop(order_id, None)
                                self._remove_persisted_position(order_id)
                                continue

                            logger.info(
                                "[OMS] Replacing %s order %s | Old: %.2f -> New: %.2f",
                                side.value if isinstance(side, Enum) else side,
                                symbol,
                                order_info["entry_price"],
                                new_price,
                            )

                            # Shift SL/TP relative to the repriced entry.
                            old_entry = float(order_info.get("entry_price") or 0.0)
                            raw_sl = order_info.get("stop_loss")
                            raw_tp = order_info.get("take_profit")
                            shifted_sl = raw_sl
                            shifted_tp = raw_tp
                            if old_entry > 0 and new_price > 0:
                                if raw_sl is not None:
                                    shifted_sl = new_price - (old_entry - float(raw_sl))
                                if raw_tp is not None:
                                    shifted_tp = new_price + (float(raw_tp) - old_entry)

                            side_for_reprice = order_info.get("side")
                            side_str_reprice = (
                                side_for_reprice.value
                                if isinstance(side_for_reprice, Enum)
                                else str(side_for_reprice or "").lower()
                            )
                            # BUY safety clamp: SL must be below the new entry.
                            if side_str_reprice == "buy" and shifted_sl is not None and float(shifted_sl) >= new_price:
                                shifted_sl = round(new_price * 0.98, 2)
                                logger.warning(
                                    "[OMS] Reprice SL clamp applied for %s: new SL=%.2f at entry %.2f",
                                    order_id,
                                    shifted_sl,
                                    new_price,
                                )

                            # FIX FATAL-04: Pass old order info so it can be restored on failure
                            old_pos_data = {
                                "symbol": order_info["symbol"],
                                "side": order_info["side"],
                                "amount": order_info["amount"],
                                "entry_price": order_info["entry_price"],
                                "stop_loss": shifted_sl,
                                "take_profit": shifted_tp,
                                "order_id": order_id,
                                "timestamp": order_info.get("timestamp"),
                                "is_partial_fill": order_info.get("is_partial_fill", False),
                                "remaining_amount": order_info.get("remaining_amount", order_info["amount"]),
                                "total_entry_cost": order_info.get("total_entry_cost", 0),
                            }
                            if not isinstance(side, OrderSide):
                                side = self._to_order_side(side)
                            new_order = OrderRequest(
                                symbol=symbol,
                                side=side,
                                amount=amount,
                                price=new_price,
                                order_type=self.order_type,
                            )
                            self._start_oms_replacement(order_id, new_order, old_pos_data)
                        except Exception as e:
                            logger.error("[OMS] Loop error: %s", e, exc_info=True)

            except Exception as e:
                logger.error("[OMS] Loop error: %s", e, exc_info=True)

    def _replace_order_async(self, new_order: OrderRequest, old_pos_data: Dict[str, Any]):
        """Helper to replace an order asynchronously for OMS.

        FIX FATAL-04: Keep old position in _open_orders until replacement succeeds.
        This prevents position loss if the replacement order fails.
        The old position data is passed in so it can be restored on failure.
        """
        old_order_id: Optional[str] = old_pos_data.get("order_id")

        try:
            result = self.execute_order(new_order)
            if result.success and result.order_id:
                _entry_cost = new_order.amount * (new_order.price or 0)
                pos_data = {
                    "symbol": new_order.symbol,
                    "side": new_order.side,
                    "amount": new_order.amount,
                    "entry_price": new_order.price,
                    "stop_loss": old_pos_data.get("stop_loss"),
                    "take_profit": old_pos_data.get("take_profit"),
                    "order_id": result.order_id,
                    "timestamp": datetime.now(),
                    "is_partial_fill": old_pos_data.get("is_partial_fill", False),
                    "remaining_amount": old_pos_data.get("remaining_amount", new_order.amount),
                    "total_entry_cost": _entry_cost,
                }
                with self._orders_lock:
                    self._open_orders[result.order_id] = pos_data
                self._persist_position(result.order_id, pos_data)
                logger.info("[OMS] Order replacement SUCCESS: %s -> %s", old_order_id, result.order_id)
            elif old_order_id is not None:
                # Replacement failed - restore old position to tracking
                # This is the key fix for FATAL-04
                err_msg = result.message if hasattr(result, "message") else "Unknown error"
                logger.error(
                    "[OMS] Replacement order FAILED: %s - Restoring old position %s to tracking", err_msg, old_order_id
                )
                with self._orders_lock:
                    self._open_orders[old_order_id] = old_pos_data
                self._persist_position(old_order_id, old_pos_data)
        except Exception as e:
            logger.error("[OMS] Failed replacing order: %s", e)
            # On exception, restore old position
            if old_order_id is not None:
                try:
                    with self._orders_lock:
                        self._open_orders[old_order_id] = old_pos_data
                    self._persist_position(old_order_id, old_pos_data)
                except Exception as restore_err:
                    logger.error("[OMS] Failed to restore old position %s: %s", old_order_id, restore_err)
        finally:
            self._clear_oms_processing(old_order_id)

    def _apply_trailing_stop(self, order_info: Dict) -> None:
        """Dynamic Trailing Stop — lock in profits while letting winners run."""
        if not self._allow_trailing_stop:
            return

        entry_price = order_info.get("entry_price", 0)
        current_sl = order_info.get("stop_loss")
        symbol = order_info.get("symbol", "")
        side = order_info.get("side")

        if not entry_price or entry_price <= 0:
            return

        side_val = side.value if isinstance(side, Enum) else str(side).lower()
        if side_val != "buy":
            return

        try:
            tick = _ws_ticker(symbol)
            if tick and tick.last > 0:
                current_price = float(tick.last)
            else:
                return
        except Exception:
            return

        profit_pct = ((current_price - entry_price) / entry_price) * 100
        if profit_pct < self._trailing_activation_pct:
            return

        new_sl = current_price * (1 - self._trailing_stop_pct / 100)

        # Guard: trailing SL must not cross the TP price (would create a conflicting double-exit)
        take_profit = order_info.get("take_profit")
        if take_profit and take_profit > 0 and new_sl >= take_profit:
            new_sl = take_profit * 0.9995

        if current_sl and new_sl <= current_sl:
            return

        order_id = order_info.get("order_id")
        # Read old_sl for logging before any writes (non-destructive, inside lock).
        old_sl = 0.0
        with self._orders_lock:
            if order_id in self._open_orders:
                old_sl = self._open_orders[order_id].get("stop_loss", 0.0)

        # M3 fix: DB-first ordering — persist the new SL and trailing_peak to the
        # database BEFORE updating in-memory state.  If the DB write fails we
        # abort and leave _open_orders unchanged, preventing the divergence that
        # previously caused trailing-stop protection to be silently lost on crash.
        if self._db and order_id:
            try:
                self._db.update_position_sl(order_id, new_sl, trailing_peak=current_price)
            except Exception as exc:
                logger.error(
                    "[Trailing Stop] DB write failed for %s — aborting memory update " "to prevent divergent state: %s",
                    order_id,
                    exc,
                    exc_info=True,
                )
                return  # Do NOT update memory if DB write failed

        with self._orders_lock:
            if order_id in self._open_orders:
                self._open_orders[order_id]["stop_loss"] = new_sl
                self._open_orders[order_id]["trailing_peak"] = current_price

        logger.debug(
            "[Trailing Stop] %s | profit +%.2f%% | SL: %.2f -> %.2f THB | Price: %.2f",
            symbol,
            profit_pct,
            old_sl,
            new_sl,
            current_price,
        )

        if self._on_trailing_stop:
            try:
                self._on_trailing_stop(symbol, old_sl, new_sl, current_price, profit_pct)
            except Exception as e:
                logger.error("[Trailing Stop] Callback error: %s", e)

    def execute_order(self, order: OrderRequest) -> OrderResult:
        """Execute an order with retry logic."""
        start_time = time.time()
        attempts = 0
        last_error: Optional[str] = None
        result: Optional[OrderResult] = None

        while attempts < self.retry_attempts:
            if hasattr(self.api_client, "is_circuit_open") and self.api_client.is_circuit_open():
                last_error = "Circuit breaker is OPEN"
                logger.warning("Order aborted before attempt: %s", last_error)
                break
            attempts += 1
            try:
                result = self._place_order(order)
                result.execution_time_ms = (time.time() - start_time) * 1000
                result.attempts = attempts
                if result.success:
                    _side_str = order.side.value if isinstance(order.side, Enum) else str(order.side)
                    side_th = "ซื้อ" if _side_str == "buy" else "ขาย"
                    logger.info(
                        "Order placed: %s | %s (%s) %.6f %s",
                        result.order_id,
                        side_th,
                        _side_str.upper(),
                        order.amount,
                        order.symbol,
                    )
                    with self._orders_lock:
                        self._order_history.append(result)
                    if order.side == OrderSide.BUY and result.filled_amount > 0 and self._db:
                        try:
                            self._db.record_held_coin(order.symbol, result.filled_amount)
                        except Exception as e:
                            logger.debug("Failed to record held coin history: %s", e)
                    return result
                last_error = result.message
                logger.warning("Order attempt %d failed: %s", attempts, last_error)

                # Skip retries for fatal errors (insufficient balance, amount too low, etc.)
                if getattr(result, "error_code", None) in (-1, 15, 18, 21):
                    logger.warning("Fatal error code %s. Stopping retries.", result.error_code)
                    break
                if hasattr(self.api_client, "is_circuit_open") and self.api_client.is_circuit_open():
                    logger.warning("Circuit breaker opened after attempt %d. Stopping retries.", attempts)
                    break

                if attempts < self.retry_attempts:
                    time.sleep(self.retry_delay)
            except Exception as e:
                last_error = str(e)
                logger.error("Order attempt %d exception: %s", attempts, e)
                if "Circuit breaker" in last_error:
                    break
                if attempts < self.retry_attempts:
                    time.sleep(self.retry_delay)

        execution_time = (time.time() - start_time) * 1000
        final_result = OrderResult(
            success=False,
            status=OrderStatus.ERROR,
            message="Order failed after %d attempts: %s" % (attempts, last_error),
            attempts=attempts,
            execution_time_ms=execution_time,
        )
        # Propagate the last known error_code so callers can detect permanent failures
        if result is not None and getattr(result, "error_code", None) is not None:
            final_result.error_code = result.error_code
        return final_result

    def _place_order(self, order: OrderRequest) -> OrderResult:
        """Place a single order via API."""
        try:
            # FIX HIGH-03: Generate idempotency key to prevent duplicate orders
            # on network retries. Uses signal_id if available, otherwise generates
            # a deterministic key based on order details (no timestamp, so retries
            # produce the same key and can be de-duplicated).
            idempotency_key = getattr(order, "idempotency_key", None)
            if not idempotency_key:
                side_val = order.side.value if isinstance(order.side, Enum) else str(order.side)
                key_data = f"{order.symbol}:{side_val}:{order.amount}:{order.price}"
                idempotency_key = hashlib.sha256(key_data.encode()).hexdigest()[:32]

            symbol = order.symbol
            price = 0.0 if order.order_type == "market" else (order.price or 0.0)
            price_dec = to_decimal(price)
            min_order_quote = to_decimal(getattr(self, "_min_order_quote", float(DEFAULT_MIN_ORDER_QUOTE)))

            if order.side == OrderSide.BUY:
                _, quote_asset = self._split_symbol(symbol)
                quote_asset_upper = quote_asset.upper()
                order_amount_dec = to_decimal(order.amount)
                reject_result = self._reject_nonpositive_amount(order_amount_dec, "BUY", symbol)
                if reject_result:
                    return reject_result
                quote_amount = order_amount_dec
                balances = self.api_client.get_balances(force_refresh=True, allow_stale=False)
                available_quote = self._get_decimal_balance(balances, quote_asset_upper)

                logger.info("[BUY] Order details:")
                logger.info("  Coin: %s", symbol)
                logger.info("  Type: %s", order.order_type.upper())
                logger.info(
                    "  %s amount: %s",
                    quote_asset_upper,
                    self._format_balance_for_display(quote_asset_upper, quote_amount),
                )
                logger.info(
                    "  %s avail: %s",
                    quote_asset_upper,
                    self._format_balance_for_display(quote_asset_upper, available_quote),
                )
                logger.info(
                    "  Price: %s %s", self._format_balance_for_display(quote_asset_upper, price_dec), quote_asset_upper
                )

                if quote_amount < min_order_quote:
                    logger.error(
                        "[Cancel] %s %.2f < minimum %.2f — not sending",
                        quote_asset_upper,
                        float(quote_amount),
                        float(min_order_quote),
                    )
                    return OrderResult(
                        False,
                        OrderStatus.REJECTED,
                        message="Order %.2f %s below minimum %.2f"
                        % (float(quote_amount), quote_asset_upper, float(min_order_quote)),
                        error_code=-1,
                        ordered_amount=float(quote_amount),
                    )

                if quote_amount > available_quote:
                    logger.warning(
                        "[Balance Check] Insufficient %s (%.2f < %.2f) — skipping",
                        quote_asset_upper,
                        float(available_quote),
                        float(quote_amount),
                    )
                    return OrderResult(
                        False, OrderStatus.REJECTED, message=f"{quote_asset_upper} insufficient", error_code=18
                    )

                quote_decimals = self.ASSET_DECIMALS.get(quote_asset_upper, 2)
                max_quote = quantize_decimal(available_quote * Decimal("0.95"), quote_decimals)
                if quote_amount > max_quote:
                    logger.warning(
                        "[Size] %.2f %s > 95%% of %.2f — capping",
                        float(quote_amount),
                        quote_asset_upper,
                        float(max_quote),
                    )
                    quote_amount = max_quote
                quote_amount = quantize_decimal(quote_amount, quote_decimals)
                snap_p, snap_amt = self._snap_limit_order_to_exchange_grid(
                    symbol, OrderSide.BUY, float(price_dec), float(quote_amount), order.order_type
                )
                price_dec = to_decimal(snap_p)
                quote_amount = to_decimal(snap_amt)

                response = self.api_client.place_bid(
                    symbol=symbol,
                    amount=float(quote_amount),
                    rate=float(price_dec),
                    order_type=order.order_type,
                    client_id=idempotency_key,
                )
                logger.info("[Response] %s", str(response)[:500])
                response_context = "place_bid"
            else:
                base_asset, quote_asset = self._split_symbol(symbol)
                base_asset_upper = base_asset.upper()
                quote_asset_upper = quote_asset.upper()
                order_amount_dec = to_decimal(order.amount)

                reject_result = self._reject_nonpositive_amount(order_amount_dec, "SELL", symbol)
                if reject_result:
                    return reject_result

                balances = self.api_client.get_balances(force_refresh=True, allow_stale=False)
                available_base = self._get_decimal_balance(balances, base_asset)

                logger.info("[SELL] Order details:")
                logger.info("  Coin: %s", symbol)
                logger.info("  Type: %s", order.order_type.upper())
                logger.info("  Amount: %s", self._format_balance_for_display(base_asset_upper, order_amount_dec))
                logger.info(
                    "  %s avail: %s",
                    base_asset_upper,
                    self._format_balance_for_display(base_asset_upper, available_base),
                )
                logger.info(
                    "  Price: %s %s", self._format_balance_for_display(quote_asset_upper, price_dec), quote_asset_upper
                )

                # Quote notional vs minimum — do not infer from raw balance amount alone.
                # Binance min notional is often ~5–10 USDT (historic Bitkub used ~10–15 THB).
                check_price = price_dec
                if check_price <= 0:
                    try:
                        from helpers import parse_ticker_last

                        ticker = self.api_client.get_ticker(symbol)
                        check_price = to_decimal(parse_ticker_last(ticker) or 0.0)
                    except Exception:
                        logger.warning("[Value Check] Could not fetch market price for zero-price validation.")

                order_value_quote = order_amount_dec * check_price
                if check_price > 0 and order_value_quote < min_order_quote:
                    logger.warning(
                        "[Value Check] %s order value %.4f %s < MIN %.2f %s — rejecting",
                        base_asset_upper,
                        float(order_value_quote),
                        quote_asset_upper,
                        float(min_order_quote),
                        quote_asset_upper,
                    )
                    return OrderResult(
                        False,
                        OrderStatus.REJECTED,
                        message="Order value %.2f %s below minimum %.2f"
                        % (
                            float(order_value_quote),
                            quote_asset_upper,
                            float(min_order_quote),
                        ),
                        error_code=18,
                    )

                if order_amount_dec > available_base:
                    logger.warning(
                        "[Balance Check] %s insufficient (%.8f < %.8f) — rejecting",
                        base_asset_upper,
                        float(available_base),
                        float(order_amount_dec),
                    )
                    return OrderResult(
                        False, OrderStatus.REJECTED, message="%s insufficient" % base_asset_upper, error_code=18
                    )

                sell_amount = quantize_decimal(order_amount_dec, self.ASSET_DECIMALS.get(base_asset_upper, 8))
                snap_p, snap_amt = self._snap_limit_order_to_exchange_grid(
                    symbol, OrderSide.SELL, float(price_dec), float(sell_amount), order.order_type
                )
                price_dec = to_decimal(snap_p)
                sell_amount = to_decimal(snap_amt)

                response = self.api_client.place_ask(
                    symbol=symbol,
                    amount=float(sell_amount),
                    rate=float(price_dec),
                    order_type=order.order_type,
                    client_id=idempotency_key,
                )
                logger.info("[Response] %s", str(response)[:500])
                response_context = "place_ask"

            response_payload, invalid_response_result = self._extract_payload_dict(
                response,
                response_context,
                "missing result payload object",
            )
            if invalid_response_result:
                return invalid_response_result

            if response.get("error", 0) != 0:
                err_code = response.get("error", 0)
                err_msg = response.get("message", "Unknown error")
                logger.error("[ORDER ERROR] Code=%d, Message=%s", err_code, err_msg)
                return OrderResult(
                    False, OrderStatus.REJECTED, message="[%d] %s" % (err_code, err_msg), error_code=err_code
                )

            data = response_payload or {}
            order_id = str(data.get("id", "") or "")
            if not order_id:
                return self._invalid_api_response(response_context, "missing order id in response payload")

            raw_filled = data.get("filled")
            explicit_fill = raw_filled is not None
            matched_value = Decimal("0")

            if order.side == OrderSide.BUY and raw_filled is None and data.get("rec") is not None:
                raw_filled = data.get("rec")
                explicit_fill = True
                matched_value = to_decimal(data.get("amt", 0.0) or 0.0) + to_decimal(data.get("fee", 0.0) or 0.0)

            filled_amt = to_decimal(raw_filled or 0.0)
            fill_rate = to_decimal(data.get("rat", order.price or 0) or 0.0)
            status = OrderStatus.PENDING
            message = "Order accepted and awaiting match"
            ordered_amount = to_decimal(order.amount)
            remaining_amount = ordered_amount

            if order.side == OrderSide.BUY and explicit_fill:
                effective_matched_value = matched_value or ordered_amount
                is_partial = Decimal("0") < effective_matched_value < ordered_amount * Decimal("0.9999")
                is_filled = effective_matched_value >= ordered_amount * Decimal("0.9999")
                remaining_amount = max(ordered_amount - effective_matched_value, Decimal("0"))
            else:
                is_partial = explicit_fill and Decimal("0") < filled_amt < ordered_amount
                is_filled = explicit_fill and filled_amt >= ordered_amount * Decimal("0.9999")

            if is_partial:
                status = OrderStatus.PARTIAL
                message = "Partial fill: %.6f/%.6f @ %.4f" % (
                    float(filled_amt),
                    float(ordered_amount),
                    float(fill_rate),
                )
            elif is_filled:
                status = OrderStatus.FILLED
                message = "Order filled"
                remaining_amount = Decimal("0")

            result = OrderResult(
                success=True,
                status=status,
                order_id=order_id,
                filled_amount=float(filled_amt),
                filled_price=float(fill_rate) if fill_rate else order.price,
                ordered_amount=float(ordered_amount),
                remaining_amount=float(remaining_amount),
                message=message,
            )

            if is_partial and order_id:
                info = PartialFillInfo(
                    order_id=order_id,
                    symbol=symbol,
                    side=order.side,
                    original_amount=float(ordered_amount),
                    filled_amount=float(filled_amt),
                    avg_fill_price=float(fill_rate or to_decimal(order.price or 0)),
                )
                self._fill_tracker.start_tracking(order_id, info)
                result.partial_fill_info = info

            return result
        except Exception as e:
            logger.error("Order placement error: %s", e, exc_info=True)
            # Preserve error code from BinanceAPIError so callers can detect permanent failures
            _err_code = getattr(e, "code", None)
            return OrderResult(
                success=False,
                status=OrderStatus.ERROR,
                message=str(e),
                ordered_amount=order.amount,
                error_code=_err_code,
            )

    def execute_entry(
        self,
        plan: ExecutionPlan,
        portfolio_value: float,
        defer_position_tracking: bool = False,
    ) -> OrderResult:
        """Execute a full entry: place order and set SL/TP."""
        # ── H5/H6: Idempotency fence ────────────────────────────────────────
        # Derive a deterministic key for this entry signal so concurrent
        # threads (or a fast re-trigger on the same symbol) cannot place the
        # same order twice.  Uses plan.signal_id when available; falls back to
        # a hash of (symbol + side + entry_price) so the key is stable across
        # retry attempts.
        side_val = plan.side.value if isinstance(plan.side, Enum) else str(plan.side)
        _idem_src = getattr(plan, "signal_id", None) or (f"{plan.symbol}:{side_val}:{plan.entry_price}")
        _idem_key = hashlib.sha256(_idem_src.encode()).hexdigest()[:32]

        with self._in_flight_lock:
            if _idem_key in self._in_flight_entries:
                logger.warning(
                    "[Idempotency] Duplicate execute_entry blocked for %s "
                    "(key=%s) — another thread is already placing this order.",
                    plan.symbol,
                    _idem_key,
                )
                return OrderResult(
                    False,
                    OrderStatus.REJECTED,
                    message="Duplicate signal — order already in-flight (key=%s)" % _idem_key,
                )
            self._in_flight_entries.add(_idem_key)

        try:
            return self._execute_entry_inner(plan, portfolio_value, defer_position_tracking)
        finally:
            with self._in_flight_lock:
                self._in_flight_entries.discard(_idem_key)

    def _execute_entry_inner(
        self,
        plan: ExecutionPlan,
        portfolio_value: float,
        defer_position_tracking: bool = False,
    ) -> OrderResult:
        """Internal entry execution — called only after the idempotency fence."""
        if self.risk_manager and plan.stop_loss and plan.take_profit:
            rr_check = self.risk_manager.validate_risk_reward(
                entry_price=plan.entry_price,
                stop_loss=plan.stop_loss,
                take_profit=plan.take_profit,
            )
            if not rr_check.allowed:
                return OrderResult(False, OrderStatus.REJECTED, message="R:R Enforcer: %s" % rr_check.reason)

        if plan.signal_timestamp:
            sig_ts = _coerce_utc(plan.signal_timestamp)
            age_seconds = (datetime.now(timezone.utc) - sig_ts).total_seconds()
            if age_seconds > 300:
                logger.warning("[SignalExpiry] Signal too old: %.0fs — rejecting", age_seconds)
                return OrderResult(
                    False, OrderStatus.REJECTED, message="Signal expired (%.0fs old, max 300s)" % age_seconds
                )

        try:
            from helpers import parse_ticker_last

            ticker = self.api_client.get_ticker(plan.symbol)
            current_price = parse_ticker_last(ticker) or 0.0
        except Exception:
            current_price = 0.0

        if current_price > 0 and plan.entry_price > 0:
            drift_pct = abs(current_price - plan.entry_price) / plan.entry_price * 100
            max_drift = plan.max_price_drift_pct
            if drift_pct > max_drift:
                logger.warning("[SignalExpiry] Price drift %.2f%% > %.1f%%", drift_pct, max_drift)
                return OrderResult(False, OrderStatus.REJECTED, message="Price drifted %.2f%%" % drift_pct)
            elif drift_pct > max_drift * 0.5:
                logger.warning(
                    "[SignalExpiry] Price drift %.2f%% (>%.1f%%) — consider re-confirming", drift_pct, max_drift * 0.5
                )

        # Position sizing
        if plan.side == OrderSide.SELL:
            base_asset, _ = self._split_symbol(plan.symbol)
            try:
                balances = self.api_client.get_balances()
                avail = float(self._get_decimal_balance(balances, base_asset))
            except Exception as e:
                logger.error("Failed to fetch crypto balance for SELL: %s", e, exc_info=True)
                avail = 0.0
            amount = avail
            if amount <= 0:
                if plan.close_position:
                    logger.warning(
                        "[Balance Check] close_position=True but %s balance = 0 "
                        "— position already closed, skipping.",
                        base_asset.upper(),
                    )
                else:
                    logger.warning("[Balance Check] %s balance = 0 — rejecting", base_asset.upper())
                return OrderResult(False, OrderStatus.REJECTED, message="%s balance = 0" % base_asset.upper())
        else:
            _, quote_asset = self._split_symbol(plan.symbol)
            quote_asset_upper = quote_asset.upper()
            if plan.close_position:
                try:
                    balances = self.api_client.get_balances()
                    avail_quote = float(self._get_decimal_balance(balances, quote_asset_upper))
                except Exception as e:
                    logger.error("Failed to fetch %s balance: %s", quote_asset_upper, e, exc_info=True)
                    avail_quote = 0.0
                amount = avail_quote / plan.entry_price if plan.entry_price else 0
                if amount <= 0:
                    logger.warning("[Balance Check] %s balance = 0 — rejecting", quote_asset_upper)
                    return OrderResult(False, OrderStatus.REJECTED, message=f"{quote_asset_upper} balance = 0")
            elif self.risk_manager:
                risk_result = self.risk_manager.calculate_position_size(
                    portfolio_value=portfolio_value,
                    entry_price=plan.entry_price,
                    stop_loss_price=plan.stop_loss,
                    take_profit_price=plan.take_profit,
                    confidence=plan.confidence,
                    symbol=getattr(plan, "symbol", None),
                )
                if not risk_result.allowed:
                    return OrderResult(False, OrderStatus.REJECTED, message="Sizing rejected: %s" % risk_result.reason)
                amount = risk_result.suggested_size
            else:
                amount = portfolio_value * 0.1

        # Rounding
        if plan.side == OrderSide.BUY:
            buy_quote_asset = self._extract_quote_asset(plan.symbol).upper()
            buy_quote_decimals = self.ASSET_DECIMALS.get(buy_quote_asset, 2)
            amount = float(quantize_decimal(amount, buy_quote_decimals))
        else:
            amount = self._round_amount(amount, plan.symbol)

        amount_dec = to_decimal(amount)
        quote_asset_upper = self._extract_quote_asset(plan.symbol).upper()
        order_value_quote = amount_dec if plan.side == OrderSide.BUY else (amount_dec * to_decimal(plan.entry_price))
        min_order_quote = to_decimal(getattr(self, "_min_order_quote", float(DEFAULT_MIN_ORDER_QUOTE)))
        if plan.side == OrderSide.SELL and not plan.close_position:
            if amount_dec <= 0 or order_value_quote < min_order_quote:
                logger.info("SELL signal but insufficient balance — skipping")
                return OrderResult(False, OrderStatus.CANCELLED, message="Insufficient balance", ordered_amount=amount)

        if order_value_quote < min_order_quote:
            logger.error(
                "[Cancel] Order value %.2f %s < minimum %.2f | amount=%.6f, price=%.2f",
                float(order_value_quote),
                quote_asset_upper,
                float(min_order_quote),
                amount,
                plan.entry_price,
            )
            return OrderResult(
                False,
                OrderStatus.REJECTED,
                message="Order value %.2f %s below minimum" % (float(order_value_quote), quote_asset_upper),
            )

        order = OrderRequest(
            symbol=plan.symbol, side=plan.side, amount=amount, price=plan.entry_price, order_type=self.order_type
        )
        with self._pending_entry_symbols_lock:
            self._pending_entry_symbols.add(plan.symbol)
        result = self.execute_order(order)

        if result.success and result.order_id:
            if defer_position_tracking:
                _plan_side = plan.side.value if isinstance(plan.side, Enum) else str(plan.side)
                logger.info(
                    "[Entry Submitted] %s %s | amount=%.6f @ %.2f | awaiting fill confirmation",
                    _plan_side.upper(),
                    plan.symbol,
                    amount,
                    plan.entry_price,
                )
                with self._pending_entry_symbols_lock:
                    self._pending_entry_symbols.discard(plan.symbol)
                return result

            actual_filled = result.filled_amount
            actual_price = result.filled_price or plan.entry_price
            is_filled_now = result.status == OrderStatus.FILLED and actual_filled > 0

            if result.status == OrderStatus.PARTIAL and result.partial_fill_info:
                recalc = self._fill_tracker.recalculate_sl_tp(
                    order_id=result.order_id,
                    original_sl=plan.stop_loss,
                    original_tp=plan.take_profit,
                    entry_price=actual_price,
                )
                act_sl = recalc.get("stop_loss", plan.stop_loss)
                act_tp = recalc.get("take_profit", plan.take_profit)
                logger.warning(
                    "[PartialFill] Recalculated SL/TP for %s: amount=%.6f, SL=%.2f, TP=%.2f",
                    result.order_id,
                    actual_filled,
                    act_sl,
                    act_tp,
                )
                _entry_cost = actual_filled if plan.side == OrderSide.BUY else actual_filled * actual_price
                pos_data = {
                    "symbol": plan.symbol,
                    "side": plan.side,
                    "amount": actual_filled,
                    "entry_price": actual_price,
                    "stop_loss": act_sl,
                    "take_profit": act_tp,
                    "order_id": result.order_id,
                    "timestamp": datetime.now(),
                    "is_partial_fill": True,
                    "remaining_amount": result.remaining_amount,
                    "total_entry_cost": _entry_cost,
                    "filled": False,
                    "strategy_source": self._resolve_strategy_source(plan.strategy_votes),
                    "entry_strategy_key": self._resolve_strategy_key(plan.strategy_votes),
                    "bot_executed": True,
                }
                with self._orders_lock:
                    self._open_orders[result.order_id] = pos_data
                self._persist_position(result.order_id, pos_data)
            else:
                tracked_amount = (
                    actual_filled if (plan.side == OrderSide.BUY and is_filled_now and actual_filled > 0) else amount
                )
                tracked_remaining_amount = (
                    actual_filled
                    if (plan.side == OrderSide.BUY and is_filled_now and actual_filled > 0)
                    else result.remaining_amount
                )
                # Use actual fill price as entry_price when available
                effective_entry = actual_price if (is_filled_now and actual_price > 0) else plan.entry_price
                # Recalculate SL/TP proportionally if fill price differs from signal price
                if (
                    is_filled_now
                    and actual_price > 0
                    and plan.entry_price > 0
                    and abs(actual_price - plan.entry_price) > 0.01
                ):
                    price_ratio = actual_price / plan.entry_price
                    effective_sl = plan.stop_loss * price_ratio if plan.stop_loss else plan.stop_loss
                    effective_tp = plan.take_profit * price_ratio if plan.take_profit else plan.take_profit
                    logger.info(
                        "[SL/TP Adjust] %s fill price %.2f differs from signal %.2f — " "SL: %.2f→%.2f, TP: %.2f→%.2f",
                        plan.symbol,
                        actual_price,
                        plan.entry_price,
                        plan.stop_loss,
                        effective_sl,
                        plan.take_profit,
                        effective_tp,
                    )
                else:
                    effective_sl = plan.stop_loss
                    effective_tp = plan.take_profit
                _entry_cost = amount if plan.side == OrderSide.BUY else amount * effective_entry
                with self._orders_lock:
                    pos_data = {
                        "symbol": plan.symbol,
                        "side": plan.side,
                        "amount": tracked_amount,
                        "entry_price": effective_entry,
                        "stop_loss": effective_sl,
                        "take_profit": effective_tp,
                        "order_id": result.order_id,
                        "timestamp": datetime.now(),
                        "is_partial_fill": False,
                        "remaining_amount": tracked_remaining_amount,
                        "total_entry_cost": _entry_cost,
                        "filled": is_filled_now,
                        "filled_amount": actual_filled if is_filled_now else 0.0,
                        "filled_price": actual_price if is_filled_now else 0.0,
                        "strategy_source": self._resolve_strategy_source(plan.strategy_votes),
                        "entry_strategy_key": self._resolve_strategy_key(plan.strategy_votes),
                        "bot_executed": True,
                    }
                    self._open_orders[result.order_id] = pos_data
                self._persist_position(result.order_id, pos_data)
                logger.info(
                    "[Position Open] %s %s | amount=%.6f @ %.2f (signal: %.2f) | SL=%.2f TP=%.2f | state=%s",
                    "BUY" if plan.side == OrderSide.BUY else "SELL",
                    plan.symbol,
                    amount,
                    effective_entry,
                    plan.entry_price,
                    effective_sl,
                    effective_tp,
                    result.status.value,
                )

            if self.risk_manager:
                self.risk_manager.record_trade(plan.symbol)

        with self._pending_entry_symbols_lock:
            self._pending_entry_symbols.discard(plan.symbol)
        return result

    def execute_exit(
        self,
        position_id: str,
        order_id: str,
        side: OrderSide,
        amount: float,
        price: Optional[float] = None,
        defer_cleanup: bool = False,
        exit_trigger: Optional[str] = None,
    ) -> OrderResult:
        """Execute exit for a position with per-position lock.

        Sniper mode: always sells the ACTUAL exchange balance for the asset,
        ensuring 100% position closure and full return to cash.

        exit_trigger: "SL", "TP", "TIME", or None.
            SL exits use market orders for guaranteed fill during crashes.
            TP/other exits use the configured order_type (default: limit).
        """
        with self._exit_in_progress_lock:
            if position_id in self._exit_in_progress:
                logger.warning("[ExitLock] Position %s exit already in progress — dropping", position_id)
                return OrderResult(False, OrderStatus.REJECTED, message="Exit already in progress")
            self._exit_in_progress.add(position_id)

        try:
            with self._orders_lock:
                pos_data = self._open_orders.get(position_id)
            if not pos_data:
                logger.warning("[ExitLock] Position %s not found — already closed.", position_id)
                return OrderResult(False, OrderStatus.REJECTED, message="Position already removed")

            # ── Sniper: use actual exchange balance for 100% exit ──
            sell_amount = amount
            symbol = pos_data["symbol"]
            if side == OrderSide.SELL:
                try:
                    base_asset = self._extract_base_asset(symbol)
                    balances = self.api_client.get_balances(force_refresh=True, allow_stale=False)
                    actual_balance = self._get_balance(balances, base_asset)
                    if actual_balance > 0:
                        if actual_balance > sell_amount:
                            logger.info(
                                "[Sniper] Actual %s balance %.8f > tracked %.8f — selling full balance",
                                base_asset,
                                actual_balance,
                                sell_amount,
                            )
                        sell_amount = actual_balance
                except Exception as bal_err:
                    logger.warning(
                        "[Sniper] Balance query failed for %s — using tracked amount %.8f: %s",
                        symbol,
                        sell_amount,
                        bal_err,
                    )

            # SL exits use market orders for guaranteed fill during fast moves.
            # TP/other exits use the configured order_type (limit by default).
            if exit_trigger == "SL":
                exit_order_type = "market"
                exit_price = 0  # Bitkub market orders require rate=0
                logger.info(
                    "[SL-Market] %s SL exit → market order for guaranteed fill | tracked_price=%.2f",
                    symbol,
                    price or 0,
                )
            else:
                exit_order_type = self.order_type
                exit_price = price or pos_data.get("entry_price", 0)

            order = OrderRequest(
                symbol=symbol,
                side=side,
                amount=sell_amount,
                price=exit_price,
                order_type=exit_order_type,
            )
            result = self.execute_order(order)
            if result.success and not defer_cleanup:
                with self._orders_lock:
                    self._open_orders.pop(position_id, None)
                self._remove_persisted_position(position_id)
            return result
        finally:
            with self._exit_in_progress_lock:
                self._exit_in_progress.discard(position_id)

    def cancel_order(
        self,
        order_id: str,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        retry: bool = True,
    ) -> bool:
        """Cancel a pending order with retry logic.

        FIX HIGH: Added retry logic to handle network timeouts and transient failures.
        Retries up to 3 times with a 1-second delay between attempts.
        """
        self._last_cancel_error = None
        self._oms_cancel_was_error_21 = False

        # Retry configuration
        max_retries = 3 if retry else 1
        retry_delay = 1.0  # seconds

        for attempt in range(1, max_retries + 1):
            try:
                # Prepare symbol and side
                if not symbol or not side:
                    with self._orders_lock:
                        if order_id in self._open_orders:
                            order_info = self._open_orders[order_id]
                            symbol = symbol or order_info.get("symbol")
                            side = self._normalize_side(side or order_info.get("side"))
                if not symbol or not side:
                    self._last_cancel_error = "missing symbol=%s or side=%s" % (symbol, side)
                    logger.error("Cannot cancel order %s: %s", order_id, self._last_cancel_error)
                    return False

                # Attempt to cancel
                response = self.api_client.cancel_order(symbol=symbol, order_id=order_id, side=side)

                # Check response
                try:
                    err_code = int(response.get("error", 0) or 0)
                except (TypeError, ValueError):
                    err_code = 0

                if err_code != 0:
                    if err_code == 21:
                        # Already filled - success
                        logger.info("[OMS] Order %s already filled (Error 21) — cleaning up", order_id)
                        self._oms_cancel_was_error_21 = True
                        self._cleanup_completed_order(order_id)
                        return True
                    # Other error - retry if attempts remaining
                    self._last_cancel_error = "API error %d: %s" % (
                        err_code,
                        response.get("message", "unknown"),
                    )
                    logger.warning(
                        "[OMS] Cancel order %s failed (attempt %d/%d): %s",
                        order_id,
                        attempt,
                        max_retries,
                        self._last_cancel_error,
                    )
                    if attempt < max_retries:
                        time.sleep(retry_delay)
                        continue
                    logger.error("Cancel order %s failed after %d attempts", order_id, max_retries)
                    return False

                # M1 fix: remove from BOTH in-memory tracking and the
                # persistent DB only AFTER the exchange confirms the cancel.
                # Previously the DB removal was missing, allowing ghost orders
                # to resurface after a process restart via sync_open_orders_from_db.
                with self._orders_lock:
                    self._open_orders.pop(order_id, None)
                self._remove_persisted_position(order_id)
                return True

            except Exception as e:
                from api_client import BinanceAPIError

                if isinstance(e, BinanceAPIError) and getattr(e, "code", 0) == 21:
                    logger.info("[OMS] Order %s already filled (Error 21) — cleaning up", order_id)
                    self._oms_cancel_was_error_21 = True
                    self._cleanup_completed_order(order_id)
                    return True
                self._last_cancel_error = str(e)
                logger.warning("[OMS] Cancel order %s error (attempt %d/%d): %s", order_id, attempt, max_retries, e)
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                logger.error("Cancel order %s error after %d attempts: %s", order_id, max_retries, e, exc_info=True)
                return False
        return False

    def get_open_orders(self) -> List[Dict]:
        """Get all open orders from tracking."""
        with self._orders_lock:
            return list(self._open_orders.values())

    def check_order_status(
        self, order_id: str, symbol: Optional[str] = None, side: Optional[str] = None
    ) -> OrderResult:
        """Check the status of an order."""
        try:
            if not symbol or not side:
                with self._orders_lock:
                    if order_id in self._open_orders:
                        tracked = self._open_orders[order_id]
                        symbol = symbol or tracked.get("symbol")
                        side = self._normalize_side(side or tracked.get("side"))
            if not symbol:
                return OrderResult(success=False, status=OrderStatus.ERROR, message="Missing symbol")

            response = self.api_client.get_order_info(symbol=symbol, order_id=order_id, side=side or "")
            response_payload, invalid_response_result = self._extract_payload_dict(
                response,
                "order_info",
                "missing payload object",
            )
            if invalid_response_result:
                return invalid_response_result
            if response.get("error", 0) != 0:
                return OrderResult(success=False, status=OrderStatus.ERROR, message=response.get("message", "Failed"))

            data = response_payload or {}
            status_str = str(data.get("status", "") or "").lower()
            if not status_str:
                return self._invalid_api_response("order_info", "missing status field")
            status_map = {
                "filled": OrderStatus.FILLED,
                "partial": OrderStatus.PARTIAL,
                "pending": OrderStatus.PENDING,
                "cancelled": OrderStatus.CANCELLED,
                "rejected": OrderStatus.REJECTED,
            }
            return OrderResult(
                success=status_str == "filled",
                status=status_map.get(status_str, OrderStatus.PENDING),
                order_id=order_id,
                filled_amount=float(to_decimal(data.get("filled", 0) or 0)),
                filled_price=float(to_decimal(data.get("avg_price", data.get("rat", 0)) or 0)),
                message="Status: %s" % status_str,
            )
        except Exception as e:
            return OrderResult(success=False, status=OrderStatus.ERROR, message=str(e))

    def _verify_order_fill(self, order_info: Dict) -> None:
        """FIX HIGH-01: Verify order fill status via API.

        Called periodically by the OMS monitor loop to detect fills
        that were missed due to network delays or race conditions.
        Does NOT modify order state — only logs and emits alerts.
        """
        order_id = order_info.get("order_id")
        if not order_id:
            return

        # FIX HIGH-05: Check _open_orders directly under lock to avoid stale data
        with self._orders_lock:
            if order_id not in self._open_orders:
                return  # Order was already removed
            current_order = self._open_orders.get(order_id, {})
            if current_order.get("filled", False):
                return  # Already marked as filled
            symbol = current_order.get("symbol")
            side_val = current_order.get("side")
            side_val = self._normalize_side(side_val)

        try:
            status_result = self.check_order_status(
                order_id,
                symbol=symbol,
                side=side_val,
            )

            if status_result.status == OrderStatus.FILLED:
                logger.info(
                    "[OMS] Order %s verified FILLED (%.6f @ %.4f) — " "removing from tracking and updating position",
                    order_id,
                    status_result.filled_amount,
                    status_result.filled_price or 0,
                )
                persisted_position = None
                with self._orders_lock:
                    if order_id in self._open_orders:
                        self._open_orders[order_id]["filled"] = True
                        self._open_orders[order_id]["filled_amount"] = status_result.filled_amount
                        self._open_orders[order_id]["filled_price"] = status_result.filled_price
                        persisted_position = dict(self._open_orders[order_id])

                # Persist a detached snapshot after releasing _orders_lock.
                if self._db and persisted_position is not None:
                    try:
                        self._db.save_position(persisted_position)
                    except Exception as db_err:
                        logger.warning("[OMS] DB update failed for filled order %s: %s", order_id, db_err)

                # 📱 Send Telegram notification with PnL in THB
                if self._notifier:
                    try:
                        from alerts import format_trade_alert

                        filled_amt = status_result.filled_amount
                        fill_price = status_result.filled_price or 0
                        value_thb = filled_amt * fill_price
                        side_str = side_val.upper() if side_val else "SELL"
                        # Spot rule: BUY fill establishes cost basis (no realized PnL).
                        # Realized PnL is reported only for SELL fills.
                        pnl_amt = None
                        pnl_pct = None
                        entry_price = order_info.get("entry_price", 0)
                        if entry_price and fill_price and side_val == "sell":
                            pnl_amt = (fill_price - entry_price) * filled_amt
                            pnl_pct = (
                                (pnl_amt / (entry_price * filled_amt)) * 100 if entry_price * filled_amt > 0 else 0
                            )
                        msg = format_trade_alert(
                            symbol=symbol or "",
                            side=side_str,
                            price=fill_price,
                            amount=filled_amt,
                            value_thb=value_thb,
                            pnl_amt=pnl_amt,
                            pnl_pct=pnl_pct,
                            status="filled",
                        )
                        self._notifier.send(msg)
                        logger.info("[OMS] Telegram notification sent for filled order %s", order_id)
                    except Exception as notify_err:
                        logger.warning("[OMS] Failed to send Telegram notification: %s", notify_err)

            elif status_result.status == OrderStatus.PARTIAL:
                logger.debug(
                    "[OMS] Order %s PARTIAL (%.6f / %.6f) — updating fill tracker",
                    order_id,
                    status_result.filled_amount,
                    order_info.get("amount", 0),
                )
                # Update partial fill info
                with self._orders_lock:
                    if order_id in self._open_orders:
                        self._open_orders[order_id]["filled_amount"] = status_result.filled_amount
                        self._open_orders[order_id]["remaining_amount"] = (
                            order_info.get("amount", 0) - status_result.filled_amount
                        )

        except Exception as e:
            logger.debug("[OMS] Fill verification for %s failed: %s", order_id, e)

    def _round_amount(self, amount: float, symbol: Optional[str] = None) -> float:
        """Round order amount to 8 decimal places for crypto (keep full precision).
        Use 2 decimals for THB/USDT only.
        """
        if symbol:
            base = self._extract_base_asset(symbol)
            # Keep full 8 decimals for crypto to avoid losing value
            if base.upper() in ("THB", "USDT"):
                return float(quantize_decimal(amount, 2))
        # Default to 8 decimals for all crypto (BTC, ETH, DOGE, etc.)
        return float(quantize_decimal(amount, 8))

    def get_execution_summary(self) -> Dict[str, Any]:
        """Get summary of execution performance."""
        with self._orders_lock:
            history = list(self._order_history)
        if not history:
            return {
                "total_orders": 0,
                "successful_orders": 0,
                "failed_orders": 0,
                "avg_execution_time_ms": 0,
                "success_rate": 0,
                "open_orders": len(self._open_orders),
            }
        total = len(history)
        successful = sum(1 for r in history if r.success)
        return {
            "total_orders": total,
            "successful_orders": successful,
            "failed_orders": total - successful,
            "avg_execution_time_ms": sum(r.execution_time_ms for r in history) / total,
            "success_rate": successful / total * 100 if total > 0 else 0,
            "open_orders": len(self._open_orders),
        }
