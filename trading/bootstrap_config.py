"""Config loading, strategy mode profile merger, and hybrid pair resolution (extracted from ``main``)."""

from __future__ import annotations

import json
import logging
import os
from os import PathLike
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from api_client import BinanceThClient
from config import TRADING
from dynamic_coin_config import (
    DEFAULT_WHITELIST_JSON,
    HybridDynamicPairResolver,
    JsonCoinWhitelistRepository,
    resolve_whitelist_path,
)
from project_paths import PROJECT_ROOT
from risk_management import RiskConfig, RiskManager
from trading.cli_pair_normalize import extract_asset_from_pair, normalize_cli_pair, normalize_pairs

logger = logging.getLogger(__name__)


def get_hybrid_dynamic_coin_settings(data_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    settings = dict((data_config or {}).get("hybrid_dynamic_coin_config") or {})
    settings.setdefault("whitelist_json_path", DEFAULT_WHITELIST_JSON)
    settings.setdefault("min_quote_balance_thb", max(float(TRADING.min_order_amount or 0.0), 100.0))
    settings.setdefault("require_supported_market", True)
    settings.setdefault("include_assets_with_balance", True)
    settings.setdefault("hot_reload_enabled", True)
    settings.setdefault("reload_interval_seconds", 30)
    return settings


def get_candidate_dynamic_pairs(
    data_config: Optional[Dict[str, Any]] = None,
    project_root: Optional[Path] = None,
) -> List[str]:
    configured_pairs = normalize_pairs((data_config or {}).get("pairs") or [])
    if configured_pairs:
        return configured_pairs

    settings = get_hybrid_dynamic_coin_settings(data_config)
    whitelist_path = resolve_whitelist_path(settings.get("whitelist_json_path"), project_root or PROJECT_ROOT)
    resolver = HybridDynamicPairResolver(JsonCoinWhitelistRepository(default_path=whitelist_path))
    return resolver.list_candidate_pairs(whitelist_path)


def get_dynamic_whitelist_path(
    data_config: Optional[Dict[str, Any]] = None,
    project_root: Optional[Path] = None,
) -> Path:
    settings = get_hybrid_dynamic_coin_settings(data_config)
    return resolve_whitelist_path(settings.get("whitelist_json_path"), project_root or PROJECT_ROOT)


def merge_unique_timeframes(existing: Iterable[str], additions: Iterable[str]) -> List[str]:
    merged: List[str] = []
    seen: set[str] = set()
    for item in list(existing or []) + list(additions or []):
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        merged.append(value)
    return merged


def resolve_mode_active_strategies(
    runtime_config: Dict[str, Any],
    active_mode: str,
    *,
    fallback: Iterable[str],
) -> List[str]:
    """Resolve active strategies from mode profile, then fallback list."""
    mode_profiles = dict(runtime_config.get("mode_indicator_profiles", {}) or {})
    profile = dict(mode_profiles.get(str(active_mode or "").strip().lower(), {}) or {})
    configured = profile.get("active_strategies")
    candidates = configured if isinstance(configured, list) and configured else list(fallback or [])
    normalized: List[str] = []
    seen: set[str] = set()
    for strategy_name in candidates:
        name = str(strategy_name or "").strip()
        if not name:
            continue
        lowered = name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(name)
    return normalized


def apply_strategy_mode_profile(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
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
        strategies_cfg["enabled"] = resolve_mode_active_strategies(
            runtime_config,
            active_mode,
            fallback=["trend_following"],
        )
        strategies_cfg["min_confidence"] = float(
            trend_mode.get("min_confidence", strategies_cfg.get("min_confidence", 0.35))
        )
        strategies_cfg["min_strategies_agree"] = int(trend_mode.get("min_strategies_agree", 1) or 1)

        risk_cfg["stop_loss_pct"] = float(trend_mode.get("stop_loss_pct", risk_cfg.get("stop_loss_pct", 4.5)))
        risk_cfg["take_profit_pct"] = float(trend_mode.get("take_profit_pct", risk_cfg.get("take_profit_pct", 10.0)))
        risk_cfg["cool_down_minutes"] = float(
            trend_mode.get("min_time_between_trades_minutes", risk_cfg.get("cool_down_minutes", 5)) or 5
        )
        risk_cfg["sl_tp_percent_source_when_dynamic"] = "risk_config"

        state_cfg["confirmations_required"] = int(
            trend_mode.get("confirmations_required", state_cfg.get("confirmations_required", 1)) or 1
        )
        state_cfg["confirmation_window_seconds"] = int(
            trend_mode.get("confirmation_window_seconds", state_cfg.get("confirmation_window_seconds", 180)) or 180
        )

        auto_exit_cfg["max_hold_hours"] = hold_hours
        mtf_cfg["enabled"] = bool(trend_mode.get("mtf_enabled", mtf_cfg.get("enabled", True)))
        trend_confirm_tf = str(trend_mode.get("confirm_timeframe") or "1h")
        mtf_cfg["timeframes"] = merge_unique_timeframes(mtf_cfg.get("timeframes") or [], [trend_tf, trend_confirm_tf])
        mtf_cfg["higher_timeframes"] = merge_unique_timeframes([], [trend_confirm_tf])
        return runtime_config

    if active_mode != "scalping":
        return runtime_config

    scalping_mode = dict(strategy_mode.get("scalping", {}) or {})

    primary_tf = str(scalping_mode.get("primary_timeframe") or trading_cfg.get("timeframe") or "15m")
    confirm_tf = str(scalping_mode.get("confirm_timeframe") or "15m")
    trend_tf = str(scalping_mode.get("trend_timeframe") or "1h")
    max_hold_minutes = int(scalping_mode.get("position_timeout_minutes", 30) or 30)
    bootstrap_timeout_hours = float(scalping_mode.get("bootstrap_position_timeout_hours", 24) or 24)

    trading_cfg["timeframe"] = primary_tf
    strategies_cfg["enabled"] = resolve_mode_active_strategies(
        runtime_config,
        active_mode,
        fallback=["scalping"],
    )
    strategies_cfg["min_confidence"] = float(
        scalping_mode.get("min_confidence", strategies_cfg.get("min_confidence", 0.35))
    )
    strategies_cfg["min_strategies_agree"] = int(scalping_mode.get("min_strategies_agree", 1) or 1)

    risk_cfg["stop_loss_pct"] = float(scalping_mode.get("stop_loss_pct", 0.75))
    risk_cfg["take_profit_pct"] = float(scalping_mode.get("take_profit_pct", 1.75))
    risk_cfg["sl_tp_percent_source_when_dynamic"] = "risk_config"
    risk_cfg["max_daily_trades"] = int(
        scalping_mode.get("max_trades_per_day", risk_cfg.get("max_daily_trades", 50)) or 50
    )
    risk_cfg["max_position_per_trade_pct"] = float(
        scalping_mode.get("max_position_per_trade_pct", risk_cfg.get("max_position_per_trade_pct", 20.0))
    )
    risk_cfg["max_risk_per_trade_pct"] = float(
        scalping_mode.get("max_risk_per_trade_pct", risk_cfg.get("max_risk_per_trade_pct", 2.0))
    )
    risk_cfg["cool_down_minutes"] = float(
        scalping_mode.get("min_time_between_trades_minutes", risk_cfg.get("cool_down_minutes", 5)) or 5
    )

    state_cfg["confirmations_required"] = int(
        scalping_mode.get("confirmations_required", state_cfg.get("confirmations_required", 1)) or 1
    )
    state_cfg["confirmation_window_seconds"] = int(scalping_mode.get("confirmation_window_seconds", 90) or 90)
    state_cfg["pending_buy_timeout_seconds"] = int(scalping_mode.get("pending_buy_timeout_seconds", 60) or 60)
    state_cfg["pending_sell_timeout_seconds"] = int(scalping_mode.get("pending_sell_timeout_seconds", 60) or 60)

    position_sizing_cfg["risk_per_trade_pct"] = float(
        scalping_mode.get("position_risk_per_trade_pct", position_sizing_cfg.get("risk_per_trade_pct", 1.0))
    )
    position_sizing_cfg["max_position_pct"] = float(
        scalping_mode.get("position_size_cap_pct", position_sizing_cfg.get("max_position_pct", 10.0))
    )
    auto_exit_cfg["max_hold_hours"] = max_hold_minutes / 60.0
    auto_exit_cfg["check_interval_seconds"] = int(
        scalping_mode.get("monitor_check_interval_seconds", auto_exit_cfg.get("check_interval_seconds", 10)) or 10
    )

    mtf_cfg["enabled"] = True
    mtf_cfg["timeframes"] = merge_unique_timeframes(
        mtf_cfg.get("timeframes") or [], [primary_tf, confirm_tf, trend_tf]
    )
    mtf_cfg["higher_timeframes"] = merge_unique_timeframes([], [trend_tf])

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
            "bootstrap_position_timeout_hours": bootstrap_timeout_hours,
        }
    )
    strategies_cfg["scalping"] = scalping_strategy_cfg
    return runtime_config


def risk_manager_from_config(config: Dict[str, Any]) -> RiskManager:
    """Build RiskManager from a merged runtime config (used at startup and after strategy mode switch)."""
    risk_config = dict(config.get("risk", {}) or {})
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
        use_fractional_kelly=bool(risk_config.get("use_fractional_kelly", True)),
        initial_balance=config.get("portfolio", {}).get("initial_balance", 1000.0),
        min_balance_threshold=config.get("portfolio", {}).get("min_balance_threshold", 100.0),
        max_open_positions=risk_config.get("max_open_positions", 3),
        max_daily_trades=risk_config.get("max_daily_trades", 10),
        cool_down_minutes=risk_config.get("cool_down_minutes", 5),
        min_order_amount=config.get("trading", {}).get("min_order_amount", 15.0),
    )
    return RiskManager(risk_params)


def normalize_optional_secret(value: Optional[Any]) -> str:
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


def resolve_telegram_credentials(config: Optional[Dict[str, Any]] = None) -> tuple[str, str]:
    runtime_config = config or {}
    api_keys = runtime_config.get("api_keys", {}) or {}
    notifications = runtime_config.get("notifications", {}) or {}

    bot_token = normalize_optional_secret(os.environ.get("TELEGRAM_BOT_TOKEN"))
    if not bot_token:
        bot_token = normalize_optional_secret(api_keys.get("telegram_bot_token"))

    chat_id = normalize_optional_secret(os.environ.get("TELEGRAM_CHAT_ID"))
    if not chat_id:
        chat_id = normalize_optional_secret(notifications.get("telegram_chat_id"))
    if not chat_id:
        chat_id = normalize_optional_secret(api_keys.get("telegram_chat_id"))

    return bot_token, chat_id


def resolve_runtime_trading_pairs(
    api_client: BinanceThClient,
    configured_pairs: Optional[Iterable[str]] = None,
    data_config: Optional[Dict[str, Any]] = None,
    project_root: Optional[Path] = None,
) -> List[str]:
    """Resolve tradable runtime pairs from JSON whitelist plus live exchange readiness."""
    settings = get_hybrid_dynamic_coin_settings(data_config)
    whitelist_path = resolve_whitelist_path(settings.get("whitelist_json_path"), project_root or PROJECT_ROOT)
    resolver = HybridDynamicPairResolver(JsonCoinWhitelistRepository(default_path=whitelist_path))
    selection = resolver.resolve(
        api_client,
        config_path=whitelist_path,
        configured_pairs=configured_pairs,
        min_quote_balance_thb=settings.get("min_quote_balance_thb"),
        min_quote_balance_for_pairs=settings.get("min_quote_balance_for_pairs"),
        require_supported_market=settings.get("require_supported_market"),
        include_assets_with_balance=settings.get("include_assets_with_balance"),
    )
    for warning in selection.warnings:
        logger.warning("Hybrid dynamic coin config: %s", warning)

    pairs = list(selection.pairs)
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
            if not asset_key or asset_key in {"THB", "USDT"}:
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

    supported_pairs = set()
    for row in supported_rows if isinstance(supported_rows, list) else []:
        symbol = str((row or {}).get("symbol", "")).upper().strip()
        if not symbol:
            continue
        normalized = normalize_cli_pair(symbol)
        if normalized:
            supported_pairs.add(normalized)

    filtered_pairs = [
        pair
        for pair in pairs
        if extract_asset_from_pair(pair).upper() in held_assets and normalize_cli_pair(pair) in supported_pairs
    ]

    return filtered_pairs


_REQUIRED_DICT_KEYS = ("trading", "risk", "strategies", "execution", "state_management")


def _validate_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Warn if required dict-type config keys are missing or have wrong type after merge."""
    for key in _REQUIRED_DICT_KEYS:
        val = cfg.get(key)
        if val is None:
            logger.warning("Config missing required section '%s' — using empty dict; defaults may apply", key)
            cfg[key] = {}
        elif not isinstance(val, dict):
            logger.error(
                "Config section '%s' has unexpected type %s (expected dict) — resetting to empty dict",
                key,
                type(val).__name__,
            )
            cfg[key] = {}
    return cfg


def load_bot_config(config_path: str | PathLike[str] | None = None) -> Dict[str, Any]:
    """Load bot configuration from YAML or JSON file."""
    resolved_config_path = Path(config_path) if config_path is not None else PROJECT_ROOT / "bot_config.yaml"

    if not resolved_config_path.exists():
        logger.warning(f"Config file not found: {resolved_config_path}, using defaults")
        return get_default_config()

    try:
        import yaml

        with open(resolved_config_path, "r", encoding="utf-8") as f:
            return _validate_config(apply_strategy_mode_profile(yaml.safe_load(f)))
    except ImportError:
        logger.warning("PyYAML not installed, trying JSON")

    json_path = resolved_config_path.with_suffix(".json")
    if json_path.exists():
        with open(json_path, "r", encoding="utf-8") as f:
            return _validate_config(apply_strategy_mode_profile(json.load(f)))

    logger.error(f"No valid config file found at {resolved_config_path}")
    return get_default_config()


def get_default_config() -> Dict[str, Any]:
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
            "allow_trailing_stop": False,
        },
        "state_management": {
            "enabled": True,
            "entry_confidence_threshold": 0.35,
            "confirmations_required": 2,
            "confirmation_window_seconds": 180,
            "pending_buy_timeout_seconds": 120,
            "pending_sell_timeout_seconds": 120,
            "allow_trailing_stop": False,
        },
        "portfolio": {"initial_balance": 1000.0, "min_balance_threshold": 100.0},
        "balance_monitor": {
            "enabled": True,
            "poll_interval_seconds": 30,
            "persist_path": "balance_monitor_state.json",
            "thb_min_threshold": 0.0,
            "coin_min_threshold": 0.0,
            "coin_min_thresholds": {},
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
        },
    }


# Backward-compatible aliases for tests and dynamic imports
_apply_strategy_mode_profile = apply_strategy_mode_profile
_get_default_config = get_default_config
_get_hybrid_dynamic_coin_settings = get_hybrid_dynamic_coin_settings
_get_candidate_dynamic_pairs = get_candidate_dynamic_pairs
_get_dynamic_whitelist_path = get_dynamic_whitelist_path
_merge_unique_timeframes = merge_unique_timeframes
_resolve_mode_active_strategies = resolve_mode_active_strategies
_risk_manager_from_config = risk_manager_from_config
_normalize_optional_secret = normalize_optional_secret
