"""Multi-timeframe candle counts for pair readiness gating."""

from typing import Any, Dict, Optional

MIN_CANDLES_FOR_TRADING_READINESS = 35
MIN_READINESS_CANDLES_CAP = 2000
MIN_READINESS_CANDLES_FLOOR = 5


def required_candles_for_trading_readiness(mtf_config: Optional[Dict[str, Any]]) -> int:
    """Minimum stored OHLC rows per gated timeframe before a pair is MTF-ready."""
    cfg = dict(mtf_config or {})
    raw = cfg.get("required_candles_for_readiness", MIN_CANDLES_FOR_TRADING_READINESS)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = int(MIN_CANDLES_FOR_TRADING_READINESS)
    return max(MIN_READINESS_CANDLES_FLOOR, min(n, MIN_READINESS_CANDLES_CAP))
