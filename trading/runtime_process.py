"""Process-level logging, signals, faulthandler, and startup-auth degraded mode (extracted from ``main``)."""

from __future__ import annotations

import faulthandler
import logging
import signal
import sys
from typing import Any, Dict, Iterable, List, Optional

from api_client import BinanceThClient
from data_collector import BinanceThCollector
from logger_setup import get_shared_console
from logger_setup import setup_logging as configure_application_logging
from process_guard import release_bot_lock
from project_paths import PROJECT_ROOT
from trading_bot import TradingBotOrchestrator
from trading.cli_pair_normalize import normalize_pairs

logger = logging.getLogger(__name__)

_FAULT_HANDLER_STREAM = None


def setup_logging(level: str = "INFO", yaml_config: Optional[Dict[str, Any]] = None) -> None:
    """Setup the shared production logging stack for the app.

    If *yaml_config* contains a ``logging:`` section, those values
    override the built-in defaults (max size, retention, etc.).
    """
    from logger_setup import load_logging_config

    log_cfg = load_logging_config(yaml_config)
    configure_application_logging(
        log_level=level,
        enable_console=log_cfg.get("enable_console", True),
        enable_files=log_cfg.get("enable_files", True),
        log_directory=str(PROJECT_ROOT / "logs"),
        max_log_size_mb=log_cfg.get("max_log_size_mb"),
        backup_count=log_cfg.get("backup_count"),
        debug_retention_days=log_cfg.get("debug_retention_days"),
        cleanup_on_startup=log_cfg.get("cleanup_on_startup", True),
        console=get_shared_console(),
        use_rich_console=log_cfg.get("enable_console", True),
    )


def configure_faulthandler_logging() -> None:
    """Enable on-demand Python stack dumps to a persistent log file for VPS debugging."""
    global _FAULT_HANDLER_STREAM
    if _FAULT_HANDLER_STREAM is not None:
        return

    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    fault_log_path = logs_dir / "faulthandler.log"
    try:
        _FAULT_HANDLER_STREAM = open(fault_log_path, "a", encoding="utf-8")
        faulthandler.enable(file=_FAULT_HANDLER_STREAM, all_threads=True)
        fault_signal = getattr(signal, "SIGUSR1", None)
        fault_register = getattr(faulthandler, "register", None)
        if fault_signal is not None and callable(fault_register):
            fault_register(fault_signal, file=_FAULT_HANDLER_STREAM, all_threads=True, chain=False)
        logger.info("Faulthandler enabled | path=%s", fault_log_path)
    except Exception as exc:
        logger.warning("Failed to enable faulthandler logging: %s", exc)


def setup_signal_handlers(
    bot: TradingBotOrchestrator,
    collector: BinanceThCollector,
    telegram_handler: Optional[Any] = None,
) -> None:
    """
    Setup signal handlers for graceful shutdown.

    Handles:
    - SIGINT (Ctrl+C)
    - SIGTERM (kill command)
    """

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, starting graceful shutdown...")

        try:
            if bot:
                bot.stop()

            if telegram_handler:
                telegram_handler.stop()

            if collector:
                collector.stop()
        finally:
            try:
                release_bot_lock()
            except Exception as lock_err:
                logger.warning("Failed to release bot lock during signal shutdown: %s", lock_err)

        logger.info("Shutdown complete")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


def clear_startup_auth_shutdown_state(api_client: Optional[BinanceThClient] = None) -> None:
    """Consume a startup auth failure so the app can continue in degraded mode."""
    import api_client as api_module

    api_module.SHOULD_SHUTDOWN = False
    api_module.SHUTDOWN_REASON = ""

    circuit_breaker = getattr(api_client, "_cb", None)
    if circuit_breaker and hasattr(circuit_breaker, "reset"):
        try:
            circuit_breaker.reset()
        except Exception as exc:
            logger.debug("Failed to reset exchange circuit breaker during startup degrade: %s", exc)


def enable_startup_auth_degraded_mode(
    config: Dict[str, Any],
    reason: str,
    configured_pairs: Optional[Iterable[str]] = None,
) -> List[str]:
    """Force a safe public-only startup mode when private exchange auth is unavailable."""
    data_config = config.setdefault("data", {})
    trading_config = config.setdefault("trading", {})
    fallback_pairs = normalize_pairs(
        configured_pairs
        or data_config.get("pairs")
        or [trading_config.get("trading_pair") or config.get("trading_pair") or ""]
    )

    config["auth_degraded"] = True
    config["auth_degraded_reason"] = reason
    config["mode"] = "dry_run"
    trading_config["mode"] = "dry_run"
    config["simulate_only"] = True
    config["read_only"] = True
    data_config["auto_detect_held_pairs"] = False
    data_config["pairs"] = fallback_pairs
    rebalance_config = config.setdefault("rebalance", {})
    rebalance_config["enabled"] = False

    top_level_pair = fallback_pairs[0] if fallback_pairs else ""
    config["trading_pair"] = top_level_pair
    trading_config["trading_pair"] = top_level_pair
    return fallback_pairs


# Private-name aliases for ``main`` re-exports
_clear_startup_auth_shutdown_state = clear_startup_auth_shutdown_state
_enable_startup_auth_degraded_mode = enable_startup_auth_degraded_mode
