"""Persist filled orders / trades to SQLite; history row lookup for state reconciliation."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from trading.order_history_utils import order_history_window_limit

logger = logging.getLogger(__name__)


def lookup_order_history_status(bot: Any, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
    """Fallback history lookup for an order when the info endpoint is inconclusive."""
    try:
        history = bot.api_client.get_order_history(symbol, limit=order_history_window_limit(getattr(bot, "config", {}) or {}))
    except Exception as e:
        logger.debug("[State] History lookup failed for %s: %s", order_id, e)
        return None

    for row in history:
        if str(row.get("id", "")) == str(order_id):
            return row
    return None


def log_filled_order(
    bot: Any,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    *,
    fee: float = 0.0,
    timestamp: Optional[datetime] = None,
    order_type: str = "limit",
) -> None:
    """Insert into orders + trades tables when a fill is confirmed."""
    if not getattr(bot, "db", None) or quantity <= 0 or price <= 0:
        return
    logged_at = timestamp or datetime.now(timezone.utc)
    try:
        bot.db.insert_order(
            pair=symbol,
            side=side,
            quantity=quantity,
            price=price,
            status="filled",
            order_type=order_type,
            fee=fee,
            timestamp=logged_at,
        )
    except Exception as exc:
        logger.error("[State] Failed to log filled %s order for %s: %s", side, symbol, exc, exc_info=True)
    try:
        bot.db.insert_trade(
            pair=symbol,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            timestamp=logged_at,
        )
    except Exception as exc:
        logger.error("[State] Failed to log filled %s trade for %s: %s", side, symbol, exc, exc_info=True)
