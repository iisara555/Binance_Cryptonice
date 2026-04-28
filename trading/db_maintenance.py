"""Periodic SQLite maintenance (old row prune, WAL checkpoint, executed-today cap)."""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def maybe_run_periodic_db_maintenance(bot: Any) -> None:
    """Once-per-interval cleanup + WAL checkpoint; trims ``_executed_today`` if oversized."""
    now_ts = time.time()
    last = float(getattr(bot, "_last_db_maintenance_at", 0.0) or 0.0)
    if last <= 0:
        bot._last_db_maintenance_at = now_ts
        return

    interval = float(getattr(bot, "_db_maintenance_interval_seconds", 24 * 3600))
    if (now_ts - last) < interval:
        return

    db = getattr(bot, "db", None)
    try:
        if db is not None:
            deleted = db.cleanup_old_data(days=90)
            total = sum(deleted.values())
            logger.info(
                "[Maintenance] DB cleanup removed %d row(s): %s",
                total,
                ", ".join(f"{k}={v}" for k, v in deleted.items()),
            )
            conn = db.get_connection()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
            logger.info("[Maintenance] WAL checkpoint (TRUNCATE) completed")
    except Exception as exc:
        logger.warning("[Maintenance] DB maintenance failed: %s", exc)

    bot._last_db_maintenance_at = now_ts

    executed = getattr(bot, "_executed_today", None)
    max_kept = int(getattr(bot, "_executed_today_max", 200))
    if isinstance(executed, list) and len(executed) > max_kept:
        bot._executed_today = executed[-max_kept:]
