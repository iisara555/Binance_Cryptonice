"""Small shared parsers for orchestration/runtime (DB/API numeric fields)."""

from typing import Any


def coerce_trade_float(val: Any, default: float = 0.0) -> float:
    """Normalize DB/API numeric fields; avoids ``None > 0`` TypeErrors in comparisons."""
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default
