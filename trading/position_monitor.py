from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from trade_executor import OrderSide
from state_management import TradeLifecycleState
from trading.cost_basis import resolve_sane_entry_cost


logger = logging.getLogger(__name__)


def _resolve_sane_entry_cost(
    *,
    symbol: str,
    amount: float,
    entry_price: float,
    reported_entry_cost: float,
) -> float:
    """Backward-compatible wrapper for shared cost-basis guard logic."""
    return resolve_sane_entry_cost(
        symbol=symbol,
        amount=amount,
        entry_price=entry_price,
        reported_entry_cost=reported_entry_cost,
    )


def _coerce_trade_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _coerce_trade_datetime(val: Any) -> Optional[datetime]:
    if isinstance(val, datetime):
        return val
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val))
    except (TypeError, ValueError):
        return None


class PositionMonitorHelper:
    def __init__(
        self,
        bot: Any,
        *,
        websocket_available: bool,
        price_tick_available: bool,
        latest_ticker_getter: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.bot = bot
        self.websocket_available = bool(websocket_available)
        self.price_tick_available = bool(price_tick_available)
        self.latest_ticker_getter = latest_ticker_getter if callable(latest_ticker_getter) else None

    def on_ws_tick(self, tick: Any) -> None:
        logger.debug(
            f"[WS] {tick.symbol} last={tick.last:,.0f} "
            f"bid={tick.bid:,.0f} ask={tick.ask:,.0f} "
            f"change={tick.percent_change_24h:+.2f}%"
        )
        override = getattr(self.bot, "__dict__", {}).get("_check_sl_tp_immediate")
        if callable(override):
            override(tick)
            return
        self.check_sl_tp_immediate(tick)

    def check_sl_tp_immediate(self, tick: Any) -> None:
        if not self.websocket_available or not self.price_tick_available:
            return

        try:
            open_orders = self.bot.executor.get_open_orders()
        except Exception:
            return

        for pos in open_orders:
            pos_symbol = pos.get("symbol", self.bot.trading_pair)
            if str(pos_symbol).upper() != str(tick.symbol).upper():
                continue

            if self.bot._state_machine_enabled:
                lifecycle = self.bot._state_manager.get_state(pos_symbol)
                if lifecycle.state != TradeLifecycleState.IN_POSITION:
                    continue

            position_id = pos.get("order_id", "")
            entry_price = _coerce_trade_float(pos.get("entry_price"), 0.0)
            stop_loss = _coerce_trade_float(pos.get("stop_loss"), 0.0)
            take_profit = _coerce_trade_float(pos.get("take_profit"), 0.0)
            side = pos.get("side", OrderSide.BUY)
            amount = _coerce_trade_float(pos.get("amount"), 0.0)

            if not entry_price:
                continue

            triggered = None
            current_price = tick.last
            opened_at = pos.get("timestamp")

            if side == OrderSide.BUY:
                if stop_loss > 0 and current_price <= stop_loss:
                    if self.bot._is_sl_hold_locked(position_id):
                        logger.info("[SLHoldGuard] Suppressing WS SL for %s", position_id)
                    else:
                        triggered = "SL"
                if not triggered:
                    roi_hit, _roi_reason = self.bot._minimal_roi_exit_signal(
                        symbol=pos_symbol,
                        side=side,
                        entry_price=entry_price,
                        current_price=current_price,
                        opened_at=opened_at,
                    )
                    if roi_hit:
                        triggered = "MINIMAL_ROI"
                if not triggered and take_profit > 0 and current_price >= take_profit:
                    triggered = "TP"
            else:
                if stop_loss > 0 and current_price >= stop_loss:
                    if self.bot._is_sl_hold_locked(position_id):
                        logger.info("[SLHoldGuard] Suppressing WS SL for %s", position_id)
                    else:
                        triggered = "SL"
                if not triggered:
                    roi_hit, _roi_reason = self.bot._minimal_roi_exit_signal(
                        symbol=pos_symbol,
                        side=side,
                        entry_price=entry_price,
                        current_price=current_price,
                        opened_at=opened_at,
                    )
                    if roi_hit:
                        triggered = "MINIMAL_ROI"
                if not triggered and take_profit > 0 and current_price <= take_profit:
                    triggered = "TP"

            if not triggered:
                continue

            logger.info(
                f"[WS-SLTP] {triggered} triggered for position {position_id} | "
                f"Entry={entry_price:,.0f} Current={current_price:,.0f} | "
                f"SL={stop_loss:,.0f} TP={take_profit:,.0f}"
            )

            with self.bot._ws_sltp_inflight_lock:
                if position_id in self.bot._ws_sltp_inflight:
                    logger.debug(f"[WS-SLTP] Exit thread already in-flight for {position_id} — skipping duplicate")
                    continue
                self.bot._ws_sltp_inflight.add(position_id)

            total_entry_cost = pos.get("total_entry_cost", entry_price * amount)
            try:
                threading.Thread(
                    target=self.bot._ws_sltp_exit_wrapper,
                    args=(position_id, pos_symbol, side, amount, current_price, triggered, entry_price, total_entry_cost),
                    daemon=True,
                ).start()
            except Exception as exc:
                logger.error(f"Failed to fire SL/TP exit thread: {exc}", exc_info=True)
                with self.bot._ws_sltp_inflight_lock:
                    self.bot._ws_sltp_inflight.discard(position_id)

    def ws_sltp_exit_wrapper(
        self,
        position_id: str,
        symbol: str,
        side: OrderSide,
        amount: float,
        current_price: float,
        triggered: str,
        entry_price: float,
        total_entry_cost: float = 0.0,
    ) -> None:
        try:
            self.bot._execute_ws_sl_tp_exit(
                position_id,
                symbol,
                side,
                amount,
                current_price,
                triggered,
                entry_price,
                total_entry_cost,
            )
        finally:
            with self.bot._ws_sltp_inflight_lock:
                self.bot._ws_sltp_inflight.discard(position_id)

    def execute_ws_sl_tp_exit(
        self,
        position_id: str,
        symbol: str,
        side: OrderSide,
        amount: float,
        current_price: float,
        triggered: str,
        entry_price: float,
        total_entry_cost: float = 0.0,
    ) -> None:
        if hasattr(self.bot.api_client, "is_circuit_open") and self.bot.api_client.is_circuit_open():
            logger.warning(
                f"[WS-SLTP] Circuit breaker OPEN — blocking {triggered} exit for "
                f"position {position_id} ({symbol}) to prevent rate limit death spiral"
            )
            return

        try:
            if self.bot._state_machine_enabled:
                self.bot._submit_managed_exit(
                    position_id=position_id,
                    pos_symbol=symbol,
                    side=side,
                    amount=amount,
                    exit_price=current_price,
                    triggered=triggered,
                    entry_price=entry_price,
                    total_entry_cost=total_entry_cost,
                    price_source="ws",
                    opened_at=None,
                )
                return

            exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            result = self.bot.executor.execute_exit(
                position_id=position_id,
                order_id=position_id,
                side=exit_side,
                amount=amount,
                price=current_price,
                exit_trigger=triggered,
            )
            if result.success:
                from trade_executor import BITKUB_FEE_PCT

                now = datetime.now()
                entry_cost = _resolve_sane_entry_cost(
                    symbol=symbol,
                    amount=amount,
                    entry_price=entry_price,
                    reported_entry_cost=total_entry_cost,
                )
                entry_fee = entry_cost * BITKUB_FEE_PCT
                gross_exit = current_price * amount
                exit_fee = gross_exit * BITKUB_FEE_PCT
                net_exit = gross_exit - exit_fee
                total_fees = entry_fee + exit_fee
                net_pnl = net_exit - entry_cost - entry_fee
                net_pnl_pct = (net_pnl / entry_cost * 100) if entry_cost > 0 else 0
                trigger_label = "Take Profit" if triggered == "TP" else ("Stop Loss" if triggered == "SL" else (triggered or "Exit"))

                try:
                    self.bot.db.log_closed_trade({
                        "symbol": symbol,
                        "side": side,
                        "amount": amount,
                        "entry_price": entry_price,
                        "exit_price": current_price,
                        "entry_cost": entry_cost,
                        "gross_exit": gross_exit,
                        "entry_fee": entry_fee,
                        "exit_fee": exit_fee,
                        "total_fees": total_fees,
                        "net_pnl": net_pnl,
                        "net_pnl_pct": net_pnl_pct,
                        "trigger": triggered,
                        "price_source": "ws",
                        "closed_at": now,
                    })
                except Exception as exc:
                    logger.error(f"[WS-SLTP] Failed to log closed trade: {exc}", exc_info=True)

                msg = self.bot._format_exit_alert(
                    symbol,
                    trigger_label,
                    amount,
                    entry_price,
                    current_price,
                    entry_cost,
                    gross_exit,
                    net_pnl,
                    net_pnl_pct,
                    total_fees,
                    now=now,
                )
                self.bot._send_alert(msg, to_telegram=True)
                self.bot._cleanup_sl_hold_entry(position_id)
            else:
                logger.error(f"[WS-SLTP] Exit failed: {result.message}")
        except Exception as exc:
            logger.error(f"[WS-SLTP] Exit error: {exc}", exc_info=True)

    def check_positions_for_sl_tp(self) -> None:
        open_orders = self.bot.executor.get_open_orders()
        if not open_orders:
            return

        price_cache: Dict[str, tuple[Any, str]] = {}

        def get_price(symbol: str) -> tuple[Any, str]:
            if symbol in price_cache:
                return price_cache[symbol]
            price, source = None, "none"
            ws_client = getattr(self.bot, "_ws_client", None)
            if ws_client and ws_client.is_connected() and self.websocket_available and self.latest_ticker_getter:
                tick = self.latest_ticker_getter(symbol)
                if tick:
                    price, source = tick.last, "ws"
            if price is None:
                try:
                    ticker = self.bot.api_client.get_ticker(symbol)
                    if isinstance(ticker, dict):
                        price = float(ticker.get("last", ticker.get("close", 0)))
                    source = "rest"
                except Exception as exc:
                    logger.warning(f"Could not get price for {symbol}: {exc}")
            price_cache[symbol] = (price, source)
            return price, source

        for pos in open_orders:
            position_id = pos.get("order_id", "")
            entry_price = _coerce_trade_float(pos.get("entry_price"), 0.0)
            stop_loss = _coerce_trade_float(pos.get("stop_loss"), 0.0)
            take_profit = _coerce_trade_float(pos.get("take_profit"), 0.0)
            raw_side = pos.get("side", OrderSide.BUY)
            # Normalise to enum so "buy"/"sell" strings compare correctly.
            if isinstance(raw_side, str):
                side = OrderSide.SELL if raw_side.lower() == "sell" else OrderSide.BUY
            else:
                side = raw_side
            amount = _coerce_trade_float(pos.get("amount"), 0.0)
            pos_symbol = pos.get("symbol", self.bot.trading_pair)

            if self.bot._state_machine_enabled:
                lifecycle = self.bot._state_manager.get_state(pos_symbol)
                if lifecycle.state != TradeLifecycleState.IN_POSITION:
                    continue

            if not entry_price:
                continue

            current_price, price_source = get_price(pos_symbol)
            if current_price is None or current_price == 0:
                continue

            triggered = None
            opened_at = _coerce_trade_datetime(pos.get("timestamp"))

            # Exit priority: SL > MinimalROI > regular TP > time-stop.
            if side == OrderSide.BUY:
                if stop_loss > 0 and current_price <= stop_loss:
                    if self.bot._is_sl_hold_locked(position_id):
                        logger.info("[SLHoldGuard] Suppressing REST SL for %s", position_id)
                    else:
                        triggered = "SL"
            else:
                if stop_loss > 0 and current_price >= stop_loss:
                    if self.bot._is_sl_hold_locked(position_id):
                        logger.info("[SLHoldGuard] Suppressing REST SL for %s", position_id)
                    else:
                        triggered = "SL"

            if not triggered:
                roi_hit, _roi_reason = self.bot._minimal_roi_exit_signal(
                    symbol=pos_symbol,
                    side=side,
                    entry_price=entry_price,
                    current_price=float(current_price),
                    opened_at=opened_at,
                )
                if roi_hit:
                    triggered = "MINIMAL_ROI"

            if not triggered and side == OrderSide.BUY and take_profit > 0 and current_price >= take_profit:
                triggered = "TP"
            elif not triggered and side != OrderSide.BUY and take_profit > 0 and current_price <= take_profit:
                triggered = "TP"

            if not triggered and self.bot._scalping_mode_enabled:
                is_bootstrap = str(position_id).startswith("bootstrap_")
                timeout_minutes = getattr(self.bot, "_bootstrap_position_timeout_minutes", None) if is_bootstrap else getattr(self.bot, "_scalping_position_timeout_minutes", None)
                if opened_at is not None and timeout_minutes and float(timeout_minutes) > 0:
                    hold_seconds = (datetime.now() - opened_at).total_seconds()
                    if hold_seconds >= (float(timeout_minutes) * 60):
                        triggered = "TIME"

            if not triggered:
                continue

            if triggered == "TIME" and not self.bot._should_allow_voluntary_exit(
                symbol=pos_symbol,
                trigger=triggered,
                entry_price=entry_price,
                exit_price=float(current_price),
                amount=amount,
                total_entry_cost=pos.get("total_entry_cost", entry_price * amount),
                side=side,
            ):
                continue

            logger.info(
                f"[{price_source.upper()}-SLTP] {triggered} triggered for "
                f"position {position_id} ({pos_symbol}) | Entry: {entry_price:,.0f} | "
                f"Current: {current_price:,.0f} | SL: {stop_loss:,.0f} | "
                f"TP: {take_profit:,.0f}"
            )

            exit_price = current_price
            if self.bot._state_machine_enabled:
                self.bot._submit_managed_exit(
                    position_id=position_id,
                    pos_symbol=pos_symbol,
                    side=side,
                    amount=amount,
                    exit_price=exit_price,
                    triggered=triggered,
                    entry_price=entry_price,
                    total_entry_cost=pos.get("total_entry_cost", entry_price * amount),
                    price_source=price_source,
                    opened_at=pos.get("timestamp"),
                )
                continue

            exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            result = self.bot.executor.execute_exit(
                position_id=position_id,
                order_id=position_id,
                side=exit_side,
                amount=amount,
                price=exit_price,
                exit_trigger=triggered,
            )

            if result.success:
                from trade_executor import BITKUB_FEE_PCT

                now = datetime.now()
                entry_cost = _resolve_sane_entry_cost(
                    symbol=pos_symbol,
                    amount=amount,
                    entry_price=entry_price,
                    reported_entry_cost=_coerce_trade_float(pos.get("total_entry_cost"), 0.0),
                )
                entry_fee = entry_cost * BITKUB_FEE_PCT
                gross_exit = exit_price * amount
                exit_fee = gross_exit * BITKUB_FEE_PCT
                net_exit = gross_exit - exit_fee
                total_fees = entry_fee + exit_fee
                net_pnl = net_exit - entry_cost - entry_fee
                net_pnl_pct = (net_pnl / entry_cost * 100) if entry_cost > 0 else 0
                trigger_label = "Take Profit" if triggered == "TP" else ("Stop Loss" if triggered == "SL" else (triggered or "Exit"))

                try:
                    self.bot.db.log_closed_trade({
                        "symbol": pos_symbol,
                        "side": side,
                        "amount": amount,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "entry_cost": entry_cost,
                        "gross_exit": gross_exit,
                        "entry_fee": entry_fee,
                        "exit_fee": exit_fee,
                        "total_fees": total_fees,
                        "net_pnl": net_pnl,
                        "net_pnl_pct": net_pnl_pct,
                        "trigger": triggered,
                        "price_source": price_source,
                        "opened_at": pos.get("timestamp"),
                        "closed_at": now,
                    })
                except Exception as exc:
                    logger.error(f"Failed to log closed trade: {exc}", exc_info=True)

                msg = self.bot._format_exit_alert(
                    pos_symbol,
                    trigger_label,
                    amount,
                    entry_price,
                    exit_price,
                    entry_cost,
                    gross_exit,
                    net_pnl,
                    net_pnl_pct,
                    total_fees,
                    now=now,
                )
                self.bot._send_alert(msg, to_telegram=True)
                self.bot._cleanup_sl_hold_entry(position_id)
            else:
                logger.error(f"Failed to execute {triggered} exit: {result.message}")