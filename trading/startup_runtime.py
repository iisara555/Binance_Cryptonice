from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dynamic_coin_config import _build_pair, _extract_supported_pairs
from helpers import extract_base_asset, normalize_side_value
from state_management import TradeLifecycleState
from trade_executor import OrderSide
from trading.coercion import coerce_trade_float as _coerce_trade_float

logger = logging.getLogger(__name__)


def _recover_bootstrap_strategy(bot: Any, symbol: str, restored_context: Dict[str, Any]) -> str:
    """Return the best-known strategy name for a coin being bootstrapped, falling back to 'bootstrap'."""
    # 1. State machine: if IN_POSITION/PENDING_SELL state has a signal_source, use it.
    if getattr(bot, "_state_machine_enabled", False):
        state_manager = getattr(bot, "_state_manager", None)
        if state_manager is not None:
            try:
                snapshot = state_manager.get_state(symbol)
                if snapshot.state in (TradeLifecycleState.IN_POSITION, TradeLifecycleState.PENDING_SELL):
                    sig = str(snapshot.signal_source or "").strip()
                    if sig and sig.lower() not in ("", "strategy"):
                        executor = getattr(bot, "executor", None)
                        if executor is not None and hasattr(executor, "_display_strategy_name"):
                            return executor._display_strategy_name(sig)
            except Exception:
                pass

    # 2. Persisted position context already has a non-bootstrap strategy_source.
    ctx_src = str(restored_context.get("strategy_source") or "").strip()
    if ctx_src and ctx_src not in ("", "bootstrap"):
        return ctx_src

    return "bootstrap"


_QUOTE_ASSETS_FOR_WALLET_BOOTSTRAP = frozenset({"USDT", "THB", "BUSD", "FDUSD", "EUR"})
_SYNTHETIC_ORDER_PREFIXES = ("bootstrap_", "manual_")


def _remote_order_id(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("orderId") or row.get("id") or row.get("order_id") or "").strip()


def _local_order_id(row: Dict[str, Any]) -> str:
    return str((row or {}).get("order_id") or (row or {}).get("id") or "").strip()


def _is_exchange_open_candidate(row: Dict[str, Any]) -> bool:
    """True for pending exchange orders, false for filled wallet positions."""
    order_id = _local_order_id(row)
    if not order_id or order_id.startswith(_SYNTHETIC_ORDER_PREFIXES):
        return False

    side = normalize_side_value((row or {}).get("side"))
    filled = bool((row or {}).get("filled"))
    partial = bool((row or {}).get("is_partial_fill"))

    if side == "sell":
        return True
    if partial:
        return True
    return not filled


def _fetch_exchange_open_order_ids(bot: Any, symbols: List[str]) -> set[str]:
    api_client = getattr(bot, "api_client", None)
    if api_client is None or not hasattr(api_client, "get_open_orders"):
        return set()

    try:
        rows = api_client.get_open_orders()
        return {_remote_order_id(row) for row in list(rows or []) if _remote_order_id(row)}
    except TypeError:
        pass
    except Exception as exc:
        logger.debug("[OrderReconcile] Fetch-all open orders unavailable, falling back per symbol: %s", exc)

    remote_ids: set[str] = set()
    for symbol in sorted({str(sym or "").upper() for sym in symbols if str(sym or "").strip()}):
        try:
            rows = api_client.get_open_orders(symbol)
        except Exception as exc:
            logger.warning("[OrderReconcile] Failed to fetch open orders for %s: %s", symbol, exc)
            continue
        remote_ids.update({_remote_order_id(row) for row in list(rows or []) if _remote_order_id(row)})
    return remote_ids


def reconcile_open_orders_with_exchange(bot: Any, *, source: str = "runtime") -> int:
    """
    Remove pending local exchange orders that no longer exist in Binance open orders.

    Filled wallet positions remain in the Position Book; only pending BUY/SELL exchange orders
    are compared against Binance's open-order endpoint.
    """
    if bool((getattr(bot, "config", {}) or {}).get("auth_degraded", False)):
        logger.info("[OrderReconcile] skipped in auth degraded mode")
        return 0

    executor = getattr(bot, "executor", None)
    db = getattr(bot, "db", None)
    if executor is None:
        return 0

    local_rows = list(executor.get_open_orders() or [])
    pending_rows = [row for row in local_rows if isinstance(row, dict) and _is_exchange_open_candidate(row)]
    if not pending_rows:
        logger.debug("[OrderReconcile] %s: no pending exchange orders to reconcile", source)
        return 0

    remote_ids = _fetch_exchange_open_order_ids(
        bot,
        [str(row.get("symbol") or "") for row in pending_rows],
    )
    removed = 0
    for row in pending_rows:
        order_id = _local_order_id(row)
        if not order_id or order_id in remote_ids:
            continue

        logger.warning(
            "[OrderReconcile] Ghost order detected during %s: %s %s id=%s -> marking cancelled",
            source,
            row.get("symbol"),
            normalize_side_value(row.get("side")),
            order_id,
        )

        if db is not None:
            row_db_id = row.get("id")
            if row_db_id:
                try:
                    db.update_order_status(int(row_db_id), "cancelled")
                except Exception as exc:
                    logger.warning("[OrderReconcile] Failed to mark order row %s cancelled: %s", row_db_id, exc)
            try:
                db.delete_position(order_id)
            except Exception as exc:
                logger.warning("[OrderReconcile] Failed to delete ghost position %s: %s", order_id, exc)

        state_manager = getattr(bot, "_state_manager", None)
        symbol = str(row.get("symbol") or "").upper()
        if state_manager is not None and symbol:
            try:
                snapshot = state_manager.get_state(symbol)
                if str(getattr(snapshot, "entry_order_id", "") or "") == order_id:
                    state_manager.cancel_pending_buy(symbol, "ghost order missing on exchange")
                elif str(getattr(snapshot, "exit_order_id", "") or "") == order_id:
                    state_manager.restore_in_position(symbol, "ghost sell order missing on exchange")
            except Exception as exc:
                logger.debug("[OrderReconcile] State cleanup skipped for %s: %s", order_id, exc)

        with executor._orders_lock:
            executor._open_orders.pop(order_id, None)
        removed += 1

    if removed:
        executor.sync_open_orders_from_db()
    logger.info("[OrderReconcile] %s complete | removed=%d pending=%d remote=%d", source, removed, len(pending_rows), len(remote_ids))
    return removed


class StartupRuntimeHelper:
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @staticmethod
    def _infer_quote_asset_from_pairs(pairs: List[str]) -> str:
        for raw in pairs or []:
            p = str(raw or "").upper()
            if p.endswith("USDT") and "_" not in p:
                return "USDT"
            if p.startswith("THB_"):
                return "THB"
        return "USDT"

    def _wallet_extra_bootstrap_pairs(self, balances: Dict[str, Any], base_pairs: List[str]) -> List[str]:
        """Pairs with spot balance but missing from the dynamic pair list (e.g. whitelist drift)."""
        if not isinstance(balances, dict):
            return []
        quote = self._infer_quote_asset_from_pairs(base_pairs)
        supported: set[str] = set()
        try:
            supported = _extract_supported_pairs(self.bot.api_client.get_symbols() or [], quote)
        except Exception as exc:
            logger.warning("[Bootstrap Positions] Wallet pair expansion skipped (exchange symbols): %s", exc)
            return []
        if not supported:
            return []

        seen = {str(p).upper() for p in base_pairs if str(p).strip()}
        extras: List[str] = []
        for asset_raw, payload in balances.items():
            asset = str(asset_raw or "").upper().strip()
            if not asset or asset in _QUOTE_ASSETS_FOR_WALLET_BOOTSTRAP:
                continue
            if isinstance(payload, dict):
                total = float(payload.get("available", 0) or 0) + float(payload.get("reserved", 0) or 0)
            else:
                try:
                    total = float(payload or 0)
                except (TypeError, ValueError):
                    total = 0.0
            if total <= 0:
                continue
            candidate = _build_pair(asset, quote)
            if not candidate or candidate in seen:
                continue
            if candidate not in supported:
                continue
            extras.append(candidate)
            seen.add(candidate)

        if extras:
            logger.info(
                "[Bootstrap Positions] Wallet holdings add %d pair(s) not in runtime list: %s",
                len(extras),
                extras,
            )
        return extras

    def bootstrap_held_coin_history(self) -> None:
        tracked_pairs = [pair.upper() for pair in self.bot._get_trading_pairs()]
        if not tracked_pairs:
            return

        try:
            balances = self.bot.api_client.get_balances()
        except Exception as exc:
            logger.debug("[Portfolio Guard] balance bootstrap skipped: %s", exc)
            balances = {}

        tracked_open_orders = {
            str(order.get("symbol", "")).upper() for order in self.bot.executor.get_open_orders() if order.get("symbol")
        }
        backfilled: list[str] = []

        for pair in tracked_pairs:
            if self.bot.db.has_ever_held(pair):
                continue

            base_asset = extract_base_asset(pair)
            balance_data = balances.get(base_asset.upper(), {}) if isinstance(balances, dict) else {}
            available_qty = float(balance_data.get("available", 0) or 0)

            if available_qty > 0 or pair in tracked_open_orders:
                self.bot.db.record_held_coin(pair, available_qty if available_qty > 0 else 0.0)
                backfilled.append(pair)

        if backfilled:
            logger.info(
                "🛡️  [Portfolio Guard] Backfilled held-coin history from live state: %s",
                backfilled,
            )

    def bootstrap_held_positions(
        self,
        balances: Optional[Dict[str, Any]] = None,
        target_pairs: Optional[List[str]] = None,
    ) -> List[str]:
        if balances is None:
            try:
                balances = self.bot.api_client.get_balances()
            except Exception as exc:
                logger.warning("[Bootstrap Positions] Cannot fetch balances: %s", exc)
                return []

        base_pairs = [pair.upper() for pair in (target_pairs or self.bot._get_trading_pairs())]
        wallet_extras = self._wallet_extra_bootstrap_pairs(balances, base_pairs)
        tracked_pairs = list(dict.fromkeys([*base_pairs, *wallet_extras]))
        if not tracked_pairs:
            return []

        tracked_symbols = {
            str(order.get("symbol", "")).upper() for order in self.bot.executor.get_open_orders() if order.get("symbol")
        }

        from helpers import parse_ticker_last

        registered: list[str] = []
        _executor = getattr(self.bot, "executor", None)
        for pair in tracked_pairs:
            if pair in tracked_symbols:
                continue
            if _executor is not None and _executor.is_entry_in_flight(pair):
                continue

            base_asset = extract_base_asset(pair)
            balance_data = balances.get(base_asset.upper(), {}) if isinstance(balances, dict) else {}
            available_qty = float(balance_data.get("available", 0) or 0)
            reserved_qty = float(balance_data.get("reserved", 0) or 0)
            total_qty = available_qty + reserved_qty

            if total_qty <= 0:
                continue

            restored_context = self.bot._resolve_bootstrap_position_context(pair, total_qty)

            try:
                ticker = self.bot.api_client.get_ticker(pair)
                current_price = parse_ticker_last(ticker) or 0.0
            except Exception:
                current_price = 0.0

            entry_price = _coerce_trade_float(restored_context.get("entry_price"), 0.0)
            if entry_price <= 0:
                entry_price = current_price

            if entry_price <= 0:
                logger.debug("[Bootstrap Positions] Skipping %s: no price available", pair)
                continue

            position_value = total_qty * entry_price
            if position_value < self.bot.min_trade_value_thb:
                logger.debug(
                    "[Bootstrap Positions] Skipping %s: value %.2f quote < min %.2f quote",
                    pair,
                    position_value,
                    self.bot.min_trade_value_thb,
                )
                continue

            stop_loss = restored_context.get("stop_loss")
            take_profit = restored_context.get("take_profit")
            if not stop_loss or not take_profit:
                stop_loss, take_profit = self.bot._build_bootstrap_position_sl_tp(pair, entry_price)
            total_entry_cost = _coerce_trade_float(restored_context.get("total_entry_cost"), 0.0)
            if total_entry_cost <= 0:
                total_entry_cost = total_qty * entry_price
            acquired_at = restored_context.get("acquired_at")
            if not isinstance(acquired_at, datetime):
                acquired_at = datetime.now()

            bootstrap_source = restored_context.get("source") or "estimated_from_ticker"
            strategy_source = _recover_bootstrap_strategy(self.bot, pair, restored_context)
            synthetic_id = f"bootstrap_{pair}_{int(datetime.now().timestamp())}"
            pos_data = {
                "symbol": pair,
                "side": OrderSide.BUY,
                "amount": total_qty,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "order_id": synthetic_id,
                "timestamp": acquired_at,
                "is_partial_fill": False,
                "remaining_amount": total_qty,
                "total_entry_cost": total_entry_cost,
                "filled": True,
                "filled_amount": total_qty,
                "filled_price": entry_price,
                "bootstrap_source": bootstrap_source,
                "strategy_source": strategy_source,
            }

            self.bot.executor.register_tracked_position(synthetic_id, pos_data)
            self.bot.db.record_held_coin(pair, total_qty)
            registered.append(
                f"{pair} ({total_qty:.8f} @ {entry_price:,.2f} | SL {float(stop_loss or 0.0):,.2f} TP {float(take_profit or 0.0):,.2f})"
            )

            if restored_context.get("source"):
                logger.info(
                    "[Bootstrap Positions] Restored %s entry context for %s @ %.2f",
                    restored_context.get("source"),
                    pair,
                    entry_price,
                )

            time.sleep(0.15)

        if registered:
            logger.info(
                "📦 [Bootstrap Positions] Registered %d held coin(s) as open positions:\n  %s",
                len(registered),
                "\n  ".join(registered),
            )

        return registered

    def reconcile_pending_trade_states(self, remote_order_ids: set[str]) -> set[str]:
        handled_order_ids: set[str] = set()
        if not getattr(self.bot, "_state_machine_enabled", False):
            return handled_order_ids
        state_manager = getattr(self.bot, "_state_manager", None)
        if state_manager is None:
            return handled_order_ids

        for snapshot in list(state_manager.list_active_states()):
            if snapshot.state == TradeLifecycleState.PENDING_BUY:
                tracked_order_id = snapshot.entry_order_id
            elif snapshot.state == TradeLifecycleState.PENDING_SELL:
                tracked_order_id = snapshot.exit_order_id
            else:
                continue

            if not tracked_order_id or tracked_order_id in remote_order_ids:
                continue

            hist = self.bot._lookup_order_history_status(snapshot.symbol, tracked_order_id)
            if self.bot._history_status_is_filled(hist):
                if snapshot.state == TradeLifecycleState.PENDING_BUY:
                    filled_amount, filled_price = self.bot._extract_history_fill_details(
                        hist,
                        fallback_amount=snapshot.filled_amount,
                        fallback_price=snapshot.entry_price,
                        fallback_cost=snapshot.total_entry_cost,
                    )
                    if filled_amount <= 0 or filled_price <= 0:
                        logger.warning(
                            "[Reconcile] Filled BUY for %s detected but amount/price unresolved", snapshot.symbol
                        )
                        continue
                    self.bot._register_filled_position_from_state(snapshot, filled_amount, filled_price)
                    state_manager.mark_entry_filled(snapshot.symbol, filled_amount, filled_price)
                    self.bot.db.record_held_coin(snapshot.symbol, filled_amount)
                    if self.bot.risk_manager:
                        self.bot.risk_manager.record_trade(snapshot.symbol)
                    logger.info(
                        "[Reconcile] Pending BUY %s filled while offline -> restored in_position %.8f @ %.2f",
                        snapshot.symbol,
                        filled_amount,
                        filled_price,
                    )
                else:
                    _, exit_price = self.bot._extract_history_fill_details(
                        hist,
                        fallback_amount=snapshot.filled_amount,
                        fallback_price=snapshot.exit_price or snapshot.entry_price,
                    )
                    completed = state_manager.complete_exit(
                        snapshot.symbol,
                        exit_price or snapshot.exit_price or snapshot.entry_price,
                    )
                    self.bot._report_completed_exit(
                        completed,
                        exit_price or snapshot.exit_price or snapshot.entry_price,
                        "reconcile",
                    )
                    logger.info(
                        "[Reconcile] Pending SELL %s filled while offline -> closed trade logged", snapshot.symbol
                    )
                handled_order_ids.add(tracked_order_id)
                continue

            if self.bot._history_status_is_cancelled(hist):
                if snapshot.state == TradeLifecycleState.PENDING_BUY:
                    state_manager.cancel_pending_buy(snapshot.symbol, "buy cancelled during downtime")
                    logger.info("[Reconcile] Pending BUY %s cancelled while offline", snapshot.symbol)
                else:
                    restore_position = {
                        "symbol": snapshot.symbol,
                        "side": OrderSide.BUY,
                        "amount": snapshot.filled_amount,
                        "entry_price": snapshot.entry_price,
                        "stop_loss": snapshot.stop_loss,
                        "take_profit": snapshot.take_profit,
                        "timestamp": snapshot.opened_at or datetime.now(),
                        "is_partial_fill": False,
                        "remaining_amount": 0.0,
                        "total_entry_cost": snapshot.total_entry_cost,
                        "filled": True,
                        "filled_amount": snapshot.filled_amount,
                        "filled_price": snapshot.entry_price,
                        "state_managed": True,
                    }
                    self.bot.executor.register_tracked_position(snapshot.entry_order_id, restore_position)
                    state_manager.restore_in_position(snapshot.symbol, "sell cancelled during downtime")
                    logger.info(
                        "[Reconcile] Pending SELL %s cancelled while offline -> restored in_position", snapshot.symbol
                    )
                handled_order_ids.add(tracked_order_id)

        return handled_order_ids

    def reconcile_open_orders_with_exchange(self, source: str = "startup") -> int:
        return reconcile_open_orders_with_exchange(self.bot, source=source)

    def reconcile_on_startup(self) -> None:
        logger.info("╔══════════════════════════════════════════════════════╗")
        logger.info("║  🔍 RECONCILIATION: Querying exchange for true state ║")
        logger.info("╚══════════════════════════════════════════════════════╝")

        reconciled_count = 0
        ghost_orders = []
        balances: Dict[str, Any] = {}

        try:
            try:
                balances = self.bot.api_client.get_balances() or {}
            except Exception as exc:
                logger.warning("[Reconcile] Failed to fetch balances for startup preservation checks: %s", exc)

            symbols_to_check = [p.upper() for p in self.bot._get_trading_pairs()]

            all_remote_orders = []
            for sym in symbols_to_check:
                try:
                    orders = self.bot.api_client.get_open_orders(sym)
                    if orders:
                        for order in orders:
                            order["_checked_symbol"] = sym
                        all_remote_orders.extend(orders)
                    time.sleep(0.2)
                except Exception as exc:
                    logger.warning("[Reconcile] Failed to get open orders for %s: %s", sym, exc, exc_info=True)

            logger.info("[Reconcile] Exchange reported %d open order(s)", len(all_remote_orders))

            remote_order_ids = set()
            for order in all_remote_orders:
                oid = str(order.get("id", ""))
                if not oid:
                    continue
                remote_order_ids.add(oid)

                typ = str(order.get("typ", order.get("side", ""))).lower()
                side_enum = OrderSide.BUY if typ in ("bid", "buy") else OrderSide.SELL

                raw_sym = str(order.get("sym") or "")
                raw_sym_upper = str(raw_sym or "").upper()
                if raw_sym_upper.endswith("_THB"):
                    local_sym = f"THB_{extract_base_asset(raw_sym_upper)}"
                else:
                    local_sym = str(raw_sym_upper or order.get("_checked_symbol") or self.bot.trading_pair).upper()

                entry_price = float(order.get("rate") or order.get("rat", 0) or 0.0)
                amount = float(order.get("amount") or order.get("amt", 0) or 0.0)
                remaining = float(order.get("unfilled") or order.get("rem", 0) or amount)

                existing = self.bot.executor._open_orders.get(oid)
                if existing:
                    with self.bot.executor._orders_lock:
                        self.bot.executor._open_orders[oid]["remaining_amount"] = remaining
                    logger.debug("[Reconcile] Order %s synced (local ↔ remote)", oid)
                else:
                    ghost_orders.append(
                        {
                            "order_id": oid,
                            "symbol": local_sym,
                            "side": side_enum,
                            "amount": amount,
                            "entry_price": entry_price,
                            "remaining": remaining,
                            "type": typ,
                        }
                    )

            imported_ghost_counts: Counter[tuple[str, str]] = Counter()
            skipped_ghost_counts: Counter[tuple[str, str]] = Counter()
            for ghost in ghost_orders:
                local_sym = str(ghost.get("symbol", "")).upper()
                side_value = ghost.get("side")
                side_str = normalize_side_value(side_value)
                amount = float(ghost.get("amount", 0.0) or 0.0)
                if local_sym in ("BTCUSDT", "THB_BTC", "BTC_THB") and side_str == "sell" and amount > 1.0:
                    skipped_ghost_counts[(local_sym, side_str)] += 1
                    logger.warning(
                        "[Reconcile] Skipping ghost order %s — amount=%.8f looks wrong for %s sell order (expected BTC < 1.0)",
                        ghost.get("order_id"),
                        amount,
                        local_sym,
                    )
                    continue

                if side_str == "sell":
                    has_matching_position = False
                    with self.bot.executor._orders_lock:
                        for existing in self.bot.executor._open_orders.values():
                            ex_sym = str(existing.get("symbol", "")).upper()
                            ex_side = normalize_side_value(existing.get("side"))
                            if ex_sym == local_sym and ex_side == "buy" and existing.get("filled"):
                                has_matching_position = True
                                break
                    if has_matching_position:
                        skipped_ghost_counts[(local_sym, side_str)] += 1
                        logger.info(
                            "[Reconcile] Ghost SELL %s for %s — skipping ghost import to preserve entry price (matched existing filled BUY position)",
                            ghost.get("order_id"),
                            local_sym,
                        )
                        continue

                oid = ghost["order_id"]
                logger.warning(
                    f"👻 [Ghost Order] {oid} found on exchange but NOT in local DB! "
                    f"Adding to tracking: {ghost['side'].value.upper()} {ghost['symbol']} "
                    f"{ghost['amount']:.8f} @ {ghost['entry_price']:,.2f}"
                )

                with self.bot.executor._orders_lock:
                    self.bot.executor._open_orders[oid] = {
                        "symbol": ghost["symbol"],
                        "side": ghost["side"],
                        "amount": ghost["amount"],
                        "entry_price": ghost["entry_price"],
                        "stop_loss": None,
                        "take_profit": None,
                        "order_id": oid,
                        "timestamp": datetime.now(),
                        "is_partial_fill": ghost["remaining"] < ghost["amount"],
                        "remaining_amount": ghost["remaining"],
                        "total_entry_cost": (
                            ghost["amount"]
                            if ghost["side"] == OrderSide.BUY
                            else ghost["entry_price"] * ghost["amount"]
                        ),
                        "filled": ghost["remaining"] < ghost["amount"],
                    }
                try:
                    self.bot.db.save_position(self.bot.executor._open_orders[oid])
                except Exception as exc:
                    logger.error("[Reconcile] Failed to persist ghost order %s: %s", oid, exc)
                imported_ghost_counts[(local_sym, side_str)] += 1
                reconciled_count += 1

            if imported_ghost_counts:
                summary = ", ".join(
                    f"{side.upper()} {symbol} x{count}"
                    for (symbol, side), count in sorted(imported_ghost_counts.items())
                )
                logger.warning("[Reconcile] Ghost orders imported summary: %s", summary)
            if skipped_ghost_counts:
                summary = ", ".join(
                    f"{side.upper()} {symbol} x{count}"
                    for (symbol, side), count in sorted(skipped_ghost_counts.items())
                )
                logger.warning("[Reconcile] Ghost orders skipped by sanity check: %s", summary)

            override = getattr(getattr(self.bot, "__dict__", {}), "get", lambda *_args, **_kwargs: None)(
                "_reconcile_pending_trade_states"
            )
            if callable(override):
                handled_order_ids = override(remote_order_ids)
            else:
                handled_order_ids = self.reconcile_pending_trade_states(remote_order_ids)

            local_order_ids = set(self.bot.executor._open_orders.keys()) - handled_order_ids
            vanished_ids = local_order_ids - remote_order_ids

            if vanished_ids:
                logger.info(
                    "[Reconcile] %d local order(s) not on exchange — checking if they were filled while bot was down",
                    len(vanished_ids),
                )

            for missing_oid in vanished_ids:
                local_pos = self.bot.executor._open_orders.get(missing_oid)
                if not local_pos:
                    continue

                sym = local_pos.get("symbol", self.bot.trading_pair)
                side_enum = local_pos.get("side", OrderSide.BUY)

                try:
                    history = self.bot.api_client.get_order_history(sym, limit=self.bot._order_history_window_limit())
                    matched = None
                    for row in history:
                        hist_id = str(row.get("id", ""))
                        if hist_id == missing_oid:
                            matched = row
                            break

                    if matched:
                        status_str = self.bot._history_status_value(matched)
                        if self.bot._history_status_is_filled(matched):
                            logger.info(
                                "✅ [Reconcile] Order %s was FILLED while bot was down. Status: %s",
                                missing_oid,
                                status_str,
                            )
                            side_val = normalize_side_value(side_enum)
                            if side_val == "buy":
                                fallback_cost = _coerce_trade_float(local_pos.get("total_entry_cost"))
                                filled_amount, filled_price = self.bot._extract_history_fill_details(
                                    matched,
                                    fallback_amount=_coerce_trade_float(local_pos.get("filled_amount"))
                                    or _coerce_trade_float(local_pos.get("amount")),
                                    fallback_price=_coerce_trade_float(local_pos.get("filled_price"))
                                    or _coerce_trade_float(local_pos.get("entry_price")),
                                    fallback_cost=fallback_cost,
                                )
                                if filled_amount > 0 and filled_price > 0:
                                    restored_position = dict(local_pos)
                                    restored_position.update(
                                        {
                                            "symbol": sym,
                                            "side": OrderSide.BUY,
                                            "amount": filled_amount,
                                            "entry_price": filled_price,
                                            "timestamp": local_pos.get("timestamp") or datetime.now(),
                                            "is_partial_fill": False,
                                            "remaining_amount": 0.0,
                                            "total_entry_cost": fallback_cost or (filled_amount * filled_price),
                                            "filled": True,
                                            "filled_amount": filled_amount,
                                            "filled_price": filled_price,
                                        }
                                    )
                                    self.bot.executor.register_tracked_position(missing_oid, restored_position)
                                    self.bot._log_filled_order(
                                        sym,
                                        "buy",
                                        filled_amount,
                                        filled_price,
                                        timestamp=local_pos.get("timestamp") or datetime.now(timezone.utc),
                                    )
                                    logger.info(
                                        "[Reconcile] Restored filled BUY %s as tracked position %.8f @ %.2f",
                                        missing_oid,
                                        filled_amount,
                                        filled_price,
                                    )
                                else:
                                    logger.warning(
                                        "[Reconcile] Filled BUY %s unresolved; leaving local tracking unchanged",
                                        missing_oid,
                                    )
                            else:
                                with self.bot.executor._orders_lock:
                                    self.bot.executor._open_orders.pop(missing_oid, None)
                                try:
                                    self.bot.db.delete_position(missing_oid)
                                except Exception as exc:
                                    logger.warning(
                                        "[Reconcile] Failed to delete DB position %s after fill: %s", missing_oid, exc
                                    )
                        elif self.bot._history_status_is_cancelled(matched):
                            logger.info(
                                "🗑️ [Reconcile] Order %s was CANCELLED on exchange. Removing from local tracking",
                                missing_oid,
                            )
                            with self.bot.executor._orders_lock:
                                self.bot.executor._open_orders.pop(missing_oid, None)
                            try:
                                self.bot.db.delete_position(missing_oid)
                            except Exception as exc:
                                logger.warning(
                                    "[Reconcile] Failed to delete cancelled DB position %s: %s", missing_oid, exc
                                )
                        else:
                            logger.warning(
                                "[Reconcile] Order %s has unusual status '%s' — keeping in local tracking for now",
                                missing_oid,
                                status_str,
                            )
                    else:
                        if self.bot._preserve_bootstrap_position_from_balances(missing_oid, local_pos, balances):
                            continue

                        logger.warning(
                            "[Reconcile] Order %s not found on exchange or history. Removing from local tracking (likely stale)",
                            missing_oid,
                        )
                        with self.bot.executor._orders_lock:
                            self.bot.executor._open_orders.pop(missing_oid, None)
                        try:
                            self.bot.db.delete_position(missing_oid)
                        except Exception as exc:
                            logger.warning("[Reconcile] Failed to delete stale DB position %s: %s", missing_oid, exc)

                except Exception as exc:
                    logger.error("[Reconcile] Failed to check history for %s: %s", missing_oid, exc)

            final_count = len(self.bot.executor._open_orders)
            logger.info(
                "╔══════════════════════════════════════════════════════╗\n"
                "║  ✅ RECONCILIATION COMPLETE                        ║\n"
                f"║     Ghost orders added:  {reconciled_count}\n"
                f"║     Orders removed:      {len(vanished_ids) if vanished_ids else 0}\n"
                f"║     Active positions:    {final_count}\n"
                "╚══════════════════════════════════════════════════════╝"
            )
        except Exception as exc:
            logger.error("[Reconcile] Reconciliation failed: %s", exc, exc_info=True)
            logger.warning("[Reconcile] Proceeding with local state only — may have stale data!")
