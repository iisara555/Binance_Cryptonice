"""WebSocket lifecycle for the orchestrator (lazy import of trading_bot ws symbols to avoid cycles)."""

from __future__ import annotations

import logging
import time
from typing import Any, List

logger = logging.getLogger(__name__)


def start_or_refresh_websocket(bot: Any, symbols: List[str], reason: str = "runtime") -> bool:
    import trading_bot as tb_module

    if not (getattr(bot, "_ws_enabled", False) and getattr(bot, "_ws_import_ok", False) and tb_module.get_websocket):
        return False
    normalized_symbols = [str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()]
    if not normalized_symbols:
        return False

    bot._last_ws_start_attempt_at = time.time()
    ws = tb_module.get_websocket(symbols=normalized_symbols, on_tick=bot._on_ws_tick)  # type: ignore[misc]
    if ws is None:
        logger.warning(
            "WebSocket backend returned no client (%s) | reason=%s | symbols=%s",
            getattr(tb_module, "_WEBSOCKET_BACKEND", "unknown"),
            reason,
            normalized_symbols,
        )
        return False
    bot._ws_client = ws
    logger.info(
        "WebSocket ready (%s) | reason=%s | symbols=%s",
        getattr(tb_module, "_WEBSOCKET_BACKEND", "unknown"),
        reason,
        normalized_symbols,
    )
    return True


def ensure_websocket_started(bot: Any) -> None:
    ws_enabled = bool(getattr(bot, "_ws_enabled", False))
    ws_import_ok = bool(getattr(bot, "_ws_import_ok", False))
    pairs = getattr(bot, "trading_pairs", None)
    trading_pairs = list(pairs) if pairs else []
    if not (ws_enabled and ws_import_ok and trading_pairs):
        return
    ws_client = getattr(bot, "_ws_client", None)
    if ws_client is not None:
        try:
            is_connected = getattr(ws_client, "is_connected", None)
            if callable(is_connected) and bool(is_connected()):
                return
        except Exception as exc:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("WebSocket is_connected() check failed: %s", exc)
        raw_state = getattr(ws_client, "state", "")
        state_text = str(getattr(raw_state, "value", raw_state) or "").strip().lower()
        if state_text in {"connecting", "reconnecting", "connected"}:
            return
    now_ts = time.time()
    retry_s = float(getattr(bot, "_ws_start_retry_interval_seconds", 20.0) or 20.0)
    if (now_ts - float(getattr(bot, "_last_ws_start_attempt_at", 0.0) or 0.0)) < retry_s:
        return
    try:
        start_or_refresh_websocket(bot, list(bot.trading_pairs), reason="retry")
    except Exception as exc:
        logger.warning("WebSocket retry start failed: %s", exc)
