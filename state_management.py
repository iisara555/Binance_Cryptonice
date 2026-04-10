"""Execution state machine for separating signal generation from order lifecycle."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("crypto-bot.state")


def normalize_buy_quantity(amount: float, entry_price: float, total_entry_cost: float) -> float:
    """Normalize BUY-side quantity when THB spend is accidentally stored as base amount."""
    raw_amount = float(amount or 0.0)
    raw_price = float(entry_price or 0.0)
    raw_cost = float(total_entry_cost or 0.0)

    if raw_amount <= 0 or raw_price <= 0 or raw_cost <= 0:
        return raw_amount

    implied_qty = raw_cost / raw_price
    if implied_qty <= 0:
        return raw_amount

    close_to_cost = abs(raw_amount - raw_cost) <= max(0.01, raw_cost * 0.01)
    clearly_larger_than_qty = raw_amount > (implied_qty * 1.5)
    if close_to_cost or clearly_larger_than_qty:
        return implied_qty

    return raw_amount


class TradeLifecycleState(str, Enum):
    """High-level execution lifecycle for one symbol."""

    IDLE = "idle"
    PENDING_BUY = "pending_buy"
    IN_POSITION = "in_position"
    PENDING_SELL = "pending_sell"


@dataclass
class TradeStateSnapshot:
    """Serializable snapshot of one symbol's current execution lifecycle."""

    symbol: str
    state: TradeLifecycleState = TradeLifecycleState.IDLE
    side: str = "buy"
    entry_order_id: str = ""
    exit_order_id: str = ""
    active_order_id: str = ""
    requested_amount: float = 0.0
    filled_amount: float = 0.0
    entry_price: float = 0.0
    exit_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    total_entry_cost: float = 0.0
    signal_confidence: float = 0.0
    signal_source: str = ""
    trigger: str = ""
    notes: str = ""
    opened_at: Optional[datetime] = None
    last_transition_at: datetime = field(default_factory=datetime.utcnow)

    @classmethod
    def from_row(cls, row: Optional[Dict[str, Any]], symbol: Optional[str] = None) -> "TradeStateSnapshot":
        if not row:
            if not symbol:
                raise ValueError("symbol is required when row is empty")
            return cls(symbol=str(symbol).upper())
        return cls(
            symbol=str(row.get("symbol") or symbol or "").upper(),
            state=TradeLifecycleState(str(row.get("state") or TradeLifecycleState.IDLE.value)),
            side=str(row.get("side") or "buy"),
            entry_order_id=str(row.get("entry_order_id") or ""),
            exit_order_id=str(row.get("exit_order_id") or ""),
            active_order_id=str(row.get("active_order_id") or ""),
            requested_amount=float(row.get("requested_amount") or 0.0),
            filled_amount=float(row.get("filled_amount") or 0.0),
            entry_price=float(row.get("entry_price") or 0.0),
            exit_price=float(row.get("exit_price") or 0.0),
            stop_loss=float(row.get("stop_loss") or 0.0),
            take_profit=float(row.get("take_profit") or 0.0),
            total_entry_cost=float(row.get("total_entry_cost") or 0.0),
            signal_confidence=float(row.get("signal_confidence") or 0.0),
            signal_source=str(row.get("signal_source") or ""),
            trigger=str(row.get("trigger") or ""),
            notes=str(row.get("notes") or ""),
            opened_at=row.get("opened_at"),
            last_transition_at=row.get("last_transition_at") or datetime.utcnow(),
        )

    def to_row(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "state": self.state.value,
            "side": self.side,
            "entry_order_id": self.entry_order_id or None,
            "exit_order_id": self.exit_order_id or None,
            "active_order_id": self.active_order_id or None,
            "requested_amount": self.requested_amount,
            "filled_amount": self.filled_amount,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "total_entry_cost": self.total_entry_cost,
            "signal_confidence": self.signal_confidence,
            "signal_source": self.signal_source or None,
            "trigger": self.trigger or None,
            "notes": self.notes or None,
            "opened_at": self.opened_at,
            "last_transition_at": self.last_transition_at,
        }


class TradeStateManager:
    """Persistent state machine that gates entries and controls order lifecycle."""

    def __init__(self, db, config: Optional[Dict[str, Any]] = None):
        self.db = db
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))
        self.min_entry_confidence = float(self.config.get("entry_confidence_threshold", 0.35) or 0.35)
        self.required_confirmations = max(1, int(self.config.get("confirmations_required", 2) or 2))
        self.confirmation_window = timedelta(seconds=int(self.config.get("confirmation_window_seconds", 180) or 180))
        self.pending_buy_timeout = timedelta(seconds=int(self.config.get("pending_buy_timeout_seconds", 120) or 120))
        self.pending_sell_timeout = timedelta(seconds=int(self.config.get("pending_sell_timeout_seconds", 120) or 120))
        self.allow_trailing_stop = bool(self.config.get("allow_trailing_stop", False))
        self._cache_lock = threading.RLock()
        self._state_cache: Dict[str, TradeStateSnapshot] = {}
        self._confirmations: Dict[str, Dict[str, Any]] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        if not self.db:
            return
        try:
            rows = self.db.list_trade_states()
            with self._cache_lock:
                self._state_cache = {
                    str(row.get("symbol") or "").upper(): TradeStateSnapshot.from_row(row)
                    for row in rows
                    if row.get("symbol")
                }
        except Exception as exc:
            logger.warning("[State] Failed to load cached trade states: %s", exc)
            with self._cache_lock:
                self._state_cache = {}

    def get_state(self, symbol: str) -> TradeStateSnapshot:
        symbol_key = str(symbol or "").upper()
        with self._cache_lock:
            state = self._state_cache.get(symbol_key)
            if state:
                return state
        if not self.db:
            return TradeStateSnapshot(symbol=symbol_key)
        row = self.db.get_trade_state(symbol_key)
        snapshot = TradeStateSnapshot.from_row(row, symbol=symbol_key)
        if row:
            with self._cache_lock:
                self._state_cache[symbol_key] = snapshot
        return snapshot

    def list_active_states(self) -> List[TradeStateSnapshot]:
        with self._cache_lock:
            return [
                snapshot for snapshot in self._state_cache.values()
                if snapshot.state != TradeLifecycleState.IDLE
            ]

    @staticmethod
    def _resolve_base_amount(
        side: str,
        amount: float,
        remaining_amount: float,
        explicit_filled_amount: float,
        entry_price: float,
        total_entry_cost: float,
        fallback_amount: float,
    ) -> float:
        """Resolve filled base quantity from mixed unit sources.

        `amount` can be either base quantity or THB (for some pending BUY flows).
        Prefer explicit filled amount. For BUY positions that are already filled,
        infer base quantity from total_entry_cost / entry_price when amount appears
        to be THB-sized.
        """
        if explicit_filled_amount > 0:
            return explicit_filled_amount

        if amount <= 0:
            return fallback_amount

        side_value = str(side or "").lower()
        if side_value != "buy":
            return amount

        if remaining_amount > 0:
            # Pending buy: keep base amount unknown unless explicitly provided.
            return fallback_amount

        if entry_price <= 0 or total_entry_cost <= 0:
            return amount

        return normalize_buy_quantity(amount, entry_price, total_entry_cost)

    def sync_in_position_states(self, positions: Iterable[Dict[str, Any]]) -> None:
        """Ensure persisted state matches already-open filled positions after restart/reconcile."""
        active_position_symbols = set()
        pending_buy_symbols = set()

        for pos in positions or []:
            symbol = str(pos.get("symbol") or "").upper()
            if not symbol:
                continue
            snapshot = self.get_state(symbol)
            if snapshot.state == TradeLifecycleState.PENDING_SELL:
                active_position_symbols.add(symbol)
                continue

            raw_side = pos.get("side")
            if isinstance(raw_side, Enum):
                side = raw_side.value.lower()
            else:
                side = str(raw_side or "").lower()

            amount = float(pos.get("amount") or 0.0)
            remaining_amount = float(pos.get("remaining_amount") or 0.0)
            filled_amount = float(pos.get("filled_amount") or 0.0)
            entry_price = float(pos.get("entry_price") or snapshot.entry_price or 0.0)
            total_entry_cost = float(pos.get("total_entry_cost") or snapshot.total_entry_cost or 0.0)
            is_partial_fill = bool(pos.get("is_partial_fill"))
            is_filled = bool(pos.get("filled"))

            inferred_in_position = not side
            if side == "sell":
                inferred_in_position = True
            elif side == "buy":
                inferred_in_position = (
                    is_filled
                    or is_partial_fill
                    or filled_amount > 0
                    or (amount > 0 and remaining_amount <= 0)
                )

            if not inferred_in_position:
                pending_buy_symbols.add(symbol)
                if snapshot.state == TradeLifecycleState.PENDING_BUY:
                    continue

                updated = TradeStateSnapshot(
                    symbol=symbol,
                    state=TradeLifecycleState.PENDING_BUY,
                    side="buy",
                    entry_order_id=str(pos.get("order_id") or snapshot.entry_order_id or ""),
                    exit_order_id="",
                    active_order_id=str(pos.get("order_id") or snapshot.active_order_id or ""),
                    requested_amount=total_entry_cost or snapshot.requested_amount or 0.0,
                    filled_amount=filled_amount,
                    entry_price=entry_price,
                    exit_price=0.0,
                    stop_loss=float(pos.get("stop_loss") or snapshot.stop_loss or 0.0),
                    take_profit=float(pos.get("take_profit") or snapshot.take_profit or 0.0),
                    total_entry_cost=total_entry_cost,
                    signal_confidence=snapshot.signal_confidence,
                    signal_source=snapshot.signal_source,
                    trigger=snapshot.trigger,
                    notes=snapshot.notes,
                    opened_at=pos.get("timestamp") or snapshot.opened_at or datetime.utcnow(),
                    last_transition_at=datetime.utcnow(),
                )
                self._persist(updated)
                continue

            active_position_symbols.add(symbol)
            normalized_filled_amount = self._resolve_base_amount(
                side=side,
                amount=amount,
                remaining_amount=remaining_amount,
                explicit_filled_amount=filled_amount,
                entry_price=entry_price,
                total_entry_cost=total_entry_cost,
                fallback_amount=snapshot.filled_amount or 0.0,
            )
            updated = TradeStateSnapshot(
                symbol=symbol,
                state=TradeLifecycleState.IN_POSITION,
                side="buy",
                entry_order_id=str(pos.get("order_id") or snapshot.entry_order_id or ""),
                exit_order_id="",
                active_order_id=str(pos.get("order_id") or snapshot.active_order_id or ""),
                requested_amount=total_entry_cost or snapshot.requested_amount or 0.0,
                filled_amount=normalized_filled_amount,
                entry_price=entry_price,
                exit_price=0.0,
                stop_loss=float(pos.get("stop_loss") or snapshot.stop_loss or 0.0),
                take_profit=float(pos.get("take_profit") or snapshot.take_profit or 0.0),
                total_entry_cost=total_entry_cost,
                signal_confidence=snapshot.signal_confidence,
                signal_source=snapshot.signal_source,
                trigger=snapshot.trigger,
                notes=snapshot.notes,
                opened_at=pos.get("timestamp") or snapshot.opened_at or datetime.utcnow(),
                last_transition_at=datetime.utcnow(),
            )
            self._persist(updated)

        for symbol, snapshot in list(self._state_cache.items()):
            if snapshot.state == TradeLifecycleState.IN_POSITION and symbol not in active_position_symbols:
                self._drop(symbol)
            elif (
                snapshot.state == TradeLifecycleState.PENDING_BUY
                and symbol not in pending_buy_symbols
                and symbol not in active_position_symbols
            ):
                self._drop(symbol)

    def _confirmation_key(self, symbol: str, action: str) -> str:
        return f"{str(symbol or '').upper()}:{str(action or '').lower()}"

    def clear_confirmation(self, symbol: str, action: Optional[str] = None) -> None:
        symbol_key = str(symbol or "").upper()
        if action:
            self._confirmations.pop(self._confirmation_key(symbol_key, action), None)
            return
        # Clear all known directional keys plus legacy key format.
        self._confirmations.pop(symbol_key, None)
        self._confirmations.pop(self._confirmation_key(symbol_key, "buy"), None)
        self._confirmations.pop(self._confirmation_key(symbol_key, "sell"), None)

    def _confirm_directional_entry(
        self,
        symbol: str,
        action: str,
        confidence: float,
        risk_passed: bool,
        signal_time: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        symbol_key = str(symbol or "").upper()
        snapshot = self.get_state(symbol_key)
        if snapshot.state != TradeLifecycleState.IDLE:
            return False, f"state={snapshot.state.value}"

        action_key = str(action or "").lower()
        if action_key not in {"buy", "sell"}:
            self.clear_confirmation(symbol_key)
            return False, f"unsupported action '{action_key}'"

        if not risk_passed:
            self.clear_confirmation(symbol_key, action=action_key)
            return False, "risk check failed"

        if confidence < self.min_entry_confidence:
            self.clear_confirmation(symbol_key, action=action_key)
            return False, (
                f"confidence {confidence:.2f} below state threshold {self.min_entry_confidence:.2f}"
            )

        now = signal_time or datetime.utcnow()
        confirm_key = self._confirmation_key(symbol_key, action_key)
        existing = self._confirmations.get(confirm_key)
        if existing and (now - existing["timestamp"]) <= self.confirmation_window:
            count = int(existing.get("count", 0)) + 1
        else:
            count = 1

        self._confirmations[confirm_key] = {
            "count": count,
            "timestamp": now,
        }

        if count < self.required_confirmations:
            return False, f"awaiting confirmation {count}/{self.required_confirmations}"

        self.clear_confirmation(symbol_key, action=action_key)
        return True, f"confirmed {count}/{self.required_confirmations}"

    def confirm_entry_signal(
        self,
        symbol: str,
        signal_type: str,
        confidence: float,
        risk_passed: bool,
        signal_time: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """Apply entry threshold + consecutive confirmation gate for BUY signals only."""
        action = str(signal_type or "").lower()
        if action != "buy":
            self.clear_confirmation(symbol)
            return False, "state machine only opens BUY entries from IDLE"
        return self._confirm_directional_entry(
            symbol=symbol,
            action="buy",
            confidence=confidence,
            risk_passed=risk_passed,
            signal_time=signal_time,
        )

    def confirm_idle_sell_signal(
        self,
        symbol: str,
        confidence: float,
        risk_passed: bool,
        signal_time: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """Apply the same confirmation gate for SELL entries from IDLE state."""
        return self._confirm_directional_entry(
            symbol=symbol,
            action="sell",
            confidence=confidence,
            risk_passed=risk_passed,
            signal_time=signal_time,
        )

    def start_pending_buy(self, symbol: str, plan, order_result, signal_source: str = "") -> TradeStateSnapshot:
        now = datetime.utcnow()
        requested_amount = float(order_result.ordered_amount or 0.0)
        snapshot = TradeStateSnapshot(
            symbol=str(symbol or plan.symbol).upper(),
            state=TradeLifecycleState.PENDING_BUY,
            side="buy",
            entry_order_id=str(order_result.order_id or ""),
            active_order_id=str(order_result.order_id or ""),
            requested_amount=requested_amount,
            filled_amount=float(order_result.filled_amount or 0.0),
            entry_price=float(plan.entry_price or 0.0),
            stop_loss=float(plan.stop_loss or 0.0),
            take_profit=float(plan.take_profit or 0.0),
            total_entry_cost=requested_amount,
            signal_confidence=float(plan.confidence or 0.0),
            signal_source=str(signal_source or ""),
            notes=f"signal_id={getattr(plan, 'signal_id', '')}",
            last_transition_at=now,
        )
        self._persist(snapshot)
        return snapshot

    def mark_entry_filled(self, symbol: str, filled_amount: float, filled_price: float) -> TradeStateSnapshot:
        if filled_amount <= 0 or filled_price <= 0:
            logger.error(
                "[State] Invalid filled state: symbol=%s amount=%s price=%s — rejecting",
                symbol, filled_amount, filled_price,
            )
            return self.get_state(symbol)
        snapshot = self.get_state(symbol)
        now = datetime.utcnow()
        total_cost = snapshot.total_entry_cost or (filled_amount * filled_price)
        updated = TradeStateSnapshot(
            symbol=snapshot.symbol,
            state=TradeLifecycleState.IN_POSITION,
            side="buy",
            entry_order_id=snapshot.entry_order_id,
            exit_order_id="",
            active_order_id=snapshot.entry_order_id,
            requested_amount=snapshot.requested_amount,
            filled_amount=filled_amount,
            entry_price=filled_price or snapshot.entry_price,
            stop_loss=snapshot.stop_loss,
            take_profit=snapshot.take_profit,
            total_entry_cost=total_cost,
            signal_confidence=snapshot.signal_confidence,
            signal_source=snapshot.signal_source,
            trigger="",
            notes=snapshot.notes,
            opened_at=snapshot.opened_at or now,
            last_transition_at=now,
        )
        self._persist(updated)
        return updated

    def cancel_pending_buy(self, symbol: str, reason: str = "") -> TradeStateSnapshot:
        snapshot = self.get_state(symbol)
        if reason:
            snapshot.notes = reason
        self._drop(snapshot.symbol)
        return snapshot

    def start_pending_sell(
        self,
        symbol: str,
        position: Dict[str, Any],
        exit_order_id: str,
        trigger: str,
        exit_price: float,
        notes: str = "",
    ) -> TradeStateSnapshot:
        snapshot = self.get_state(symbol)
        now = datetime.utcnow()
        raw_amount = float(position.get("amount") or 0.0)
        raw_remaining = float(position.get("remaining_amount") or 0.0)
        raw_filled = float(position.get("filled_amount") or 0.0)
        entry_price = float(position.get("entry_price") or snapshot.entry_price or 0.0)
        total_entry_cost = float(position.get("total_entry_cost") or snapshot.total_entry_cost or 0.0)
        normalized_filled = self._resolve_base_amount(
            side="buy",
            amount=raw_amount,
            remaining_amount=raw_remaining,
            explicit_filled_amount=raw_filled,
            entry_price=entry_price,
            total_entry_cost=total_entry_cost,
            fallback_amount=snapshot.filled_amount or 0.0,
        )
        updated = TradeStateSnapshot(
            symbol=str(symbol or position.get("symbol") or snapshot.symbol).upper(),
            state=TradeLifecycleState.PENDING_SELL,
            side="sell",
            entry_order_id=str(position.get("order_id") or snapshot.entry_order_id or ""),
            exit_order_id=str(exit_order_id or ""),
            active_order_id=str(exit_order_id or ""),
            requested_amount=total_entry_cost or snapshot.requested_amount or 0.0,
            filled_amount=normalized_filled,
            entry_price=entry_price,
            exit_price=float(exit_price or 0.0),
            stop_loss=float(position.get("stop_loss") or snapshot.stop_loss or 0.0),
            take_profit=float(position.get("take_profit") or snapshot.take_profit or 0.0),
            total_entry_cost=total_entry_cost,
            signal_confidence=snapshot.signal_confidence,
            signal_source=snapshot.signal_source,
            trigger=str(trigger or ""),
            notes=notes or snapshot.notes,
            opened_at=position.get("timestamp") or snapshot.opened_at or now,
            last_transition_at=now,
        )
        self._persist(updated)
        return updated

    def restore_in_position(self, symbol: str, notes: str = "") -> TradeStateSnapshot:
        snapshot = self.get_state(symbol)
        now = datetime.utcnow()
        updated = TradeStateSnapshot(
            symbol=snapshot.symbol,
            state=TradeLifecycleState.IN_POSITION,
            side="buy",
            entry_order_id=snapshot.entry_order_id,
            exit_order_id="",
            active_order_id=snapshot.entry_order_id,
            requested_amount=snapshot.requested_amount,
            filled_amount=snapshot.filled_amount,
            entry_price=snapshot.entry_price,
            exit_price=0.0,
            stop_loss=snapshot.stop_loss,
            take_profit=snapshot.take_profit,
            total_entry_cost=snapshot.total_entry_cost,
            signal_confidence=snapshot.signal_confidence,
            signal_source=snapshot.signal_source,
            trigger="",
            notes=notes or snapshot.notes,
            opened_at=snapshot.opened_at,
            last_transition_at=now,
        )
        self._persist(updated)
        return updated

    def complete_exit(self, symbol: str, exit_price: float) -> TradeStateSnapshot:
        snapshot = self.get_state(symbol)
        completed = TradeStateSnapshot(
            symbol=snapshot.symbol,
            state=TradeLifecycleState.PENDING_SELL,
            side=snapshot.side,
            entry_order_id=snapshot.entry_order_id,
            exit_order_id=snapshot.exit_order_id,
            active_order_id=snapshot.active_order_id,
            requested_amount=snapshot.requested_amount,
            filled_amount=snapshot.filled_amount,
            entry_price=snapshot.entry_price,
            exit_price=exit_price,
            stop_loss=snapshot.stop_loss,
            take_profit=snapshot.take_profit,
            total_entry_cost=snapshot.total_entry_cost,
            signal_confidence=snapshot.signal_confidence,
            signal_source=snapshot.signal_source,
            trigger=snapshot.trigger,
            notes=snapshot.notes,
            opened_at=snapshot.opened_at,
            last_transition_at=snapshot.last_transition_at,
        )
        self._drop(snapshot.symbol)
        return completed

    def is_timed_out(self, snapshot: TradeStateSnapshot) -> bool:
        now = datetime.utcnow()
        if snapshot.state == TradeLifecycleState.PENDING_BUY:
            elapsed = (now - snapshot.last_transition_at).total_seconds()
            if elapsed < 0:
                logger.warning(
                    "[State] Clock skew detected for %s (elapsed=%ds), forcing timeout",
                    snapshot.symbol, int(elapsed),
                )
                return True
            return elapsed >= self.pending_buy_timeout.total_seconds()
        if snapshot.state == TradeLifecycleState.PENDING_SELL:
            elapsed = (now - snapshot.last_transition_at).total_seconds()
            if elapsed < 0:
                logger.warning(
                    "[State] Clock skew detected for %s (elapsed=%ds), forcing timeout",
                    snapshot.symbol, int(elapsed),
                )
                return True
            return elapsed >= self.pending_sell_timeout.total_seconds()
        return False

    def _persist(self, snapshot: TradeStateSnapshot) -> None:
        if not self.db:
            with self._cache_lock:
                self._state_cache[snapshot.symbol] = snapshot
            return
        self.db.save_trade_state(snapshot.to_row())
        with self._cache_lock:
            self._state_cache[snapshot.symbol] = snapshot

    def _drop(self, symbol: str) -> None:
        symbol_key = str(symbol or "").upper()
        with self._cache_lock:
            self._state_cache.pop(symbol_key, None)
        if self.db:
            self.db.delete_trade_state(symbol_key)