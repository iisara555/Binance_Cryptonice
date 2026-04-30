"""
Assemble CLI snapshot DTO fragments (no Rich). Called from ``TradingBotApp.get_cli_snapshot``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from state_facade import TradeStateFacade

logger = logging.getLogger(__name__)


def build_open_position_rows_for_cli(
    app: Any,
    open_orders: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build ``positions`` list for the command-center snapshot (prices, PnL, SL/TP)."""
    positions: List[Dict[str, Any]] = []
    bot = getattr(app, "bot", None)
    state_facade = getattr(bot, "_state_facade", None) if bot else None

    for position in open_orders:
        symbol = str(position.get("symbol") or "")
        side_value = position.get("side")
        side = str(getattr(side_value, "value", side_value) or "")
        entry_price = float(position.get("entry_price") or 0.0)
        bootstrap_src = position.get("bootstrap_source") or ""
        strategy_source = (
            str(
                position.get("strategy_source")
                or position.get("source_strategy")
                or position.get("signal_strategy")
                or "-"
            ).strip()
            or "-"
        )
        if strategy_source == "-":
            trig = str(position.get("trigger") or "").strip().lower()
            if trig == "manual_import":
                strategy_source = "manual"
            else:
                boot = str(position.get("bootstrap_source") or "").strip()
                if boot:
                    strategy_source = "bootstrap"
        current_price = app._get_cli_price(symbol, False) if symbol else None
        if (not current_price or current_price <= 0) and symbol:
            cached = app._cli_price_cache.get(symbol)
            if cached:
                current_price = cached[0]
        if (not current_price or current_price <= 0) and symbol:
            current_price = app._get_cli_position_price_hint(symbol, include_entry_price=False)

        pnl_pct = None
        if current_price and entry_price > 0:
            if side.lower() == "sell":
                pnl_pct = ((entry_price - current_price) / entry_price) * 100.0
            else:
                pnl_pct = ((current_price - entry_price) / entry_price) * 100.0
        if str(bootstrap_src) == "estimated_from_ticker":
            pnl_pct = None

        current_price_value: Optional[float] = None
        try:
            if current_price is not None:
                current_price_value = float(current_price)
        except (TypeError, ValueError):
            current_price_value = None

        pos_sl = position.get("stop_loss")
        pos_tp = position.get("take_profit")
        if isinstance(state_facade, TradeStateFacade):
            pos_sl, pos_tp = state_facade.enrich_sl_tp_from_snapshot(symbol, pos_sl, pos_tp)
        elif (not pos_sl or float(pos_sl or 0) == 0) or (not pos_tp or float(pos_tp or 0) == 0):
            _state_manager = getattr(bot, "_state_manager", None) if bot else None
            if _state_manager and symbol:
                try:
                    snapshot = _state_manager.get_state(symbol)
                    if not pos_sl or float(pos_sl or 0) == 0:
                        pos_sl = snapshot.stop_loss if snapshot.stop_loss else pos_sl
                    if not pos_tp or float(pos_tp or 0) == 0:
                        pos_tp = snapshot.take_profit if snapshot.take_profit else pos_tp
                except Exception as exc:
                    logger.debug("[CLI] Failed loading persisted SL/TP for %s: %s", symbol, exc)

        sl_distance_pct = None
        tp_distance_pct = None
        if current_price_value is not None and current_price_value > 0.0:
            if pos_sl is not None:
                try:
                    sl_distance_pct = ((float(pos_sl) - current_price_value) / current_price_value) * 100.0
                except (TypeError, ValueError):
                    sl_distance_pct = None
            if pos_tp is not None:
                try:
                    tp_distance_pct = ((float(pos_tp) - current_price_value) / current_price_value) * 100.0
                except (TypeError, ValueError):
                    tp_distance_pct = None

        positions.append(
            {
                "symbol": symbol or "-",
                "side": side or "buy",
                "entry_price": entry_price,
                "current_price": current_price,
                "pnl_pct": pnl_pct,
                "stop_loss": pos_sl,
                "take_profit": pos_tp,
                "sl_distance_pct": sl_distance_pct,
                "tp_distance_pct": tp_distance_pct,
                "bootstrap_source": bootstrap_src,
                "strategy_source": strategy_source,
            }
        )

    return positions


def compute_cli_balance_websocket_health(
    *,
    balance_monitor_status: Dict[str, Any],
    balance_age_seconds: Optional[int],
    balance_poll_interval_seconds: float,
    websocket_status: Dict[str, Any],
    ws_last_activity_seconds: Optional[int],
) -> tuple[str, str]:
    """Return ``(balance_health, websocket_health)`` display strings."""
    balance_stale_after_seconds = max(balance_poll_interval_seconds * 2.0, 60.0)

    if not balance_monitor_status.get("enabled", False):
        balance_health = "OFF"
    elif not balance_monitor_status.get("running", False):
        balance_health = "STOPPED"
    elif balance_age_seconds is None:
        balance_health = "NO DATA"
    elif balance_age_seconds > balance_stale_after_seconds:
        balance_health = f"STALE {balance_age_seconds}s"
    else:
        balance_health = f"OK {balance_age_seconds}s"

    ws_state = str(websocket_status.get("state") or "not_started").lower()
    if not websocket_status.get("enabled", False):
        websocket_health = "OFF"
    elif ws_state != "connected":
        websocket_health = ws_state.upper()
    elif ws_last_activity_seconds is None:
        websocket_health = "NO DATA"
    elif ws_last_activity_seconds > 30:
        websocket_health = f"STALE {ws_last_activity_seconds}s"
    else:
        websocket_health = f"OK {ws_last_activity_seconds}s"

    return balance_health, websocket_health
