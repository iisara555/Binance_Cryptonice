from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from state_management import TradeLifecycleState, TradeStateSnapshot, normalize_buy_quantity
from trade_executor import OrderResult, OrderSide, OrderStatus
from trading.cost_basis import resolve_sane_entry_cost
from trading.orchestrator import TradeDecision

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


class ManagedLifecycleHelper:
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @staticmethod
    def resolve_fill_amount(
        snapshot: TradeStateSnapshot,
        result: OrderResult,
        fallback_price: float,
    ) -> tuple[float, float]:
        fill_price = float(result.filled_price or fallback_price or snapshot.entry_price or snapshot.exit_price or 0.0)
        fill_amount = float(result.filled_amount or 0.0)
        if fill_amount > 0 and snapshot.state == TradeLifecycleState.PENDING_BUY:
            total_entry_cost = float(snapshot.total_entry_cost or result.ordered_amount or 0.0)
            normalized_fill_amount = normalize_buy_quantity(fill_amount, fill_price, total_entry_cost)
            if normalized_fill_amount != fill_amount:
                return normalized_fill_amount, fill_price

        if fill_amount > 0:
            return fill_amount, fill_price

        if snapshot.state == TradeLifecycleState.PENDING_BUY:
            if fill_price > 0 and snapshot.total_entry_cost > 0:
                return snapshot.total_entry_cost / fill_price, fill_price
            return 0.0, fill_price

        return float(snapshot.filled_amount or 0.0), fill_price

    def register_filled_position_from_state(
        self,
        snapshot: TradeStateSnapshot,
        filled_amount: float,
        filled_price: float,
    ) -> None:
        pos_data = {
            "symbol": snapshot.symbol,
            "side": OrderSide.BUY,
            "amount": filled_amount,
            "entry_price": filled_price,
            "stop_loss": snapshot.stop_loss,
            "take_profit": snapshot.take_profit,
            "timestamp": snapshot.opened_at or datetime.now(),
            "is_partial_fill": False,
            "remaining_amount": filled_amount,
            "total_entry_cost": snapshot.total_entry_cost,
            "filled": True,
            "filled_amount": filled_amount,
            "filled_price": filled_price,
            "state_managed": True,
        }
        self.bot.executor.register_tracked_position(snapshot.entry_order_id, pos_data)
        self.bot._register_sl_hold_entry(snapshot.entry_order_id)
        self.bot._log_filled_order(
            snapshot.symbol,
            "buy",
            filled_amount,
            filled_price,
            timestamp=snapshot.opened_at or datetime.now(timezone.utc),
        )

    def report_completed_exit(
        self,
        snapshot: TradeStateSnapshot,
        exit_price: float,
        price_source: str,
    ) -> None:
        from trade_executor import BITKUB_FEE_PCT

        amount = float(snapshot.filled_amount or 0.0)
        if amount <= 0:
            logger.warning("[State] Exit report skipped for %s: filled amount is 0", snapshot.symbol)
            return

        entry_cost = _resolve_sane_entry_cost(
            symbol=snapshot.symbol,
            amount=amount,
            entry_price=float(snapshot.entry_price or 0.0),
            reported_entry_cost=float(snapshot.total_entry_cost or 0.0),
        )
        entry_fee = entry_cost * BITKUB_FEE_PCT
        gross_exit = exit_price * amount
        exit_fee = gross_exit * BITKUB_FEE_PCT
        net_exit = gross_exit - exit_fee
        total_fees = entry_fee + exit_fee
        net_pnl = net_exit - entry_cost - entry_fee
        net_pnl_pct = (net_pnl / entry_cost * 100) if entry_cost > 0 else 0.0
        now = datetime.now()

        self.bot._log_filled_order(
            snapshot.symbol,
            "sell",
            amount,
            exit_price,
            fee=exit_fee,
            timestamp=now,
        )

        try:
            self.bot.db.log_closed_trade(
                {
                    "symbol": snapshot.symbol,
                    "side": "buy",
                    "amount": amount,
                    "entry_price": snapshot.entry_price,
                    "exit_price": exit_price,
                    "entry_cost": entry_cost,
                    "gross_exit": gross_exit,
                    "entry_fee": entry_fee,
                    "exit_fee": exit_fee,
                    "total_fees": total_fees,
                    "net_pnl": net_pnl,
                    "net_pnl_pct": net_pnl_pct,
                    "trigger": snapshot.trigger,
                    "price_source": price_source,
                    "opened_at": snapshot.opened_at,
                    "closed_at": now,
                }
            )
        except Exception as exc:
            logger.error("[State] Failed to log closed trade for %s: %s", snapshot.symbol, exc, exc_info=True)

        trigger_label = (
            "Take Profit"
            if snapshot.trigger == "TP"
            else ("Stop Loss" if snapshot.trigger == "SL" else (snapshot.trigger or "Exit"))
        )
        msg = self.bot._format_exit_alert(
            snapshot.symbol,
            trigger_label,
            amount,
            snapshot.entry_price,
            exit_price,
            entry_cost,
            gross_exit,
            net_pnl,
            net_pnl_pct,
            total_fees,
            now=now,
        )
        self.bot._send_alert(msg, to_telegram=True)
        self.bot._log_position_trace(
            "EXIT_FILLED",
            snapshot.symbol,
            entry_order_id=snapshot.entry_order_id,
            exit_order_id=snapshot.exit_order_id,
            amount=amount,
            entry_price=snapshot.entry_price,
            exit_price=exit_price,
            stop_loss=snapshot.stop_loss,
            take_profit=snapshot.take_profit,
            trigger=snapshot.trigger,
            notes=f"price_source={price_source};net_pnl_pct={net_pnl_pct:+.2f}%",
        )
        guard = getattr(self.bot, "_pair_loss_guard", None)
        if guard is not None and hasattr(guard, "record_closed_pnl"):
            try:
                guard.record_closed_pnl(str(snapshot.symbol), float(net_pnl))
            except Exception as exc:
                logger.debug("Pair loss streak guard record skipped: %s", exc)
        risk_manager = getattr(self.bot, "risk_manager", None)
        if risk_manager is not None:
            record_trade_activity = getattr(risk_manager, "record_trade_activity", None)
            if callable(record_trade_activity):
                record_trade_activity()
        self.bot._cleanup_sl_hold_entry(snapshot.entry_order_id)

        if str(snapshot.trigger or "").upper() == "TIME":
            state_manager = getattr(self.bot, "_state_manager", None)
            cooldown_minutes = float(getattr(getattr(risk_manager, "config", None), "cool_down_minutes", 0) or 0)
            if state_manager is not None and cooldown_minutes > 0:
                state_manager.block_new_entries_after_exit(
                    snapshot.symbol,
                    duration_seconds=cooldown_minutes * 60.0,
                    trigger="TIME",
                    blocked_at=now,
                )

    def submit_managed_entry(self, decision: TradeDecision, portfolio: Dict[str, Any]) -> None:
        result = self.bot.executor.execute_entry(
            decision.plan,
            self.bot._get_risk_portfolio_value(portfolio),
            defer_position_tracking=True,
        )
        if not result.success or not result.order_id:
            decision.status = "failed"
            logger.error("Trade execution failed: %s", result.message)
            return

        self.bot._remember_consumed_signal_trigger(decision.signal)

        snapshot = self.bot._state_manager.start_pending_buy(
            decision.plan.symbol,
            decision.plan,
            result,
            signal_source=self.bot.signal_source.value,
        )
        if result.status == OrderStatus.FILLED and result.filled_amount > 0:
            filled_price = float(result.filled_price or decision.plan.entry_price or 0.0)
            self.bot._register_filled_position_from_state(snapshot, result.filled_amount, filled_price)
            snapshot = self.bot._state_manager.mark_entry_filled(
                decision.plan.symbol,
                result.filled_amount,
                filled_price,
            )
            self.bot.db.record_held_coin(decision.plan.symbol, result.filled_amount)
            if self.bot.risk_manager:
                self.bot.risk_manager.record_trade()
        decision.status = snapshot.state.value
        self.bot._executed_today.append(
            {
                "decision": decision,
                "result": result,
                "timestamp": datetime.now(),
            }
        )
        self.bot._log_position_trace(
            "ENTRY_SUBMITTED",
            decision.plan.symbol,
            entry_order_id=snapshot.entry_order_id or result.order_id or "",
            amount=float(result.filled_amount or snapshot.filled_amount or snapshot.requested_amount or 0.0),
            entry_price=float(result.filled_price or decision.plan.entry_price or snapshot.entry_price or 0.0),
            stop_loss=float(decision.plan.stop_loss or snapshot.stop_loss or 0.0),
            take_profit=float(decision.plan.take_profit or snapshot.take_profit or 0.0),
            notes=f"state={snapshot.state.value};status={result.status.value}",
        )
        coin = self.bot._format_coin_symbol(decision.plan.symbol)
        quote = self.bot._status_helper.quote_asset() if hasattr(self.bot, "_status_helper") else "USDT"
        msg = self.bot._format_alert_block(
            f"📥 <b>ส่งคำสั่งซื้อ</b>  {coin}",
            [
                f"ราคา  <code>{decision.plan.entry_price:,.0f}</code> {quote}  ({decision.plan.confidence:.0%})",
                "ขนาดไม้จะคำนวณตามยอดคงเหลือ/ความเสี่ยงตอนส่งออเดอร์",
                f"SL <code>{decision.plan.stop_loss:,.0f}</code>  TP <code>{decision.plan.take_profit:,.0f}</code>",
            ],
        )
        self.bot._send_alert(msg, to_telegram=True)

    def submit_managed_exit(
        self,
        position_id: str,
        pos_symbol: str,
        side: OrderSide,
        amount: float,
        exit_price: float,
        triggered: str,
        entry_price: float,
        total_entry_cost: float,
        price_source: str,
        opened_at: Optional[datetime],
    ) -> bool:
        if not self.bot._state_machine_enabled:
            return False

        snapshot = self.bot._state_manager.get_state(pos_symbol)
        if snapshot.state != TradeLifecycleState.IN_POSITION:
            logger.debug(
                "[State] Skip managed exit for %s because lifecycle=%s",
                pos_symbol,
                snapshot.state.value,
            )
            return False

        exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY

        # ── Pre-check: detect dust before hitting the API ──
        _raw_min = getattr(self.bot.executor, "_min_order_thb", None)
        min_order_quote = float(_raw_min) if isinstance(_raw_min, (int, float)) else 15.0
        order_value_quote = amount * exit_price
        if order_value_quote < min_order_quote:
            logger.warning(
                "[Dust] Skipping %s exit for %s — value %.2f quote < min %.0f quote. Force-closing as dust.",
                triggered,
                pos_symbol,
                order_value_quote,
                min_order_quote,
            )
            self.bot.executor.remove_tracked_position(position_id)
            self.bot._cleanup_sl_hold_entry(position_id)
            self.bot._state_manager._drop(pos_symbol)
            try:
                self.bot.db.log_closed_trade(
                    {
                        "symbol": pos_symbol,
                        "side": side,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "amount": amount,
                        "realized_pnl": -(total_entry_cost),
                        "pnl_pct": -100.0,
                        "trigger": "DUST",
                        "status": "dust_closed",
                    }
                )
            except Exception as db_exc:
                logger.warning("[Dust] DB log failed for %s: %s", pos_symbol, db_exc)
            return True

        result = self.bot.executor.execute_exit(
            position_id=position_id,
            order_id=position_id,
            side=exit_side,
            amount=amount,
            price=exit_price,
            defer_cleanup=True,
            exit_trigger=triggered,
        )
        if not result.success or not result.order_id:
            error_code = getattr(result, "error_code", None)
            # Fallback: parse error code from message like "[15] Amount too low"
            if error_code is None and result.message:
                import re

                _m = re.search(r"\[(15|18)\]", result.message)
                if _m:
                    error_code = int(_m.group(1))
            # Permanent failures like "Amount too low" (15) or "Order value below minimum" (18)
            # should force-close the position as dust to prevent infinite retry loops.
            if error_code in (15, 18):
                logger.warning(
                    "[Dust] Force-closing %s position %s — amount too low to sell (error %s: %s)",
                    pos_symbol,
                    position_id,
                    error_code,
                    result.message,
                )
                self.bot.executor.remove_tracked_position(position_id)
                self.bot._cleanup_sl_hold_entry(position_id)
                self.bot._state_manager._drop(pos_symbol)
                try:
                    self.bot.db.log_closed_trade(
                        {
                            "symbol": pos_symbol,
                            "side": side,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "amount": amount,
                            "realized_pnl": -(entry_price * amount),
                            "pnl_pct": -100.0,
                            "trigger": "DUST",
                            "status": "dust_closed",
                        }
                    )
                except Exception as db_exc:
                    logger.warning("[Dust] DB log failed for %s: %s", pos_symbol, db_exc)
                return True  # Return True to prevent retry
            logger.error("Failed to submit %s exit for %s: %s", triggered, pos_symbol, result.message)
            return False

        position_data = {
            "order_id": position_id,
            "symbol": pos_symbol,
            "amount": amount,
            "entry_price": entry_price,
            "stop_loss": snapshot.stop_loss,
            "take_profit": snapshot.take_profit,
            "timestamp": opened_at or datetime.now(),
            "total_entry_cost": total_entry_cost,
        }
        self.bot.executor.remove_tracked_position(position_id)
        pending = self.bot._state_manager.start_pending_sell(
            pos_symbol,
            position_data,
            exit_order_id=result.order_id,
            trigger=triggered,
            exit_price=exit_price,
            notes=f"price_source={price_source}",
        )
        self.bot._log_position_trace(
            "EXIT_SUBMITTED",
            pos_symbol,
            entry_order_id=snapshot.entry_order_id or position_id,
            exit_order_id=result.order_id or "",
            amount=amount,
            entry_price=entry_price,
            exit_price=exit_price,
            stop_loss=snapshot.stop_loss,
            take_profit=snapshot.take_profit,
            trigger=triggered,
            notes=f"state={pending.state.value};price_source={price_source}",
        )

        if result.status == OrderStatus.FILLED:
            completed = self.bot._state_manager.complete_exit(
                pos_symbol,
                exit_price=float(result.filled_price or exit_price or 0.0),
            )
            self.bot._report_completed_exit(completed, float(result.filled_price or exit_price or 0.0), price_source)
            return True

        logger.info(
            "[State] %s -> %s | exit order submitted %s for %s",
            pos_symbol,
            pending.state.value,
            result.order_id,
            triggered,
        )
        return True

    def advance_managed_trade_states(self) -> None:
        if not self.bot._state_machine_enabled:
            return

        self.bot._state_manager.sync_in_position_states(self.bot.executor.get_open_orders())
        for snapshot in list(self.bot._state_manager.list_active_states()):
            try:
                if snapshot.state == TradeLifecycleState.PENDING_BUY:
                    status = self.bot.executor.check_order_status(
                        snapshot.entry_order_id,
                        symbol=snapshot.symbol,
                        side="buy",
                    )
                    if status.status == OrderStatus.ERROR:
                        hist = self.bot._lookup_order_history_status(snapshot.symbol, snapshot.entry_order_id)
                        hist_status = str((hist or {}).get("status") or "").lower()
                        if hist_status in ("filled", "match", "done", "complete"):
                            status = OrderResult(
                                success=True,
                                status=OrderStatus.FILLED,
                                order_id=snapshot.entry_order_id,
                                filled_price=(hist or {}).get("rate"),
                            )

                    if status.status == OrderStatus.FILLED:
                        filled_amount, filled_price = self.bot._resolve_fill_amount(
                            snapshot, status, snapshot.entry_price
                        )
                        if filled_amount <= 0 or filled_price <= 0:
                            logger.warning("[State] Filled BUY for %s but amount/price unresolved", snapshot.symbol)
                            continue
                        self.bot._register_filled_position_from_state(snapshot, filled_amount, filled_price)
                        self.bot._state_manager.mark_entry_filled(snapshot.symbol, filled_amount, filled_price)
                        self.bot.db.record_held_coin(snapshot.symbol, filled_amount)
                        if self.bot.risk_manager:
                            self.bot.risk_manager.record_trade()
                        logger.info(
                            "[State] %s -> in_position | order=%s amount=%.8f @ %.2f",
                            snapshot.symbol,
                            snapshot.entry_order_id,
                            filled_amount,
                            filled_price,
                        )
                        if self.bot.send_alerts:
                            quote = (
                                self.bot._status_helper.quote_asset() if hasattr(self.bot, "_status_helper") else "USDT"
                            )
                            coin = self.bot._format_coin_symbol(snapshot.symbol)
                            msg = self.bot._format_alert_block(
                                f"✅ <b>ซื้อสำเร็จ (Filled)</b>  {coin}",
                                [
                                    f"ได้ของจำนวน: <code>{filled_amount:.6f}</code>",
                                    f"ที่ราคา: <code>{filled_price:,.2f}</code> {quote}",
                                    f"มูลค่าไม้: <code>{filled_amount * filled_price:,.2f}</code> {quote}",
                                ],
                            )
                            self.bot._send_alert(msg, to_telegram=True)
                        continue

                    if self.bot._state_manager.is_timed_out(snapshot):
                        cancelled = self.bot.executor.cancel_order(
                            snapshot.entry_order_id, symbol=snapshot.symbol, side="buy"
                        )
                        stale_fill = getattr(self.bot.executor, "_oms_cancel_was_error_21", False)
                        self.bot.executor._oms_cancel_was_error_21 = False
                        if stale_fill:
                            fallback = OrderResult(
                                success=True,
                                status=OrderStatus.FILLED,
                                order_id=snapshot.entry_order_id,
                                filled_price=snapshot.entry_price,
                            )
                            filled_amount, filled_price = self.bot._resolve_fill_amount(
                                snapshot, fallback, snapshot.entry_price
                            )
                            if filled_amount > 0 and filled_price > 0:
                                self.bot._register_filled_position_from_state(snapshot, filled_amount, filled_price)
                                self.bot._state_manager.mark_entry_filled(snapshot.symbol, filled_amount, filled_price)
                                self.bot.db.record_held_coin(snapshot.symbol, filled_amount)
                                if self.bot.risk_manager:
                                    self.bot.risk_manager.record_trade()
                            continue
                        if cancelled:
                            self.bot._state_manager.cancel_pending_buy(snapshot.symbol, "buy timeout cancel")
                            logger.info("[State] %s -> idle | pending buy timed out", snapshot.symbol)
                            if self.bot.send_alerts:
                                msg = self.bot._format_alert_block(
                                    f"⏰ <b>ยกเลิกคำสั่งซื้อ (ตกรถ)</b>  {self.bot._format_coin_symbol(snapshot.symbol)}",
                                    ["ออเดอร์ Limit ไม่ถูกจับคู่ภายในเวลาที่กำหนด", "ดึงออเดอร์กลับและรอสัญญาณใหม่"],
                                )
                                self.bot._send_alert(msg, to_telegram=True)
                        else:
                            logger.warning("[State] Failed to cancel timed-out buy for %s", snapshot.symbol)

                elif snapshot.state == TradeLifecycleState.PENDING_SELL:
                    status = self.bot.executor.check_order_status(
                        snapshot.exit_order_id,
                        symbol=snapshot.symbol,
                        side="sell",
                    )
                    if status.status == OrderStatus.ERROR:
                        hist = self.bot._lookup_order_history_status(snapshot.symbol, snapshot.exit_order_id)
                        hist_status = str((hist or {}).get("status") or "").lower()
                        if hist_status in ("filled", "match", "done", "complete"):
                            status = OrderResult(
                                success=True,
                                status=OrderStatus.FILLED,
                                order_id=snapshot.exit_order_id,
                                filled_price=(hist or {}).get("rate"),
                            )

                    if status.status == OrderStatus.FILLED:
                        _, exit_price = self.bot._resolve_fill_amount(snapshot, status, snapshot.exit_price)
                        completed = self.bot._state_manager.complete_exit(snapshot.symbol, exit_price)
                        self.bot._report_completed_exit(completed, exit_price, "order")
                        logger.info("[State] %s -> idle | exit filled", snapshot.symbol)
                        continue

                    if self.bot._state_manager.is_timed_out(snapshot):
                        cancelled = self.bot.executor.cancel_order(
                            snapshot.exit_order_id, symbol=snapshot.symbol, side="sell"
                        )
                        stale_fill = getattr(self.bot.executor, "_oms_cancel_was_error_21", False)
                        self.bot.executor._oms_cancel_was_error_21 = False
                        if stale_fill:
                            completed = self.bot._state_manager.complete_exit(
                                snapshot.symbol, snapshot.exit_price or snapshot.entry_price
                            )
                            self.bot._report_completed_exit(
                                completed, snapshot.exit_price or snapshot.entry_price, "stale_cancel"
                            )
                            continue

                        if cancelled:
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
                            self.bot._state_manager.restore_in_position(snapshot.symbol, "sell timeout cancel")
                            logger.info("[State] %s restored to in_position after sell timeout", snapshot.symbol)
                        else:
                            logger.warning("[State] Failed to cancel timed-out sell for %s", snapshot.symbol)
            except Exception as exc:
                logger.error("[State] Advance error for %s: %s", snapshot.symbol, exc, exc_info=True)

    def try_submit_managed_signal_sell(self, decision: TradeDecision) -> bool:
        if not self.bot._state_machine_enabled or decision.plan.side != OrderSide.SELL:
            return False

        symbol = str(decision.plan.symbol or "").upper()
        snapshot = self.bot._state_manager.get_state(symbol)
        if snapshot.state != TradeLifecycleState.IN_POSITION:
            return False

        open_orders = self.bot.executor.get_open_orders() or []
        position = None
        for row in open_orders:
            row_symbol = str(row.get("symbol") or "").upper()
            if row_symbol != symbol:
                continue
            side_value = str(getattr(row.get("side"), "value", row.get("side")) or "").lower()
            if side_value and side_value != "buy":
                continue
            position = row
            break

        if not position:
            logger.warning("[State] SELL signal for %s ignored: no open position found for managed exit", symbol)
            return False

        position_id = str(position.get("order_id") or snapshot.entry_order_id or "")
        amount = float(position.get("remaining_amount") or position.get("amount") or snapshot.filled_amount or 0.0)
        entry_price = float(position.get("entry_price") or snapshot.entry_price or decision.plan.entry_price or 0.0)
        total_entry_cost = float(position.get("total_entry_cost") or snapshot.total_entry_cost or 0.0)
        opened_at = position.get("timestamp") or snapshot.opened_at
        position_side_raw = position.get("side") or snapshot.side or OrderSide.BUY
        position_side_value = str(getattr(position_side_raw, "value", position_side_raw) or "buy").lower()
        position_side = OrderSide.BUY if position_side_value == "buy" else OrderSide.SELL
        if not position_id or amount <= 0:
            logger.warning("[State] SELL signal for %s ignored: incomplete managed exit payload", symbol)
            return False

        signal_exit_price = float(decision.plan.entry_price or entry_price)
        if not self.bot._should_allow_voluntary_exit(
            symbol=symbol,
            trigger="SIGSELL",
            entry_price=entry_price,
            exit_price=signal_exit_price,
            amount=amount,
            total_entry_cost=total_entry_cost,
            side=position_side,
        ):
            return False

        submitted = self.bot._submit_managed_exit(
            position_id=position_id,
            pos_symbol=symbol,
            side=position_side,
            amount=amount,
            exit_price=signal_exit_price,
            triggered="SIGSELL",
            entry_price=entry_price,
            total_entry_cost=total_entry_cost,
            price_source="signal",
            opened_at=opened_at,
        )
        if submitted:
            decision.status = "pending_sell"
        return bool(submitted)

    @staticmethod
    def signal_trigger_cache_key(symbol: str, signal_type: str) -> str:
        return f"{str(symbol or '').upper()}:{str(signal_type or '').lower()}"

    @staticmethod
    def get_signal_trigger_token(signal: Optional[Any]) -> str:
        if signal is None:
            return ""
        for source_signal in list(getattr(signal, "signals", []) or []):
            metadata = getattr(source_signal, "metadata", {}) or {}
            trigger_timestamp = str(metadata.get("macd_cross_timestamp") or "").strip()
            if trigger_timestamp:
                return trigger_timestamp
        return ""

    def is_reused_signal_trigger(self, signal: Optional[Any]) -> bool:
        signal_type = getattr(getattr(signal, "signal_type", None), "value", getattr(signal, "signal_type", ""))
        cache_key = self.signal_trigger_cache_key(getattr(signal, "symbol", ""), signal_type)
        trigger_token = self.get_signal_trigger_token(signal)
        if not trigger_token:
            return False
        consumed = getattr(self.bot, "_last_consumed_signal_triggers", {}) or {}
        return consumed.get(cache_key) == trigger_token

    def remember_consumed_signal_trigger(self, signal: Optional[Any]) -> None:
        trigger_token = self.get_signal_trigger_token(signal)
        if not trigger_token:
            return
        signal_type = getattr(getattr(signal, "signal_type", None), "value", getattr(signal, "signal_type", ""))
        cache_key = self.signal_trigger_cache_key(getattr(signal, "symbol", ""), signal_type)
        consumed = getattr(self.bot, "_last_consumed_signal_triggers", None)
        if consumed is None:
            consumed = {}
            self.bot._last_consumed_signal_triggers = consumed
        consumed[cache_key] = trigger_token
