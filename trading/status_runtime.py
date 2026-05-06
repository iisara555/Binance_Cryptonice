from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from helpers import extract_base_asset, normalize_side_value

logger = logging.getLogger(__name__)


class StatusRuntimeHelper:
    def __init__(
        self,
        bot: Any,
        *,
        required_candles: int,
        websocket_available: bool,
        latest_ticker_getter: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.bot = bot
        self.required_candles = int(required_candles)
        self.websocket_available = bool(websocket_available)
        self.latest_ticker_getter = latest_ticker_getter if callable(latest_ticker_getter) else None

    @staticmethod
    def _coerce_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_iso_time(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        text = str(value or "").strip()
        if not text or text == "-":
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def format_alert_block(header: str, lines: List[str], now: Optional[datetime] = None) -> str:
        timestamp = (now or datetime.now()).strftime("%H:%M:%S")
        return "\n".join([header, "-" * 22, *lines, f"Time: <code>{timestamp}</code>"])

    @staticmethod
    def format_coin_symbol(symbol: str) -> str:
        return extract_base_asset(symbol)

    def quote_asset(self) -> str:
        config = getattr(self.bot, "config", {}) or {}
        hybrid_cfg = (config.get("data", {}) or {}).get("hybrid_dynamic_coin_config", {}) or {}
        return str(hybrid_cfg.get("quote_asset") or "USDT").upper()

    def get_trailing_trace_context(self) -> Dict[str, Any]:
        executor = getattr(self.bot, "executor", None)
        return {
            "enabled": bool(getattr(executor, "_allow_trailing_stop", False)),
            "activation_pct": self._coerce_float(getattr(executor, "_trailing_activation_pct", 0.0), 0.0),
            "distance_pct": self._coerce_float(getattr(executor, "_trailing_stop_pct", 0.0), 0.0),
        }

    def log_position_trace(
        self,
        event: str,
        symbol: str,
        *,
        entry_order_id: str = "",
        exit_order_id: str = "",
        amount: float = 0.0,
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
        previous_stop_loss: float = 0.0,
        current_price: float = 0.0,
        profit_pct: Optional[float] = None,
        trigger: str = "",
        notes: str = "",
    ) -> None:
        trace_parts = [
            f"event={str(event or '').upper()}",
            f"symbol={str(symbol or '').upper()}",
        ]
        coin = self.format_coin_symbol(symbol)
        if coin:
            trace_parts.append(f"coin={coin}")
        if entry_order_id:
            trace_parts.append(f"entry_order={entry_order_id}")
        if exit_order_id:
            trace_parts.append(f"exit_order={exit_order_id}")
        if amount > 0:
            trace_parts.append(f"amount={float(amount):.8f}")
        if entry_price > 0:
            trace_parts.append(f"entry={float(entry_price):,.2f}")
        if exit_price > 0:
            trace_parts.append(f"exit={float(exit_price):,.2f}")
        if previous_stop_loss > 0:
            trace_parts.append(f"prev_sl={float(previous_stop_loss):,.2f}")
        if stop_loss > 0:
            trace_parts.append(f"sl={float(stop_loss):,.2f}")
        if take_profit > 0:
            trace_parts.append(f"tp={float(take_profit):,.2f}")
        if current_price > 0:
            trace_parts.append(f"price={float(current_price):,.2f}")
        if profit_pct is not None:
            trace_parts.append(f"profit={float(profit_pct):+.2f}%")
        if trigger:
            trace_parts.append(f"trigger={trigger}")

        trailing = self.get_trailing_trace_context()
        trace_parts.append(f"trailing={'on' if trailing['enabled'] else 'off'}")
        if trailing["enabled"]:
            trace_parts.append(f"trail_activate={float(trailing['activation_pct']):.2f}%")
            trace_parts.append(f"trail_gap={float(trailing['distance_pct']):.2f}%")
        if notes:
            trace_parts.append(f"notes={notes}")

        logger.info("[PositionTrace] %s", " | ".join(trace_parts))

    def format_exit_alert(
        self,
        symbol: str,
        trigger_label: str,
        amount: float,
        entry_price: float,
        exit_price: float,
        entry_cost: float,
        gross_exit: float,
        net_pnl: float,
        net_pnl_pct: float,
        total_fees: float,
        now: Optional[datetime] = None,
    ) -> str:
        pnl_emoji = "✅" if net_pnl >= 0 else "🔻"
        coin = self.format_coin_symbol(symbol)
        quote = self.quote_asset()
        return self.format_alert_block(
            f"📤 <b>Position Closed</b>  {coin}  ({trigger_label})",
            [
                f"Size <code>{amount:.8f}</code> {coin}",
                f"Buy <code>{entry_cost:,.2f}</code> {quote} @ <code>{entry_price:,.2f}</code>",
                f"Sell <code>{gross_exit:,.2f}</code> {quote} @ <code>{exit_price:,.2f}</code>",
                f"{pnl_emoji} PnL <code>{net_pnl:+,.0f}</code> {quote} ({net_pnl_pct:+.2f}%)",
                f"Fees <code>{total_fees:,.0f}</code> {quote}",
            ],
            now=now,
        )

    def format_trade_alert(self, decision: Any, result: Any) -> str:
        plan = decision.plan
        now = datetime.now()
        fill_price = result.filled_price or plan.entry_price
        notional = result.filled_amount * fill_price
        quote = self.quote_asset()
        sl = plan.stop_loss if plan else 0
        tp = plan.take_profit if plan else 0
        conf = plan.confidence if plan else 0
        coin = self.format_coin_symbol(plan.symbol) if plan else ""

        # Extract strategy name(s) from votes — show top contributors
        votes: Dict[str, int] = getattr(plan, "strategy_votes", {}) or {}
        if votes:
            sorted_strategies = sorted(votes.items(), key=lambda x: x[1], reverse=True)
            strategy_label = ", ".join(name for name, _ in sorted_strategies[:3])
        else:
            strategy_label = "N/A"

        return self.format_alert_block(
            f"📥 <b>Position Opened</b>  {coin}",
            [
                f"Strategy <code>{strategy_label}</code>",
                f"Size <code>{float(result.filled_amount or 0.0):.8f}</code> {coin}",
                f"Fill Price <code>{fill_price:,.0f}</code> {quote}",
                f"Notional <code>{notional:,.0f}</code> {quote}  |  Confidence {conf:.0%}",
                f"SL <code>{sl:,.0f}</code>  |  TP <code>{tp:,.0f}</code>",
            ],
            now=now,
        )

    def format_skip_alert(self, decision: Any) -> str:
        plan = decision.plan
        reason = getattr(decision.risk_check, "reason", getattr(decision.risk_check, "reasons", "Unknown reason"))
        coin = self.format_coin_symbol(plan.symbol) if plan else ""
        side = normalize_side_value(getattr(plan, "side", ""), default="n/a").upper() if plan else "N/A"
        return f"🛡️ <b>Risk Control</b>  {coin} {side}\nReason: {reason}"

    def format_pending_alert(self, decision: Any, portfolio: Dict[str, Any]) -> str:
        plan = decision.plan
        coin = self.format_coin_symbol(plan.symbol) if plan else ""
        quote = self.quote_asset()
        with self.bot._pending_decisions_lock:
            decision_id = len(self.bot._pending_decisions) - 1
        return self.format_alert_block(
            f"⏳ <b>Approval Required</b>  {coin}",
            [
                f"Price <code>{plan.entry_price:,.0f}</code> {quote}  |  Confidence {plan.confidence:.0%}",
                f"Balance <code>{portfolio['balance']:,.0f}</code> {quote}",
                f"ID: <code>{decision_id}</code>  /approve {decision_id}",
            ],
        )

    def format_dry_run_alert(self, decision: Any, portfolio: Dict[str, Any]) -> str:
        del portfolio
        plan = decision.plan
        coin = self.format_coin_symbol(plan.symbol) if plan else ""
        side = normalize_side_value(getattr(plan, "side", ""), default="n/a").upper() if plan else "N/A"
        return (
            f"🧪 <b>Dry Run</b>  {coin} {side} @ {plan.entry_price:,.0f} {self.quote_asset()}  ({plan.confidence:.0%})"
        )

    def build_multi_timeframe_status(self) -> Dict[str, Any]:
        timeframes = list(getattr(self.bot, "mtf_timeframes", []) or [])
        status = {
            "enabled": bool(getattr(self.bot, "mtf_enabled", False)),
            "mode": "confirmation",
            "timeframes": timeframes,
            "required_candles": self.required_candles,
            "require_htf_confirmation": bool(getattr(self.bot, "_mtf_confirmation_required", False)),
            "primary_timeframe": getattr(self.bot, "timeframe", "1h"),
            "pairs": [],
            "last_signals": dict(getattr(self.bot, "_last_mtf_status", {}) or {}),
        }
        if not status["enabled"] or not timeframes:
            return status

        db = getattr(self.bot, "db", None)
        if db is None:
            return status

        gating_timeframes = list(timeframes)
        require_htf_confirmation = bool(getattr(self.bot, "_mtf_confirmation_required", False))
        primary_timeframe = str(getattr(self.bot, "timeframe", "1h") or "1h")
        if not require_htf_confirmation and primary_timeframe in timeframes:
            primary_idx = timeframes.index(primary_timeframe)
            gating_timeframes = timeframes[: primary_idx + 1]
        if not gating_timeframes:
            gating_timeframes = list(timeframes)
        gating_set = set(gating_timeframes)
        status["readiness_timeframes"] = list(gating_timeframes)

        conn = None
        cursor = None
        try:
            conn = db.get_connection()
            cursor = conn.cursor()
            pair_summaries = []
            for pair in list(getattr(self.bot, "trading_pairs", []) or []):
                timeframe_rows = []
                waiting_total = 0
                blocking_timeframes: List[str] = []
                for timeframe in timeframes:
                    cursor.execute(
                        "SELECT COUNT(*), MAX(timestamp) FROM prices WHERE pair = ? AND timeframe = ?",
                        (pair, timeframe),
                    )
                    count, latest = cursor.fetchone()
                    count_value = int(count or 0)
                    waiting_candles = max(0, self.required_candles - count_value)
                    required_for_readiness = timeframe in gating_set
                    if required_for_readiness:
                        waiting_total += waiting_candles
                    if required_for_readiness and waiting_candles > 0:
                        blocking_timeframes.append(f"{timeframe}:{waiting_candles}")
                    timeframe_rows.append(
                        {
                            "timeframe": timeframe,
                            "count": count_value,
                            "required_candles": self.required_candles,
                            "waiting_candles": waiting_candles,
                            "required_for_readiness": required_for_readiness,
                            "latest": (
                                latest.isoformat()
                                if hasattr(latest, "isoformat")
                                else (str(latest) if latest else None)
                            ),
                        }
                    )

                pair_summaries.append(
                    {
                        "pair": pair,
                        "timeframes": timeframe_rows,
                        "required_candles": self.required_candles,
                        "waiting_candles": waiting_total,
                        "waiting_summary": ", ".join(blocking_timeframes) if blocking_timeframes else "ready",
                        "ready": all(
                            (row.get("waiting_candles") or 0) <= 0
                            for row in timeframe_rows
                            if row.get("required_for_readiness", True)
                        ),
                    }
                )

            status["pairs"] = pair_summaries
        except Exception as exc:
            logger.debug("Failed to build multi-timeframe status: %s", exc)
            status["error"] = str(exc)
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception as exc:
                    logger.warning("Failed to close MTF status cursor: %s", exc)
            if conn is not None:
                try:
                    conn.close()
                except Exception as exc:
                    logger.warning("Failed to close MTF status DB connection: %s", exc)

        return status

    def get_dashboard_multi_timeframe_status(self, allow_refresh: bool = True) -> Dict[str, Any]:
        now = time.time()
        cache = getattr(self.bot, "_multi_timeframe_status_cache", {"data": None, "timestamp": 0.0})
        cache_ttl_config = getattr(self.bot, "_cache_ttl", {}) or {}
        cache_ttl = max(float(cache_ttl_config.get("market_data", 10) or 10), 15.0)
        if cache.get("data") is not None and (
            (now - float(cache.get("timestamp", 0.0) or 0.0)) < cache_ttl or not allow_refresh
        ):
            return cache["data"]

        if not allow_refresh:
            return {
                "enabled": bool(getattr(self.bot, "mtf_enabled", False)),
                "mode": "confirmation",
                "timeframes": list(getattr(self.bot, "mtf_timeframes", []) or []),
                "require_htf_confirmation": bool(getattr(self.bot, "_mtf_confirmation_required", False)),
                "primary_timeframe": getattr(self.bot, "timeframe", "1h"),
                "pairs": [],
                "last_signals": dict(getattr(self.bot, "_last_mtf_status", {}) or {}),
            }

        status = self.build_multi_timeframe_status()
        self.bot._multi_timeframe_status_cache = {"data": status, "timestamp": now}
        return status

    def safe_pending_count(self) -> int:
        with self.bot._pending_decisions_lock:
            return len(self.bot._pending_decisions)

    def get_pending_decisions(self) -> List[Dict[str, Any]]:
        with self.bot._pending_decisions_lock:
            decisions_copy = list(self.bot._pending_decisions)
        return [
            {
                "id": index,
                "side": normalize_side_value(getattr(decision.plan, "side", ""), default=""),
                "entry_price": decision.plan.entry_price,
                "confidence": decision.plan.confidence,
                "strategy_votes": decision.plan.strategy_votes,
                "signal_source": decision.signal_source.value,
                "risk_check_passed": decision.risk_check.passed,
                "decision_time": decision.decision_time.isoformat(),
            }
            for index, decision in enumerate(decisions_copy)
        ]

    def _instance_override(self, name: str) -> Optional[Callable[..., Any]]:
        override = getattr(self.bot, "__dict__", {}).get(name)
        if callable(override):
            return override
        return None

    def get_status(self, lightweight: bool = False) -> Dict[str, Any]:
        t_all = time.perf_counter()
        paused, pause_reason = self.bot._is_paused()
        trading_pairs = getattr(self.bot, "trading_pairs", [])
        trading_pair = getattr(self.bot, "trading_pair", "")
        portfolio_state_getter = getattr(self.bot, "_get_portfolio_state", lambda *args, **kwargs: {})
        t_pf0 = time.perf_counter()
        try:
            portfolio_state = portfolio_state_getter(allow_refresh=not lightweight) or {}
        except TypeError:
            portfolio_state = portfolio_state_getter() or {}
        portfolio_ms = (time.perf_counter() - t_pf0) * 1000.0
        risk_manager = getattr(self.bot, "risk_manager", None)

        ws_client = getattr(self.bot, "_ws_client", None)
        ws_enabled = bool(getattr(self.bot, "_ws_enabled", False))
        import_ok = bool(getattr(self.bot, "_ws_import_ok", False))
        pair_list = list(trading_pairs or [])

        ws_last_error: Optional[str] = None
        # Prefer config/runtime flags over a stale client object (e.g. FAILED after dependency/off).
        if not ws_enabled:
            ws_state = "disabled"
            live_price = None
        elif not import_ok:
            ws_state = "no_backend"
            live_price = None
        elif ws_client:
            if self.bot._ws_client.is_connected():
                ws_state = "connected"
            else:
                raw_state = getattr(ws_client, "state", None)
                state_text = str(getattr(raw_state, "value", raw_state) or "").strip().lower()
                if state_text in {"connecting", "reconnecting", "failed", "disconnected"}:
                    ws_state = state_text
                else:
                    ws_state = "disconnected"
            live_symbol = pair_list[0] if pair_list else trading_pair
            live_tick = (
                self.latest_ticker_getter(live_symbol)
                if self.websocket_available and self.latest_ticker_getter
                else None
            )
            live_price = live_tick.last if live_tick else None
            try:
                stats = ws_client.get_stats() if callable(getattr(ws_client, "get_stats", None)) else None
                if isinstance(stats, dict):
                    err = stats.get("last_error")
                    if err:
                        ws_last_error = str(err)[:200]
            except Exception:
                pass
        elif not pair_list:
            ws_state = "no_pairs"
            live_price = None
        else:
            ws_state = "not_started"
            live_price = None

        balance_monitor = getattr(self.bot, "_balance_monitor", None)
        balance_state = balance_monitor.get_state() if balance_monitor else None
        balances = dict((balance_state or {}).get("balances") or {})
        quote_asset = self.quote_asset()
        raw_balance_monitor_cfg = (getattr(self.bot, "config", {}) or {}).get("balance_monitor", {}) or {}
        try:
            max_event_age_hours = float(raw_balance_monitor_cfg.get("event_tape_max_age_hours", 12.0) or 12.0)
        except (TypeError, ValueError):
            max_event_age_hours = 12.0
        max_event_age_seconds = max(0.0, max_event_age_hours * 3600.0)
        now_dt = datetime.now()
        active_assets = {
            str(asset or "").upper()
            for asset, payload in balances.items()
            if self._coerce_float((payload or {}).get("total"), 0.0) > 0
        }
        active_assets.add(quote_asset)
        balance_events: List[Dict[str, str]] = []
        seen_event_keys: set[str] = set()
        for row in list((balance_state or {}).get("last_events") or []):
            event_type = str(row.get("event_type") or "BAL")
            coin = str(row.get("coin") or "").upper()
            amount = self._coerce_float(row.get("amount"), 0.0)
            occurred_at_raw = row.get("occurred_at")
            occurred_dt = self._parse_iso_time(occurred_at_raw)
            if occurred_dt is not None and max_event_age_seconds > 0:
                try:
                    age_seconds = abs((now_dt - occurred_dt.replace(tzinfo=None)).total_seconds())
                    if age_seconds > max_event_age_seconds:
                        continue
                except Exception:
                    pass

            # Drop stale legacy history rows from previous exchange context.
            if coin and coin not in active_assets:
                continue

            event_key = f"{event_type}|{coin}|{amount:.8f}|{occurred_at_raw}"
            if event_key in seen_event_keys:
                continue
            seen_event_keys.add(event_key)
            message = f"{event_type} {coin} {amount:,.4f}".strip()
            balance_events.append(
                {
                    "timestamp": str(row.get("occurred_at") or "-"),
                    "type": event_type,
                    "message": message,
                }
            )
            if len(balance_events) >= 5:
                break

        recent_trades: List[Dict[str, str]] = []
        for row in list(getattr(self.bot, "_executed_today", []) or [])[-5:]:
            decision = row.get("decision") if isinstance(row, dict) else None
            result = row.get("result") if isinstance(row, dict) else None
            side = "-"
            symbol = "-"
            if decision and getattr(decision, "plan", None):
                side = str(getattr(getattr(decision.plan, "side", None), "value", "-") or "-")
                symbol = str(getattr(decision.plan, "symbol", "-") or "-")
            status = str(getattr(result, "status", "filled") or "filled")
            timestamp = row.get("timestamp") if isinstance(row, dict) else None
            recent_trades.append(
                {
                    "timestamp": timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp or "-"),
                    "symbol": symbol,
                    "side": side,
                    "status": status,
                }
            )

        t_mtf0 = time.perf_counter()
        mtf_override = self._instance_override("_get_dashboard_multi_timeframe_status")
        if mtf_override is not None:
            mtf_status = mtf_override(allow_refresh=not lightweight) or {}
        else:
            mtf_status = self.get_dashboard_multi_timeframe_status(allow_refresh=not lightweight)
        mtf_ms = (time.perf_counter() - t_mtf0) * 1000.0

        t_filter0 = time.perf_counter()
        tradable_pairs = self.bot._filter_pairs_by_candle_readiness(list(trading_pairs), allow_refresh=not lightweight)
        filter_ms = (time.perf_counter() - t_filter0) * 1000.0

        executor = getattr(self.bot, "executor", None)
        open_positions = 0
        if executor and hasattr(executor, "get_open_orders"):
            try:
                open_positions = len(executor.get_open_orders())
            except Exception:
                open_positions = 0

        risk_portfolio_value = self.bot._get_risk_portfolio_value(portfolio_state)
        executed_today_count = len(getattr(self.bot, "_executed_today", []))
        if risk_manager is not None:
            raw_trade_count_today = getattr(risk_manager, "trade_count_today", executed_today_count)
            try:
                executed_today_count = int(raw_trade_count_today)
            except (TypeError, ValueError):
                executed_today_count = len(getattr(self.bot, "_executed_today", []))

        pending_override = self._instance_override("_safe_pending_count")
        pending_count = pending_override() if pending_override is not None else self.safe_pending_count()

        status_total_ms = (time.perf_counter() - t_all) * 1000.0
        if status_total_ms >= 500.0:
            non_quote_balance_rows = sum(
                1
                for a, p in balances.items()
                if str(a or "").upper() != quote_asset and self._coerce_float((p or {}).get("total"), 0.0) > 0
            )
            logger.warning(
                "[STATUS PERF] lightweight=%s total_ms=%.1f portfolio_ms=%.1f mtf_ms=%.1f "
                "filter_ms=%.1f post_portfolio_ms=%.1f ws_state=%s non_quote_balance_rows=%d",
                lightweight,
                status_total_ms,
                portfolio_ms,
                mtf_ms,
                filter_ms,
                max(0.0, status_total_ms - portfolio_ms),
                ws_state,
                non_quote_balance_rows,
            )

        return {
            "running": getattr(self.bot, "running", False),
            "mode": self.bot.mode.value,
            "signal_source": self.bot.signal_source.value,
            "strategy": "sniper_directional",
            "strategy_engine": {
                "enabled": True,
                "strategies": list(getattr(self.bot, "enabled_strategies", [])),
            },
            "auth_degraded": {
                "active": getattr(self.bot, "_auth_degraded", False),
                "reason": getattr(self.bot, "_auth_degraded_reason", ""),
                "public_only": getattr(self.bot, "_auth_degraded", False),
            },
            "trading_pairs": trading_pairs,
            "tradable_pairs": tradable_pairs,
            "trading_paused": {
                "active": paused,
                "reason": pause_reason,
            },
            "timeframe": getattr(self.bot, "timeframe", None),
            "interval_seconds": getattr(self.bot, "interval_seconds", 0),
            "loop_count": getattr(self.bot, "_loop_count", 0),
            "last_loop": (
                self.bot._last_loop_time.isoformat() if getattr(self.bot, "_last_loop_time", None) is not None else None
            ),
            "pending_decisions": pending_count,
            "executed_today": executed_today_count,
            "open_positions": open_positions,
            "balance_monitor": {
                "enabled": bool(balance_monitor),
                "running": bool(balance_monitor and balance_monitor.running),
                "updated_at": balance_state.get("updated_at") if balance_state else None,
                "last_event_count": len(balance_state.get("last_events", [])) if balance_state else 0,
            },
            "balance_events": balance_events,
            "recent_trades": recent_trades,
            "multi_timeframe": mtf_status,
            "websocket": {
                "enabled": getattr(self.bot, "_ws_enabled", False),
                "state": ws_state,
                "live_price": live_price,
                "last_error": ws_last_error,
            },
            "risk_summary": risk_manager.get_risk_summary(risk_portfolio_value) if risk_manager else {},
        }
