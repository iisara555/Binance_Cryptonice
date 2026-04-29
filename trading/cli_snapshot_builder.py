"""CLI snapshot assembly extracted from ``main.TradingBotApp``."""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

from helpers import format_exchange_time, now_exchange_time, parse_as_exchange_time

from cli_snapshot_build import build_open_position_rows_for_cli, compute_cli_balance_websocket_health
from cli_snapshot_dto import build_balance_breakdown_lines, quote_cash_totals_strings
from trading.portfolio_runtime import PortfolioRuntimeHelper
from signal_generator import ensure_signal_flow_record, get_latest_signal_flow_snapshot

logger = logging.getLogger(__name__)

def _resolve_cli_asset_quote_rate(app,
    asset: str,
    quote_asset: str,
    live_dashboard_active: bool,
) -> Optional[float]:
    """Resolve asset->quote conversion rate using direct and inverse market symbols."""
    asset_symbol = str(asset or "").upper()
    quote_symbol = str(quote_asset or "").upper()
    if not asset_symbol or not quote_symbol:
        return None
    if asset_symbol == quote_symbol:
        return 1.0

    def _read_price(symbol: str) -> Optional[float]:
        if not symbol:
            return None
        price = app._get_cli_price(symbol, False)
        if (not price or price <= 0) and not live_dashboard_active:
            price = app._get_cli_price(symbol, True)
        if not price or price <= 0:
            cached = app._cli_price_cache.get(symbol)
            if cached:
                price = cached[0]
        if not price or price <= 0:
            price = app._get_cli_position_price_hint(symbol)
        try:
            parsed = float(price or 0.0)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    direct_pairs = [
        f"{asset_symbol}{quote_symbol}",
        f"{quote_symbol}_{asset_symbol}",
    ]
    inverse_pairs = [
        f"{quote_symbol}{asset_symbol}",
        f"{asset_symbol}_{quote_symbol}",
    ]

    seen: set[str] = set()
    for pair in direct_pairs:
        if pair in seen:
            continue
        seen.add(pair)
        direct_price = _read_price(pair)
        if direct_price and direct_price > 0:
            return direct_price

    for pair in inverse_pairs:
        if pair in seen:
            continue
        seen.add(pair)
        inverse_price = _read_price(pair)
        if inverse_price and inverse_price > 0:
            return 1.0 / inverse_price

    return None

def _summarize_cli_candle_readiness(multi_timeframe_status: Optional[Dict[str, Any]]) -> str:
    status = dict(multi_timeframe_status or {})
    pairs = list(status.get("pairs") or [])
    if not pairs:
        return "-"

    total_pairs = len(pairs)
    ready_pairs = 0
    lagging_pairs = 0
    for pair_status in pairs:
        if pair_status.get("ready"):
            ready_pairs += 1
        else:
            lagging_pairs += 1

    summary = f"{ready_pairs}/{total_pairs} ready"
    if lagging_pairs:
        summary += f" ({lagging_pairs} lagging)"
    return summary

def _summarize_cli_candle_waiting(multi_timeframe_status: Optional[Dict[str, Any]], limit: int = 3) -> str:
    status = dict(multi_timeframe_status or {})
    pairs = list(status.get("pairs") or [])
    waiting_rows = []
    for pair_status in pairs:
        waiting_candles = int(pair_status.get("waiting_candles", 0) or 0)
        if waiting_candles <= 0:
            continue
        pair = str(pair_status.get("pair") or "").replace("THB_", "")
        waiting_summary = str(pair_status.get("waiting_summary") or "").strip()
        if waiting_summary and waiting_summary.lower() != "ready":
            waiting_rows.append(f"{pair} {waiting_summary}")
        else:
            waiting_rows.append(f"{pair} {waiting_candles}")

    if not waiting_rows:
        return "Ready"
    return "; ".join(waiting_rows[:limit])

def _get_cli_balance_summary(app, portfolio_state: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate total portfolio value in quote terms and per-asset valuation details."""
    fallback_balance = float(portfolio_state.get("balance", 0.0) or 0.0)
    quote_asset = app._get_quote_asset()

    def _get_fresh_cached_summary() -> Optional[Dict[str, Any]]:
        cached_summary = getattr(app, "_cli_balance_summary_cache", None)
        cached_at = float(getattr(app, "_cli_balance_summary_cached_at", 0.0) or 0.0)
        cache_ttl = float(getattr(app, "_cli_balance_summary_cache_seconds", 10.0) or 10.0)
        if cached_summary and (time.time() - cached_at) < cache_ttl:
            return cached_summary
        return None

    if not app.api_client or app.config.get("auth_degraded", False):
        return {
            "total_balance": fallback_balance,
            "breakdown": [{"asset": quote_asset, "amount": fallback_balance, "value_thb": fallback_balance}],
        }

    live_dashboard_active = bool(getattr(app, "_live_dashboard_active", False))
    allow_rest_fallback = not live_dashboard_active

    balance_state: Dict[str, Any] = {}
    if live_dashboard_active and app.bot and getattr(app.bot, "_balance_monitor", None):
        try:
            balance_state = app.bot.get_balance_state() or {}
        except Exception:
            balance_state = {}

        updated_at = parse_as_exchange_time((balance_state or {}).get("updated_at"))
        raw_poll_interval = getattr(getattr(app.bot, "_balance_monitor", None), "poll_interval_seconds", 30.0)
        try:
            poll_interval_seconds = float(raw_poll_interval or 30.0)
        except (TypeError, ValueError):
            poll_interval_seconds = 30.0
        stale_after_seconds = max(poll_interval_seconds * 2.0, 60.0)
        if updated_at is not None:
            try:
                if (now_exchange_time() - updated_at).total_seconds() > stale_after_seconds:
                    balance_state = {}
            except Exception:
                balance_state = {}

    if balance_state:
        balances = balance_state.get("balances") or {}
    elif not live_dashboard_active:
        try:
            balances = app.api_client.get_balances()
        except Exception:
            balances = {}
    else:
        # Live dashboard: never block with REST. Use stale cache instead.
        cached_summary = _get_fresh_cached_summary()
        if cached_summary:
            return cached_summary
        balances = {}

    if not isinstance(balances, dict):
        balances = {}

    if not balances:
        cached_summary = _get_fresh_cached_summary()
        if live_dashboard_active and cached_summary:
            return cached_summary
        return {
            "total_balance": fallback_balance,
            "breakdown": [{"asset": quote_asset, "amount": fallback_balance, "value_thb": fallback_balance}],
        }

    total_value = 0.0
    breakdown: List[Dict[str, Any]] = []
    for asset, payload in balances.items():
        symbol = str(asset or "").upper()
        if not symbol:
            continue

        if isinstance(payload, dict):
            available = float(payload.get("available", 0.0) or 0.0)
            reserved = float(payload.get("reserved", 0.0) or 0.0)
        else:
            available = float(payload or 0.0)
            reserved = 0.0

        amount = available + reserved
        if amount <= 0:
            continue

        if symbol == quote_asset:
            total_value += amount
            breakdown.append({"asset": symbol, "amount": amount, "value_thb": amount})
            continue

        conversion_rate = app._resolve_cli_asset_quote_rate(symbol, quote_asset, live_dashboard_active)
        if conversion_rate and conversion_rate > 0:
            value_thb = amount * conversion_rate
            total_value += value_thb
            breakdown.append({"asset": symbol, "amount": amount, "value_thb": value_thb})

    if total_value <= 0:
        cached_summary = _get_fresh_cached_summary()
        if live_dashboard_active and cached_summary:
            return cached_summary
        return {
            "total_balance": fallback_balance,
            "breakdown": [{"asset": quote_asset, "amount": fallback_balance, "value_thb": fallback_balance}],
        }

    breakdown.sort(key=lambda item: float(item.get("value_thb") or 0.0), reverse=True)
    summary = {
        "total_balance": total_value,
        "breakdown": breakdown,
    }
    app._cli_balance_summary_cache = summary
    app._cli_balance_summary_cached_at = time.time()
    return summary

def _parse_reason_bool(reason: str, key: str) -> Optional[bool]:
    text = str(reason or "")
    match = re.search(rf"{re.escape(key)}=(True|False)", text)
    if not match:
        return None
    return match.group(1) == "True"

def _humanize_alignment_wait_reason(reason: str) -> str:
    """Short English labels for Signal Radar status (same intent as CLI Signal Flow)."""
    s = str(reason or "").strip()
    if not s:
        return "No data"
    low = s.lower()
    m = re.search(r"Insufficient data \((\d+)/(\d+) bars\)", s, re.I)
    if m:
        return f"Insufficient bars {m.group(1)}/{m.group(2)}"
    if "waiting for first signal cycle" in low:
        return "First cycle"
    if "collecting" in low:
        return s[:40] if len(s) > 40 else s
    return s[:44] + ("…" if len(s) > 44 else "")

def _describe_signal_alignment_status(record: Dict[str, Any], steps: Dict[str, Any]) -> str:
    data_check = steps.get("Sniper:DataCheck", {})
    data_check_result = str(data_check.get("result") or "").upper()
    data_check_reason = str(data_check.get("reason") or "").strip()
    bootstrap = steps.get("Bootstrap", {})
    bootstrap_reason = str(bootstrap.get("reason") or "").strip()
    has_sniper_diagnostics = any(str(name).startswith("Sniper:") for name in (steps or {}).keys())

    if not record:
        return "Waiting for signal data"
    if data_check_result == "REJECT" and data_check_reason:
        return _humanize_alignment_wait_reason(data_check_reason)
    if data_check_result == "REJECT":
        return "Waiting for candles"
    # Ignore stale bootstrap text after we have real sniper diagnostics.
    if bootstrap_reason and not has_sniper_diagnostics:
        return _humanize_alignment_wait_reason(bootstrap_reason)
    return "Ready"

def _build_pair_runtime_context(
    multi_timeframe_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, str]]:
    context: Dict[str, Dict[str, str]] = {}
    for row in list((multi_timeframe_status or {}).get("pairs") or []):
        pair = str(row.get("pair") or "").upper()
        if not pair:
            continue

        timeframe_rows = list(row.get("timeframes") or [])
        ready_count = sum(1 for item in timeframe_rows if int(item.get("waiting_candles", 0) or 0) <= 0)
        total_count = len(timeframe_rows)
        latest_raw = next((item.get("latest") for item in reversed(timeframe_rows) if item.get("latest")), None)
        waiting_summary = str(row.get("waiting_summary") or "").strip()
        if bool(row.get("ready")):
            pair_state = "Ready"
            wait_detail = "-"
        elif waiting_summary and waiting_summary.lower() != "ready":
            pair_state = f"Collecting {waiting_summary}"
            wait_detail = waiting_summary
        else:
            pair_state = "Collecting"
            wait_detail = "-"

        context[pair] = {
            "tf_ready": f"{ready_count}/{total_count}" if total_count else "-",
            "pair_state": pair_state,
            "wait_detail": wait_detail,
            "market_update": format_exchange_time(latest_raw),
        }
    return context

def _build_cli_signal_alignment(app,
    trading_pairs: List[str],
    multi_timeframe_status: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    flow = get_latest_signal_flow_snapshot()
    pair_runtime_context = _build_pair_runtime_context(multi_timeframe_status)
    rows: List[Dict[str, Any]] = []
    for pair in trading_pairs or []:
        record = flow.get(str(pair or "").upper(), {})
        runtime_context = pair_runtime_context.get(str(pair or "").upper(), {})
        if not record:
            wait_hint = str(runtime_context.get("wait_detail") or "").strip()
            if not wait_hint or wait_hint == "-":
                wait_hint = str(runtime_context.get("pair_state") or "").strip()
            if not wait_hint or wait_hint in {"-", "Ready"}:
                wait_hint = "Waiting for first signal cycle"
            record = ensure_signal_flow_record(str(pair or ""), wait_hint)
        steps = dict(record.get("steps") or {})
        macro = steps.get("Sniper:MacroTrend", {})
        micro = steps.get("Sniper:MicroTrend", {})
        trigger = steps.get("Sniper:MACDTrigger", {})

        macro_reason = str(macro.get("reason") or "")
        micro_reason = str(micro.get("reason") or "")
        trigger_reason = str(trigger.get("reason") or "")

        macro_buy = _parse_reason_bool(macro_reason, "buy_ok")
        macro_sell = _parse_reason_bool(macro_reason, "sell_ok")
        micro_buy = _parse_reason_bool(micro_reason, "buy_ok")
        micro_sell = _parse_reason_bool(micro_reason, "sell_ok")

        trigger_buy_now = _parse_reason_bool(trigger_reason, "buy_now")
        trigger_buy_prev = _parse_reason_bool(trigger_reason, "buy_prev")
        trigger_sell_now = _parse_reason_bool(trigger_reason, "sell_now")
        trigger_sell_prev = _parse_reason_bool(trigger_reason, "sell_prev")
        trigger_buy = bool(trigger_buy_now) or bool(trigger_buy_prev)
        trigger_sell = bool(trigger_sell_now) or bool(trigger_sell_prev)

        trend_buy = bool(macro_buy) and bool(micro_buy)
        trend_sell = bool(macro_sell) and bool(micro_sell)
        final_action = "HOLD"
        if trend_buy and trigger_buy:
            final_action = "BUY"
        elif trend_sell and trigger_sell:
            final_action = "SELL"

        status = _describe_signal_alignment_status(record, steps)
        if status != "Ready" and final_action == "HOLD":
            final_action = "WAIT"

        rows.append(
            {
                "symbol": pair,
                "macro": str(macro.get("result") or "N/A"),
                "micro": str(micro.get("result") or "N/A"),
                "trigger": str(trigger.get("result") or "N/A"),
                "trend": "BUY" if trend_buy else ("SELL" if trend_sell else "MIXED"),
                "trigger_side": "BUY" if trigger_buy else ("SELL" if trigger_sell else "NONE"),
                "action": final_action,
                "tf_ready": str(runtime_context.get("tf_ready") or "-"),
                "pair_state": str(runtime_context.get("pair_state") or "-"),
                "wait_detail": str(runtime_context.get("wait_detail") or "-"),
                "market_update": str(runtime_context.get("market_update") or "-"),
                "status": status,
                "updated_at": app._format_cli_timestamp(record.get("updated_at")),
            }
        )
    rows.sort(
        key=lambda row: (
            0 if str(row.get("wait_detail") or "-") not in ("", "-") else 1,
            0 if str(row.get("action") or "").upper() == "WAIT" else 1,
            str(row.get("symbol") or ""),
        )
    )
    return rows

def _format_cli_recent_events(bot_status: Dict[str, Any], limit: int = 3) -> List[Dict[str, str]]:
    events: List[Dict[str, str]] = []

    for item in list(bot_status.get("recent_trades") or []):
        timestamp = format_exchange_time(item.get("timestamp"))
        symbol = str(item.get("symbol") or "-")
        side = str(item.get("side") or "-").upper()
        status = str(item.get("status") or "-").upper()
        events.append(
            {
                "timestamp": timestamp,
                "type": "TRADE",
                "message": f"{symbol} {side} {status}",
            }
        )

    for item in list(bot_status.get("balance_events") or []):
        events.append(
            {
                "timestamp": format_exchange_time(item.get("timestamp")),
                "type": str(item.get("type") or "BAL"),
                "message": str(item.get("message") or "-")[:120],
            }
        )

    events.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
    return events[: max(1, int(limit or 3))]

def get_cli_snapshot(app, bot_name: Optional[str] = None) -> Dict[str, Any]:
    """Build a lightweight runtime snapshot for the Rich command center."""
    snapshot_started = time.perf_counter()

    def _warn_snapshot_step(step: str, started_at: float) -> None:
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        if elapsed_ms >= 500.0:
            logger.warning("[CLI PERF] get_cli_snapshot step=%s took %.1fms", step, elapsed_ms)

    step_started = time.perf_counter()
    bot_status = {}
    if app.bot and hasattr(app.bot, "get_status"):
        try:
            bot_status = app.bot.get_status(lightweight=bool(getattr(app, "_live_dashboard_active", False)))
        except TypeError:
            bot_status = app.bot.get_status()
    _warn_snapshot_step("bot_status", step_started)

    step_started = time.perf_counter()
    risk_level, risk_style = app._derive_risk_level()
    open_orders = app.executor.get_open_orders() if app.executor else []
    positions = build_open_position_rows_for_cli(app, open_orders)
    _warn_snapshot_step("positions", step_started)

    step_started = time.perf_counter()
    portfolio_state = {
        "balance": 0.0,
        "timestamp": None,
    }
    if app.bot and hasattr(app.bot, "_get_portfolio_state"):
        try:
            portfolio_state = app.bot._get_portfolio_state(
                allow_refresh=not bool(getattr(app, "_live_dashboard_active", False))
            )
        except TypeError:
            portfolio_state = app.bot._get_portfolio_state()
    trading_pairs = list(bot_status.get("trading_pairs") or (app.config.get("data") or {}).get("pairs") or [])
    primary_symbol = trading_pairs[0] if trading_pairs else ""
    api_latency_ms = app._sample_api_latency(primary_symbol)
    now_dt = now_exchange_time()
    last_market_raw = bot_status.get("last_loop") or portfolio_state.get("timestamp")
    last_market_dt = parse_as_exchange_time(last_market_raw)
    market_age_seconds: Optional[int] = None
    if last_market_dt is not None:
        try:
            market_age_seconds = max(0, int((now_dt - last_market_dt).total_seconds()))
        except Exception:
            market_age_seconds = None

    balance_monitor_status = dict(bot_status.get("balance_monitor") or {})
    balance_updated_at = parse_as_exchange_time(balance_monitor_status.get("updated_at"))
    balance_age_seconds: Optional[int] = None
    if balance_updated_at is not None:
        try:
            balance_age_seconds = max(0, int((now_dt - balance_updated_at).total_seconds()))
        except Exception:
            balance_age_seconds = None

    balance_monitor = getattr(app.bot, "_balance_monitor", None) if app.bot else None
    raw_balance_poll_interval = getattr(balance_monitor, "poll_interval_seconds", 30.0)
    try:
        balance_poll_interval_seconds = float(raw_balance_poll_interval or 30.0)
    except (TypeError, ValueError):
        balance_poll_interval_seconds = 30.0

    websocket_status = dict(bot_status.get("websocket") or {})
    ws_client = getattr(app.bot, "_ws_client", None) if app.bot else None
    ws_last_activity_seconds: Optional[int] = None
    if ws_client is not None:
        try:
            ws_stats = ws_client.get_stats() or {}
            raw_last_activity = ws_stats.get("last_activity_ago")
            if raw_last_activity is not None:
                ws_last_activity_seconds = max(0, int(float(raw_last_activity)))
        except Exception:
            ws_last_activity_seconds = None

    balance_health, websocket_health = compute_cli_balance_websocket_health(
        balance_monitor_status=balance_monitor_status,
        balance_age_seconds=balance_age_seconds,
        balance_poll_interval_seconds=balance_poll_interval_seconds,
        websocket_status=websocket_status,
        ws_last_activity_seconds=ws_last_activity_seconds,
    )
    _warn_snapshot_step("portfolio_state", step_started)

    step_started = time.perf_counter()
    refresh_baseline = max(1.0, float(getattr(app, "_cli_refresh_interval_seconds", 2.0) or 2.0))
    freshness = "fresh"
    if market_age_seconds is not None and market_age_seconds > int(refresh_baseline * 5):
        freshness = "critical"
    elif market_age_seconds is not None and market_age_seconds > int(refresh_baseline * 2):
        freshness = "warning"

    # Include ALL whitelist coins in signal alignment, not just tradable ones
    try:
        _, document, whitelist_assets = app._load_runtime_pairlist_document()
        quote_asset = str(document.get("quote_asset") or "USDT").upper()
        all_signal_pairs = list(
            dict.fromkeys(
                trading_pairs
                + [
                    (f"{asset}{quote_asset}" if quote_asset == "USDT" else f"{quote_asset}_{asset}")
                    for asset in whitelist_assets
                    if (f"{asset}{quote_asset}" if quote_asset == "USDT" else f"{quote_asset}_{asset}")
                    not in trading_pairs
                ]
            )
        )
    except Exception:
        all_signal_pairs = list(trading_pairs or [])
    signal_alignment = _build_cli_signal_alignment(
        app,
        all_signal_pairs,
        bot_status.get("multi_timeframe"),
    )
    recent_events = _format_cli_recent_events(bot_status, limit=3)
    risk_summary = dict(bot_status.get("risk_summary") or {})
    candle_readiness = _summarize_cli_candle_readiness(bot_status.get("multi_timeframe"))
    candle_waiting = _summarize_cli_candle_waiting(bot_status.get("multi_timeframe"))
    balance_summary = app._get_cli_balance_summary(portfolio_state)
    _warn_snapshot_step("snapshot_sections", step_started)

    step_started = time.perf_counter()
    quote_asset = app._get_quote_asset()
    total_balance_quote = float(balance_summary.get("total_balance", 0.0) or 0.0)
    live_dashboard_for_fx = bool(getattr(app, "_live_dashboard_active", False))
    usdt_thb_rate = app._resolve_cli_asset_quote_rate("USDT", "THB", live_dashboard_for_fx)

    def _usdt_suffix(amt: float) -> str:
        return app._format_cli_usdt_thb_suffix(amt, usdt_thb_rate)

    balance_breakdown = build_balance_breakdown_lines(
        quote_asset=quote_asset,
        breakdown=list(balance_summary.get("breakdown") or []),
        total_balance_quote=total_balance_quote,
        usdt_thb_suffix=_usdt_suffix,
    )
    _warn_snapshot_step("balance_breakdown", step_started)

    cash_avail_quote = float(portfolio_state.get("balance", 0.0) or 0.0)
    available_balance_str, total_balance_str = quote_cash_totals_strings(
        quote_asset,
        cash_avail_quote,
        total_balance_quote,
        _usdt_suffix,
    )

    total_elapsed_ms = (time.perf_counter() - snapshot_started) * 1000.0
    if total_elapsed_ms >= 1000.0:
        logger.warning("[CLI PERF] get_cli_snapshot total took %.1fms", total_elapsed_ms)

    risk_portfolio_value = float(PortfolioRuntimeHelper.get_risk_portfolio_value(portfolio_state))
    try:
        min_balance_floor = float((app.config.get("portfolio") or {}).get("min_balance_threshold", 100.0) or 100.0)
    except (TypeError, ValueError):
        min_balance_floor = 100.0
    portfolio_meets_floor = risk_portfolio_value >= min_balance_floor
    risk_floor_display = (
        f"{risk_portfolio_value:.2f} / {min_balance_floor:.0f} {quote_asset} "
        f"({'OK' if portfolio_meets_floor else 'BELOW MIN — entries blocked'})"
    )

    return {
        "bot_name": bot_name or app._cli_bot_name,
        "mode": app._derive_cli_mode(bot_status),
        "strategy_mode": str(
            app.config.get("active_strategy_mode")
            or app.config.get("strategy_mode", {}).get("active")
            or "standard"
        ),
        "risk_level": risk_level,
        "risk_style": risk_style,
        "positions": positions,
        "pairs": ", ".join(trading_pairs) if trading_pairs else "NONE",
        "strategies": ", ".join((bot_status.get("strategy_engine") or {}).get("strategies") or []),
        "commands_hint": "Type in footer chat",
        "chat": app._get_cli_chat_snapshot(),
        "ui": {
            "log_level_filter": str(app._cli_log_level_filter or "INFO"),
            "footer_mode": str(app._cli_footer_mode or "compact"),
        },
        "updated_at": now_exchange_time().strftime("%H:%M:%S"),
        "signal_alignment": signal_alignment,
        "recent_events": recent_events,
        "system": {
            "last_market_update": app._format_cli_timestamp(last_market_raw),
            "market_age_seconds": market_age_seconds,
            "freshness": freshness,
            "api_latency": f"{api_latency_ms:.0f} ms" if api_latency_ms is not None else "-",
            "websocket_health": websocket_health,
            "websocket_last_error": str(websocket_status.get("last_error") or "")[:160],
            "balance_health": balance_health,
            "candle_readiness": candle_readiness,
            "candle_waiting": candle_waiting,
            "available_balance": available_balance_str,
            "total_balance": total_balance_str,
            "balance_breakdown": balance_breakdown,
            "trade_count": str(risk_summary.get("trades_today", bot_status.get("executed_today", 0))),
            "max_daily_trades": str(risk_summary.get("max_daily_trades", "-")),
            "daily_loss": (
                f"{risk_summary.get('daily_loss', 0):.2f} / {risk_summary.get('daily_loss_max', 0):.2f} {quote_asset}"
                if risk_summary
                else "-"
            ),
            "daily_loss_pct": f"{risk_summary.get('daily_loss_pct', 0):.2f}%" if risk_summary else "-",
            "max_open_positions": str(risk_summary.get("max_open_positions", "-")),
            "cooling_down": "Yes" if risk_summary.get("cooling_down") else "No",
            "risk_per_trade": f"{float((app.config.get('risk', {}) or {}).get('max_risk_per_trade_pct', 0.0) or 0.0):.1f}%",
            "risk_portfolio_value_quote": round(risk_portfolio_value, 4),
            "min_balance_threshold_quote": min_balance_floor,
            "portfolio_meets_trade_floor": portfolio_meets_floor,
            "risk_floor_display": risk_floor_display,
        },
        "auth_degraded_reason": str(app.config.get("auth_degraded_reason") or ""),
    }
