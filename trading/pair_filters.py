"""
Optional Binance spot pair filters (24h quote volume) — no Freqtrade dependency.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Tuple

if TYPE_CHECKING:
    from api_client import BinanceThClient

logger = logging.getLogger(__name__)


def filter_pairs_by_min_quote_volume(
    client: "BinanceThClient",
    pairs: List[str],
    min_quote_volume_24h: float,
) -> Tuple[List[str], List[str]]:
    """
    Drop pairs whose rolling 24h ``quote_volume`` (in quote, e.g. USDT) is below the floor.

    Returns ``(filtered_pairs, warnings)``. On API failure, returns the original list with a warning.
    """
    warnings: List[str] = []
    floor = float(min_quote_volume_24h or 0.0)
    if floor <= 0 or not pairs:
        return list(pairs), warnings

    try:
        batch = client.get_tickers_batch([str(p).strip() for p in pairs if str(p or "").strip()])
    except Exception as exc:
        warnings.append(f"24h volume filter skipped (ticker batch failed): {exc}")
        return list(pairs), warnings

    kept: List[str] = []
    for sym in pairs:
        raw = str(sym or "").strip()
        if not raw:
            continue
        row: Dict[str, Any] = batch.get(raw) or batch.get(raw.upper()) or {}
        qv = float(row.get("quote_volume") or 0.0)
        if qv >= floor:
            kept.append(raw)
        else:
            bs = getattr(client, "_to_binance_symbol", lambda x: x)(raw)
            warnings.append(f"{raw} ({bs}): quote_volume_24h={qv:.2f} below min {floor:.2f} — excluded")
    return kept, warnings
