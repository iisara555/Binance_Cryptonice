"""
Crypto Trading Bot - Main Entry Point
======================================
Entry point หลัก:
- Load config
- Start collector (background)
- Start trading bot (main loop)
- Graceful shutdown
"""

import sys
import os
import time
import logging
import signal
import threading
import json
import shlex
import re
from datetime import datetime
from os import PathLike
from pathlib import Path
from typing import Optional, Dict, Any, List, Iterable

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows fallback
    msvcrt = None  # type: ignore[assignment]

# Linux raw terminal input support
_termios = None
try:
    import termios as _termios  # type: ignore[import-untyped]
except ImportError:
    pass

# Add project root to path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import project modules
from config import BITKUB, TRADING, validate_config
from api_client import BitkubClient, BitkubAPIError
from data_collector import BitkubCollector
from signal_generator import SignalGenerator, get_latest_signal_flow_snapshot
from risk_management import RiskManager, RiskConfig, get_default_sl_tp
from trade_executor import TradeExecutor
from trading_bot import TradingBotOrchestrator
from telegram_bot import TelegramBotHandler
from alerts import AlertSystem
from dynamic_coin_config import (
    DEFAULT_WHITELIST_JSON,
    HybridDynamicPairResolver,
    JsonCoinWhitelistRepository,
    resolve_whitelist_path,
)
from logger_setup import setup_logging as configure_application_logging
from logger_setup import get_shared_console
from health_server import BotHealthServer
from process_guard import acquire_bot_lock, release_bot_lock, get_lock_status
from helpers import format_bitkub_time, get_current_price, now_bitkub, parse_as_bitkub_time
from cli_ui import CLICommandCenter

logger = logging.getLogger(__name__)


def _normalize_pairs(pairs: Iterable[str]) -> List[str]:
    """Normalize pair strings and drop blanks while preserving order."""
    normalized: List[str] = []
    seen: set[str] = set()
    for pair in pairs or []:
        value = str(pair or "").strip().upper()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _normalize_cli_pair(value: Any) -> str:
    """Normalize user-facing pair inputs like BTC, THB_BTC, or BTC_THB."""
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    if raw.startswith("THB_"):
        return raw
    if raw.endswith("_THB"):
        return f"THB_{raw.split('_', 1)[0]}"
    if "_" not in raw:
        return f"THB_{raw}"
    return raw


def _extract_asset_from_pair(value: Any) -> str:
    normalized = _normalize_cli_pair(value)
    if normalized.startswith("THB_"):
        return normalized.split("_", 1)[1]
    return normalized


def _clear_startup_auth_shutdown_state(api_client: Optional[BitkubClient] = None) -> None:
    """Consume a startup auth failure so the app can continue in degraded mode."""
    import api_client as api_module

    api_module.SHOULD_SHUTDOWN = False
    api_module.SHUTDOWN_REASON = ""

    circuit_breaker = getattr(api_client, "_cb", None)
    if circuit_breaker and hasattr(circuit_breaker, "reset"):
        try:
            circuit_breaker.reset()
        except Exception as exc:
            logger.debug("Failed to reset Bitkub circuit breaker during startup degrade: %s", exc)


def _enable_startup_auth_degraded_mode(
    config: Dict[str, Any],
    reason: str,
    configured_pairs: Optional[Iterable[str]] = None,
) -> List[str]:
    """Force a safe public-only startup mode when private Bitkub auth is unavailable."""
    data_config = config.setdefault("data", {})
    trading_config = config.setdefault("trading", {})
    fallback_pairs = _normalize_pairs(
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


def _get_hybrid_dynamic_coin_settings(data_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    settings = dict((data_config or {}).get("hybrid_dynamic_coin_config") or {})
    settings.setdefault("whitelist_json_path", DEFAULT_WHITELIST_JSON)
    settings.setdefault("min_quote_balance_thb", max(float(TRADING.min_order_amount or 0.0), 100.0))
    settings.setdefault("require_supported_market", True)
    settings.setdefault("include_assets_with_balance", True)
    settings.setdefault("hot_reload_enabled", True)
    settings.setdefault("reload_interval_seconds", 30)
    return settings


def _get_candidate_dynamic_pairs(data_config: Optional[Dict[str, Any]] = None, project_root: Optional[Path] = None) -> List[str]:
    configured_pairs = _normalize_pairs((data_config or {}).get("pairs") or [])
    if configured_pairs:
        return configured_pairs

    settings = _get_hybrid_dynamic_coin_settings(data_config)
    whitelist_path = resolve_whitelist_path(settings.get("whitelist_json_path"), project_root or PROJECT_ROOT)
    resolver = HybridDynamicPairResolver(JsonCoinWhitelistRepository(default_path=whitelist_path))
    return resolver.list_candidate_pairs(whitelist_path)


def _get_dynamic_whitelist_path(data_config: Optional[Dict[str, Any]] = None, project_root: Optional[Path] = None) -> Path:
    settings = _get_hybrid_dynamic_coin_settings(data_config)
    return resolve_whitelist_path(settings.get("whitelist_json_path"), project_root or PROJECT_ROOT)


def _merge_unique_timeframes(existing: Iterable[str], additions: Iterable[str]) -> List[str]:
    merged: List[str] = []
    seen: set[str] = set()
    for item in list(existing or []) + list(additions or []):
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        merged.append(value)
    return merged


def _apply_strategy_mode_profile(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    runtime_config = dict(config or {})
    strategy_mode = dict(runtime_config.get("strategy_mode", {}) or {})
    active_mode = str(strategy_mode.get("active") or "standard").strip().lower()
    runtime_config["active_strategy_mode"] = active_mode
    if active_mode == "standard":
        return runtime_config

    trading_cfg = runtime_config.setdefault("trading", {})
    strategies_cfg = runtime_config.setdefault("strategies", {})
    risk_cfg = runtime_config.setdefault("risk", {})
    state_cfg = runtime_config.setdefault("state_management", {})
    auto_trader_cfg = runtime_config.setdefault("auto_trader", {})
    position_sizing_cfg = auto_trader_cfg.setdefault("position_sizing", {})
    auto_exit_cfg = auto_trader_cfg.setdefault("auto_exit", {})
    mtf_cfg = runtime_config.setdefault("multi_timeframe", {})

    if active_mode == "trend_only":
        trend_mode = dict(strategy_mode.get("trend_only", {}) or {})
        trend_tf = str(trend_mode.get("primary_timeframe") or trading_cfg.get("timeframe") or "15m")
        hold_hours = float(trend_mode.get("max_hold_hours", auto_exit_cfg.get("max_hold_hours", 48)) or 48)

        trading_cfg["timeframe"] = trend_tf
        strategies_cfg["enabled"] = ["trend_following"]
        strategies_cfg["min_confidence"] = float(trend_mode.get("min_confidence", strategies_cfg.get("min_confidence", 0.35)))
        strategies_cfg["min_strategies_agree"] = int(trend_mode.get("min_strategies_agree", 1) or 1)

        risk_cfg["stop_loss_pct"] = float(trend_mode.get("stop_loss_pct", risk_cfg.get("stop_loss_pct", 4.5)))
        risk_cfg["take_profit_pct"] = float(trend_mode.get("take_profit_pct", risk_cfg.get("take_profit_pct", 10.0)))
        risk_cfg["cool_down_minutes"] = float(trend_mode.get("min_time_between_trades_minutes", risk_cfg.get("cool_down_minutes", 5)) or 5)

        state_cfg["confirmations_required"] = int(trend_mode.get("confirmations_required", state_cfg.get("confirmations_required", 1)) or 1)
        state_cfg["confirmation_window_seconds"] = int(trend_mode.get("confirmation_window_seconds", state_cfg.get("confirmation_window_seconds", 180)) or 180)

        auto_exit_cfg["max_hold_hours"] = hold_hours
        mtf_cfg["enabled"] = bool(trend_mode.get("mtf_enabled", mtf_cfg.get("enabled", True)))
        trend_confirm_tf = str(trend_mode.get("confirm_timeframe") or "1h")
        mtf_cfg["timeframes"] = _merge_unique_timeframes(mtf_cfg.get("timeframes") or [], [trend_tf, trend_confirm_tf])
        mtf_cfg["higher_timeframes"] = _merge_unique_timeframes([], [trend_confirm_tf])
        return runtime_config

    if active_mode != "scalping":
        return runtime_config

    scalping_mode = dict(strategy_mode.get("scalping", {}) or {})

    primary_tf = str(scalping_mode.get("primary_timeframe") or "5m")
    confirm_tf = str(scalping_mode.get("confirm_timeframe") or "15m")
    trend_tf = str(scalping_mode.get("trend_timeframe") or "1h")
    max_hold_minutes = int(scalping_mode.get("position_timeout_minutes", 30) or 30)

    trading_cfg["timeframe"] = primary_tf
    strategies_cfg["enabled"] = ["scalping"]
    strategies_cfg["min_confidence"] = float(scalping_mode.get("min_confidence", strategies_cfg.get("min_confidence", 0.35)))
    strategies_cfg["min_strategies_agree"] = int(scalping_mode.get("min_strategies_agree", 1) or 1)

    risk_cfg["stop_loss_pct"] = float(scalping_mode.get("stop_loss_pct", 0.75))
    risk_cfg["take_profit_pct"] = float(scalping_mode.get("take_profit_pct", 1.75))
    risk_cfg["max_daily_trades"] = int(scalping_mode.get("max_trades_per_day", risk_cfg.get("max_daily_trades", 50)) or 50)
    risk_cfg["max_position_per_trade_pct"] = float(scalping_mode.get("max_position_per_trade_pct", risk_cfg.get("max_position_per_trade_pct", 20.0)))
    risk_cfg["max_risk_per_trade_pct"] = float(scalping_mode.get("max_risk_per_trade_pct", risk_cfg.get("max_risk_per_trade_pct", 2.0)))
    risk_cfg["cool_down_minutes"] = float(scalping_mode.get("min_time_between_trades_minutes", risk_cfg.get("cool_down_minutes", 5)) or 5)

    state_cfg["confirmations_required"] = int(scalping_mode.get("confirmations_required", state_cfg.get("confirmations_required", 1)) or 1)
    state_cfg["confirmation_window_seconds"] = int(scalping_mode.get("confirmation_window_seconds", 90) or 90)
    state_cfg["pending_buy_timeout_seconds"] = int(scalping_mode.get("pending_buy_timeout_seconds", 60) or 60)
    state_cfg["pending_sell_timeout_seconds"] = int(scalping_mode.get("pending_sell_timeout_seconds", 60) or 60)

    position_sizing_cfg["risk_per_trade_pct"] = float(scalping_mode.get("position_risk_per_trade_pct", position_sizing_cfg.get("risk_per_trade_pct", 1.0)))
    position_sizing_cfg["max_position_pct"] = float(scalping_mode.get("position_size_cap_pct", position_sizing_cfg.get("max_position_pct", 10.0)))
    auto_exit_cfg["max_hold_hours"] = max_hold_minutes / 60.0
    auto_exit_cfg["check_interval_seconds"] = int(scalping_mode.get("monitor_check_interval_seconds", auto_exit_cfg.get("check_interval_seconds", 10)) or 10)

    mtf_cfg["enabled"] = True
    mtf_cfg["timeframes"] = _merge_unique_timeframes(mtf_cfg.get("timeframes") or [], [primary_tf, confirm_tf, trend_tf])
    mtf_cfg["higher_timeframes"] = _merge_unique_timeframes([], [trend_tf])

    scalping_strategy_cfg = dict(strategies_cfg.get("scalping", {}) or {})
    scalping_strategy_cfg.update(
        {
            "primary_timeframe": primary_tf,
            "confirm_timeframe": confirm_tf,
            "trend_timeframe": trend_tf,
            "fast_ema": int(scalping_mode.get("fast_ema", 9) or 9),
            "slow_ema": int(scalping_mode.get("slow_ema", 21) or 21),
            "rsi_period": int(scalping_mode.get("rsi_period", 7) or 7),
            "rsi_oversold": float(scalping_mode.get("rsi_oversold", 34) or 34),
            "rsi_overbought": float(scalping_mode.get("rsi_overbought", 66) or 66),
            "bollinger_period": int(scalping_mode.get("bollinger_period", 20) or 20),
            "bollinger_std": float(scalping_mode.get("bollinger_std", 2.0) or 2.0),
            "min_entry_confidence": float(scalping_mode.get("min_confidence", 0.30)),
            "stop_loss_pct": float(scalping_mode.get("stop_loss_pct", 0.75)),
            "take_profit_pct": float(scalping_mode.get("take_profit_pct", 1.75)),
            "position_timeout_minutes": max_hold_minutes,
        }
    )
    strategies_cfg["scalping"] = scalping_strategy_cfg
    return runtime_config


def _normalize_optional_secret(value: Optional[Any]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    placeholder_markers = (
        "YOUR_",
        "REPLACE_",
        "CHANGE_",
        "<YOUR",
    )
    if any(text.upper().startswith(marker) for marker in placeholder_markers):
        return ""
    return text


def _resolve_telegram_credentials(config: Optional[Dict[str, Any]] = None) -> tuple[str, str]:
    runtime_config = config or {}
    api_keys = runtime_config.get("api_keys", {}) or {}
    notifications = runtime_config.get("notifications", {}) or {}

    bot_token = _normalize_optional_secret(os.environ.get("TELEGRAM_BOT_TOKEN"))
    if not bot_token:
        bot_token = _normalize_optional_secret(api_keys.get("telegram_bot_token"))

    chat_id = _normalize_optional_secret(os.environ.get("TELEGRAM_CHAT_ID"))
    if not chat_id:
        chat_id = _normalize_optional_secret(notifications.get("telegram_chat_id"))
    if not chat_id:
        chat_id = _normalize_optional_secret(api_keys.get("telegram_chat_id"))

    return bot_token, chat_id


def resolve_runtime_trading_pairs(
    api_client: BitkubClient,
    configured_pairs: Optional[Iterable[str]] = None,
    data_config: Optional[Dict[str, Any]] = None,
    project_root: Optional[Path] = None,
) -> List[str]:
    """Resolve tradable THB pairs from JSON whitelist plus live Bitkub readiness."""
    settings = _get_hybrid_dynamic_coin_settings(data_config)
    whitelist_path = resolve_whitelist_path(settings.get("whitelist_json_path"), project_root or PROJECT_ROOT)
    resolver = HybridDynamicPairResolver(JsonCoinWhitelistRepository(default_path=whitelist_path))
    selection = resolver.resolve(
        api_client,
        config_path=whitelist_path,
        configured_pairs=configured_pairs,
        min_quote_balance_thb=settings.get("min_quote_balance_thb"),
        require_supported_market=settings.get("require_supported_market"),
        include_assets_with_balance=settings.get("include_assets_with_balance"),
    )
    for warning in selection.warnings:
        logger.warning("Hybrid dynamic coin config: %s", warning)

    pairs = list(selection.pairs)
    # Keep resolver behavior for whitelist-based dynamic selection.
    # Only enforce strict holdings filtering for explicit operator-provided
    # configured pairs.
    if configured_pairs is None or data_config is not None:
        return pairs

    try:
        balances = api_client.get_balances() or {}
        supported_rows = api_client.get_symbols() or []
    except Exception as exc:
        logger.warning("Could not validate holdings for runtime pairs: %s", exc)
        return pairs

    held_assets = set()
    if isinstance(balances, dict):
        for asset, payload in balances.items():
            asset_key = str(asset or "").upper().strip()
            if not asset_key or asset_key == "THB":
                continue
            available = 0.0
            reserved = 0.0
            if isinstance(payload, dict):
                available = float(payload.get("available", 0.0) or 0.0)
                reserved = float(payload.get("reserved", 0.0) or 0.0)
            elif isinstance(payload, (int, float, str)):
                available = float(payload or 0.0)
            if (available + reserved) > 0:
                held_assets.add(asset_key)

    supported_thb_pairs = set()
    for row in supported_rows if isinstance(supported_rows, list) else []:
        symbol = str((row or {}).get("symbol", "")).upper().strip()
        if not symbol:
            continue
        normalized = _normalize_cli_pair(symbol)
        if normalized.startswith("THB_"):
            supported_thb_pairs.add(normalized)

    filtered_pairs = [
        pair
        for pair in pairs
        if _extract_asset_from_pair(pair).upper() in held_assets and pair in supported_thb_pairs
    ]

    return filtered_pairs


def load_bot_config(config_path: str | PathLike[str] | None = None) -> Dict[str, Any]:
    """Load bot configuration from YAML or JSON file."""
    resolved_config_path = Path(config_path) if config_path is not None else PROJECT_ROOT / "bot_config.yaml"
    
    if not resolved_config_path.exists():
        logger.warning(f"Config file not found: {resolved_config_path}, using defaults")
        return _get_default_config()
    
    # Try YAML first
    try:
        import yaml
        with open(resolved_config_path, "r", encoding="utf-8") as f:
            return _apply_strategy_mode_profile(yaml.safe_load(f))
    except ImportError:
        logger.warning("PyYAML not installed, trying JSON")
    
    # Try JSON
    json_path = resolved_config_path.with_suffix(".json")
    if json_path.exists():
        import json
        with open(json_path, "r", encoding="utf-8") as f:
            return _apply_strategy_mode_profile(json.load(f))
    
    logger.error(f"No valid config file found at {resolved_config_path}")
    return _get_default_config()


def _get_default_config() -> Dict[str, Any]:
    """Return default configuration."""
    return {
        "trading_pair": "",
        "mode": "semi_auto",
        "simulate_only": True,
        "trading": {
            "trading_pair": "",
            "interval_seconds": 60,
            "timeframe": "1h",
            "mode": "semi_auto",
        },
        "strategies": {
            "enabled": ["trend_following", "mean_reversion", "breakout", "scalping"],
            "min_confidence": 0.5,
            "min_strategies_agree": 2,
            "scalping": {
                "fast_ema": 9,
                "slow_ema": 21,
                "rsi_period": 7,
                "rsi_oversold": 34,
                "rsi_overbought": 66,
                "bollinger_period": 20,
                "bollinger_std": 2.0,
                "min_entry_confidence": 0.3,
                "stop_loss_pct": 0.75,
                "take_profit_pct": 1.75,
                "position_timeout_minutes": 30,
            },
        },
        "risk": {
            "max_risk_per_trade_pct": 4.0,
            "max_daily_loss_pct": 10.0,
            "max_position_per_trade_pct": 10.0,
            "max_open_positions": 3,
            "max_daily_trades": 10,
            "stop_loss_pct": -4.5,
            "take_profit_pct": 10.0,
            "cool_down_minutes": 5,
            "atr_multiplier": 3.0,
            "atr_period": 14,
            "use_dynamic_sl_tp": True,
            "correlation_threshold": 0.75,
        },
        "notifications": {
            "alert_channel": "telegram",
            "send_alerts": True,
            "telegram_command_polling_enabled": True,
            "telegram_chat_id": "",
        },
        "execution": {
            "order_type": "limit",
            "retry_attempts": 3,
            "retry_delay_seconds": 5,
            "order_timeout_seconds": 30,
            "allow_trailing_stop": False
        },
        "state_management": {
            "enabled": True,
            "entry_confidence_threshold": 0.35,
            "confirmations_required": 2,
            "confirmation_window_seconds": 180,
            "pending_buy_timeout_seconds": 120,
            "pending_sell_timeout_seconds": 120,
            "allow_trailing_stop": False
        },
        "portfolio": {
            "initial_balance": 1000.0,
            "min_balance_threshold": 100.0
        },
        "balance_monitor": {
            "enabled": True,
            "poll_interval_seconds": 30,
            "persist_path": "balance_monitor_state.json",
            "thb_min_threshold": 0.0,
            "coin_min_threshold": 0.0,
            "coin_min_thresholds": {}
        },

        "data": {
            "collect_interval_seconds": 60,
            "auto_detect_held_pairs": True,
            "pairs": [],
            "portfolio_guard": {
                "held_coins_only": True,
            },
            "hybrid_dynamic_coin_config": {
                "whitelist_json_path": DEFAULT_WHITELIST_JSON,
                "min_quote_balance_thb": max(float(TRADING.min_order_amount or 0.0), 100.0),
                "require_supported_market": True,
                "include_assets_with_balance": True,
            },
        },
        "cli_ui": {
            "enabled": True,
            "refresh_interval_seconds": 2.0,
            "bot_name": "Crypto Bot V1",
            "command_listener_enabled": True,
        }
    }


def setup_logging(level: str = "INFO", yaml_config: Optional[Dict[str, Any]] = None):
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


def setup_signal_handlers(bot: TradingBotOrchestrator, collector: BitkubCollector, telegram_handler=None):
    """
    Setup signal handlers for graceful shutdown.

    Handles:
    - SIGINT (Ctrl+C)
    - SIGTERM (kill command)
    """
    def signal_handler(signum, frame):
        logger.info(f"ได้รับสัญญาณ {signum}, กำลังเริ่มขั้นตอนปิดระบบอย่างปลอดภัย (Graceful shutdown)...")

        try:
            # Stop bot first (will stop main loop)
            if bot:
                bot.stop()

            # Stop Telegram handler
            if telegram_handler:
                telegram_handler.stop()

            # Stop collector
            if collector:
                collector.stop()
        finally:
            # Always release singleton lock on signal-driven shutdown.
            try:
                release_bot_lock()
            except Exception as lock_err:
                logger.warning("Failed to release bot lock during signal shutdown: %s", lock_err)

        logger.info("ปิดระบบเสร็จสมบูรณ์")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


class TradingBotApp:
    """
    Main application class that coordinates all components.
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None, config_path: str | PathLike[str] | None = None):
        """
        Initialize the trading bot application.
        
        Args:
            config: Bot configuration dict
        """
        self._config_path = Path(config_path) if config_path is not None else Path(os.environ.get("BOT_CONFIG_PATH", PROJECT_ROOT / "bot_config.yaml"))
        self.config = _apply_strategy_mode_profile(config or load_bot_config(self._config_path))
        self.bot: Optional[TradingBotOrchestrator] = None
        self.collector: Optional[BitkubCollector] = None
        self.api_client: Optional[BitkubClient] = None
        self.signal_generator: Optional[SignalGenerator] = None
        self.risk_manager: Optional[RiskManager] = None
        self.executor: Optional[TradeExecutor] = None
        self.alert_sender = None
        self.telegram_handler: Optional[TelegramBotHandler] = None
        self.trading_disabled = threading.Event()  # Kill switch event
        self._shutdown_event = threading.Event()
        self._pair_reload_thread: Optional[threading.Thread] = None
        self._pair_reload_lock = threading.Lock()
        self._pair_reload_signature: Optional[tuple[str, bool, int, int]] = None
        self.health_server: Optional[BotHealthServer] = None
        self._health_server_started = False
        self._app_started_at = time.time()
        self._status_interval_seconds = 30
        self._last_status_log_at = 0.0
        cli_config = self.config.setdefault("cli_ui", {})
        self._cli_ui_enabled = bool(cli_config.get("enabled", True))
        self._cli_refresh_interval_seconds = max(1.0, float(cli_config.get("refresh_interval_seconds", 2.0) or 2.0))
        self._cli_bot_name = str(cli_config.get("bot_name") or "Crypto Bot V1")
        self._cli_commands_enabled = bool(cli_config.get("command_listener_enabled", True))
        self._live_dashboard_active = False
        self._cli_price_cache: Dict[str, tuple[Optional[float], float]] = {}
        self._cli_price_cache_ttl = 2.0
        self._api_latency_ms: Optional[float] = None
        self._api_latency_checked_at = 0.0
        self._api_latency_cache_seconds = 15.0
        self._cli_command_thread: Optional[threading.Thread] = None
        self._cli_chat_lock = threading.Lock()
        self._cli_chat_input = ""
        self._cli_chat_history: List[Dict[str, str]] = []
        self._cli_chat_status = "Enter=send | Tab=autocomplete | Up/Down=history | Backspace=edit | Esc=clear"
        self._cli_chat_max_lines = 4
        self._cli_command_history: List[str] = []
        self._cli_history_index: Optional[int] = None
        self._cli_history_max_items = 50
        self._cli_pending_confirmation: Optional[Dict[str, Any]] = None
        self._cli_suggestion_limit = 5
        self._cli_log_level_filter = "INFO"
        self._cli_footer_mode = "compact"
        self._restart_requested = False
        self._restart_reason = ""
        self._restart_lock = threading.Lock()
    

    def _ensure_cli_chat_runtime_state(self) -> None:
        if getattr(self, "_cli_chat_lock", None) is None:
            self._cli_chat_lock = threading.Lock()
        if not hasattr(self, "_cli_chat_input"):
            self._cli_chat_input = ""
        if not hasattr(self, "_cli_chat_history"):
            self._cli_chat_history = []
        if not hasattr(self, "_cli_chat_status"):
            self._cli_chat_status = "Enter=send | Tab=autocomplete | Up/Down=history | Backspace=edit | Esc=clear"
        if not hasattr(self, "_cli_chat_max_lines"):
            self._cli_chat_max_lines = 4
        if not hasattr(self, "_cli_command_history"):
            self._cli_command_history = []
        if not hasattr(self, "_cli_history_index"):
            self._cli_history_index = None
        if not hasattr(self, "_cli_history_max_items"):
            self._cli_history_max_items = 50
        if not hasattr(self, "_cli_pending_confirmation"):
            self._cli_pending_confirmation = None
        if not hasattr(self, "_cli_suggestion_limit"):
            self._cli_suggestion_limit = 5
        if not hasattr(self, "_cli_log_level_filter"):
            self._cli_log_level_filter = "INFO"
        if not hasattr(self, "_cli_footer_mode"):
            self._cli_footer_mode = "compact"
        if not hasattr(self, "_restart_requested"):
            self._restart_requested = False
        if not hasattr(self, "_restart_reason"):
            self._restart_reason = ""
        if not hasattr(self, "_restart_lock"):
            self._restart_lock = threading.Lock()

    @staticmethod
    def _normalize_cli_log_level(value: str) -> str:
        alias = {
            "warn": "WARNING",
            "err": "ERROR",
            "fatal": "CRITICAL",
        }
        normalized = str(value or "").strip().lower()
        if normalized in alias:
            return alias[normalized]
        return normalized.upper()

    @staticmethod
    def _normalize_cli_footer_mode(value: str) -> str:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in {"compact", "verbose"} else ""

    def _set_cli_chat_status(self, status: str) -> None:
        self._ensure_cli_chat_runtime_state()
        with self._cli_chat_lock:
            self._cli_chat_status = str(status or "")

    def _append_cli_chat_message(self, role: str, message: str) -> None:
        self._ensure_cli_chat_runtime_state()
        text = str(message or "").strip()
        if not text:
            return
        with self._cli_chat_lock:
            self._cli_chat_history.append({"role": role, "message": text})
            self._cli_chat_history = self._cli_chat_history[-self._cli_chat_max_lines :]

    def _get_cli_chat_snapshot(self) -> Dict[str, Any]:
        self._ensure_cli_chat_runtime_state()
        with self._cli_chat_lock:
            input_text = self._cli_chat_input
            history = list(self._cli_chat_history)
            status = self._cli_chat_status
            pending = dict(self._cli_pending_confirmation) if self._cli_pending_confirmation else None

        return {
            "input": input_text,
            "history": history,
            "status": status,
            "pending_confirmation": pending,
            "suggestions": self._get_cli_suggestions(input_text, pending_confirmation=pending),
        }

    def _submit_cli_chat_command(self, raw_command: str) -> str:
        self._ensure_cli_chat_runtime_state()
        command_text = str(raw_command or "").strip()
        if not command_text:
            self._set_cli_chat_status("Command ignored: empty input")
            return ""

        self._append_cli_chat_message("user", command_text)
        with self._cli_chat_lock:
            self._cli_command_history.append(command_text)
            self._cli_command_history = self._cli_command_history[-self._cli_history_max_items :]
            self._cli_history_index = None
        result = self.process_cli_command(command_text)
        if result:
            self._append_cli_chat_message("bot", result)
        return result

    def _navigate_cli_history(self, direction: int) -> None:
        self._ensure_cli_chat_runtime_state()
        with self._cli_chat_lock:
            if not self._cli_command_history:
                self._cli_chat_status = "No command history"
                return

            if self._cli_history_index is None:
                if direction < 0:
                    self._cli_history_index = len(self._cli_command_history) - 1
                else:
                    self._cli_chat_status = "Already at newest input"
                    return
            else:
                next_index = self._cli_history_index + direction
                if next_index < 0:
                    next_index = 0
                if next_index >= len(self._cli_command_history):
                    self._cli_history_index = None
                    self._cli_chat_input = ""
                    self._cli_chat_status = "Returned to live input"
                    return
                self._cli_history_index = next_index

            self._cli_chat_input = self._cli_command_history[self._cli_history_index]
            self._cli_chat_status = f"History {self._cli_history_index + 1}/{len(self._cli_command_history)}"

    def _get_cli_known_pairs(self) -> List[str]:
        runtime_config = getattr(self, "config", {}) or {}
        data_config = runtime_config.get("data", {}) or {}
        known_pairs = list(data_config.get("pairs") or [])
        try:
            _, _, configured_assets = self._load_runtime_pairlist_document()
            known_pairs.extend(f"THB_{asset}" for asset in configured_assets)
        except Exception:
            pass
        known_pairs.extend(order.get("symbol") for order in self.list_active_orders() if order.get("symbol"))
        known_pairs.extend([
            "THB_BTC",
            "THB_ETH",
            "THB_SOL",
            "THB_XRP",
            "THB_DOGE",
            "THB_ADA",
            "THB_BNB",
            "THB_DOT",
            "THB_LINK",
        ])
        return _normalize_pairs(known_pairs)

    def _match_cli_suggestions(self, options: Iterable[str], prefix: str) -> List[str]:
        self._ensure_cli_chat_runtime_state()
        normalized_prefix = str(prefix or "").strip().lower()
        suggestions: List[str] = []
        seen: set[str] = set()
        for option in options or []:
            value = str(option or "").strip()
            lowered = value.lower()
            if not value or lowered in seen:
                continue
            if normalized_prefix and not lowered.startswith(normalized_prefix):
                continue
            seen.add(lowered)
            suggestions.append(value)
            if len(suggestions) >= self._cli_suggestion_limit:
                break
        return suggestions

    def _get_cli_suggestions(
        self,
        input_text: str,
        pending_confirmation: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        self._ensure_cli_chat_runtime_state()
        text = str(input_text or "")
        stripped = text.strip()
        has_trailing_space = text.endswith(" ")
        known_pairs = self._get_cli_known_pairs()
        active_order_ids = [order.get("order_id") for order in self.list_active_orders() if order.get("order_id")]
        default_pair = known_pairs[0] if known_pairs else "THB_BTC"
        pending = pending_confirmation or getattr(self, "_cli_pending_confirmation", None)

        if pending and not stripped:
            return ["confirm", "cancel"]

        if not stripped:
            suggestions = [
                "help",
                "status",
                "orders",
                "mode show",
                "mode cycle",
                "mode standard",
                "mode trend",
                "mode scalp",
                "mode set standard",
                "mode set standard restart",
                "risk show",
                "risk set 2.0",
                "ui",
                "ui log info",
                "ui footer compact",
                f"buy {default_pair} 500",
                f"track {default_pair} 0.001 1500000",
                f"sell {default_pair} 0.001",
                "pairs list",
                f"pairs add {default_pair}",
                f"pairs remove {default_pair}",
                "pairs reload",
            ]
            if active_order_ids:
                suggestions.insert(7, f"close {active_order_ids[0]}")
            if pending:
                suggestions = ["confirm", "cancel"] + suggestions
            return suggestions[: self._cli_suggestion_limit]

        try:
            parts = shlex.split(text)
        except ValueError:
            parts = stripped.split()
        if not parts:
            return []

        first = parts[0].lower()
        if len(parts) == 1 and not has_trailing_space:
            command_options = ["help", "status", "orders", "mode", "risk", "ui", "buy", "track", "sell", "close", "pairs"]
            if pending:
                command_options.extend(["confirm", "cancel"])
            return self._match_cli_suggestions(command_options, stripped)

        if first == "mode":
            return self._match_cli_suggestions([
                "mode show",
                "mode cycle",
                "mode standard",
                "mode set standard",
                "mode set standard restart",
                "mode trend",
                "mode trend_only",
                "mode set trend_only",
                "mode set trend_only restart",
                "mode scalp",
                "mode scalping",
                "mode set scalping",
                "mode set scalping restart",
                "mode cycle restart",
            ], stripped)

        if pending and first in {"c", "co", "con", "conf", "confi", "confirm", "ca", "can", "canc", "cance", "cancel"}:
            return self._match_cli_suggestions(["confirm", "cancel"], stripped)

        if first == "risk":
            return self._match_cli_suggestions(["risk show", "risk set 1.0", "risk set 2.0", "risk set 4.0"], stripped)

        if first == "buy":
            if len(parts) == 1 or (len(parts) == 2 and not has_trailing_space):
                return self._match_cli_suggestions([f"buy {pair} 500" for pair in known_pairs], stripped)
            pair = _normalize_cli_pair(parts[1]) if len(parts) >= 2 else default_pair
            return self._match_cli_suggestions([f"buy {pair} 100", f"buy {pair} 500", f"buy {pair} 1000"], stripped)

        if first == "track":
            if len(parts) == 1 or (len(parts) == 2 and not has_trailing_space):
                return self._match_cli_suggestions([f"track {pair} 0.001 1500000" for pair in known_pairs], stripped)
            pair = _normalize_cli_pair(parts[1]) if len(parts) >= 2 else default_pair
            return self._match_cli_suggestions(
                [
                    f"track {pair} 0.001 1500000",
                    f"track {pair} 0.01 2500",
                    f"track {pair} 100 2.5",
                ],
                stripped,
            )

        if first == "sell":
            suggestions = [f"sell {pair} 0.001" for pair in known_pairs]
            suggestions.extend(f"sell {order_id}" for order_id in active_order_ids)
            return self._match_cli_suggestions(suggestions, stripped)

        if first == "close":
            return self._match_cli_suggestions([f"close {order_id}" for order_id in active_order_ids], stripped)

        if first == "pairs":
            pair_commands = ["pairs list", "pairs reload"]
            pair_commands.extend(f"pairs add {pair}" for pair in known_pairs)
            pair_commands.extend(f"pairs remove {pair}" for pair in known_pairs)
            return self._match_cli_suggestions(pair_commands, stripped)

        if first == "ui":
            ui_commands = [
                "ui",
                "ui log debug",
                "ui log info",
                "ui log warning",
                "ui log error",
                "ui log critical",
                "ui footer compact",
                "ui footer verbose",
            ]
            return self._match_cli_suggestions(ui_commands, stripped)

        return []

    def _accept_cli_suggestion(self) -> None:
        self._ensure_cli_chat_runtime_state()
        with self._cli_chat_lock:
            input_text = self._cli_chat_input
            pending = dict(self._cli_pending_confirmation) if self._cli_pending_confirmation else None
        suggestions = self._get_cli_suggestions(input_text, pending_confirmation=pending)
        if not suggestions:
            self._set_cli_chat_status("No suggestion available")
            return
        with self._cli_chat_lock:
            self._cli_chat_input = suggestions[0]
            self._cli_chat_status = f"Suggestion applied: {suggestions[0]}"

    def _should_confirm_cli_command(self, command: str, args: List[str]) -> bool:
        normalized = str(command or "").lower()
        if normalized == "buy":
            return len(args) == 2
        if normalized == "track":
            return len(args) == 3
        if normalized == "sell":
            return len(args) in {1, 2}
        if normalized == "close":
            return len(args) == 1
        if normalized == "risk":
            return len(args) == 2 and str(args[0] or "").lower() == "set"
        if normalized == "mode":
            if len(args) == 1 and str(args[0] or "").lower() in {"standard", "std", "trend", "trend_only", "trendonly", "scalp", "scalper", "scalping", "cycle"}:
                return True
            if len(args) == 2 and str(args[0] or "").lower() == "set":
                return True
            if len(args) == 2 and str(args[1] or "").lower() == "restart" and str(args[0] or "").lower() in {"standard", "std", "trend", "trend_only", "trendonly", "scalp", "scalper", "scalping", "cycle"}:
                return True
            return len(args) == 3 and str(args[0] or "").lower() == "set" and str(args[2] or "").lower() == "restart"
        return False

    def _build_cli_confirmation_request(self, command: str, args: List[str], command_text: str) -> Dict[str, Any]:
        normalized = str(command or "").lower()
        summary = f"Confirm command: {command_text}"
        if normalized == "buy" and len(args) == 2:
            summary = f"Confirm market BUY {_normalize_cli_pair(args[0])} with {float(args[1]):,.2f} THB"
        elif normalized == "track" and len(args) == 3:
            summary = (
                f"Confirm tracked position {_normalize_cli_pair(args[0])} "
                f"qty {float(args[1]):,.8f} @ {float(args[2]):,.4f}"
            )
        elif normalized == "sell" and len(args) == 2:
            summary = f"Confirm market SELL {_normalize_cli_pair(args[0])} amount {float(args[1]):,.8f}"
        elif normalized == "sell" and len(args) == 1:
            summary = f"Confirm market SELL for target {args[0]}"
        elif normalized == "close" and len(args) == 1:
            summary = f"Confirm close active order {args[0]} via market SELL"
        elif normalized == "risk" and len(args) == 2:
            summary = f"Confirm risk change to {float(args[1]):.2f}% per trade"
        elif normalized == "mode" and len(args) == 1:
            summary = f"Confirm strategy mode change to {self._resolve_target_strategy_mode(args[0])} (restart required)"
        elif normalized == "mode" and len(args) == 2 and str(args[1] or "").lower() == "restart":
            summary = f"Confirm strategy mode change to {self._resolve_target_strategy_mode(args[0])} and restart bot"
        elif normalized == "mode" and len(args) == 2 and str(args[0] or "").lower() == "set":
            summary = f"Confirm strategy mode change to {self._resolve_target_strategy_mode(args[1])} (restart required)"
        elif normalized == "mode" and len(args) == 3 and str(args[0] or "").lower() == "set" and str(args[2] or "").lower() == "restart":
            summary = f"Confirm strategy mode change to {self._resolve_target_strategy_mode(args[1])} and restart bot"

        return {
            "command": normalized,
            "args": list(args),
            "command_text": command_text,
            "summary": summary,
        }

    def list_active_orders(self) -> List[Dict[str, Any]]:
        """Return active tracked orders for runtime command handling."""
        if not self.executor:
            return []

        orders: List[Dict[str, Any]] = []
        for order in self.executor.get_open_orders() or []:
            side_value = order.get("side")
            side = str(getattr(side_value, "value", side_value) or "")
            remaining_amount = float(order.get("remaining_amount") or order.get("amount") or 0.0)
            orders.append({
                "order_id": str(order.get("order_id") or ""),
                "symbol": str(order.get("symbol") or "").upper(),
                "side": side.lower(),
                "amount": float(order.get("amount") or 0.0),
                "remaining_amount": remaining_amount,
                "entry_price": float(order.get("entry_price") or 0.0),
                "filled": bool(order.get("filled", False)),
                "timestamp": order.get("timestamp"),
            })
        return orders

    def set_runtime_risk_pct(self, risk_pct: float) -> Dict[str, Any]:
        """Update max risk per trade for the running process."""
        try:
            normalized = float(risk_pct)
        except (TypeError, ValueError) as exc:
            raise ValueError("Risk must be a number") from exc

        if normalized <= 0:
            raise ValueError("Risk must be greater than 0")
        if normalized > 8.0:
            raise ValueError("Risk must not exceed 8.0%")

        self.config.setdefault("risk", {})["max_risk_per_trade_pct"] = normalized
        if self.bot:
            self.bot.config.setdefault("risk", {})["max_risk_per_trade_pct"] = normalized
        if self.risk_manager:
            self.risk_manager.config.max_risk_per_trade_pct = normalized

        logger.warning("[CLI] Runtime risk updated: max_risk_per_trade_pct=%.2f%%", normalized)
        return {
            "status": "ok",
            "risk_pct": normalized,
            "risk_level": self._derive_risk_level()[0],
        }

    @staticmethod
    def _normalize_strategy_mode(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        valid_modes = {"standard", "trend_only", "scalping"}
        if normalized not in valid_modes:
            raise ValueError("Mode must be one of: standard, trend_only, scalping")
        return normalized

    def get_runtime_mode_status(self) -> Dict[str, Any]:
        strategy_mode_cfg = dict(self.config.get("strategy_mode", {}) or {})
        active_mode = str(self.config.get("active_strategy_mode") or strategy_mode_cfg.get("active") or "standard").lower()
        return {
            "status": "ok",
            "active_mode": active_mode,
            "config_path": str(self._config_path),
            "timeframe": str((self.config.get("trading", {}) or {}).get("timeframe") or "-"),
            "enabled_strategies": list((self.config.get("strategies", {}) or {}).get("enabled") or []),
        }

    def _resolve_target_strategy_mode(self, selector: Any) -> str:
        normalized = str(selector or "").strip().lower()
        alias_map = {
            "std": "standard",
            "standard": "standard",
            "trend": "trend_only",
            "trendonly": "trend_only",
            "trend_only": "trend_only",
            "scalp": "scalping",
            "scalper": "scalping",
            "scalping": "scalping",
        }
        if normalized == "cycle":
            current = str(self.get_runtime_mode_status().get("active_mode") or "standard").lower()
            rotation = ["standard", "trend_only", "scalping"]
            try:
                current_index = rotation.index(current)
            except ValueError:
                current_index = 0
            return rotation[(current_index + 1) % len(rotation)]
        if normalized in alias_map:
            return alias_map[normalized]
        return self._normalize_strategy_mode(normalized)

    def set_runtime_strategy_mode(self, mode: str) -> Dict[str, Any]:
        normalized_mode = self._resolve_target_strategy_mode(mode)
        config_path = self._config_path
        config_path.parent.mkdir(parents=True, exist_ok=True)

        if config_path.suffix.lower() == ".json":
            existing: Dict[str, Any] = {}
            if config_path.exists():
                try:
                    existing = json.loads(config_path.read_text(encoding="utf-8")) or {}
                except Exception as exc:
                    raise RuntimeError(f"Failed to parse config JSON: {exc}") from exc
            existing.setdefault("strategy_mode", {})["active"] = normalized_mode
            config_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        else:
            yaml_text = ""
            if config_path.exists():
                yaml_text = config_path.read_text(encoding="utf-8")
            if yaml_text and re.search(r"(?m)^strategy_mode:\s*$", yaml_text):
                if re.search(r"(?m)^\s{2}active:\s*['\"]?[^#\r\n]+['\"]?", yaml_text):
                    yaml_text = re.sub(
                        r"(?m)^(\s{2}active:\s*)['\"]?[^#\r\n]+(['\"]?)(\s*(?:#.*)?)$",
                        rf'\1"{normalized_mode}"\3',
                        yaml_text,
                        count=1,
                    )
                else:
                    yaml_text = re.sub(
                        r"(?m)^(strategy_mode:\s*)$",
                        rf"\1\n  active: \"{normalized_mode}\"",
                        yaml_text,
                        count=1,
                    )
            else:
                prefix = yaml_text.rstrip() + ("\n\n" if yaml_text.strip() else "")
                yaml_text = prefix + f"strategy_mode:\n  active: \"{normalized_mode}\"\n"
            config_path.write_text(yaml_text if yaml_text.endswith("\n") else yaml_text + "\n", encoding="utf-8")

        self.config.setdefault("strategy_mode", {})["active"] = normalized_mode
        self.config["active_strategy_mode"] = normalized_mode
        logger.warning("[CLI] Strategy mode persisted to %s: active=%s (restart required)", config_path, normalized_mode)
        return {
            "status": "ok",
            "active_mode": normalized_mode,
            "config_path": str(config_path),
            "restart_required": True,
        }

    def request_process_restart(self, reason: str = "runtime cli request") -> Dict[str, Any]:
        with self._restart_lock:
            self._restart_requested = True
            self._restart_reason = str(reason or "runtime cli request")

        def _async_stop() -> None:
            try:
                self.stop()
            except Exception as exc:
                logger.error("Failed to stop app during restart request: %s", exc, exc_info=True)

        threading.Thread(target=_async_stop, daemon=True, name="CLIProcessRestart").start()
        logger.warning("[CLI] Restart requested: %s", self._restart_reason)
        return {"status": "ok", "restart_requested": True, "reason": self._restart_reason}

    def _perform_requested_restart(self) -> None:
        with self._restart_lock:
            if not self._restart_requested:
                return
            restart_reason = self._restart_reason or "runtime cli request"
        logger.warning("Restarting process in-place: %s", restart_reason)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _load_runtime_pairlist_document(self) -> tuple[Path, Dict[str, Any], List[str]]:
        data_config = self.config.setdefault("data", {})
        settings = _get_hybrid_dynamic_coin_settings(data_config)
        whitelist_path = resolve_whitelist_path(settings.get("whitelist_json_path"), PROJECT_ROOT)

        raw_document: Dict[str, Any] = {}
        if whitelist_path.exists():
            try:
                loaded = json.loads(whitelist_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw_document = loaded
            except Exception as exc:
                logger.warning("Failed to parse runtime pairlist %s: %s", whitelist_path, exc)

        raw_entries = raw_document.get("assets")
        if raw_entries is None:
            raw_entries = raw_document.get("whitelist")
        if raw_entries is None:
            raw_entries = raw_document.get("pairs")

        normalized_assets: List[str] = []
        seen_assets: set[str] = set()
        for entry in raw_entries or []:
            enabled = True
            if isinstance(entry, dict):
                enabled = bool(entry.get("enabled", True))
                entry = entry.get("symbol") or entry.get("asset") or entry.get("pair")
            asset = _extract_asset_from_pair(entry)
            if not asset or asset == "THB" or not enabled or asset in seen_assets:
                continue
            seen_assets.add(asset)
            normalized_assets.append(asset)

        raw_document.setdefault("version", 1)
        raw_document.setdefault("quote_asset", "THB")
        raw_document.setdefault("min_quote_balance_thb", settings.get("min_quote_balance_thb", 100.0))
        raw_document.setdefault("require_supported_market", settings.get("require_supported_market", True))
        raw_document.setdefault("include_assets_with_balance", settings.get("include_assets_with_balance", True))
        return whitelist_path, raw_document, normalized_assets

    def _write_runtime_pairlist_document(self, path: Path, document: Dict[str, Any], assets: List[str]) -> None:
        updated = dict(document)
        updated["assets"] = list(assets)
        updated.pop("whitelist", None)
        updated.pop("pairs", None)
        path.write_text(json.dumps(updated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def add_runtime_pairs(self, pairs: Iterable[str]) -> Dict[str, Any]:
        """Add assets to the persisted pairlist and refresh active runtime pairs."""
        normalized_pairs = _normalize_pairs(_normalize_cli_pair(pair) for pair in (pairs or []))
        if not normalized_pairs:
            raise ValueError("At least one pair is required")

        whitelist_path, document, current_assets = self._load_runtime_pairlist_document()
        updated_assets = list(current_assets)
        added_pairs: List[str] = []
        for pair in normalized_pairs:
            asset = _extract_asset_from_pair(pair)
            if asset not in updated_assets:
                updated_assets.append(asset)
                added_pairs.append(pair)

        if added_pairs:
            self._write_runtime_pairlist_document(whitelist_path, document, updated_assets)

        if self.config.setdefault("data", {}).get("auto_detect_held_pairs", True):
            active_pairs = self.refresh_runtime_pairs(reason="cli pair add", force=True)
        else:
            current_runtime_pairs = self.config.setdefault("data", {}).get("pairs") or []
            active_pairs = self._apply_runtime_pairs_update(
                _normalize_pairs(list(current_runtime_pairs) + normalized_pairs),
                reason="cli pair add",
                force=True,
            )

        return {
            "status": "ok",
            "added_pairs": added_pairs,
            "pairlist_path": str(whitelist_path),
            "active_pairs": active_pairs,
        }

    def remove_runtime_pairs(self, pairs: Iterable[str]) -> Dict[str, Any]:
        """Remove assets from the persisted pairlist and refresh active runtime pairs."""
        normalized_pairs = _normalize_pairs(_normalize_cli_pair(pair) for pair in (pairs or []))
        if not normalized_pairs:
            raise ValueError("At least one pair is required")

        whitelist_path, document, current_assets = self._load_runtime_pairlist_document()
        remove_assets = {_extract_asset_from_pair(pair) for pair in normalized_pairs}
        updated_assets = [asset for asset in current_assets if asset not in remove_assets]
        removed_pairs = [f"THB_{asset}" for asset in current_assets if asset in remove_assets]
        self._write_runtime_pairlist_document(whitelist_path, document, updated_assets)

        if self.config.setdefault("data", {}).get("auto_detect_held_pairs", True):
            active_pairs = self.refresh_runtime_pairs(reason="cli pair remove", force=True)
        else:
            current_runtime_pairs = [
                pair for pair in (self.config.setdefault("data", {}).get("pairs") or [])
                if _extract_asset_from_pair(pair) not in remove_assets
            ]
            active_pairs = self._apply_runtime_pairs_update(current_runtime_pairs, reason="cli pair remove", force=True)

        return {
            "status": "ok",
            "removed_pairs": removed_pairs,
            "pairlist_path": str(whitelist_path),
            "active_pairs": active_pairs,
        }

    def get_runtime_pairlist_status(self) -> Dict[str, Any]:
        whitelist_path, _, configured_assets = self._load_runtime_pairlist_document()
        return {
            "status": "ok",
            "pairlist_path": str(whitelist_path),
            "configured_pairs": [f"THB_{asset}" for asset in configured_assets],
            "active_pairs": list(self.config.setdefault("data", {}).get("pairs") or []),
        }

    def _ensure_manual_trade_allowed(self) -> None:
        if self.config.get("auth_degraded", False):
            raise RuntimeError("Manual trading is blocked in auth degraded mode")
        if self.config.get("read_only", False) or self.config.get("simulate_only", False):
            raise RuntimeError("Manual trading is blocked in read-only or simulation mode")
        if not self.executor or not self.api_client:
            raise RuntimeError("Trading components are not initialized")

    def _sync_runtime_position_state(self) -> None:
        executor = self.executor
        if not executor:
            return
        if self.bot and getattr(self.bot, "_state_machine_enabled", False):
            state_manager = getattr(self.bot, "_state_manager", None)
            if state_manager is not None:
                try:
                    state_manager.sync_in_position_states(executor.get_open_orders())
                except Exception as exc:
                    logger.warning("[CLI] Failed to sync state machine after manual command: %s", exc)

    def submit_manual_market_buy(self, pair: str, thb_amount: float) -> Dict[str, Any]:
        """Submit a market buy in THB and track it like a runtime position."""
        self._ensure_manual_trade_allowed()

        symbol = _normalize_cli_pair(pair)
        try:
            amount_value = float(thb_amount)
        except (TypeError, ValueError) as exc:
            raise ValueError("BUY amount must be a number") from exc
        if amount_value < 15.0:
            raise ValueError("BUY amount must be at least 15 THB")

        executor = self.executor
        if not executor:
            raise RuntimeError("Trading executor is not available")

        from trade_executor import OrderRequest, OrderSide, OrderStatus

        result = executor.execute_order(
            OrderRequest(
                symbol=symbol,
                side=OrderSide.BUY,
                amount=round(amount_value, 2),
                price=0.0,
                order_type="market",
            )
        )
        if not result.success or not result.order_id:
            raise RuntimeError(result.message or "Market BUY failed")

        reference_price = float(result.filled_price or self._get_cli_price(symbol) or 0.0)
        tracked_payload = {
            "symbol": symbol,
            "side": OrderSide.BUY,
            "amount": round(amount_value, 2),
            "entry_price": reference_price,
            "stop_loss": None,
            "take_profit": None,
            "timestamp": datetime.now(),
            "is_partial_fill": result.status == OrderStatus.PARTIAL,
            "remaining_amount": float(result.remaining_amount or 0.0),
            "total_entry_cost": round(amount_value, 2),
            "filled": result.status == OrderStatus.FILLED,
            "filled_amount": float(result.filled_amount or 0.0),
            "filled_price": reference_price,
        }
        executor.register_tracked_position(result.order_id, tracked_payload)
        self._sync_runtime_position_state()
        logger.warning("[CLI] Manual market BUY submitted: %s %.2f THB | order_id=%s", symbol, amount_value, result.order_id)
        return {
            "status": "ok",
            "side": "buy",
            "symbol": symbol,
            "thb_amount": round(amount_value, 2),
            "order_id": result.order_id,
            "filled_amount": float(result.filled_amount or 0.0),
            "filled_price": reference_price,
        }

    def _build_manual_position_sl_tp(self, symbol: str, entry_price: float) -> tuple[Optional[float], Optional[float]]:
        if entry_price <= 0:
            return None, None

        default_sl_pct, default_tp_pct = get_default_sl_tp(symbol)
        risk_cfg = (self.config.get("risk", {}) or {})

        try:
            stop_loss_pct = float(risk_cfg.get("stop_loss_pct", default_sl_pct) or default_sl_pct)
        except (TypeError, ValueError):
            stop_loss_pct = float(default_sl_pct)
        try:
            take_profit_pct = float(risk_cfg.get("take_profit_pct", default_tp_pct) or default_tp_pct)
        except (TypeError, ValueError):
            take_profit_pct = float(default_tp_pct)

        stop_loss_pct = -abs(stop_loss_pct) if stop_loss_pct else float(default_sl_pct)
        take_profit_pct = abs(take_profit_pct) if take_profit_pct else float(default_tp_pct)

        stop_loss = round(entry_price * (1 + (stop_loss_pct / 100.0)), 6)
        take_profit = round(entry_price * (1 + (take_profit_pct / 100.0)), 6)
        return stop_loss, take_profit

    def track_manual_position(self, pair: str, coin_amount: float, entry_price: float) -> Dict[str, Any]:
        """Register a manually held coin with its real average cost for SL/TP management."""
        self._ensure_manual_trade_allowed()

        symbol = _normalize_cli_pair(pair)
        try:
            quantity = float(coin_amount)
        except (TypeError, ValueError) as exc:
            raise ValueError("Tracked amount must be a number") from exc
        try:
            avg_cost = float(entry_price)
        except (TypeError, ValueError) as exc:
            raise ValueError("Tracked entry price must be a number") from exc

        if quantity <= 0:
            raise ValueError("Tracked amount must be greater than 0")
        if avg_cost <= 0:
            raise ValueError("Tracked entry price must be greater than 0")
        if (quantity * avg_cost) < 15.0:
            raise ValueError("Tracked position value must be at least 15 THB")

        active_orders = self.list_active_orders()
        existing = next((order for order in active_orders if order.get("symbol") == symbol), None)
        if existing is not None:
            raise ValueError(f"Symbol already tracked: {symbol} ({existing.get('order_id')})")

        stop_loss, take_profit = self._build_manual_position_sl_tp(symbol, avg_cost)
        position_id = f"manual_{symbol}_{int(time.time())}"
        tracked_payload = {
            "symbol": symbol,
            "side": "buy",
            "amount": quantity,
            "entry_price": avg_cost,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "timestamp": datetime.now(),
            "is_partial_fill": False,
            "remaining_amount": quantity,
            "total_entry_cost": round(quantity * avg_cost, 8),
            "filled": True,
            "filled_amount": quantity,
            "filled_price": avg_cost,
            "trigger": "manual_import",
            "notes": "cli_manual_track",
        }
        executor = self.executor
        if not executor:
            raise RuntimeError("Trading executor is not available")
        executor.register_tracked_position(position_id, tracked_payload)
        if self.api_client:
            try:
                self.api_client.get_balances(force_refresh=True, allow_stale=False)
            except Exception:
                logger.debug("[CLI] Balance refresh failed after track command", exc_info=True)
        self._sync_runtime_position_state()
        logger.warning(
            "[CLI] Manual position tracked: %s %.8f @ %.4f | order_id=%s | SL=%.4f | TP=%.4f",
            symbol,
            quantity,
            avg_cost,
            position_id,
            float(stop_loss or 0.0),
            float(take_profit or 0.0),
        )
        return {
            "status": "ok",
            "side": "buy",
            "symbol": symbol,
            "amount": quantity,
            "entry_price": avg_cost,
            "order_id": position_id,
            "stop_loss": float(stop_loss or 0.0),
            "take_profit": float(take_profit or 0.0),
            "total_entry_cost": round(quantity * avg_cost, 8),
        }

    def submit_manual_market_sell(self, target: str, amount: Optional[float] = None) -> Dict[str, Any]:
        """Submit a market sell either by pair+amount or by tracked order id."""
        self._ensure_manual_trade_allowed()

        tracked_order = next((order for order in self.list_active_orders() if order["order_id"] == str(target)), None)
        if tracked_order is not None:
            if amount is not None:
                raise ValueError("Use close <order_id> for active orders or sell <pair> <amount> for manual quantity sells")
            symbol = tracked_order["symbol"]
            sell_amount = float(tracked_order.get("remaining_amount") or tracked_order.get("amount") or 0.0)
            tracked_order_id = tracked_order["order_id"]
        else:
            symbol = _normalize_cli_pair(target)
            tracked_order_id = ""
            if amount is None:
                raise ValueError("SELL amount must be provided when selling by pair")
            try:
                sell_amount = float(amount)
            except (TypeError, ValueError) as exc:
                raise ValueError("SELL amount must be a number") from exc

        if sell_amount <= 0:
            raise ValueError("SELL amount must be greater than 0")

        executor = self.executor
        if not executor:
            raise RuntimeError("Trading executor is not available")

        from trade_executor import OrderRequest, OrderSide

        result = executor.execute_order(
            OrderRequest(
                symbol=symbol,
                side=OrderSide.SELL,
                amount=sell_amount,
                price=0.0,
                order_type="market",
            )
        )
        if not result.success:
            raise RuntimeError(result.message or "Market SELL failed")

        if tracked_order_id:
            executor.remove_tracked_position(tracked_order_id)
            self._sync_runtime_position_state()

        logger.warning("[CLI] Manual market SELL submitted: %s %.8f | order_id=%s", symbol, sell_amount, result.order_id)
        return {
            "status": "ok",
            "side": "sell",
            "symbol": symbol,
            "amount": sell_amount,
            "order_id": result.order_id,
            "closed_order_id": tracked_order_id,
            "filled_amount": float(result.filled_amount or 0.0),
            "filled_price": float(result.filled_price or self._get_cli_price(symbol) or 0.0),
        }

    def _format_cli_command_help(self) -> str:
        return (
            "Commands:\n"
            "  help\n"
            "  status\n"
            "  orders\n"
            "  mode show\n"
            "  mode cycle\n"
            "  mode <standard|trend|scalp>\n"
            "  mode set <standard|trend_only|scalping>\n"
            "  mode <standard|trend|scalp> restart\n"
            "  mode set <standard|trend_only|scalping> restart\n"
            "  confirm\n"
            "  cancel\n"
            "  risk show\n"
            "  risk set <percent>\n"
            "  ui\n"
            "  ui log <debug|info|warning|error|critical>\n"
            "  ui footer <compact|verbose>\n"
            "  buy <PAIR> <THB_AMOUNT>\n"
            "  track <PAIR> <COIN_AMOUNT> <ENTRY_PRICE>\n"
            "  sell <PAIR> <COIN_AMOUNT>\n"
            "  close <ORDER_ID>\n"
            "  pairs list\n"
            "  pairs add <PAIR|ASSET> [MORE...]\n"
            "  pairs remove <PAIR|ASSET> [MORE...]\n"
            "  pairs reload\n\n"
            "Footer chat shortcuts:\n"
            "  Enter send command\n"
            "  Tab autocomplete\n"
            "  Up/Down recall history\n"
            "  Esc clear current input"
        )

    def _execute_cli_command(self, command: str, args: List[str]) -> str:
        if command == "help":
            return self._format_cli_command_help()

        if command == "status":
            health = self.get_health_status()
            active_pairs = ", ".join(health.get("pairs") or []) or "NONE"
            return (
                f"Status: {health.get('status')} | mode={health.get('mode')} | "
                f"simulate_only={health.get('simulate_only')} | read_only={health.get('read_only')} | "
                f"pairs={active_pairs}"
            )

        if command == "orders":
            active_orders = self.list_active_orders()
            if not active_orders:
                return "Active orders: none"
            lines = ["Active orders:"]
            for order in active_orders:
                lines.append(
                    f"  {order['order_id']} | {order['symbol']} | {order['side'].upper()} | "
                    f"remaining={order['remaining_amount']:.8f} | entry={order['entry_price']:,.4f}"
                )
            return "\n".join(lines)

        if command == "mode":
            if not args or args[0].lower() == "show":
                result = self.get_runtime_mode_status()
                enabled = ", ".join(result["enabled_strategies"]) or "NONE"
                return (
                    f"Strategy mode: {result['active_mode']} | timeframe={result['timeframe']} | "
                    f"strategies={enabled} | path={result['config_path']}"
                )
            if len(args) == 1:
                result = self.set_runtime_strategy_mode(args[0])
                return (
                    f"Strategy mode saved: {result['active_mode']} | path={result['config_path']} | "
                    "restart bot to apply fully"
                )
            if len(args) == 2 and args[1].lower() == "restart":
                result = self.set_runtime_strategy_mode(args[0])
                self.request_process_restart(reason=f"mode change to {result['active_mode']}")
                return (
                    f"Strategy mode saved: {result['active_mode']} | path={result['config_path']} | "
                    "restarting now"
                )
            if len(args) == 2 and args[0].lower() == "set":
                result = self.set_runtime_strategy_mode(args[1])
                return (
                    f"Strategy mode saved: {result['active_mode']} | path={result['config_path']} | "
                    "restart bot to apply fully"
                )
            if len(args) == 3 and args[0].lower() == "set" and args[2].lower() == "restart":
                result = self.set_runtime_strategy_mode(args[1])
                self.request_process_restart(reason=f"mode change to {result['active_mode']}")
                return (
                    f"Strategy mode saved: {result['active_mode']} | path={result['config_path']} | "
                    "restarting now"
                )
            return "Usage: mode show | mode cycle | mode <standard|trend|scalp> [restart] | mode set <standard|trend_only|scalping> [restart]"

        if command == "risk":
            if not args or args[0].lower() == "show":
                risk = float((self.config.get("risk", {}) or {}).get("max_risk_per_trade_pct", 0.0) or 0.0)
                level, _ = self._derive_risk_level()
                risk_cfg = self.config.get("risk", {}) or {}
                lines = [
                    f"Risk: {risk:.2f}% per trade ({level})",
                    f"SL: {risk_cfg.get('stop_loss_pct', '-')}% | TP: {risk_cfg.get('take_profit_pct', '-')}%",
                    f"Max positions: {risk_cfg.get('max_open_positions', '-')} | Max daily trades: {risk_cfg.get('max_daily_trades', '-')}",
                    f"Daily loss limit: {risk_cfg.get('max_daily_loss_pct', '-')}% | Cooldown: {risk_cfg.get('cool_down_minutes', '-')}m",
                ]
                bot_ref = self.bot
                risk_manager = getattr(bot_ref, 'risk_manager', None) if bot_ref else None
                if bot_ref and risk_manager:
                    portfolio_state = bot_ref._get_portfolio_state() if hasattr(bot_ref, '_get_portfolio_state') else {}
                    rs = risk_manager.get_risk_summary(portfolio_state.get('balance', 0))
                    lines.append(f"Today: {rs.get('trades_today', 0)}/{rs.get('max_daily_trades', '-')} trades | Loss: {rs.get('daily_loss', 0):.2f}/{rs.get('daily_loss_max', 0):.2f} THB ({rs.get('daily_loss_pct', 0):.2f}%)")
                    lines.append(f"Cooldown active: {'Yes' if rs.get('cooling_down') else 'No'}")
                return "\n".join(lines)
            if len(args) == 2 and args[0].lower() == "set":
                result = self.set_runtime_risk_pct(float(args[1]))
                return f"Runtime risk updated to {result['risk_pct']:.2f}% per trade ({result['risk_level']})"
            return "Usage: risk show | risk set <percent>"

        if command == "ui":
            if not args:
                return (
                    f"UI settings: log={self._cli_log_level_filter}+ | footer={self._cli_footer_mode}. "
                    "Use: ui log <debug|info|warning|error|critical> | ui footer <compact|verbose>"
                )

            if len(args) == 2 and args[0].lower() == "log":
                selected = self._normalize_cli_log_level(args[1])
                valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
                if selected not in valid_levels:
                    return "Usage: ui log <debug|info|warning|error|critical>"
                self._cli_log_level_filter = selected
                return f"UI log filter set to {selected}+"

            if len(args) == 2 and args[0].lower() == "footer":
                mode = self._normalize_cli_footer_mode(args[1])
                if mode not in {"compact", "verbose"}:
                    return "Usage: ui footer <compact|verbose>"
                self._cli_footer_mode = mode
                return f"UI footer mode set to {mode}"

            return "Usage: ui | ui log <debug|info|warning|error|critical> | ui footer <compact|verbose>"

        if command == "buy":
            if len(args) != 2:
                return "Usage: buy <PAIR> <THB_AMOUNT>"
            result = self.submit_manual_market_buy(args[0], float(args[1]))
            return (
                f"Market BUY submitted: {result['symbol']} {result['thb_amount']:.2f} THB | "
                f"order_id={result['order_id']} | filled_price={result['filled_price']:,.4f}"
            )

        if command == "track":
            if len(args) != 3:
                return "Usage: track <PAIR> <COIN_AMOUNT> <ENTRY_PRICE>"
            result = self.track_manual_position(args[0], float(args[1]), float(args[2]))
            return (
                f"Tracked position: {result['symbol']} {result['amount']:.8f} @ {result['entry_price']:,.4f} | "
                f"order_id={result['order_id']} | SL={result['stop_loss']:,.4f} | TP={result['take_profit']:,.4f}"
            )

        if command == "sell":
            if len(args) == 1:
                result = self.submit_manual_market_sell(args[0])
                return (
                    f"Market SELL submitted: {result['symbol']} {result['amount']:.8f} | "
                    f"order_id={result['order_id']}"
                )
            if len(args) == 2:
                result = self.submit_manual_market_sell(args[0], float(args[1]))
                return (
                    f"Market SELL submitted: {result['symbol']} {result['amount']:.8f} | "
                    f"order_id={result['order_id']}"
                )
            return "Usage: sell <PAIR> <COIN_AMOUNT> or sell <ORDER_ID>"

        if command == "close":
            if len(args) != 1:
                return "Usage: close <ORDER_ID>"
            result = self.submit_manual_market_sell(args[0])
            return (
                f"Active order closed via market SELL: {result['symbol']} {result['amount']:.8f} | "
                f"closed={result['closed_order_id']}"
            )

        if command == "pairs":
            if not args or args[0].lower() == "list":
                result = self.get_runtime_pairlist_status()
                configured = ", ".join(result["configured_pairs"]) or "NONE"
                active = ", ".join(result["active_pairs"]) or "NONE"
                return f"Pairlist: configured={configured} | active={active} | path={result['pairlist_path']}"
            subcommand = args[0].lower()
            if subcommand == "add" and len(args) >= 2:
                result = self.add_runtime_pairs(args[1:])
                added = ", ".join(result["added_pairs"]) or "none"
                active = ", ".join(result["active_pairs"]) or "NONE"
                return f"Pairs added: {added} | active={active}"
            if subcommand == "remove" and len(args) >= 2:
                result = self.remove_runtime_pairs(args[1:])
                removed = ", ".join(result["removed_pairs"]) or "none"
                active = ", ".join(result["active_pairs"]) or "NONE"
                return f"Pairs removed: {removed} | active={active}"
            if subcommand == "reload":
                active_pairs = self.refresh_runtime_pairs(reason="cli pair reload", force=True)
                active = ", ".join(active_pairs) or "NONE"
                return f"Runtime pairs reloaded: {active}"
            return "Usage: pairs list | pairs add <PAIR...> | pairs remove <PAIR...> | pairs reload"

        return f"Unknown command: {command}. Type 'help'"

    def process_cli_command(self, raw_command: str) -> str:
        """Parse and execute a runtime CLI command."""
        self._ensure_cli_chat_runtime_state()
        command_text = str(raw_command or "").strip()
        if not command_text:
            self._set_cli_chat_status("Command ignored: empty input")
            return ""

        try:
            parts = shlex.split(command_text)
        except ValueError as exc:
            self._set_cli_chat_status("CLI parse error")
            return f"CLI parse error: {exc}"

        if not parts:
            self._set_cli_chat_status("Command ignored: empty input")
            return ""

        command = parts[0].lower()
        args = parts[1:]

        def respond(message: str, status: str) -> str:
            self._set_cli_chat_status(status)
            return message

        try:
            if command == "cancel":
                with self._cli_chat_lock:
                    had_pending = self._cli_pending_confirmation is not None
                    self._cli_pending_confirmation = None
                if had_pending:
                    return respond("Pending confirmation cancelled", "Confirmation cancelled")
                return respond("No pending confirmation", "No pending confirmation")

            if command == "confirm":
                with self._cli_chat_lock:
                    pending = dict(self._cli_pending_confirmation) if self._cli_pending_confirmation else None
                    self._cli_pending_confirmation = None
                if not pending:
                    return respond("No pending confirmation", "No pending confirmation")
                result = self._execute_cli_command(str(pending.get("command") or ""), list(pending.get("args") or []))
                return respond(result, f"Confirmed: {pending.get('summary')}")

            if command in {"help", "?"}:
                result = self._execute_cli_command("help", args)
                return respond(result, f"Completed: {command_text}")

            if self._should_confirm_cli_command(command, args):
                confirmation = self._build_cli_confirmation_request(command, args, command_text)
                with self._cli_chat_lock:
                    self._cli_pending_confirmation = confirmation
                return respond(
                    f"{confirmation['summary']}\nType 'confirm' to continue or 'cancel' to abort.",
                    f"Pending confirmation: {confirmation['summary']}",
                )

            result = self._execute_cli_command(command, args)
            return respond(result, f"Completed: {command_text}")
        except Exception as exc:
            logger.error("[CLI] Command failed: %s", exc)
            return respond(f"CLI command failed: {exc}", f"Error: {command_text}")

    def _start_cli_command_listener(self) -> None:
        """Start a background stdin listener for runtime commands."""
        if not self._cli_commands_enabled:
            return
        if self._cli_command_thread and self._cli_command_thread.is_alive():
            return
        if not getattr(sys.stdin, "isatty", lambda: False)():
            logger.info("CLI command listener skipped: stdin is not interactive")
            return

        def _command_loop() -> None:
            logger.info("CLI command listener ready in Rich footer chat. Type 'help'.")

            if msvcrt is not None and self._live_dashboard_active:
                # ── Windows: char-by-char via msvcrt ──
                while not self._shutdown_event.is_set():
                    if not msvcrt.kbhit():
                        time.sleep(0.05)
                        continue

                    key = msvcrt.getwch()
                    if key in {"\x00", "\xe0"}:
                        try:
                            special_key = msvcrt.getwch()
                        except Exception:
                            pass
                        else:
                            if special_key in {"H", "\x48"}:
                                self._navigate_cli_history(-1)
                            elif special_key in {"P", "\x50"}:
                                self._navigate_cli_history(1)
                        continue
                    if key == "\r":
                        with self._cli_chat_lock:
                            raw_command = self._cli_chat_input
                            self._cli_chat_input = ""
                        result = self._submit_cli_chat_command(raw_command)
                        if result:
                            logger.info("[CLI] %s", result.replace("\n", "\n[CLI] "))
                        continue
                    if key in {"\x08", "\x7f"}:
                        with self._cli_chat_lock:
                            self._cli_chat_input = self._cli_chat_input[:-1]
                            self._cli_chat_status = "Editing command"
                        continue
                    if key == "\t":
                        self._accept_cli_suggestion()
                        continue
                    if key == "\x1b":
                        with self._cli_chat_lock:
                            self._cli_chat_input = ""
                            self._cli_chat_status = "Input cleared"
                        continue
                    if key == "\x03":
                        self._shutdown_event.set()
                        break
                    if key.isprintable():
                        with self._cli_chat_lock:
                            self._cli_chat_input += key
                            self._cli_history_index = None
                            self._cli_chat_status = "Typing..."
                return

            if _termios is not None and self._live_dashboard_active:
                # ── Linux: line-buffered input (typed chars echo, Rich overwrites on refresh) ──
                while not self._shutdown_event.is_set():
                    try:
                        line = sys.stdin.readline()
                    except EOFError:
                        break
                    except OSError:
                        time.sleep(2)
                        continue
                    if not line:
                        time.sleep(0.1)
                        continue
                    raw_command = line.strip()
                    if not raw_command:
                        continue
                    result = self._submit_cli_chat_command(raw_command)
                    if result:
                        logger.info("[CLI] %s", result.replace("\n", "\n[CLI] "))
                return

            while not self._shutdown_event.is_set():
                try:
                    raw_command = input("bot> ")
                except EOFError:
                    break
                except Exception as exc:
                    if self._shutdown_event.is_set():
                        break
                    logger.debug("CLI input loop error: %s", exc)
                    continue

                result = self._submit_cli_chat_command(raw_command)
                if result:
                    logger.info("[CLI] %s", result.replace("\n", "\n[CLI] "))

        self._cli_command_thread = threading.Thread(
            target=_command_loop,
            daemon=True,
            name="RuntimeCLICommands",
        )
        self._cli_command_thread.start()

    def get_balance_state(self):
        """Delegate to bot.get_balance_state() for other modules."""
        if self.bot and hasattr(self.bot, 'get_balance_state'):
            return self.bot.get_balance_state()
        return {'updated_at': None, 'balances': {}, 'api_health': {}, 'last_events': []}

    def _derive_cli_mode(self, bot_status: Dict[str, Any]) -> str:
        mode = str(bot_status.get("mode") or self.config.get("mode") or "dry_run").lower()
        if bool((bot_status.get("auth_degraded") or {}).get("active", False) or self.config.get("auth_degraded", False)):
            return "DEGRADED"
        if self.config.get("read_only", False):
            return "READ ONLY"
        if mode == "full_auto" and not self.config.get("simulate_only", True):
            return "LIVE"
        if mode == "semi_auto":
            return "SEMI AUTO"
        if self.config.get("simulate_only", False):
            return "SIMULATION"
        if mode == "dry_run":
            return "DRY RUN"
        return mode.replace("_", " ").upper()

    def _derive_risk_level(self) -> tuple[str, str]:
        risk_pct = float((self.config.get("risk", {}) or {}).get("max_risk_per_trade_pct", 0.0) or 0.0)
        if risk_pct > 5.0:
            return "HIGH", "bold red"
        if risk_pct >= 2.0:
            return "MEDIUM", "bold yellow"
        return "LOW", "bold green"

    @staticmethod
    def _format_cli_timestamp(value: Any) -> str:
        return format_bitkub_time(value)

    def _sample_api_latency(self, symbol: str) -> Optional[float]:
        now = time.time()
        if now - self._api_latency_checked_at < self._api_latency_cache_seconds:
            return self._api_latency_ms
        self._api_latency_checked_at = now
        if not self.api_client or not symbol or self.config.get("auth_degraded", False):
            self._api_latency_ms = None
            return None
        started = time.perf_counter()
        try:
            self.api_client.get_ticker(symbol)
            self._api_latency_ms = (time.perf_counter() - started) * 1000.0
        except Exception:
            self._api_latency_ms = None
        return self._api_latency_ms

    def _get_cli_price(self, symbol: str) -> Optional[float]:
        now = time.time()
        cached = self._cli_price_cache.get(symbol)
        if cached and (now - cached[1]) < self._cli_price_cache_ttl:
            return cached[0]
        ws_client = getattr(self.bot, "_ws_client", None) if self.bot else None
        price, _ = get_current_price(symbol=symbol, api_client=self.api_client, ws_client=ws_client)
        self._cli_price_cache[symbol] = (price, now)
        return price

    def _get_cli_balance_summary(self, portfolio_state: Dict[str, Any]) -> Dict[str, Any]:
        """Calculate total portfolio value in THB and per-asset valuation details."""
        fallback_balance = float(portfolio_state.get("balance", 0.0) or 0.0)
        if not self.api_client or self.config.get("auth_degraded", False):
            return {
                "total_balance": fallback_balance,
                "breakdown": [{"asset": "THB", "amount": fallback_balance, "value_thb": fallback_balance}],
            }

        try:
            balances = self.api_client.get_balances()
        except Exception:
            return {
                "total_balance": fallback_balance,
                "breakdown": [{"asset": "THB", "amount": fallback_balance, "value_thb": fallback_balance}],
            }

        if not isinstance(balances, dict):
            return {
                "total_balance": fallback_balance,
                "breakdown": [{"asset": "THB", "amount": fallback_balance, "value_thb": fallback_balance}],
            }

        total_value = 0.0
        breakdown: List[Dict[str, Any]] = []
        for asset, payload in balances.items():
            symbol = str(asset or "").upper()
            if not symbol:
                continue

            if isinstance(payload, dict):
                available = float(payload.get("available", 0.0) or 0.0)
                reserved = float(payload.get("reserved", 0.0) or 0.0)
            else:
                available = float(payload or 0.0)
                reserved = 0.0

            amount = available + reserved
            if amount <= 0:
                continue

            if symbol == "THB":
                total_value += amount
                breakdown.append({"asset": symbol, "amount": amount, "value_thb": amount})
                continue

            current_price = self._get_cli_price(f"THB_{symbol}")
            if current_price and current_price > 0:
                value_thb = amount * current_price
                total_value += value_thb
                breakdown.append({"asset": symbol, "amount": amount, "value_thb": value_thb})

        if total_value <= 0:
            return {
                "total_balance": fallback_balance,
                "breakdown": [{"asset": "THB", "amount": fallback_balance, "value_thb": fallback_balance}],
            }

        breakdown.sort(key=lambda item: float(item.get("value_thb") or 0.0), reverse=True)
        return {
            "total_balance": total_value,
            "breakdown": breakdown,
        }

    @staticmethod
    def _parse_reason_bool(reason: str, key: str) -> Optional[bool]:
        text = str(reason or "")
        match = re.search(rf"{re.escape(key)}=(True|False)", text)
        if not match:
            return None
        return match.group(1) == "True"

    @staticmethod
    def _describe_signal_alignment_status(record: Dict[str, Any], steps: Dict[str, Any]) -> str:
        data_check = steps.get("Sniper:DataCheck", {})
        data_check_result = str(data_check.get("result") or "").upper()
        data_check_reason = str(data_check.get("reason") or "").strip()

        if not record:
            return "Waiting for first signal flow"
        if data_check_result == "REJECT" and data_check_reason:
            return f"Waiting: {data_check_reason}"
        if data_check_result == "REJECT":
            return "Waiting for market data"
        return "Ready"

    @staticmethod
    def _build_pair_runtime_context(multi_timeframe_status: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, str]]:
        context: Dict[str, Dict[str, str]] = {}
        for row in list((multi_timeframe_status or {}).get("pairs") or []):
            pair = str(row.get("pair") or "").upper()
            if not pair:
                continue

            timeframe_rows = list(row.get("timeframes") or [])
            ready_count = sum(1 for item in timeframe_rows if int(item.get("count", 0) or 0) > 0)
            total_count = len(timeframe_rows)
            latest_raw = next((item.get("latest") for item in reversed(timeframe_rows) if item.get("latest")), None)

            context[pair] = {
                "tf_ready": f"{ready_count}/{total_count}" if total_count else "-",
                "pair_state": "Ready" if bool(row.get("ready")) else "Collecting",
                "market_update": TradingBotApp._format_cli_timestamp(latest_raw),
            }
        return context

    def _build_cli_signal_alignment(
        self,
        trading_pairs: List[str],
        multi_timeframe_status: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        flow = get_latest_signal_flow_snapshot()
        pair_runtime_context = self._build_pair_runtime_context(multi_timeframe_status)
        rows: List[Dict[str, Any]] = []
        for pair in trading_pairs:
            record = flow.get(str(pair or "").upper(), {})
            runtime_context = pair_runtime_context.get(str(pair or "").upper(), {})
            steps = dict(record.get("steps") or {})
            macro = steps.get("Sniper:MacroTrend", {})
            micro = steps.get("Sniper:MicroTrend", {})
            trigger = steps.get("Sniper:MACDTrigger", {})

            macro_reason = str(macro.get("reason") or "")
            micro_reason = str(micro.get("reason") or "")
            trigger_reason = str(trigger.get("reason") or "")

            macro_buy = self._parse_reason_bool(macro_reason, "buy_ok")
            macro_sell = self._parse_reason_bool(macro_reason, "sell_ok")
            micro_buy = self._parse_reason_bool(micro_reason, "buy_ok")
            micro_sell = self._parse_reason_bool(micro_reason, "sell_ok")

            trigger_buy_now = self._parse_reason_bool(trigger_reason, "buy_now")
            trigger_buy_prev = self._parse_reason_bool(trigger_reason, "buy_prev")
            trigger_sell_now = self._parse_reason_bool(trigger_reason, "sell_now")
            trigger_sell_prev = self._parse_reason_bool(trigger_reason, "sell_prev")
            trigger_buy = bool(trigger_buy_now) or bool(trigger_buy_prev)
            trigger_sell = bool(trigger_sell_now) or bool(trigger_sell_prev)

            trend_buy = bool(macro_buy) and bool(micro_buy)
            trend_sell = bool(macro_sell) and bool(micro_sell)
            final_action = "HOLD"
            if trend_buy and trigger_buy:
                final_action = "BUY"
            elif trend_sell and trigger_sell:
                final_action = "SELL"

            status = self._describe_signal_alignment_status(record, steps)
            if status != "Ready" and final_action == "HOLD":
                final_action = "WAIT"

            rows.append(
                {
                    "symbol": pair,
                    "macro": str(macro.get("result") or "N/A"),
                    "micro": str(micro.get("result") or "N/A"),
                    "trigger": str(trigger.get("result") or "N/A"),
                    "trend": "BUY" if trend_buy else ("SELL" if trend_sell else "MIXED"),
                    "trigger_side": "BUY" if trigger_buy else ("SELL" if trigger_sell else "NONE"),
                    "action": final_action,
                    "tf_ready": str(runtime_context.get("tf_ready") or "-"),
                    "pair_state": str(runtime_context.get("pair_state") or "-"),
                    "market_update": str(runtime_context.get("market_update") or "-"),
                    "status": status,
                    "updated_at": self._format_cli_timestamp(record.get("updated_at")),
                }
            )
        return rows

    @staticmethod
    def _format_cli_recent_events(bot_status: Dict[str, Any], limit: int = 5) -> List[Dict[str, str]]:
        events: List[Dict[str, str]] = []

        for item in list(bot_status.get("recent_trades") or []):
            timestamp = TradingBotApp._format_cli_timestamp(item.get("timestamp"))
            symbol = str(item.get("symbol") or "-")
            side = str(item.get("side") or "-").upper()
            status = str(item.get("status") or "-").upper()
            events.append(
                {
                    "timestamp": timestamp,
                    "type": "TRADE",
                    "message": f"{symbol} {side} {status}",
                }
            )

        for item in list(bot_status.get("balance_events") or []):
            events.append(
                {
                    "timestamp": TradingBotApp._format_cli_timestamp(item.get("timestamp")),
                    "type": str(item.get("type") or "BAL"),
                    "message": str(item.get("message") or "-")[:120],
                }
            )

        events.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
        return events[:max(1, int(limit or 5))]

    def get_cli_snapshot(self, bot_name: Optional[str] = None) -> Dict[str, Any]:
        """Build a lightweight runtime snapshot for the Rich command center."""
        bot_status = self.bot.get_status() if self.bot and hasattr(self.bot, "get_status") else {}
        risk_level, risk_style = self._derive_risk_level()
        positions: List[Dict[str, Any]] = []
        open_orders = self.executor.get_open_orders() if self.executor else []

        # Pre-fetch state machine snapshots for SL/TP fallback
        _state_manager = getattr(self.bot, "_state_manager", None) if self.bot else None

        for position in open_orders:
            symbol = str(position.get("symbol") or "")
            side_value = position.get("side")
            side = str(getattr(side_value, "value", side_value) or "")
            entry_price = float(position.get("entry_price") or 0.0)
            current_price = self._get_cli_price(symbol) if symbol else None
            pnl_pct = 0.0
            if current_price and entry_price > 0:
                if side.lower() == "sell":
                    pnl_pct = ((entry_price - current_price) / entry_price) * 100.0
                else:
                    pnl_pct = ((current_price - entry_price) / entry_price) * 100.0

            # Resolve SL/TP: prefer executor value, fallback to state machine snapshot
            pos_sl = position.get("stop_loss")
            pos_tp = position.get("take_profit")
            if (not pos_sl or float(pos_sl or 0) == 0) or (not pos_tp or float(pos_tp or 0) == 0):
                if _state_manager and symbol:
                    try:
                        snapshot = _state_manager.get_state(symbol)
                        if not pos_sl or float(pos_sl or 0) == 0:
                            pos_sl = snapshot.stop_loss if snapshot.stop_loss else pos_sl
                        if not pos_tp or float(pos_tp or 0) == 0:
                            pos_tp = snapshot.take_profit if snapshot.take_profit else pos_tp
                    except Exception:
                        pass

            positions.append({
                "symbol": symbol or "-",
                "side": side or "buy",
                "entry_price": entry_price,
                "current_price": current_price,
                "pnl_pct": pnl_pct,
                "stop_loss": pos_sl,
                "take_profit": pos_tp,
                "sl_distance_pct": (((float(pos_sl) - float(current_price)) / float(current_price)) * 100.0) if current_price and pos_sl else None,
                "tp_distance_pct": (((float(pos_tp) - float(current_price)) / float(current_price)) * 100.0) if current_price and pos_tp else None,
            })

        portfolio_state = self.bot._get_portfolio_state() if self.bot and hasattr(self.bot, "_get_portfolio_state") else {
            "balance": 0.0,
            "timestamp": None,
        }
        trading_pairs = list(bot_status.get("trading_pairs") or self.config.get("data", {}).get("pairs") or [])
        primary_symbol = trading_pairs[0] if trading_pairs else ""
        api_latency_ms = self._sample_api_latency(primary_symbol)
        now_dt = now_bitkub()
        last_market_raw = bot_status.get("last_loop") or portfolio_state.get("timestamp")
        last_market_dt = parse_as_bitkub_time(last_market_raw)
        market_age_seconds: Optional[int] = None
        if last_market_dt is not None:
            try:
                market_age_seconds = max(0, int((now_dt - last_market_dt).total_seconds()))
            except Exception:
                market_age_seconds = None
        refresh_baseline = max(1.0, float(getattr(self, "_cli_refresh_interval_seconds", 2.0) or 2.0))
        freshness = "fresh"
        if market_age_seconds is not None and market_age_seconds > int(refresh_baseline * 5):
            freshness = "critical"
        elif market_age_seconds is not None and market_age_seconds > int(refresh_baseline * 2):
            freshness = "warning"

        signal_alignment = self._build_cli_signal_alignment(
            trading_pairs,
            bot_status.get("multi_timeframe"),
        )
        recent_events = self._format_cli_recent_events(bot_status, limit=5)
        risk_summary = dict(bot_status.get("risk_summary") or {})
        balance_summary = self._get_cli_balance_summary(portfolio_state)
        total_balance_thb = float(balance_summary.get("total_balance", 0.0) or 0.0)
        balance_breakdown = []
        for item in list(balance_summary.get("breakdown") or []):
            asset = str(item.get("asset") or "").upper()
            amount = float(item.get("amount", 0.0) or 0.0)
            value_thb = float(item.get("value_thb", 0.0) or 0.0)
            if asset and value_thb > 0:
                allocation_pct = (value_thb / total_balance_thb * 100.0) if total_balance_thb > 0 else 0.0
                if asset == "THB":
                    amount_text = f"{amount:,.2f}"
                else:
                    amount_text = f"{amount:,.8f}"
                balance_breakdown.append(f"{asset} {amount_text} = {value_thb:,.2f} THB ({allocation_pct:.2f}%)")

        return {
            "bot_name": bot_name or self._cli_bot_name,
            "mode": self._derive_cli_mode(bot_status),
            "strategy_mode": str(self.config.get("active_strategy_mode") or self.config.get("strategy_mode", {}).get("active") or "standard"),
            "risk_level": risk_level,
            "risk_style": risk_style,
            "positions": positions,
            "pairs": ", ".join(trading_pairs) if trading_pairs else "NONE",
            "strategies": ", ".join((bot_status.get("strategy_engine") or {}).get("strategies") or []),
            "commands_hint": "Type in footer chat",
            "chat": self._get_cli_chat_snapshot(),
            "ui": {
                "log_level_filter": str(self._cli_log_level_filter or "INFO"),
                "footer_mode": str(self._cli_footer_mode or "compact"),
            },
            "updated_at": now_bitkub().strftime("%H:%M:%S"),
            "signal_alignment": signal_alignment,
            "recent_events": recent_events,
            "system": {
                "last_market_update": self._format_cli_timestamp(last_market_raw),
                "market_age_seconds": market_age_seconds,
                "freshness": freshness,
                "api_latency": f"{api_latency_ms:.0f} ms" if api_latency_ms is not None else "-",
                "available_balance": f"{float(portfolio_state.get('balance', 0.0) or 0.0):,.2f} THB",
                "total_balance": f"{total_balance_thb:,.2f} THB",
                "balance_breakdown": balance_breakdown,
                "trade_count": str(risk_summary.get("trades_today", bot_status.get("executed_today", 0))),
                "max_daily_trades": str(risk_summary.get("max_daily_trades", "-")),
                "daily_loss": f"{risk_summary.get('daily_loss', 0):.2f} / {risk_summary.get('daily_loss_max', 0):.2f} THB" if risk_summary else "-",
                "daily_loss_pct": f"{risk_summary.get('daily_loss_pct', 0):.2f}%" if risk_summary else "-",
                "max_open_positions": str(risk_summary.get("max_open_positions", "-")),
                "cooling_down": "Yes" if risk_summary.get("cooling_down") else "No",
                "risk_per_trade": f"{float((self.config.get('risk', {}) or {}).get('max_risk_per_trade_pct', 0.0) or 0.0):.1f}%",
            },
            "auth_degraded_reason": str(self.config.get("auth_degraded_reason") or ""),
        }

    def _emit_terminal_status(self) -> None:
        """Emit a compact status line for terminal-first operations."""
        if self._live_dashboard_active:
            return
        if not self.bot:
            return
        try:
            bot_status = self.bot.get_status() if hasattr(self.bot, "get_status") else {}
            pairs = list(bot_status.get("trading_pairs") or self.config.get("data", {}).get("pairs") or [])
            open_positions = len(self.executor.get_open_orders() or []) if self.executor else 0
            executed_today = int(bot_status.get("executed_today", 0) or 0)
            signal_source = str(bot_status.get("signal_source") or "strategy").upper()
            mode = str(bot_status.get("mode") or self.config.get("mode") or "unknown").upper()
            logger.debug(
                "[CLI STATUS] mode=%s source=%s pairs=%s open_positions=%d executed_today=%d",
                mode,
                signal_source,
                ",".join(pairs) if pairs else "NONE",
                open_positions,
                executed_today,
            )
        except Exception as exc:
            logger.debug("Failed to emit terminal status heartbeat: %s", exc)

    def _get_whitelist_file_signature(self) -> tuple[str, bool, int, int]:
        data_config = self.config.get("data", {})
        path = _get_dynamic_whitelist_path(data_config, PROJECT_ROOT)
        try:
            stat = path.stat()
            return (str(path), True, int(stat.st_mtime_ns), int(stat.st_size))
        except FileNotFoundError:
            return (str(path), False, 0, 0)

    def _get_protected_runtime_pairs(self) -> List[str]:
        protected: List[str] = []
        seen: set[str] = set()

        if self.executor:
            try:
                for order in self.executor.get_open_orders() or []:
                    symbol = str(order.get("symbol") or "").upper()
                    if symbol and symbol not in seen:
                        seen.add(symbol)
                        protected.append(symbol)
            except Exception as exc:
                logger.debug("Failed to collect protected open-order pairs: %s", exc)

        if self.bot and hasattr(self.bot, "_state_manager"):
            try:
                for snapshot in self.bot._state_manager.list_active_states():
                    symbol = str(getattr(snapshot, "symbol", "") or "").upper()
                    if symbol and symbol not in seen:
                        seen.add(symbol)
                        protected.append(symbol)
            except Exception as exc:
                logger.debug("Failed to collect protected state-machine pairs: %s", exc)

        return protected

    def _apply_runtime_pairs_update(self, resolved_pairs: List[str], reason: str, force: bool = False) -> List[str]:
        base_pairs = _normalize_pairs(resolved_pairs)
        protected_pairs = [pair for pair in self._get_protected_runtime_pairs() if pair not in base_pairs]
        final_pairs = base_pairs + protected_pairs

        data_config = self.config.setdefault("data", {})
        current_pairs = _normalize_pairs(data_config.get("pairs") or [])
        if not force and final_pairs == current_pairs:
            return current_pairs

        data_config["pairs"] = list(final_pairs)
        top_level_pair = final_pairs[0] if final_pairs else ""
        self.config["trading_pair"] = top_level_pair
        self.config.setdefault("trading", {})["trading_pair"] = top_level_pair

        if self.collector and hasattr(self.collector, "set_pairs"):
            self.collector.set_pairs(final_pairs)
        elif self.collector:
            self.collector.pairs = list(final_pairs)

        if self.bot and hasattr(self.bot, "update_runtime_pairs"):
            self.bot.update_runtime_pairs(final_pairs, reason=reason)

        if self.telegram_handler:
            self.telegram_handler.pairs = list(final_pairs)

        if protected_pairs:
            logger.info("Protected runtime pairs retained during %s: %s", reason, protected_pairs)
        logger.info("Runtime trading pairs updated via %s: %s", reason, final_pairs)
        return final_pairs

    def refresh_runtime_pairs(self, reason: str = "manual refresh", force: bool = False) -> List[str]:
        data_config = self.config.setdefault("data", {})
        auto_detect_held_pairs = data_config.get("auto_detect_held_pairs", True)
        if not auto_detect_held_pairs:
            return _normalize_pairs(data_config.get("pairs") or [])

        with self._pair_reload_lock:
            try:
                refresh_data_config = dict(data_config)
                refresh_data_config["pairs"] = []
                if self.config.get("auth_degraded", False):
                    resolved_pairs = _get_candidate_dynamic_pairs(refresh_data_config, PROJECT_ROOT)
                else:
                    if self.api_client is None:
                        return _normalize_pairs(data_config.get("pairs") or [])
                    resolved_pairs = resolve_runtime_trading_pairs(
                        self.api_client,
                        configured_pairs=[],
                        data_config=refresh_data_config,
                        project_root=PROJECT_ROOT,
                    )
            except BitkubAPIError as exc:
                logger.warning("Runtime pair refresh skipped due to Bitkub API error: %s", exc)
                return _normalize_pairs(data_config.get("pairs") or [])
            except Exception as exc:
                logger.error("Runtime pair refresh failed: %s", exc, exc_info=True)
                return _normalize_pairs(data_config.get("pairs") or [])

            return self._apply_runtime_pairs_update(resolved_pairs, reason=reason, force=force)

    def _start_pair_hot_reload(self):
        data_config = self.config.get("data", {})
        settings = _get_hybrid_dynamic_coin_settings(data_config)
        if not data_config.get("auto_detect_held_pairs", True):
            return
        if not settings.get("hot_reload_enabled", True):
            return
        if self._pair_reload_thread and self._pair_reload_thread.is_alive():
            return

        interval = max(5.0, float(settings.get("reload_interval_seconds", 30) or 30))
        whitelist_path = _get_dynamic_whitelist_path(data_config, PROJECT_ROOT)
        self._pair_reload_signature = self._get_whitelist_file_signature()

        def _watch_loop():
            while not self._shutdown_event.wait(interval):
                signature = self._get_whitelist_file_signature()
                if signature != self._pair_reload_signature:
                    self._pair_reload_signature = signature
                    self.refresh_runtime_pairs(reason="hybrid coin whitelist hot reload", force=True)

        self._pair_reload_thread = threading.Thread(
            target=_watch_loop,
            daemon=True,
            name="HybridCoinHotReload",
        )
        self._pair_reload_thread.start()
        logger.info(
            "Hybrid Dynamic Coin Config hot reload enabled | interval=%ss | path=%s",
            int(interval),
            whitelist_path,
        )

    def get_health_status(self) -> Dict[str, Any]:
        collector_running = bool(self.collector and getattr(self.collector, "running", False))
        bot_running = bool(self.bot and getattr(self.bot, "running", False))
        initialized = all([
            self.api_client is not None,
            self.collector is not None,
            self.bot is not None,
            self.executor is not None,
            self.signal_generator is not None,
            self.risk_manager is not None,
        ])

        auth_degraded = {
            "active": bool(self.config.get("auth_degraded", False)),
            "reason": str(self.config.get("auth_degraded_reason", "") or ""),
        }

        bot_status: Dict[str, Any] = {}
        if self.bot and hasattr(self.bot, "get_status"):
            try:
                bot_status = self.bot.get_status()
            except Exception as exc:
                logger.debug("Failed to build bot health status payload: %s", exc)

        healthy = initialized and collector_running and bot_running and not self._shutdown_event.is_set()
        status = "degraded" if healthy and auth_degraded["active"] else "ok" if healthy else "error"

        return {
            "status": status,
            "healthy": healthy,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "uptime_seconds": round(time.time() - self._app_started_at, 1),
            "mode": self.config.get("trading", {}).get("mode") or self.config.get("mode"),
            "simulate_only": bool(self.config.get("simulate_only", False)),
            "read_only": bool(self.config.get("read_only", False)),
            "auth_degraded": auth_degraded,
            "pairs": list((self.config.get("data", {}) or {}).get("pairs") or []),
            "collector": {
                "running": collector_running,
                "pairs": self.collector.get_pairs() if self.collector else [],
            },
            "bot": {
                "running": bot_running,
                "status": bot_status,
            },
            "telegram": {
                "polling_enabled": bool(self.telegram_handler),
            },
        }

    def _start_health_server(self):
        if self.health_server is not None and self._health_server_started:
            return

        monitoring_config = self.config.get("monitoring", {}) or {}
        health_port = int(monitoring_config.get("health_check_port", 0) or 0)
        health_path = str(monitoring_config.get("health_check_path") or "/health")
        if health_port <= 0:
            logger.info("Bot health HTTP server disabled (monitoring.health_check_port <= 0)")
            return

        self.health_server = BotHealthServer(
            host="0.0.0.0",
            port=health_port,
            path=health_path,
            status_provider=self.get_health_status,
        )
        try:
            self._health_server_started = self.health_server.start()
        except Exception as exc:
            self._health_server_started = False
            logger.warning("Failed to start bot health HTTP server on port %s: %s", health_port, exc)

    def _stop_health_server(self):
        if self.health_server is None:
            return
        try:
            self.health_server.stop()
        except Exception as exc:
            logger.warning("Failed to stop bot health HTTP server cleanly: %s", exc)
        finally:
            self.health_server = None
            self._health_server_started = False
    
    def initialize(self):
        """Initialize all components."""
        logger.info("กำลังเริ่มต้นระบบ Crypto Trading Bot...")
        logger.info(f"โหมดการทำงาน: {self.config.get('trading', {}).get('mode', 'semi_auto')}")

        # Validate configuration before starting
        critical_errors, warnings = validate_config()
        for err in warnings:
            logger.warning(f"คำเตือนจาก Config: {err}")
        if critical_errors:
            for err in critical_errors:
                logger.error(f"ข้อผิดพลาดจาก Config: {err}")
            logger.error("มีข้อผิดพลาดร้ายแรงเกี่ยวกับคอนฟิก — ปฏิเสธการเริ่มต้นระบบ")
            return False
        logger.info("ตรวจสอบ Config ผ่านเรียบร้อย")
        
        # 1. Initialize API Client
        self.api_client = BitkubClient(
            api_key=BITKUB.api_key,
            api_secret=BITKUB.api_secret,
            base_url=BITKUB.base_url
        )
        logger.info("API Client initialized")

        data_config = self.config.setdefault("data", {})
        configured_pairs = data_config.get("pairs") or []
        candidate_pairs = _get_candidate_dynamic_pairs(data_config, PROJECT_ROOT)
        auto_detect_held_pairs = data_config.get("auto_detect_held_pairs", True)
        auth_degraded = bool(self.config.get("auth_degraded", False))
        if auto_detect_held_pairs and not auth_degraded:
            try:
                with self.api_client.suppress_fatal_auth_handling("startup pair auto-detection"):
                    resolved_pairs = resolve_runtime_trading_pairs(
                        self.api_client,
                        configured_pairs=configured_pairs,
                        data_config=data_config,
                        project_root=PROJECT_ROOT,
                    )
            except BitkubAPIError as exc:
                if exc.code != 5:
                    raise

                reason = (
                    f"Bitkub private API blocked during startup: {exc.message}; "
                    "running in degraded public-only mode"
                )
                _clear_startup_auth_shutdown_state(self.api_client)
                resolved_pairs = _enable_startup_auth_degraded_mode(self.config, reason, candidate_pairs)
                self.trading_disabled.set()
                auth_degraded = True
                logger.warning(
                    "Bitkub auth error 5 during startup — continuing in degraded mode: %s",
                    exc,
                )
                logger.warning(
                    "Degraded mode active: trading, rebalancing, reconciliation, and private balance sync are disabled until credentials are fixed"
                )

            data_config["pairs"] = resolved_pairs
            top_level_pair = resolved_pairs[0] if resolved_pairs else ""
            self.config["trading_pair"] = top_level_pair
            self.config.setdefault("trading", {})["trading_pair"] = top_level_pair
            if auth_degraded and resolved_pairs:
                logger.warning(f"Degraded startup pairs from config: {' add '.join(resolved_pairs)}")
            elif auth_degraded:
                logger.warning("Degraded startup mode active with no configured pairs — bot will run collector/monitor only")
            elif resolved_pairs:
                logger.info(f"คู่เหรียญที่ถือบน Bitkub และจะติดตาม: {' add '.join(resolved_pairs)}")
            else:
                logger.warning("Bitkub ไม่พบเหรียญที่ถืออยู่ในคู่ THB ที่เทรดได้ — bot จะเริ่มแบบไม่มีคู่เหรียญ active")
        else:
            resolved_pairs = _normalize_pairs(configured_pairs)
            data_config["pairs"] = resolved_pairs
            top_level_pair = resolved_pairs[0] if resolved_pairs else str(
                self.config.get("trading", {}).get("trading_pair") or self.config.get("trading_pair") or ""
            ).upper()
            self.config["trading_pair"] = top_level_pair
            self.config.setdefault("trading", {})["trading_pair"] = top_level_pair
            if resolved_pairs:
                logger.info(f"คู่เหรียญที่เทรดจาก config: {' add '.join(resolved_pairs)}")
            else:
                logger.warning("Config ไม่มีคู่เหรียญที่ใช้งานอยู่ — bot จะเริ่มแบบไม่มีคู่เหรียญ active")
        
        # 2. Initialize Risk Manager
        risk_config = self.config.get("risk", {})
        risk_params = RiskConfig(
            max_risk_per_trade_pct=risk_config.get("max_risk_per_trade_pct", 4.0),
            max_daily_loss_pct=risk_config.get("max_daily_loss_pct", 10.0),
            max_position_per_trade_pct=risk_config.get("max_position_per_trade_pct", 10.0),
            max_drawdown_threshold_pct=risk_config.get("max_drawdown_threshold_pct", 12.0),
            drawdown_soft_reduce_start_pct=risk_config.get("drawdown_soft_reduce_start_pct", 5.0),
            min_drawdown_risk_multiplier=risk_config.get("min_drawdown_risk_multiplier", 0.35),
            drawdown_block_new_entries=risk_config.get("drawdown_block_new_entries", True),
            stop_loss_pct=risk_config.get("stop_loss_pct", -4.5),
            take_profit_pct=risk_config.get("take_profit_pct", 10.0),
            atr_multiplier=risk_config.get("atr_multiplier", 3.0),
            atr_period=risk_config.get("atr_period", 14),
            use_dynamic_sl_tp=risk_config.get("use_dynamic_sl_tp", True),
            initial_balance=self.config.get("portfolio", {}).get("initial_balance", 1000.0),
            min_balance_threshold=self.config.get("portfolio", {}).get("min_balance_threshold", 100.0),
            max_open_positions=risk_config.get("max_open_positions", 3),
            max_daily_trades=risk_config.get("max_daily_trades", 10),
            cool_down_minutes=risk_config.get("cool_down_minutes", 5),
            min_order_amount=self.config.get("trading", {}).get("min_order_amount", 15.0),
        )
        self.risk_manager = RiskManager(risk_params)
        logger.info("ระบบจัดการความเสี่ยงพร้อมทำงาน (Risk Manager initialized)")
        
        # 3. Initialize Signal Generator
        strategies_config = self.config.get("strategies", {})
        self.signal_generator = SignalGenerator({
            "min_confidence": strategies_config.get("min_confidence", 0.5),
            "min_strategies_agree": strategies_config.get("min_strategies_agree", 2),
            "max_open_positions": risk_config.get("max_open_positions", 3),
            "max_daily_trades": risk_config.get("max_daily_trades", 10),
            "scalping": strategies_config.get("scalping", {}),
        })
        logger.info("ระบบสร้างสัญญาณเทรดพร้อมทำงาน (Signal Generator initialized)")
        
        # 4. Initialize Trade Executor (with DB persistence)
        from database import get_database
        db = get_database()
        
        execution_config = self.config.get("execution", {})
        state_config = self.config.get("state_management", {})
        telegram_enabled = os.environ.get("TELEGRAM_ENABLED", "true").strip().lower() in (
            "1", "true", "yes", "on"
        )
        notif_config = self.config.get("notifications", {}) or {}
        telegram_command_polling_enabled = bool(notif_config.get("telegram_command_polling_enabled", True))
        bot_token, chat_id = _resolve_telegram_credentials(self.config)

        # 5. Setup alert system before executor so OMS notifications can reuse the shared transport.
        self.alert_system = AlertSystem(bot_token=bot_token, chat_id=chat_id)
        self.alert_sender = self.alert_system.create_trade_sender()

        self.executor = TradeExecutor(
            api_client=self.api_client,
            config={
                "retry_attempts": execution_config.get("retry_attempts", 3),
                "retry_delay_seconds": execution_config.get("retry_delay_seconds", 5),
                "order_timeout_seconds": execution_config.get("order_timeout_seconds", 30),
                "order_type": execution_config.get("order_type", "limit"),
                "allow_trailing_stop": state_config.get(
                    "allow_trailing_stop",
                    execution_config.get("allow_trailing_stop", True),
                ),
            },
            risk_manager=self.risk_manager,
            db=db,
            notifier=self.alert_system.telegram,
        )
        logger.info("ระบบประมวลผลคำสั่งซื้อขายพร้อมทำงาน (Trade Executor initialized)")
        if telegram_enabled and self.alert_system.telegram.enabled:
            logger.info("เปิดใช้งานการแจ้งเตือนผ่าน Telegram (พร้อม Rate Limiting)")
        else:
            logger.info("การแจ้งเตือน Telegram ถูกปิด (ใช้ Console log แทน)")
        
        # 6. Initialize Trading Bot Orchestrator
        self.bot = TradingBotOrchestrator(
            config=self.config,
            api_client=self.api_client,
            signal_generator=self.signal_generator,
            risk_manager=self.risk_manager,
            executor=self.executor,
            alert_sender=self.alert_sender,
            alert_system=self.alert_system,
            trading_disabled_event=self.trading_disabled,
        )
        logger.info("Trading Bot Orchestrator พร้อมทำงาน")

        # 6b. Initialize Telegram Bot Handler
        telegram_pairs = list(self.config.get("data", {}).get("pairs") or [])
        if not telegram_enabled:
            logger.info("ปิดการใช้งาน Telegram ผ่าน TELEGRAM_ENABLED=false")
        elif not telegram_command_polling_enabled:
            logger.info("ปิดการใช้งานคำสั่ง Telegram polling ผ่าน notifications.telegram_command_polling_enabled=false")
        elif not bot_token:
            logger.info("ไม่ได้ตั้งค่า Telegram Bot Token — ปิดการใช้งานคำสั่งผ่าน Telegram")
        elif not chat_id:
            logger.info("ไม่ได้ตั้งค่า Telegram Chat ID — ปิดการใช้งานคำสั่งผ่าน Telegram")
        else:
            self.telegram_handler = TelegramBotHandler(
                app_ref=self,
                bot_token=bot_token,
                chat_id=chat_id,
                pairs=telegram_pairs,
                trading_disabled=self.trading_disabled,
            )
            self.telegram_handler.start()
            logger.info("Telegram bot handler เริ่มทำงาน")
        
        # 7. Initialize Data Collector (background)
        data_config = self.config.get("data", {})
        pairs = list(data_config.get("pairs") or [])
        interval = data_config.get("collect_interval_seconds", 60)
        
        self.collector = BitkubCollector(
            pairs=pairs,
            interval=interval,
            multi_timeframe_config=self.config.get("multi_timeframe", {}),
        )
        logger.info("ระบบเก็บข้อมูลเริ่มทำงาน (Data Collector initialized)")
        
        logger.info("คอมโพเนนต์ทั้งหมดเริ่มต้นสำเร็จพร้อมใช้งาน")
        return True
    
    def start(self, register_signal_handlers: bool = True):
        """Start all components."""
        logger.info("กำลังเริ่มระบบ Crypto Trading Bot...")
        self._shutdown_event.clear()
        self._app_started_at = time.time()
        collector = self.collector
        bot = self.bot
        if collector is None or bot is None:
            raise RuntimeError("TradingBotApp.start() called before initialize() completed")
        
        # Start data collector (background thread)
        collector.start()
        logger.info("ระบบเก็บข้อมูล Data Collector เริ่มต้นสำเร็จ (ทำงานเบื้องหลัง)")
        
        # Small delay to let collector get initial data
        time.sleep(2)
        
        # Start trading bot (main loop)
        bot.start()
        logger.info("Trading Bot เริ่มต้นสำเร็จ (main loop)")

        self._start_health_server()

        self._start_pair_hot_reload()
        
        # Setup signal handlers only for standalone/main-thread execution.
        if register_signal_handlers:
            setup_signal_handlers(bot, collector, self.telegram_handler)
        
        logger.info("=" * 50)
        logger.info("✨ CRYPTO TRADING BOT กำลังทำงาน (RUNNING) ✨")
        logger.info(f"โหมดการทำงาน (Mode): {self.config.get('trading', {}).get('mode', 'semi_auto').upper()}")
        pairs = list(self.config.get('data', {}).get('pairs') or [])
        logger.info(f"คู่เหรียญที่เทรด (Pair): {' add '.join(pairs) if pairs else 'ไม่มี'}")
        logger.info("กด Ctrl+C เพื่อหยุดระบบ")
        logger.info("=" * 50)
        self._emit_terminal_status()
    
    def stop(self):
        """Stop all components gracefully."""
        logger.info("กำลังหยุดการทำงาน Crypto Trading Bot...")
        self._shutdown_event.set()

        self._stop_health_server()

        if self._pair_reload_thread and self._pair_reload_thread.is_alive():
            self._pair_reload_thread.join(timeout=5)
            self._pair_reload_thread = None
        
        # Stop bot first
        if self.bot:
            self.bot.stop()

        # Stop Telegram handler
        if self.telegram_handler:
            self.telegram_handler.stop()

        # Stop collector
        if self.collector:
            self.collector.stop()

        # Release process lock
        release_bot_lock()
        
        logger.info("คอมโพเนนต์ทั้งหมดถูกหยุดการทำงานแล้ว (All components stopped)")
    
    def run(self, register_signal_handlers: bool = True):
        """Run the bot until shutdown signal received."""
        if not self.initialize():
            logger.error("Initialization failed — not starting bot")
            return
        command_center: Optional[CLICommandCenter] = None
        if self._cli_ui_enabled:
            try:
                command_center = CLICommandCenter(
                    self,
                    bot_name=self._cli_bot_name,
                    refresh_interval_seconds=self._cli_refresh_interval_seconds,
                    console=get_shared_console(),
                )
            except Exception as exc:
                logger.warning("CLI command center disabled: %s", exc)

        try:
            if command_center:
                self._live_dashboard_active = True
                logger.info("CLI dashboard enabled — starting Rich Live display")
                command_center.start_log_capture()
                with command_center.create_live() as live:
                    self.start(register_signal_handlers=register_signal_handlers)
                    self._start_cli_command_listener()
                    next_refresh_at = 0.0
                    _render_failure_count = 0
                    while self.bot and self.bot.running:
                        from api_client import SHOULD_SHUTDOWN as _shutdown_flag
                        if _shutdown_flag:
                            logger.critical(
                                "🚨 Main thread detected SHOULD_SHUTDOWN — "
                                "initiating graceful exit"
                            )
                            break
                        now = time.time()
                        if now >= next_refresh_at:
                            try:
                                live.update(command_center.render(), refresh=True)
                                _render_failure_count = 0
                            except Exception as render_exc:
                                _render_failure_count += 1
                                if _render_failure_count <= 3:
                                    logger.warning("CLI render error (%d): %s", _render_failure_count, render_exc)
                                if _render_failure_count >= 10:
                                    logger.error("CLI render failed %d times — disabling Live dashboard", _render_failure_count)
                                    break
                            next_refresh_at = now + command_center.refresh_interval_seconds
                        # Auto-restart CLI listener if thread died
                        if self._cli_command_thread and not self._cli_command_thread.is_alive():
                            self._cli_command_thread = None
                            self._start_cli_command_listener()
                        time.sleep(1)
                # Fallback: if Live dashboard broke, continue running without it
                if self.bot and self.bot.running and _render_failure_count >= 10:
                    self._live_dashboard_active = False
                    logger.info("Falling back to plain log mode (no Live dashboard)")
                    while self.bot and self.bot.running:
                        from api_client import SHOULD_SHUTDOWN as _shutdown_flag
                        if _shutdown_flag:
                            logger.critical(
                                "🚨 Main thread detected SHOULD_SHUTDOWN — "
                                "initiating graceful exit"
                            )
                            break
                        now = time.time()
                        if now - self._last_status_log_at >= self._status_interval_seconds:
                            self._emit_terminal_status()
                            self._last_status_log_at = now
                        time.sleep(1)
            else:
                self.start(register_signal_handlers=register_signal_handlers)
                self._start_cli_command_listener()
                while self.bot and self.bot.running:
                    from api_client import SHOULD_SHUTDOWN as _shutdown_flag
                    if _shutdown_flag:
                        logger.critical(
                            "🚨 Main thread detected SHOULD_SHUTDOWN — "
                            "initiating graceful exit"
                        )
                        break
                    now = time.time()
                    if now - self._last_status_log_at >= self._status_interval_seconds:
                        self._emit_terminal_status()
                        self._last_status_log_at = now
                    time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        finally:
            self._live_dashboard_active = False
            if command_center:
                command_center.stop_log_capture()
            self.stop()
            self._perform_requested_restart()


def main():
    """Main entry point."""
    # Load config first so logging can use YAML settings
    config_path = os.environ.get("BOT_CONFIG_PATH", None)
    config = load_bot_config(config_path)

    # Setup logging with config-driven settings
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    setup_logging(log_level, yaml_config=config)

    startup_test_mode = os.environ.get("BOT_STARTUP_TEST_MODE", "").strip().lower() in (
        "1", "true", "yes", "on"
    )
    if startup_test_mode:
        os.environ["TELEGRAM_ENABLED"] = "false"
        os.environ["BOT_READ_ONLY"] = "true"

    if startup_test_mode:
        config.setdefault("trading", {})
        config["mode"] = "dry_run"
        config["trading"]["mode"] = "dry_run"
        config["simulate_only"] = True
        config["read_only"] = True
        logger.info("BOT_STARTUP_TEST_MODE enabled: forcing dry_run + read_only + TELEGRAM_ENABLED=false")
    
    # Override with environment variables if set
    if os.environ.get("BOT_MODE"):
        config["mode"] = os.environ["BOT_MODE"]
    if os.environ.get("TRADING_PAIR"):
        config["trading_pair"] = os.environ["TRADING_PAIR"]
    if os.environ.get("SIMULATE_ONLY"):
        config["simulate_only"] = os.environ["SIMULATE_ONLY"].lower() in ("true", "1", "yes")
    
    # Validate live trading warning
    if not config.get("simulate_only", True) and not TRADING.live_trading:
        logger.warning("=" * 60)
        logger.warning("⚠️  WARNING: LIVE TRADING IS ENABLED!")
        logger.warning("⚠️  Real orders WILL be placed on the exchange!")
        logger.warning("⚠️  Make sure you understand the risks!")
        logger.warning("=" * 60)
        time.sleep(3)
    
    # Acquire singleton process lock — prevent duplicate bot instances
    if not acquire_bot_lock(source="main"):
        lock_info = get_lock_status()
        logger.critical(
            "Cannot start: another bot instance is already running "
            "(PID=%s, started=%s). Kill it first or remove bot.pid",
            lock_info.get("pid"), lock_info.get("started_at"),
        )
        sys.exit(1)

    # Proactive IP check — detect changes before Bitkub rejects us
    try:
        from api_client import check_ip_change_on_startup
        check_ip_change_on_startup()
    except Exception as exc:
        logger.debug("IP check skipped: %s", exc)

    # Create and run app
    app = TradingBotApp(config, config_path=config_path)
    
    try:
        app.run()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        app.stop()
        sys.exit(1)
    finally:
        release_bot_lock()


if __name__ == "__main__":
    main()
