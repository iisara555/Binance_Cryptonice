"""Candle / price-history retention policy and SQLite cleanup."""

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def default_candle_retention_policy() -> Dict[str, int]:
    return {
        "1m": 7,
        "5m": 14,
        "15m": 30,
        "1h": 60,
        "4h": 90,
        "1d": 180,
    }


def build_candle_retention_policy(config: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Merge YAML ``candle_retention`` (``timeframes`` map) onto defaults."""
    policy = default_candle_retention_policy()
    raw_policy = dict((config or {}).get("timeframes") or {})
    for timeframe, raw_days in raw_policy.items():
        normalized_timeframe = str(timeframe or "").strip()
        if not normalized_timeframe:
            continue
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            logger.warning("[Retention] Invalid retention value for %s: %r", normalized_timeframe, raw_days)
            continue
        if days <= 0:
            logger.warning("[Retention] Ignoring non-positive retention for %s: %s", normalized_timeframe, days)
            continue
        policy[normalized_timeframe] = days
    return policy


def run_candle_retention_cleanup(bot: Any, reason: str) -> None:
    """Execute retention delete + optional VACUUM; updates ``bot._last_candle_retention_cleanup_at``."""
    if not getattr(bot, "_candle_retention_enabled", False):
        return
    policy = getattr(bot, "_candle_retention_policy", None) or {}
    if not policy:
        return

    db = getattr(bot, "db", None)
    if db is None:
        return

    try:
        deleted = db.cleanup_price_history_by_timeframe(policy)
        bot._last_candle_retention_cleanup_at = time.time()
        total_deleted = int(deleted.get("total", 0) or 0)
        detail = ", ".join(
            f"{timeframe}={int(deleted.get(timeframe, 0) or 0)}" for timeframe in policy.keys()
        )
        logger.info("[Retention] Candle cleanup (%s) removed %d row(s): %s", reason, total_deleted, detail)

        vacuum = bool(getattr(bot, "_candle_retention_vacuum", False))
        if total_deleted > 0 and vacuum:
            if db.vacuum():
                logger.info("[Retention] SQLite VACUUM completed after %s cleanup", reason)
            else:
                logger.warning("[Retention] SQLite VACUUM skipped/failed after %s cleanup", reason)
    except Exception as exc:
        logger.warning("[Retention] Candle cleanup (%s) failed: %s", reason, exc)


def maybe_run_scheduled_candle_retention(bot: Any) -> None:
    if not getattr(bot, "_candle_retention_enabled", False) or not getattr(bot, "_candle_retention_policy", None):
        return

    now_ts = time.time()
    last = float(getattr(bot, "_last_candle_retention_cleanup_at", 0.0) or 0.0)
    if last <= 0:
        bot._last_candle_retention_cleanup_at = now_ts
        return

    interval = float(getattr(bot, "_candle_retention_interval_seconds", 3600))
    if (now_ts - last) < interval:
        return

    run_candle_retention_cleanup(bot, "scheduled")
