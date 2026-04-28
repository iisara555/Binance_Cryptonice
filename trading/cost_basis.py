from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def resolve_sane_entry_cost(
    *,
    symbol: str,
    amount: float,
    entry_price: float,
    reported_entry_cost: float,
) -> float:
    """Return a conservative entry cost when tracked cost drifts from amount*entry."""
    implied_entry_cost = float(amount) * float(entry_price)
    raw_entry_cost = float(reported_entry_cost or 0.0)

    if implied_entry_cost <= 0:
        return max(raw_entry_cost, 0.0)
    if raw_entry_cost <= 0:
        return implied_entry_cost

    allowed_gap = max(3.0, implied_entry_cost * 0.05)
    if abs(raw_entry_cost - implied_entry_cost) > allowed_gap:
        logger.warning(
            "[PnL Guard] %s entry_cost mismatch: reported=%.8f implied=%.8f (gap=%.8f). Using implied cost.",
            symbol,
            raw_entry_cost,
            implied_entry_cost,
            raw_entry_cost - implied_entry_cost,
        )
        return implied_entry_cost

    return raw_entry_cost
