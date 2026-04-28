"""Filter trading pairs by MTF candle readiness; optional collector warm backfill."""

from __future__ import annotations

import logging
import time
from typing import Any, List

logger = logging.getLogger(__name__)


def filter_pairs_by_candle_readiness(bot: Any, pairs: List[str], allow_refresh: bool = True) -> List[str]:
    """Subset of ``pairs`` that have complete candle readiness when MTF is enabled."""
    if not pairs or not bool(getattr(bot, "mtf_enabled", False)):
        bot._last_candle_readiness_skipped = ()
        return list(pairs)

    try:
        mtf_status = bot._get_dashboard_multi_timeframe_status(allow_refresh=allow_refresh) or {}
    except Exception as exc:
        logger.debug("Failed to evaluate candle readiness filter: %s", exc)
        return list(pairs)

    pair_rows = list(mtf_status.get("pairs") or [])
    if not pair_rows:
        bot._last_candle_readiness_skipped = ()
        return list(pairs)

    ready_pairs = {str(row.get("pair") or "").upper() for row in pair_rows if row.get("ready")}
    filtered_pairs = [pair for pair in pairs if str(pair).upper() in ready_pairs]
    skipped = tuple(pair for pair in pairs if pair not in filtered_pairs)

    if skipped:
        if skipped != getattr(bot, "_last_candle_readiness_skipped", None):
            logger.info("[Candle Guard] Skipping pairs without complete candle readiness: %s", list(skipped))
            bot._last_candle_readiness_skipped = skipped
        collector = getattr(bot, "collector", None)
        should_backfill = bool(
            allow_refresh and collector is not None and getattr(collector, "multi_timeframe_enabled", False)
        )
        if should_backfill:
            now_ts = time.time()
            if (now_ts - float(getattr(bot, "_last_candle_backfill_attempt_at", 0.0) or 0.0)) >= 120.0:
                try:
                    bot._last_candle_backfill_attempt_at = now_ts
                    collector._warm_pairs_backfill(list(skipped))
                    bot._multi_timeframe_status_cache = {"data": None, "timestamp": 0.0}
                    logger.info("[Candle Guard] Triggered warm backfill for lagging pairs: %s", list(skipped))
                except Exception as exc:
                    logger.debug("Warm backfill trigger failed for %s: %s", list(skipped), exc)
    else:
        bot._last_candle_readiness_skipped = ()

    return filtered_pairs
