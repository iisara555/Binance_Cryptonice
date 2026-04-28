"""Apply runtime pair list updates and refresh WebSocket subscriptions."""

from __future__ import annotations

import logging
from typing import Any, List

logger = logging.getLogger(__name__)


def update_runtime_pairs(bot: Any, pairs: List[str], reason: str = "runtime refresh") -> List[str]:
    """Normalize pair list, sync config, refresh or stop WebSocket when needed."""
    normalized: List[str] = []
    seen: set[str] = set()
    for pair in pairs or []:
        value = str(pair or "").strip().upper()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)

    current_pairs = list(bot.trading_pairs)
    if normalized == current_pairs:
        return current_pairs

    bot.config.setdefault("data", {})["pairs"] = list(normalized)
    bot.trading_pairs = list(normalized)
    bot.trading_pair = normalized[0] if normalized else ""
    bot.config["trading_pair"] = bot.trading_pair
    bot.config.setdefault("trading", {})["trading_pair"] = bot.trading_pair

    if getattr(bot, "_ws_enabled", False) and getattr(bot, "_ws_import_ok", False):
        try:
            if normalized:
                bot._start_or_refresh_websocket(normalized, reason=f"pair_update:{reason}")
            else:
                import trading_bot as tb_module

                if tb_module.stop_websocket:
                    tb_module.stop_websocket()
                    bot._ws_client = None
        except Exception as exc:
            logger.error("Failed to refresh WebSocket subscriptions for %s: %s", normalized, exc)

    bot._multi_timeframe_status_cache = {"data": None, "timestamp": 0.0}

    logger.info("[Pairs] Runtime pairs updated via %s: %s -> %s", reason, current_pairs, normalized)
    return normalized
