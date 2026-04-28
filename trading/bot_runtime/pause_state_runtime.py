"""Manual + monitoring reconciler pause reasons (orchestrator delegates here)."""

from __future__ import annotations

import logging
from typing import Any, Tuple

logger = logging.getLogger(__name__)


def is_paused(bot: Any) -> Tuple[bool, str]:
    """Return (is_paused, reason) checking both reconciliation and manual pause."""
    with bot._pause_state_lock:
        manual_paused = bool(getattr(bot, "_trading_paused", False))
        manual_reason = str(getattr(bot, "_pause_reason", "") or "")
    mon_paused = False
    mon_reason = ""
    monitoring = getattr(bot, "_monitoring", None)
    if monitoring and hasattr(monitoring, "_reconciler"):
        mon_paused, mon_raw = monitoring._reconciler.is_paused()
        mon_reason = str(mon_raw or "")
    if manual_paused and mon_paused:
        parts = [p for p in (manual_reason, mon_reason) if p]
        return True, (" | ".join(parts) if parts else "paused")
    if manual_paused:
        return True, manual_reason
    if mon_paused:
        return True, mon_reason
    return False, ""


def set_pause_reason(bot: Any, key: str, reason: str) -> None:
    with bot._pause_state_lock:
        bot._pause_reasons[str(key)] = str(reason)
        bot._trading_paused = True
        bot._pause_reason = " | ".join(bot._pause_reasons.values())
        pause_reason = bot._pause_reason
    logger.warning("Trading PAUSED: %s", pause_reason)


def clear_pause_reason(bot: Any, key: str) -> None:
    with bot._pause_state_lock:
        bot._pause_reasons.pop(str(key), None)
        bot._trading_paused = bool(bot._pause_reasons)
        bot._pause_reason = " | ".join(bot._pause_reasons.values())
        is_paused_now = bot._trading_paused
    if not is_paused_now:
        logger.info("Trading RESUMED - auto pause cleared")
