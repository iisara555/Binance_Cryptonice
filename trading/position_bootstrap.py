"""
Balance-driven position bootstrap and reconciliation (extracted from ``TradingBotOrchestrator``).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from helpers import extract_base_asset, normalize_side_value
from indicators import calculate_adx, calculate_atr
from risk_management import resolve_effective_sl_tp_percentages
from state_management import TradeLifecycleState
from trade_executor import OrderSide
from trading.coercion import coerce_trade_float
from trading.order_history_utils import (
    extract_history_fill_details,
    history_side_value,
    history_status_is_filled,
    history_status_value,
    history_timestamp_value,
)

logger = logging.getLogger(__name__)

BALANCE_RECONCILE_GRACE_SECONDS: float = 180.0  # 3 minutes

_DUST_THRESHOLD_PCT: float = 0.01   # 1% of tracked amount
_DUST_THRESHOLD_FLOOR: float = 1e-8  # absolute minimum threshold
_DUST_THRESHOLD_CAP: float = 1e-6   # absolute maximum threshold


def _normalize_balances_from_api(raw: Any) -> Dict[str, Dict[str, float]]:
    """Match ``TradingBotOrchestrator.get_balance_state`` normalization for reconcile."""
    normalized: Dict[str, Dict[str, float]] = {}
    for sym, payload in (raw or {}).items():
        if isinstance(payload, dict):
            available = float(payload.get("available", 0.0) or 0.0)
            reserved = float(payload.get("reserved", 0.0) or 0.0)
        else:
            available = float(payload or 0.0)
            reserved = 0.0
        normalized[str(sym).upper()] = {
            "available": available,
            "reserved": reserved,
            "total": available + reserved,
        }
    return normalized


def bootstrap_quantity_tolerance(quantity: float) -> float:
    return max(abs(float(quantity or 0.0)) * 0.05, 1e-8)


class PositionBootstrapHelper:
    """Delegates to ``TradingBotOrchestrator`` for DB/executor/config; holds bootstrap-only logic."""

    __slots__ = ("_bot",)

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    def _merge_reconcile_balance_snapshot(self, balance_state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Return a snapshot dict with non-empty ``balances`` when possible.

        If ``get_balance_state()`` / monitor returns ``balances: {}`` (startup, poll glitch),
        fall back to ``api_client.get_balances()`` so manual sells still clear tracked positions.
        """
        b = self._bot
        snap_source = balance_state if balance_state is not None else b.get_balance_state()
        snapshot: Dict[str, Any] = dict(snap_source or {})
        balances = snapshot.get("balances")
        if not isinstance(balances, dict):
            balances = {}
        if balances:
            snapshot["balances"] = balances
            return snapshot

        if getattr(b, "_auth_degraded", False):
            logger.debug("[Balance Reconcile] skipped: empty balances while auth degraded")
            return None

        api = getattr(b, "api_client", None)
        if api is None or not callable(getattr(api, "get_balances", None)):
            logger.debug("[Balance Reconcile] skipped: empty balances and no working API client")
            return None

        try:
            raw = api.get_balances()
        except Exception as exc:
            logger.warning("[Balance Reconcile] get_balances() fallback failed: %s", exc)
            return None

        normalized = _normalize_balances_from_api(raw)
        if not normalized:
            logger.debug("[Balance Reconcile] API returned empty balance map")
            return None

        snapshot["balances"] = normalized
        if not snapshot.get("updated_at"):
            snapshot["updated_at"] = datetime.now().isoformat()
        logger.info("[Balance Reconcile] using REST balances fallback (%d asset row(s))", len(normalized))
        return snapshot

    def reconcile_tracked_positions_with_balance_state(
        self,
        balance_state: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Drop filled tracked positions whose base-asset balance is gone on the exchange.

        Works for any pair format understood by ``extract_base_asset`` (e.g. ``BTCUSDT``, ``THB_BTC``).
        """
        b = self._bot
        if not b.executor:
            return []

        snapshot = self._merge_reconcile_balance_snapshot(balance_state)
        if snapshot is None:
            return []
        balances = snapshot.get("balances") or {}
        if not balances:
            return []

        now = datetime.now()
        removed_symbols: List[str] = []
        for position in b.executor.get_open_orders() or []:
            symbol = str(position.get("symbol") or "").upper()
            order_id = str(position.get("order_id") or "")
            if not symbol or not order_id:
                continue

            side = normalize_side_value(position.get("side"))

            filled_amount = float(position.get("filled_amount") or 0.0)
            amount = float(position.get("amount") or 0.0)
            remaining_amount = float(position.get("remaining_amount") or 0.0)
            is_partial_fill = bool(position.get("is_partial_fill"))
            is_filled = bool(position.get("filled"))

            represents_live_coin = False
            if side == "buy":
                represents_live_coin = (
                    is_filled or is_partial_fill or filled_amount > 0.0 or (amount > 0.0 and remaining_amount <= 0.0)
                )
            elif side == "sell":
                represents_live_coin = True
            elif not side:
                represents_live_coin = True

            if not represents_live_coin:
                continue

            pos_ts = position.get("timestamp")
            if pos_ts is not None:
                if isinstance(pos_ts, str):
                    try:
                        pos_ts = datetime.fromisoformat(pos_ts)
                    except (ValueError, TypeError):
                        pos_ts = None
                if isinstance(pos_ts, datetime):
                    age_seconds = (now - pos_ts).total_seconds()
                    if age_seconds < BALANCE_RECONCILE_GRACE_SECONDS:
                        continue

            base_asset = extract_base_asset(symbol)
            if not base_asset:
                continue

            balance_total = b._extract_total_balance(snapshot, base_asset)
            tracked_amount = max(filled_amount, amount, remaining_amount, 0.0)
            dust_threshold = min(max(tracked_amount * _DUST_THRESHOLD_PCT, _DUST_THRESHOLD_FLOOR), _DUST_THRESHOLD_CAP)
            if balance_total > dust_threshold:
                continue

            b.executor.remove_tracked_position(order_id)
            b.db.record_held_coin(symbol, 0.0)
            removed_symbols.append(symbol)
            logger.warning(
                "[Balance Reconcile] Removed stale tracked position %s (%s) after balance dropped to %.8f",
                symbol,
                order_id,
                balance_total,
            )

        added_symbols = self.bootstrap_missing_positions_from_balance_state(snapshot)

        if (removed_symbols or added_symbols) and b._state_machine_enabled:
            b._state_manager.sync_in_position_states(b.executor.get_open_orders())

        return removed_symbols

    def bootstrap_missing_positions_from_balance_state(
        self,
        balance_state: Optional[Dict[str, Any]] = None,
        target_pairs: Optional[List[str]] = None,
    ) -> List[str]:
        b = self._bot
        if not b.executor:
            return []

        snapshot = balance_state or b.get_balance_state()
        balances = (snapshot or {}).get("balances") or {}
        if not balances:
            return []

        try:
            source_pairs = target_pairs if target_pairs is not None else (b._get_trading_pairs() or [])
        except Exception:
            source_pairs = target_pairs or []

        active_pairs = [str(pair).strip().upper() for pair in source_pairs if str(pair).strip()]
        if not active_pairs:
            return []

        try:
            open_orders = b.executor.get_open_orders() or []
        except Exception:
            open_orders = []
        if not isinstance(open_orders, list):
            open_orders = []
        tracked_symbols = {
            str(position.get("symbol") or "").upper()
            for position in open_orders
            if isinstance(position, dict)
            and str(position.get("symbol") or "").strip()
            and normalize_side_value(position.get("side")) == "buy"
        }

        missing_pairs: List[str] = []
        for pair in active_pairs:
            if pair in tracked_symbols:
                continue

            base_asset = extract_base_asset(pair)
            if b._extract_total_balance(snapshot, base_asset) <= 0:
                continue

            missing_pairs.append(pair)

        if not missing_pairs:
            return []

        registered = b._bootstrap_held_positions(balances=balances, target_pairs=missing_pairs)
        if registered:
            logger.info("[Balance Reconcile] Bootstrapped held wallet positions into Position Book: %s", registered)
        return registered

    def preserve_bootstrap_position_from_balances(
        self,
        order_id: str,
        local_pos: Dict[str, Any],
        balances: Optional[Dict[str, Any]] = None,
    ) -> bool:
        b = self._bot
        if not b.executor or not b.db:
            return False

        bootstrap_id = str(order_id or "")
        if not bootstrap_id.startswith("bootstrap_"):
            return False

        symbol = str(local_pos.get("symbol") or "").upper()
        if not symbol:
            return False

        side_value = local_pos.get("side", OrderSide.BUY)
        side_str = normalize_side_value(side_value)
        if side_str != "buy":
            return False

        base_asset = extract_base_asset(symbol)
        balance_payload = (balances or {}).get(base_asset, {}) if isinstance(balances, dict) else {}
        balance_total = b._extract_total_balance({"balances": {base_asset: balance_payload}}, base_asset)
        if balance_total <= 0:
            return False

        preserved = dict(local_pos)
        tracked_amount = max(
            float(local_pos.get("filled_amount") or 0.0),
            float(local_pos.get("amount") or 0.0),
            0.0,
        )
        preserved_amount = float(balance_total)
        external_excess = 0.0
        if tracked_amount > 0:
            preserved_amount = min(float(balance_total), tracked_amount)
            external_excess = max(float(balance_total) - tracked_amount, 0.0)

        preserved_entry = float(local_pos.get("entry_price") or 0.0)
        preserved_sl = local_pos.get("stop_loss")
        preserved_tp = local_pos.get("take_profit")
        restored_context = b._resolve_bootstrap_position_context(symbol, float(balance_total))
        restored_source = str(restored_context.get("source") or "")
        restored_entry = coerce_trade_float(restored_context.get("entry_price"), 0.0)
        preserved_timestamp = restored_context.get("acquired_at") or local_pos.get("timestamp")
        if restored_source and restored_source != "bootstrap_position" and restored_entry > 0:
            preserved_amount = float(balance_total)
            external_excess = 0.0
            preserved_entry = restored_entry
            preserved_sl = restored_context.get("stop_loss")
            preserved_tp = restored_context.get("take_profit")
        if preserved_entry > 0 and (not preserved_sl or not preserved_tp):
            fallback_sl, fallback_tp = b._build_bootstrap_position_sl_tp(symbol, preserved_entry)
            preserved_sl = preserved_sl or fallback_sl
            preserved_tp = preserved_tp or fallback_tp

        preserved.update(
            {
                "amount": preserved_amount,
                "entry_price": preserved_entry,
                "remaining_amount": 0.0,
                "filled": True,
                "filled_amount": preserved_amount,
                "filled_price": preserved_entry,
                "stop_loss": preserved_sl,
                "take_profit": preserved_tp,
                "timestamp": preserved_timestamp,
            }
        )
        if preserved_entry > 0:
            restored_cost = coerce_trade_float(restored_context.get("total_entry_cost"), 0.0)
            preserved["total_entry_cost"] = (
                restored_cost
                if (restored_source and restored_source != "bootstrap_position" and restored_cost > 0)
                else (preserved_amount * preserved_entry)
            )

        with b.executor._orders_lock:
            b.executor._open_orders[bootstrap_id] = preserved

        try:
            b.db.save_position(preserved)
        except Exception as exc:
            logger.warning(
                "[Reconcile] Failed to persist preserved bootstrap position %s: %s",
                bootstrap_id,
                exc,
            )

            logger.warning(
                "[Reconcile] Live %s balance exceeds tracked bootstrap size by %.8f. "
                "Keeping tracked entry/SL/TP unchanged and not averaging external coins into position %s.",
                base_asset,
                external_excess,
                bootstrap_id,
            )

        logger.info(
            "[Reconcile] Preserved bootstrap position %s using live %s balance %.8f, entry %.2f, SL=%s, TP=%s%s",
            bootstrap_id,
            base_asset,
            preserved_amount,
            preserved_entry,
            f"{float(preserved_sl):.2f}" if preserved_sl else "n/a",
            f"{float(preserved_tp):.2f}" if preserved_tp else "n/a",
            f" via {restored_source}" if restored_source and restored_source != "bootstrap_position" else "",
        )
        return True

    def build_bootstrap_position_sl_tp(
        self, symbol: str, entry_price: float
    ) -> tuple[Optional[float], Optional[float]]:
        b = self._bot
        if entry_price <= 0:
            return None, None

        db = getattr(b, "db", None)
        if db is not None:
            try:
                candles = db.get_candles(symbol, interval="1h", limit=50)
                if candles is not None and len(candles) >= 15:
                    high = candles["high"].astype(float)
                    low = candles["low"].astype(float)
                    close = candles["close"].astype(float)

                    atr_series = calculate_atr(high, low, close, period=14)
                    current_atr = float(atr_series.iloc[-1]) if len(atr_series) > 0 else 0.0

                    if current_atr > 0:
                        adx_series = calculate_adx(high, low, close, period=14)
                        current_adx = (
                            float(adx_series.iloc[-1]) if len(adx_series) > 0 and not adx_series.empty else 25.0
                        )
                        if not (0 < current_adx < 100):
                            current_adx = 25.0

                        if current_adx > 50:
                            sl_mult, tp_mult = 2.0, 3.0
                        elif current_adx > 30:
                            sl_mult, tp_mult = 1.5, 3.0
                        else:
                            sl_mult, tp_mult = 1.0, 2.5

                        stop_loss = round(entry_price - (sl_mult * current_atr), 6)
                        take_profit = round(entry_price + (tp_mult * current_atr), 6)

                        sl_pct = ((stop_loss - entry_price) / entry_price) * 100
                        tp_pct = ((take_profit - entry_price) / entry_price) * 100
                        logger.info(
                            "[Bootstrap] %s dynamic SL/TP: ATR=%.2f ADX=%.1f mult=%.1f/%.1f "
                            "→ SL=%.2f (%.1f%%) TP=%.2f (+%.1f%%)",
                            symbol,
                            current_atr,
                            current_adx,
                            sl_mult,
                            tp_mult,
                            stop_loss,
                            sl_pct,
                            take_profit,
                            tp_pct,
                        )
                        return stop_loss, take_profit
            except Exception as exc:
                logger.debug("[Bootstrap] %s ATR calc failed, using fallback: %s", symbol, exc)

        risk_cfg = dict(b.config.get("risk", {}) or {})
        stop_loss_pct, take_profit_pct = resolve_effective_sl_tp_percentages(symbol, risk_cfg)

        stop_loss = round(entry_price * (1 + (stop_loss_pct / 100.0)), 6)
        take_profit = round(entry_price * (1 + (take_profit_pct / 100.0)), 6)
        logger.info(
            "[Bootstrap] %s fallback SL/TP: %.1f%% / +%.1f%% → SL=%.2f TP=%.2f",
            symbol,
            stop_loss_pct,
            take_profit_pct,
            stop_loss,
            take_profit,
        )
        return stop_loss, take_profit

    def resolve_bootstrap_position_context(
        self,
        symbol: str,
        quantity: float,
    ) -> Dict[str, Any]:
        """Recover bootstrap entry context from persisted position/state before estimating from market price."""
        b = self._bot
        symbol_key = str(symbol or "").upper()
        if not symbol_key:
            return {}

        best: Dict[str, Any] = {}
        bootstrap_best: Dict[str, Any] = {}

        db = getattr(b, "db", None)
        if db is not None and hasattr(db, "load_all_positions"):
            try:
                rows = list(db.load_all_positions() or [])
            except Exception as exc:
                logger.debug("[Bootstrap Positions] Failed to load persisted positions for %s: %s", symbol_key, exc)
                rows = []

            matching_rows = []
            bootstrap_rows = []
            for row in rows:
                row_symbol = str(row.get("symbol") or "").upper()
                row_side = row.get("side", "buy")
                if hasattr(row_side, "value"):
                    row_side = row_side.value
                if row_symbol != symbol_key or str(row_side or "").lower() != "buy":
                    continue

                entry_price = coerce_trade_float(row.get("entry_price"), 0.0)
                if entry_price <= 0:
                    continue
                order_id = str(row.get("order_id") or "")
                if order_id.startswith("bootstrap_"):
                    bootstrap_rows.append(row)
                else:
                    matching_rows.append(row)

            if matching_rows:
                matching_rows.sort(key=lambda row: row.get("timestamp") or datetime.min, reverse=True)
                row = matching_rows[0]
                entry_price = coerce_trade_float(row.get("entry_price"), 0.0)
                total_entry_cost = coerce_trade_float(row.get("total_entry_cost"), 0.0)
                acquired_at = row.get("timestamp")
                if entry_price > 0:
                    best = {
                        "entry_price": entry_price,
                        "stop_loss": row.get("stop_loss"),
                        "take_profit": row.get("take_profit"),
                        "total_entry_cost": total_entry_cost if total_entry_cost > 0 else (quantity * entry_price),
                        "acquired_at": acquired_at,
                        "source": "persisted_position",
                    }
            elif bootstrap_rows:
                bootstrap_rows.sort(key=lambda row: row.get("timestamp") or datetime.min, reverse=True)
                row = bootstrap_rows[0]
                entry_price = coerce_trade_float(row.get("entry_price"), 0.0)
                total_entry_cost = coerce_trade_float(row.get("total_entry_cost"), 0.0)
                acquired_at = row.get("timestamp")
                if entry_price > 0:
                    bootstrap_best = {
                        "entry_price": entry_price,
                        "stop_loss": row.get("stop_loss"),
                        "take_profit": row.get("take_profit"),
                        "total_entry_cost": total_entry_cost if total_entry_cost > 0 else (quantity * entry_price),
                        "acquired_at": acquired_at,
                        "source": "bootstrap_position",
                    }

        if not best:
            history_best = self.resolve_bootstrap_exchange_history_context(symbol_key, quantity)
            if history_best:
                best = history_best

        if db is not None and hasattr(db, "get_trades"):
            try:
                recent_trades = list(db.get_trades(pair=symbol_key, limit=20) or [])
            except Exception as exc:
                logger.debug("[Bootstrap Positions] Failed to load trade history for %s: %s", symbol_key, exc)
                recent_trades = []

            trade_events: list[Dict[str, Any]] = []
            for trade in recent_trades:
                trade_side = getattr(trade, "side", "")
                trade_pair = getattr(trade, "pair", "")
                normalized_side = str(trade_side or "").lower()
                if str(trade_pair or "").upper() != symbol_key or normalized_side not in ("buy", "sell"):
                    continue
                trade_price = coerce_trade_float(getattr(trade, "price", 0.0), 0.0)
                trade_qty = coerce_trade_float(getattr(trade, "quantity", 0.0), 0.0)
                if trade_price <= 0 or trade_qty <= 0:
                    continue
                trade_events.append(
                    {
                        "side": normalized_side,
                        "quantity": trade_qty,
                        "price": trade_price,
                        "total_cost": trade_qty * trade_price if normalized_side == "buy" else 0.0,
                        "timestamp": getattr(trade, "timestamp", None),
                    }
                )

            if trade_events and not best:
                trade_history_best = self.build_weighted_inventory_context(
                    trade_events,
                    quantity,
                    source="trade_history",
                )
                if trade_history_best:
                    best = trade_history_best

        if db is not None and hasattr(db, "get_trade_state"):
            try:
                state_row = db.get_trade_state(symbol_key)
            except Exception as exc:
                logger.debug("[Bootstrap Positions] Failed to load trade state for %s: %s", symbol_key, exc)
                state_row = None

            state_value = str((state_row or {}).get("state") or "").lower()
            state_entry = coerce_trade_float((state_row or {}).get("entry_price"), 0.0)
            if (
                state_entry > 0
                and state_value
                in (
                    TradeLifecycleState.IN_POSITION.value,
                    TradeLifecycleState.PENDING_SELL.value,
                )
                and not best
            ):
                best = {
                    "entry_price": state_entry,
                    "stop_loss": (state_row or {}).get("stop_loss"),
                    "take_profit": (state_row or {}).get("take_profit"),
                    "total_entry_cost": coerce_trade_float((state_row or {}).get("total_entry_cost"), 0.0)
                    or (quantity * state_entry),
                    "acquired_at": (state_row or {}).get("opened_at"),
                    "source": "trade_state",
                }

        return best or bootstrap_best

    def build_weighted_inventory_context(
        self,
        events: List[Dict[str, Any]],
        quantity: float,
        *,
        source: str,
    ) -> Dict[str, Any]:
        if not events:
            return {}

        sorted_events = sorted(events, key=lambda event: event.get("timestamp") or datetime.min)
        lots: List[Dict[str, Any]] = []

        for event in sorted_events:
            side = str(event.get("side") or "").lower()
            event_qty = coerce_trade_float(event.get("quantity"), 0.0)
            event_price = coerce_trade_float(event.get("price"), 0.0)
            event_cost = coerce_trade_float(event.get("total_cost"), 0.0)
            if event_qty <= 0 or event_price <= 0:
                continue

            if side in ("buy", "bid"):
                lots.append(
                    {
                        "quantity": event_qty,
                        "price": event_price,
                        "cost": event_cost if event_cost > 0 else (event_qty * event_price),
                        "timestamp": event.get("timestamp"),
                    }
                )
                continue

            if side not in ("sell", "ask"):
                continue

            remaining_to_match = event_qty
            while remaining_to_match > 1e-12 and lots:
                head = lots[0]
                head_qty = coerce_trade_float(head.get("quantity"), 0.0)
                if head_qty <= 1e-12:
                    lots.pop(0)
                    continue
                matched_qty = min(remaining_to_match, head_qty)
                head_cost = coerce_trade_float(head.get("cost"), 0.0)
                residual_qty = max(head_qty - matched_qty, 0.0)
                if residual_qty <= 1e-12:
                    lots.pop(0)
                else:
                    head["quantity"] = residual_qty
                    if head_cost > 0:
                        head["cost"] = head_cost * (residual_qty / head_qty)
                remaining_to_match -= matched_qty

        inventory_qty = sum(coerce_trade_float(lot.get("quantity"), 0.0) for lot in lots)
        inventory_notional = sum(
            coerce_trade_float(lot.get("quantity"), 0.0) * coerce_trade_float(lot.get("price"), 0.0) for lot in lots
        )
        inventory_cost = sum(coerce_trade_float(lot.get("cost"), 0.0) for lot in lots)

        if inventory_qty <= 0 or inventory_notional <= 0 or inventory_cost <= 0:
            return {}

        qty_tolerance = bootstrap_quantity_tolerance(quantity)
        if quantity > 0 and abs(inventory_qty - quantity) > qty_tolerance:
            return {}

        entry_price = inventory_notional / inventory_qty if inventory_qty > 0 else 0.0
        if entry_price <= 0:
            return {}

        acquired_candidates = [
            timestamp for timestamp in (lot.get("timestamp") for lot in lots) if isinstance(timestamp, datetime)
        ]
        acquired_at = min(acquired_candidates) if acquired_candidates else None

        return {
            "entry_price": entry_price,
            "stop_loss": None,
            "take_profit": None,
            "total_entry_cost": inventory_cost,
            "acquired_at": acquired_at,
            "source": source,
        }

    def resolve_bootstrap_exchange_history_context(
        self,
        symbol: str,
        quantity: float,
    ) -> Dict[str, Any]:
        b = self._bot
        try:
            history = list(
                b.api_client.get_order_history(symbol, limit=b._order_history_window_limit()) or []
            )
        except Exception as exc:
            logger.debug("[Bootstrap Positions] Failed to load exchange order history for %s: %s", symbol, exc)
            return {}

        qty_tolerance = bootstrap_quantity_tolerance(quantity)
        close_matches: list[Dict[str, Any]] = []
        fallback_matches: list[Dict[str, Any]] = []
        history_events: list[Dict[str, Any]] = []

        for row in history:
            status_value = history_status_value(row)
            if status_value and not history_status_is_filled(row):
                continue

            side_value = history_side_value(row)
            if side_value and side_value not in ("buy", "bid", "sell", "ask"):
                continue

            filled_amount, filled_price = extract_history_fill_details(row)
            if filled_amount <= 0 or filled_price <= 0:
                continue

            history_events.append(
                {
                    "side": side_value,
                    "quantity": filled_amount,
                    "price": filled_price,
                    "total_cost": 0.0,
                    "timestamp": history_timestamp_value(row),
                }
            )

            raw_cost = coerce_trade_float(row.get("amt"), 0.0)
            if raw_cost <= 0:
                raw_cost = coerce_trade_float(row.get("amount"), 0.0)
            if side_value in ("buy", "bid"):
                history_events[-1]["total_cost"] = raw_cost if raw_cost > 0 else (filled_amount * filled_price)

            candidate = {
                "entry_price": filled_price,
                "stop_loss": None,
                "take_profit": None,
                "total_entry_cost": raw_cost if raw_cost > 0 else (filled_amount * filled_price),
                "acquired_at": history_timestamp_value(row),
                "source": "exchange_history",
            }

            if side_value in ("buy", "bid"):
                if quantity > 0 and abs(filled_amount - quantity) <= qty_tolerance:
                    close_matches.append(candidate)
                else:
                    fallback_matches.append(candidate)

        weighted_context = self.build_weighted_inventory_context(
            history_events,
            quantity,
            source="exchange_history",
        )
        if weighted_context:
            return weighted_context

        if close_matches:
            return close_matches[0]
        if fallback_matches:
            return fallback_matches[0]
        return {}
