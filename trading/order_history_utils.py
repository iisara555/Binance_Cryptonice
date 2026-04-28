"""Order history row parsing (OMS / bootstrap) — shared by trading_bot and runtimes."""

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from helpers import normalize_side_value
from trading.coercion import coerce_trade_float


def history_timestamp_value(row: Optional[Dict[str, Any]]) -> datetime:
    """Normalize exchange order-history row timestamps to ``datetime``."""
    if not row:
        return datetime.min

    raw_ts = row.get("ts") or row.get("timestamp") or row.get("created_at") or row.get("updated_at")
    if isinstance(raw_ts, datetime):
        return raw_ts
    if isinstance(raw_ts, (int, float)):
        try:
            ts_value = float(raw_ts)
            if ts_value > 1e12:
                ts_value /= 1000.0
            return datetime.fromtimestamp(ts_value)
        except (OverflowError, OSError, ValueError):
            return datetime.min
    if isinstance(raw_ts, str) and raw_ts.strip():
        try:
            return datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min
    return datetime.min


def order_history_window_limit(config: Optional[Dict[str, Any]]) -> int:
    cfg = config or {}
    data_config = dict(cfg.get("data", {}) or {})
    raw_limit = data_config.get("order_history_limit", cfg.get("order_history_limit", 200))
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 200
    return max(50, min(limit, 500))


def history_status_value(row: Optional[Dict[str, Any]]) -> str:
    if not row:
        return ""
    return str(row.get("status") or row.get("typ") or "").lower()


def history_side_value(row: Optional[Dict[str, Any]]) -> str:
    if not row:
        return ""
    return normalize_side_value(row.get("side") or row.get("sd") or "")


def history_status_is_filled(row: Optional[Dict[str, Any]]) -> bool:
    return history_status_value(row) in ("filled", "match", "done", "complete")


def history_status_is_cancelled(row: Optional[Dict[str, Any]]) -> bool:
    return history_status_value(row) in ("cancel", "cancelled")


def extract_history_fill_details(
    row: Optional[Dict[str, Any]],
    *,
    fallback_amount: float = 0.0,
    fallback_price: float = 0.0,
    fallback_cost: float = 0.0,
) -> Tuple[float, float]:
    if not row:
        return fallback_amount, fallback_price

    fill_price = (
        coerce_trade_float(row.get("filled_price"))
        or coerce_trade_float(row.get("avg_price"))
        or coerce_trade_float(row.get("rate"))
        or coerce_trade_float(row.get("rat"))
        or coerce_trade_float(row.get("price"))
        or fallback_price
    )
    history_side = history_side_value(row)
    explicit_base_amount = (
        coerce_trade_float(row.get("filled"))
        or coerce_trade_float(row.get("filled_amount"))
        or coerce_trade_float(row.get("executed_amount"))
        or coerce_trade_float(row.get("executed"))
        or coerce_trade_float(row.get("rec"))
    )
    if explicit_base_amount > 0:
        fill_amount = explicit_base_amount
    else:
        raw_amount = coerce_trade_float(row.get("amount"))
        raw_cost = coerce_trade_float(row.get("amt")) or raw_amount
        fee_value = coerce_trade_float(row.get("fee"))
        if history_side in ("buy", "bid") and raw_cost > 0 and fill_price > 0:
            net_cost = raw_cost - fee_value if fee_value > 0 and raw_cost > fee_value else raw_cost
            fill_amount = net_cost / fill_price if net_cost > 0 else 0.0
        else:
            fill_amount = raw_amount or raw_cost or fallback_amount
    if fill_amount <= 0 and fill_price > 0 and fallback_cost > 0:
        fill_amount = fallback_cost / fill_price
    return fill_amount, fill_price
