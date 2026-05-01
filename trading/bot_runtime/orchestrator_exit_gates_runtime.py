"""Minimal ROI table exit hints and voluntary exit min-profit gate (SIGSELL/TIME)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional, Tuple

from helpers import calc_net_pnl
from minimal_roi import compute_net_profit_pct

logger = logging.getLogger(__name__)


def coerce_opened_at(value: Any) -> Optional[datetime]:
    """Parse persisted or exchange ``opened_at`` into naive local datetime."""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def minimal_roi_exit_signal(
    bot: Any,
    *,
    symbol: str,
    side: Any,
    entry_price: float,
    current_price: float,
    opened_at: Any,
) -> Tuple[bool, str]:
    """Return (should_consider_exit, detail_reason) from minimal-roi stepping tables."""
    if not bool(getattr(bot, "_minimal_roi_enabled", False)):
        return False, ""
    tables = getattr(bot, "_minimal_roi_tables", {}) or {}
    mode = str(getattr(bot, "_active_strategy_mode", "standard") or "standard")
    table = tables.get(mode) or tables.get("standard") or next(iter(tables.values()), None)
    if table is None:
        return False, ""
    opened_dt = coerce_opened_at(opened_at)
    if opened_dt is None:
        return False, ""
    hold_minutes = max((datetime.now() - opened_dt).total_seconds() / 60.0, 0.0)
    side_value = str(getattr(side, "value", side) or "BUY").upper()
    net_profit = compute_net_profit_pct(
        float(entry_price or 0.0),
        float(current_price or 0.0),
        side=side_value,
    )
    hit, reason = table.should_exit(net_profit, hold_minutes)
    if hit:
        logger.info("[MinimalROI] %s exit allowed: %s", symbol, reason)
    return hit, reason


def should_allow_voluntary_exit(
    bot: Any,
    symbol: str,
    trigger: str,
    entry_price: float,
    exit_price: float,
    amount: float,
    total_entry_cost: float = 0.0,
    side: Any = "buy",
) -> bool:
    """Block SIGSELL/TIME exits when net PnL%% is below configured floor (optional)."""
    trigger_value = str(trigger or "").upper()
    if trigger_value not in {"SIGSELL", "TIME"}:
        return True
    if not bool(getattr(bot, "_enforce_min_profit_gate_for_voluntary_exit", False)):
        return True

    amount_value = float(amount or 0.0)
    entry_value = float(entry_price or 0.0)
    exit_value = float(exit_price or 0.0)
    entry_cost = float(total_entry_cost or 0.0)
    if amount_value <= 0 or entry_value <= 0 or exit_value <= 0:
        return True
    if entry_cost <= 0:
        entry_cost = entry_value * amount_value
    if entry_cost <= 0:
        return True

    from execution import BINANCE_TH_FEE_PCT

    side_value = str(getattr(side, "value", side) or "buy").lower()

    pnl = calc_net_pnl(
        entry_cost=entry_cost,
        exit_price=exit_value,
        quantity=amount_value,
        side=side_value,
        fee_pct=BINANCE_TH_FEE_PCT,
    )
    net_pnl_pct = float(pnl.get("net_pnl_pct", 0.0) or 0.0)
    min_net_profit_pct = float(getattr(bot, "_min_voluntary_exit_net_profit_pct", 0.0) or 0.0)
    if net_pnl_pct >= min_net_profit_pct:
        return True

    logger.info(
        "[ExitGate] Suppressed %s exit for %s | entry=%.2f exit=%.2f amount=%.8f | net_pnl_pct=%.3f < %.3f",
        trigger_value,
        str(symbol or "").upper(),
        entry_value,
        exit_value,
        amount_value,
        net_pnl_pct,
        min_net_profit_pct,
    )
    return False
