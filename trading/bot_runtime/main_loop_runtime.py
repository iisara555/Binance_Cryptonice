"""Main trading loop body (timing, maintenance, iteration) — orchestrator facade stays thin."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from alerts import AlertLevel, format_fatal_auth_alert
from api_client import BinanceAuthException
from .websocket_runtime import ensure_websocket_started

logger = logging.getLogger(__name__)


def run_trading_main_loop(bot: Any) -> None:
    """Core ``while bot.running`` loop: WS ensure → retention/DB hooks → `_run_iteration` → sleep.

    Mirrors previous ``TradingBotOrchestrator._main_loop``; kept here to shrink orchestrator surface.
    """
    while bot.running:
        try:
            ensure_websocket_started(bot)
            bot._last_loop_time = datetime.now()
            bot._loop_count += 1
            bot._maybe_run_candle_retention_cleanup()
            bot._maybe_run_db_maintenance()

            logger.debug("Loop #%s started at %s", getattr(bot, "_loop_count", 0), getattr(bot, "_last_loop_time", None))

            bot._run_iteration()

        except BinanceAuthException as exc:
            logger.critical("🚨 GRACEFUL SHUTDOWN: %s", exc.message)
            alert_system = getattr(bot, "alert_system", None)
            if alert_system is not None:
                try:
                    title = "FATAL: Exchange Auth Error"
                    alert_system.send(
                        AlertLevel.CRITICAL,
                        format_fatal_auth_alert(exc.message, title=title),
                    )
                except Exception as alert_exc:
                    logger.warning("Failed to send fatal auth alert: %s", alert_exc)
            logger.critical("Graceful shutdown — check API Key/Secret in .env and restart the bot")
            bot.running = False
            break

        except Exception as e:
            logger.error("Main loop error: %s", e, exc_info=True)

        elapsed = (datetime.now() - (bot._last_loop_time or datetime.now())).total_seconds()
        sleep_time = max(1, getattr(bot, "interval_seconds", 60) - elapsed)
        # Use shutdown event for instant exit response instead of blocking sleep.
        shutdown_event = getattr(bot, "_shutdown_event", None)
        if shutdown_event is not None:
            shutdown_event.wait(timeout=sleep_time)
        else:
            time.sleep(sleep_time)

