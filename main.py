"""
Crypto Trading Bot - Main Entry Point
======================================
Main entry point:
- Load config
- Start collector (background)
- Start trading bot (main loop)
- Graceful shutdown
"""

import atexit
import json
import logging
import os
import re
import shlex
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from os import PathLike
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows fallback
    msvcrt = None  # type: ignore[assignment]

_termios = None
try:
    import termios as _termios  # type: ignore[import-untyped]
except ImportError:
    _termios = None

# Add project root to path
from project_paths import PROJECT_ROOT

sys.path.insert(0, str(PROJECT_ROOT))

from alerts import AlertSystem
from api_client import BinanceAPIError, BinanceThClient
from cli_command_dispatch import CliCommandDispatcher
from cli_ui import CLICommandCenter

# Import project modules
from cli_snapshot_build import build_open_position_rows_for_cli, compute_cli_balance_websocket_health
from cli_snapshot_dto import build_balance_breakdown_lines, quote_cash_totals_strings
from config import BINANCE, TRADING, validate_config
from data_collector import BinanceThCollector, resolve_startup_backfill_timeframes
from dynamic_coin_config import (
    DEFAULT_WHITELIST_JSON,
    HybridDynamicPairResolver,
    JsonCoinWhitelistRepository,
    resolve_whitelist_path,
)
from health_server import BotHealthServer
from helpers import format_exchange_time, get_current_price, now_exchange_time, parse_as_exchange_time
from logger_setup import get_shared_console
from process_guard import acquire_bot_lock, get_lock_status, release_bot_lock
from risk_management import RiskManager
from signal_generator import (
    SignalGenerator,
    ensure_signal_flow_record,
    get_latest_signal_flow_snapshot,
)
from strategies.adaptive_router import AdaptiveStrategyRouter, ModeDecision
from symbol_registry import set_whitelist_json_path
from telegram_bot import TelegramBotHandler
from trading.bootstrap_config import (
    apply_strategy_mode_profile as _apply_strategy_mode_profile,
    get_candidate_dynamic_pairs as _get_candidate_dynamic_pairs,
    get_default_config as _get_default_config,
    get_dynamic_whitelist_path as _get_dynamic_whitelist_path,
    get_hybrid_dynamic_coin_settings as _get_hybrid_dynamic_coin_settings,
    load_bot_config,
    resolve_runtime_trading_pairs,
    resolve_telegram_credentials as _resolve_telegram_credentials,
    risk_manager_from_config as _risk_manager_from_config,
)
from trading.cli_pair_normalize import (
    extract_asset_from_pair as _extract_asset_from_pair,
    normalize_cli_pair as _normalize_cli_pair,
    normalize_pairs as _normalize_pairs,
    sanitize_cli_input_line as _sanitize_cli_input_line,
)
from trading.manual_trading_service import ManualTradingService
from trading.portfolio_runtime import PortfolioRuntimeHelper
from trading.runtime_pairlist_service import RuntimePairlistService
from trading.runtime_process import (
    clear_startup_auth_shutdown_state as _clear_startup_auth_shutdown_state,
    configure_faulthandler_logging as _configure_faulthandler_logging,
    enable_startup_auth_degraded_mode as _enable_startup_auth_degraded_mode,
    setup_logging,
    setup_signal_handlers,
)
from trade_executor import TradeExecutor
from trading_bot import TradingBotOrchestrator
from weekly_review import WeeklyReviewer

logger = logging.getLogger(__name__)


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
        self._config_path = (
            Path(config_path)
            if config_path is not None
            else Path(os.environ.get("BOT_CONFIG_PATH", PROJECT_ROOT / "bot_config.yaml"))
        )
        self.config = _apply_strategy_mode_profile(config or load_bot_config(self._config_path))
        if "read_only" not in self.config and isinstance(self.config.get("portfolio"), dict):
            pr = self.config["portfolio"].get("read_only")
            if pr is not None:
                self.config["read_only"] = bool(pr)
        self.bot: Optional[TradingBotOrchestrator] = None
        self.collector: Optional[BinanceThCollector] = None
        self.api_client: Optional[BinanceThClient] = None
        self.signal_generator: Optional[SignalGenerator] = None
        self.risk_manager: Optional[RiskManager] = None
        self.executor: Optional[TradeExecutor] = None
        self.alert_sender = None
        self.telegram_handler: Optional[TelegramBotHandler] = None
        self.adaptive_router: Optional[AdaptiveStrategyRouter] = None
        self.trading_disabled = threading.Event()  # Kill switch event
        self._shutdown_event = threading.Event()
        self._pair_reload_thread: Optional[threading.Thread] = None
        self._weekly_review_thread: Optional[threading.Thread] = None
        self._pair_reload_lock = threading.Lock()
        self._pair_reload_signature: Optional[tuple[str, bool, int, int]] = None
        self._last_weekly_review_key: Optional[str] = None
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
        self._cli_balance_summary_cache: Optional[Dict[str, Any]] = None
        self._cli_balance_summary_cached_at = 0.0
        self._cli_balance_summary_cache_seconds = 10.0
        self._cli_command_thread: Optional[threading.Thread] = None
        self._cli_chat_lock = threading.Lock()
        self._cli_chat_input = ""
        self._cli_chat_history: List[Dict[str, str]] = []
        self._cli_chat_status = self._get_default_cli_chat_status()
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
        self._last_mode_check_time = 0.0
        self._current_strategy_mode = "standard"
        self._cli_dispatcher = CliCommandDispatcher(self)
        self._pairlist = RuntimePairlistService(self)
        self._manual = ManualTradingService(self)

    def _get_default_cli_chat_status(self) -> str:
        if _termios is not None and self._live_dashboard_active:
            return "Linux tmux: Enter=send | Tab=autocomplete | Backspace=edit | arrows ignored"
        return "Enter=send | Tab=autocomplete | Up/Down=history | Backspace=edit | Esc=clear"

    def _ensure_cli_chat_runtime_state(self) -> None:
        if getattr(self, "_cli_chat_lock", None) is None:
            self._cli_chat_lock = threading.Lock()
        if not hasattr(self, "_cli_chat_input"):
            self._cli_chat_input = ""
        if not hasattr(self, "_cli_chat_history"):
            self._cli_chat_history = []
        if not hasattr(self, "_cli_chat_status"):
            self._cli_chat_status = self._get_default_cli_chat_status()
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
            _, document, configured_assets = self._load_runtime_pairlist_document()
            quote_asset = str(document.get("quote_asset") or "USDT").upper()
            known_pairs.extend(
                f"{asset}{quote_asset}" if quote_asset == "USDT" else f"{quote_asset}_{asset}"
                for asset in configured_assets
            )
        except Exception as exc:
            logger.debug("[CLI] Failed to load runtime pair list document: %s", exc)
        known_pairs.extend(order.get("symbol") for order in self.list_active_orders() if order.get("symbol"))
        known_pairs.extend(
            [
                "BTCUSDT",
                "ETHUSDT",
                "SOLUSDT",
                "XRPUSDT",
                "DOGEUSDT",
                "ADAUSDT",
                "BNBUSDT",
                "DOTUSDT",
                "LINKUSDT",
            ]
        )
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
        default_pair = known_pairs[0] if known_pairs else "BTCUSDT"
        pending = pending_confirmation or getattr(self, "_cli_pending_confirmation", None)

        if pending and not stripped:
            return ["confirm", "cancel"]

        if not stripped:
            suggestions = [
                "help",
                "status",
                "orders",
                "mode show",
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
            command_options = [
                "help",
                "status",
                "orders",
                "mode",
                "risk",
                "ui",
                "buy",
                "track",
                "sell",
                "close",
                "pairs",
            ]
            if pending:
                command_options.extend(["confirm", "cancel"])
            return self._match_cli_suggestions(command_options, stripped)

        if first == "mode":
            return self._match_cli_suggestions(
                [
                    "mode show",
                    "mode set standard",
                    "mode set standard restart",
                    "mode set trend_only",
                    "mode set trend_only restart",
                    "mode set scalping",
                    "mode set scalping restart",
                ],
                stripped,
            )

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
            if len(args) == 2 and str(args[0] or "").lower() == "set":
                return True
            return len(args) == 3 and str(args[0] or "").lower() == "set" and str(args[2] or "").lower() == "restart"
        return False

    def _build_cli_confirmation_request(self, command: str, args: List[str], command_text: str) -> Dict[str, Any]:
        normalized = str(command or "").lower()
        summary = f"Confirm command: {command_text}"
        if normalized == "buy" and len(args) == 2:
            summary = f"Confirm market BUY {_normalize_cli_pair(args[0])} with {float(args[1]):,.2f} quote"
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
        elif normalized == "mode" and len(args) == 2 and str(args[0] or "").lower() == "set":
            summary = f"Confirm strategy mode change to {str(args[1] or '').lower()} (restart required)"
        elif (
            normalized == "mode"
            and len(args) == 3
            and str(args[0] or "").lower() == "set"
            and str(args[2] or "").lower() == "restart"
        ):
            summary = f"Confirm strategy mode change to {str(args[1] or '').lower()} and restart bot"

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
            orders.append(
                {
                    "order_id": str(order.get("order_id") or ""),
                    "symbol": str(order.get("symbol") or "").upper(),
                    "side": side.lower(),
                    "amount": float(order.get("amount") or 0.0),
                    "remaining_amount": remaining_amount,
                    "entry_price": float(order.get("entry_price") or 0.0),
                    "filled": bool(order.get("filled", False)),
                    "timestamp": order.get("timestamp"),
                }
            )
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
        active_mode = str(
            self.config.get("active_strategy_mode") or strategy_mode_cfg.get("active") or "standard"
        ).lower()
        return {
            "status": "ok",
            "active_mode": active_mode,
            "config_path": str(self._config_path),
            "timeframe": str((self.config.get("trading", {}) or {}).get("timeframe") or "-"),
            "enabled_strategies": list((self.config.get("strategies", {}) or {}).get("enabled") or []),
        }

    def set_runtime_strategy_mode(self, mode: str) -> Dict[str, Any]:
        normalized_mode = self._normalize_strategy_mode(mode)
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
                yaml_text = prefix + f'strategy_mode:\n  active: "{normalized_mode}"\n'
            config_path.write_text(yaml_text if yaml_text.endswith("\n") else yaml_text + "\n", encoding="utf-8")

        self.config.setdefault("strategy_mode", {})["active"] = normalized_mode
        self.config["active_strategy_mode"] = normalized_mode
        logger.warning(
            "[CLI] Strategy mode persisted to %s: active=%s (restart required)", config_path, normalized_mode
        )
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
        return self._pairlist.load_runtime_pairlist_document()

    def _write_runtime_pairlist_document(self, path: Path, document: Dict[str, Any], assets: List[str]) -> None:
        self._pairlist.write_runtime_pairlist_document(path, document, assets)

    def add_runtime_pairs(self, pairs: Iterable[str]) -> Dict[str, Any]:
        """Add assets to the persisted pairlist and refresh active runtime pairs."""
        return self._pairlist.add_runtime_pairs(pairs)

    def remove_runtime_pairs(self, pairs: Iterable[str]) -> Dict[str, Any]:
        """Remove assets from the persisted pairlist and refresh active runtime pairs."""
        return self._pairlist.remove_runtime_pairs(pairs)

    def get_runtime_pairlist_status(self) -> Dict[str, Any]:
        return self._pairlist.get_runtime_pairlist_status()

    def submit_manual_market_buy(self, pair: str, thb_amount: float) -> Dict[str, Any]:
        """Submit a market buy in quote currency and track it like a runtime position."""
        return self._manual.submit_manual_market_buy(pair, thb_amount)

    def track_manual_position(self, pair: str, coin_amount: float, entry_price: float) -> Dict[str, Any]:
        """Register a manually held coin with its real average cost for SL/TP management."""
        return self._manual.track_manual_position(pair, coin_amount, entry_price)

    def submit_manual_market_sell(self, target: str, amount: Optional[float] = None) -> Dict[str, Any]:
        """Submit a market sell either by pair+amount or by tracked order id."""
        return self._manual.submit_manual_market_sell(target, amount)

    def _format_cli_command_help(self) -> str:
        return CliCommandDispatcher.format_help()

    def _execute_cli_command(self, command: str, args: List[str]) -> str:
        return self._cli_dispatcher.execute(command, args)


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
                while not self._shutdown_event.is_set():
                    if not msvcrt.kbhit():
                        time.sleep(0.05)
                        continue

                    key = msvcrt.getwch()
                    if key in {"\x00", "\xe0"}:
                        special_key = None
                        try:
                            special_key = msvcrt.getwch()
                        except Exception as exc:
                            logger.debug("[CLI] Failed reading special key: %s", exc)
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
                self._set_cli_chat_status("Linux tmux mode: Enter=send | Backspace=edit | arrow keys ignored")
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
                    raw_command = _sanitize_cli_input_line(line).strip()
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
        if self.bot and hasattr(self.bot, "get_balance_state"):
            return self.bot.get_balance_state()
        return {"updated_at": None, "balances": {}, "api_health": {}, "last_events": []}

    def _derive_cli_mode(self, bot_status: Dict[str, Any]) -> str:
        mode = str(bot_status.get("mode") or self.config.get("mode") or "dry_run").lower()
        if bool(
            (bot_status.get("auth_degraded") or {}).get("active", False) or self.config.get("auth_degraded", False)
        ):
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
        return format_exchange_time(value)

    def _get_quote_asset(self) -> str:
        data_cfg = self.config.get("data", {}) if isinstance(self.config, dict) else {}
        hybrid_cfg = data_cfg.get("hybrid_dynamic_coin_config", {}) if isinstance(data_cfg, dict) else {}
        quote = str(hybrid_cfg.get("quote_asset") or "").strip().upper()
        if not quote:
            cash_assets = (self.config.get("rebalance", {}) or {}).get("cash_assets") or []
            quote = str(cash_assets[0] if cash_assets else "").strip().upper()
        return quote or "USDT"

    @staticmethod
    def _format_cli_usdt_thb_suffix(usdt_amount: float, rate_thb_per_usdt: Any) -> str:
        """Append approximate THB for USDT-held amounts when a spot USDT/THB rate exists."""
        try:
            rate = float(rate_thb_per_usdt or 0.0)
        except (TypeError, ValueError):
            return ""
        if rate <= 0:
            return ""
        try:
            thb = float(usdt_amount) * rate
        except (TypeError, ValueError):
            return ""
        return f"  ≈ {thb:,.2f} THB"

    def _sample_api_latency(self, symbol: str) -> Optional[float]:
        now = time.time()
        if now - self._api_latency_checked_at < self._api_latency_cache_seconds:
            return self._api_latency_ms
        self._api_latency_checked_at = now
        if not self.api_client or not symbol or self.config.get("auth_degraded", False):
            self._api_latency_ms = None
            return None
        # In live dashboard mode, never block the render loop with a REST call.
        # Fire a background thread to measure latency and return the cached value.
        if getattr(self, "_live_dashboard_active", False):
            if not getattr(self, "_api_latency_bg_running", False):
                self._api_latency_bg_running = True

                def _bg_probe(sym: str) -> None:
                    try:
                        started = time.perf_counter()
                        self.api_client.get_ticker(sym)
                        self._api_latency_ms = (time.perf_counter() - started) * 1000.0
                    except Exception:
                        self._api_latency_ms = None
                    finally:
                        self._api_latency_bg_running = False

                threading.Thread(target=_bg_probe, args=(symbol,), daemon=True).start()
            return self._api_latency_ms
        started = time.perf_counter()
        try:
            self.api_client.get_ticker(symbol)
            self._api_latency_ms = (time.perf_counter() - started) * 1000.0
        except Exception:
            self._api_latency_ms = None
        return self._api_latency_ms

    def _get_cli_price(self, symbol: str, allow_rest_fallback: bool = True) -> Optional[float]:
        now = time.time()
        cached = self._cli_price_cache.get(symbol)
        if cached and (now - cached[1]) < self._cli_price_cache_ttl:
            return cached[0]
        ws_client = getattr(self.bot, "_ws_client", None) if self.bot else None
        price, source = get_current_price(
            symbol=symbol,
            api_client=self.api_client if allow_rest_fallback else None,
            ws_client=ws_client,
        )
        if source == "ws_stale" and not allow_rest_fallback:
            # Accept stale WS price for dashboard display — still better than nothing.
            # Cache it so subsequent calls don't re-query within TTL.
            if price is not None:
                self._cli_price_cache[symbol] = (price, now)
                return price
            return cached[0] if cached else None
        if price is None and cached:
            return cached[0]
        if price is not None and source != "ws_stale":
            self._cli_price_cache[symbol] = (price, now)
        return price

    def _get_cli_position_price_hint(self, symbol: str, include_entry_price: bool = True) -> Optional[float]:
        executor = getattr(self, "executor", None)
        if not symbol or not executor:
            return None

        try:
            open_orders = executor.get_open_orders() or []
        except Exception:
            return None

        hint_keys = (
            ("current_price", "filled_price", "entry_price")
            if include_entry_price
            else ("current_price", "filled_price")
        )

        for position in open_orders:
            if str(position.get("symbol") or "").upper() != str(symbol).upper():
                continue
            for key in hint_keys:
                try:
                    price = float(position.get(key) or 0.0)
                except (TypeError, ValueError):
                    price = 0.0
                if price > 0:
                    return price
        return None

    def _resolve_cli_asset_quote_rate(
        self,
        asset: str,
        quote_asset: str,
        live_dashboard_active: bool,
    ) -> Optional[float]:
        from trading import cli_snapshot_builder as csb

        return csb._resolve_cli_asset_quote_rate(self, asset, quote_asset, live_dashboard_active)

    def _build_cli_signal_alignment(
        self,
        trading_pairs: List[str],
        multi_timeframe_status: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        from trading import cli_snapshot_builder as csb

        return csb._build_cli_signal_alignment(self, trading_pairs, multi_timeframe_status)

    @staticmethod
    def _summarize_cli_candle_readiness(multi_timeframe_status: Optional[Dict[str, Any]]) -> str:
        from trading import cli_snapshot_builder as csb

        return csb._summarize_cli_candle_readiness(multi_timeframe_status)

    @staticmethod
    def _summarize_cli_candle_waiting(
        multi_timeframe_status: Optional[Dict[str, Any]], limit: int = 3
    ) -> str:
        from trading import cli_snapshot_builder as csb

        return csb._summarize_cli_candle_waiting(multi_timeframe_status, limit)

    @staticmethod
    def _build_pair_runtime_context(
        multi_timeframe_status: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Dict[str, str]]:
        from trading import cli_snapshot_builder as csb

        return csb._build_pair_runtime_context(multi_timeframe_status)

    @staticmethod
    def _format_cli_recent_events(bot_status: Dict[str, Any], limit: int = 3) -> List[Dict[str, str]]:
        from trading import cli_snapshot_builder as csb

        return csb._format_cli_recent_events(bot_status, limit)

    def _get_cli_balance_summary(self, portfolio_state: Dict[str, Any]) -> Dict[str, Any]:
        from trading import cli_snapshot_builder as csb

        return csb._get_cli_balance_summary(self, portfolio_state)

    def get_cli_snapshot(self, bot_name: Optional[str] = None) -> Dict[str, Any]:
        """Build a lightweight runtime snapshot for the Rich command center."""
        from trading.cli_snapshot_builder import get_cli_snapshot as _build_cli_snapshot
        return _build_cli_snapshot(self, bot_name)

    def _check_adaptive_mode_switch(self) -> None:
        """Check if adaptive router recommends a mode switch and apply if needed."""
        if not self.adaptive_router or not self.adaptive_router.enabled:
            return

        try:
            # Get current trading pair for analysis
            trading_pairs = list(self.config.get("data", {}).get("pairs") or [])
            if not trading_pairs:
                return

            # Use first pair for analysis (could be extended to multi-pair analysis)
            symbol = trading_pairs[0]

            # Get latest price data from collector if available
            data = None
            if self.collector and hasattr(self.collector, "get_pair_candles"):
                try:
                    data = self.collector.get_pair_candles(symbol, "15m", limit=200)
                except Exception:
                    data = None

            # Run adaptive mode switching
            decision: ModeDecision = self.adaptive_router.auto_switch_mode(symbol, data)

            if decision.should_switch:
                new_mode = decision.recommended_mode
                logger.warning(
                    f"[AdaptiveRouter] MODE SWITCH TRIGGERED: {self._current_strategy_mode} → {new_mode} | "
                    f"{decision.reasoning} | Confidence: {decision.confidence:.2f}"
                )

                # Apply the new strategy mode
                self._apply_new_strategy_mode(new_mode)
            else:
                logger.debug(f"[AdaptiveRouter] {decision.reasoning}")

        except Exception as e:
            logger.warning(f"[AdaptiveRouter] Mode switch check failed: {e}", exc_info=True)

    def _apply_new_strategy_mode(self, mode: str) -> None:
        """Apply a new strategy mode by updating config and reloading strategy engine."""
        mode = str(mode or "standard").lower()

        if mode == self._current_strategy_mode:
            logger.debug(f"[AdaptiveRouter] Already in mode {mode}, skipping")
            return

        try:
            # Update config with new mode profile
            strategy_mode_cfg = self.config.setdefault("strategy_mode", {})
            strategy_mode_cfg["active"] = mode
            self.config["active_strategy_mode"] = mode

            # Re-apply strategy mode profile
            updated_config = _apply_strategy_mode_profile(self.config)
            self.config = updated_config
            self._current_strategy_mode = mode

            self.risk_manager = _risk_manager_from_config(self.config)
            if self.executor:
                self.executor.risk_manager = self.risk_manager

            # Restart signal generator with new strategy config
            if self.signal_generator:
                strategies_config = self.config.get("strategies", {})
                self.signal_generator = SignalGenerator(
                    {
                        "min_confidence": strategies_config.get("min_confidence", 0.5),
                        "min_strategies_agree": strategies_config.get("min_strategies_agree", 2),
                        "max_open_positions": self.config.get("risk", {}).get("max_open_positions", 3),
                        "max_daily_trades": self.config.get("risk", {}).get("max_daily_trades", 10),
                        "strategies": {
                            "enabled": list(strategies_config.get("enabled") or []),
                        },
                        "mode_indicator_profiles": dict(self.config.get("mode_indicator_profiles", {}) or {}),
                        "scalping": strategies_config.get("scalping", {}),
                        "sniper": strategies_config.get("sniper", {}) or strategies_config.get("scalping", {}),
                        "machete_v8b_lite": strategies_config.get("machete_v8b_lite", {}),
                        "simple_scalp_plus": strategies_config.get("simple_scalp_plus", {}),
                        "trend_following": strategies_config.get("trend_following", {}),
                        "mean_reversion": strategies_config.get("mean_reversion", {}),
                        "breakout": strategies_config.get("breakout", {}),
                    }
                )
                if self.signal_generator.set_database:
                    from database import get_database

                    self.signal_generator.set_database(get_database())

            if self.bot and self.signal_generator:
                self.bot.apply_runtime_strategy_refresh(
                    self.config,
                    self.signal_generator,
                    risk_manager=self.risk_manager,
                )

            # Update trade executor config if needed
            if self.executor:
                risk_cfg = self.config.get("risk", {})
                self.executor.retry_delay = risk_cfg.get("retry_delay_seconds", 5)
                self.executor.order_timeout = risk_cfg.get("order_timeout_seconds", 30)

            logger.info(f"[AdaptiveRouter] Strategy mode switched to: {mode}")
            logger.info(f"[AdaptiveRouter] Configuration updated with new strategy profile")

        except Exception as e:
            logger.error(f"[AdaptiveRouter] Failed to apply new strategy mode {mode}: {e}", exc_info=True)

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
            except BinanceAPIError as exc:
                logger.warning("Runtime pair refresh skipped due to exchange API error: %s", exc)
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

    def _run_weekly_review_once(self) -> None:
        if not self.alert_system:
            logger.debug("[WeeklyReview] Alert system unavailable, skipping run")
            return
        try:
            from database import get_database

            week_end = datetime.now(timezone.utc)
            week_start = week_end - timedelta(days=7)
            reviewer = WeeklyReviewer(
                db=get_database(),
                config=self.config,
                alert_system=self.alert_system,
            )
            stats = reviewer.run_review(week_start=week_start, week_end=week_end)
            logger.info(
                "[WeeklyReview] Completed weekly review | grade=%s return=%+.2f%% trades=%d",
                stats.grade,
                stats.week_return_pct,
                stats.total_trades,
            )
        except Exception as exc:
            logger.error("[WeeklyReview] Scheduled review failed: %s", exc, exc_info=True)

    def _start_weekly_review_scheduler(self) -> None:
        review_cfg = dict(self.config.get("weekly_review", {}) or {})
        if not bool(review_cfg.get("enabled", False)):
            return
        if self._weekly_review_thread and self._weekly_review_thread.is_alive():
            return

        day_of_week = int(review_cfg.get("day_of_week", 6))
        hour_utc = int(review_cfg.get("hour_utc", 17))
        interval = max(30.0, float(review_cfg.get("scheduler_poll_seconds", 60) or 60))

        def _watch_loop() -> None:
            while not self._shutdown_event.wait(interval):
                now_utc = datetime.now(timezone.utc)
                if now_utc.weekday() != day_of_week or now_utc.hour != hour_utc:
                    continue
                iso_year, iso_week, _ = now_utc.isocalendar()
                run_key = f"{iso_year}-W{iso_week:02d}"
                if run_key == self._last_weekly_review_key:
                    continue
                self._run_weekly_review_once()
                self._last_weekly_review_key = run_key

        self._weekly_review_thread = threading.Thread(
            target=_watch_loop,
            daemon=True,
            name="WeeklyReviewScheduler",
        )
        self._weekly_review_thread.start()
        logger.info(
            "Weekly review scheduler enabled | day_of_week=%d hour_utc=%d interval=%ss",
            day_of_week,
            hour_utc,
            int(interval),
        )

    def get_health_status(self) -> Dict[str, Any]:
        collector_running = bool(self.collector and getattr(self.collector, "running", False))
        bot_running = bool(self.bot and getattr(self.bot, "running", False))
        initialized = all(
            [
                self.api_client is not None,
                self.collector is not None,
                self.bot is not None,
                self.executor is not None,
                self.signal_generator is not None,
                self.risk_manager is not None,
            ]
        )

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

        # Default loopback only — binding 0.0.0.0 exposes /health to the entire
        # internet on VPS unless a host firewall restricts the port. Use
        # monitoring.health_check_host: "0.0.0.0" only behind a trusted LB/proxy.
        health_host = str(monitoring_config.get("health_check_host") or "").strip() or "127.0.0.1"
        if health_host == "0.0.0.0":
            logger.warning(
                "monitoring.health_check_host is 0.0.0.0 — /health is reachable on all "
                "interfaces; prefer 127.0.0.1 with SSH tunnel, or restrict the port with UFW"
            )

        self.health_server = BotHealthServer(
            host=health_host,
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
        logger.info("Initializing Crypto Trading Bot...")
        logger.info(f"Trading mode: {self.config.get('trading', {}).get('mode', 'semi_auto')}")

        # Extract current strategy mode from config
        self._current_strategy_mode = str(self.config.get("active_strategy_mode") or "standard").lower()
        logger.info(f"Initial strategy mode: {self._current_strategy_mode}")

        # Validate configuration before starting
        critical_errors, warnings = validate_config()
        for err in warnings:
            logger.warning(f"Config warning: {err}")
        if critical_errors:
            for err in critical_errors:
                logger.error(f"Config error: {err}")
            logger.error("Critical configuration errors — refusing to start")
            return False
        logger.info("ตรวจสอบ Config ผ่านเรียบร้อย")

        set_whitelist_json_path(
            _get_hybrid_dynamic_coin_settings(self.config.get("data") or {}).get("whitelist_json_path")
        )

        # 1. Initialize API Client
        self.api_client = BinanceThClient(
            api_key=BINANCE.api_key, api_secret=BINANCE.api_secret, base_url=BINANCE.base_url
        )
        logger.info("API Client initialized")

        # AUDIT FIX: explicit exchange-connection health check using the public
        # /api/v1/time endpoint (no signing → does not depend on key validity).
        # If the host is unreachable we fail fast instead of silently sliding
        # into "degraded mode" on the first signed call.
        try:
            import requests as _hc_requests  # lazy import to avoid top-level coupling

            hc_url = f"{BINANCE.base_url.rstrip('/')}/api/v1/time"
            hc_resp = _hc_requests.get(hc_url, timeout=5)
            hc_resp.raise_for_status()
            hc_payload = hc_resp.json()
            server_ms = int(hc_payload.get("serverTime", 0)) if isinstance(hc_payload, dict) else int(hc_payload)
            local_ms = int(time.time() * 1000)
            skew_ms = abs(server_ms - local_ms)
            if server_ms <= 0:
                raise RuntimeError(f"Unexpected serverTime payload from {hc_url}: {hc_payload!r}")
            logger.info(
                "Exchange health check OK: %s reachable, clock skew %dms",
                BINANCE.base_url,
                skew_ms,
            )
            if skew_ms > 5000:
                logger.warning(
                    "Clock skew %dms exceeds Binance recvWindow safety margin — signed requests may fail until system clock is synced",
                    skew_ms,
                )
        except Exception as exc:
            logger.error(
                "Exchange health check FAILED against %s (%s). Refusing to start: live trading without a confirmed exchange connection is unsafe.",
                BINANCE.base_url,
                exc,
            )
            return False

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
            except BinanceAPIError as exc:
                if exc.code != 5:
                    raise

                reason = (
                    f"Exchange private API blocked during startup: {exc.message}; "
                    "running in degraded public-only mode"
                )
                _clear_startup_auth_shutdown_state(self.api_client)
                resolved_pairs = _enable_startup_auth_degraded_mode(self.config, reason, candidate_pairs)
                self.trading_disabled.set()
                auth_degraded = True
                logger.warning(
                    "Exchange auth error during startup — continuing in degraded mode: %s",
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
                logger.warning(
                    "Degraded startup mode active with no configured pairs — bot will run collector/monitor only"
                )
            elif resolved_pairs:
                logger.info(f"Binance TH held pairs to track: {' add '.join(resolved_pairs)}")
            else:
                logger.warning(
                    "Binance TH found no held assets in tradable pairs — bot will start with no active pairs"
                )
        else:
            resolved_pairs = _normalize_pairs(configured_pairs)
            data_config["pairs"] = resolved_pairs
            top_level_pair = (
                resolved_pairs[0]
                if resolved_pairs
                else str(
                    self.config.get("trading", {}).get("trading_pair") or self.config.get("trading_pair") or ""
                ).upper()
            )
            self.config["trading_pair"] = top_level_pair
            self.config.setdefault("trading", {})["trading_pair"] = top_level_pair
            if resolved_pairs:
                logger.info(f"Trading pairs from config: {' add '.join(resolved_pairs)}")
            else:
                logger.warning("Config ไม่มีคู่เหรียญที่ใช้งานอยู่ — bot จะเริ่มแบบไม่มีคู่เหรียญ active")

        pairs_for_runtime = list(data_config.get("pairs") or [])
        for warn in self.api_client.validate_symbol_exchange_info(pairs_for_runtime):
            logger.warning("Exchange symbol validation: %s", warn)

        pair_filters_cfg = dict(data_config.get("pair_filters") or {})
        try:
            min_quote_vol = float(pair_filters_cfg.get("min_quote_volume_24h", 0) or 0)
        except (TypeError, ValueError):
            min_quote_vol = 0.0
        if min_quote_vol > 0 and pairs_for_runtime:
            from trading.pair_filters import filter_pairs_by_min_quote_volume

            filtered_pairs, vol_warnings = filter_pairs_by_min_quote_volume(
                self.api_client, pairs_for_runtime, min_quote_vol
            )
            for vw in vol_warnings:
                logger.warning("%s", vw)
            if filtered_pairs != pairs_for_runtime:
                data_config["pairs"] = filtered_pairs
                if filtered_pairs:
                    self.config["trading_pair"] = filtered_pairs[0]
                    self.config.setdefault("trading", {})["trading_pair"] = filtered_pairs[0]
                    logger.info(
                        "Pair volume filter (min_quote_volume_24h=%.2f): %s",
                        min_quote_vol,
                        " add ".join(filtered_pairs),
                    )
                else:
                    self.config["trading_pair"] = ""
                    self.config.setdefault("trading", {})["trading_pair"] = ""
                    logger.warning(
                        "Pair volume filter removed all pairs (min_quote_volume_24h=%.2f)",
                        min_quote_vol,
                    )

        # 2. Initialize Risk Manager
        self.risk_manager = _risk_manager_from_config(self.config)
        logger.info("Risk Manager initialized")

        # 3. Initialize Signal Generator
        strategies_config = self.config.get("strategies", {})
        risk_section = dict(self.config.get("risk", {}) or {})
        self.signal_generator = SignalGenerator(
            {
                "min_confidence": strategies_config.get("min_confidence", 0.5),
                "min_strategies_agree": strategies_config.get("min_strategies_agree", 2),
                "max_open_positions": risk_section.get("max_open_positions", 3),
                "max_daily_trades": risk_section.get("max_daily_trades", 10),
                "strategies": {
                    "enabled": list(strategies_config.get("enabled") or []),
                },
                "mode_indicator_profiles": dict(self.config.get("mode_indicator_profiles", {}) or {}),
                "scalping": strategies_config.get("scalping", {}),
                "sniper": strategies_config.get("sniper", {}) or strategies_config.get("scalping", {}),
                "machete_v8b_lite": strategies_config.get("machete_v8b_lite", {}),
                "simple_scalp_plus": strategies_config.get("simple_scalp_plus", {}),
                "trend_following": strategies_config.get("trend_following", {}),
                "mean_reversion": strategies_config.get("mean_reversion", {}),
                "breakout": strategies_config.get("breakout", {}),
            }
        )
        logger.info("Signal Generator initialized")

        # 4. Initialize Trade Executor (with DB persistence)
        from database import get_database

        db = get_database()

        execution_config = self.config.get("execution", {})
        state_config = self.config.get("state_management", {})
        telegram_enabled = os.environ.get("TELEGRAM_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
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
                "trailing_stop_pct": execution_config.get("trailing_stop_pct", 1.0),
                "trailing_activation_pct": execution_config.get("trailing_activation_pct", 0.5),
                "allow_trailing_stop": state_config.get(
                    "allow_trailing_stop",
                    execution_config.get("allow_trailing_stop", True),
                ),
            },
            risk_manager=self.risk_manager,
            db=db,
            notifier=self.alert_system.telegram,
        )
        logger.info("Trade Executor initialized")
        if telegram_enabled and self.alert_system.telegram.enabled:
            logger.info("Telegram notifications enabled (with rate limiting)")
        else:
            logger.info("Telegram notifications disabled (using console log)")

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
        logger.info("Trading Bot Orchestrator ready")

        # 6b. Initialize Telegram Bot Handler
        telegram_pairs = list(self.config.get("data", {}).get("pairs") or [])
        if not telegram_enabled:
            logger.info("Telegram disabled via TELEGRAM_ENABLED=false")
        elif not telegram_command_polling_enabled:
            logger.info("Telegram command polling disabled via notifications.telegram_command_polling_enabled=false")
        elif not bot_token:
            logger.info("Telegram Bot Token not set — Telegram commands disabled")
        elif not chat_id:
            logger.info("Telegram Chat ID not set — Telegram commands disabled")
        else:
            self.telegram_handler = TelegramBotHandler(
                app_ref=self,
                bot_token=bot_token,
                chat_id=chat_id,
                pairs=telegram_pairs,
                trading_disabled=self.trading_disabled,
            )
            self.telegram_handler.start()
            logger.info("Telegram bot handler started")

        # 7. Initialize Data Collector (background)
        data_config = self.config.get("data", {})
        pairs = list(data_config.get("pairs") or [])
        interval = data_config.get("collect_interval_seconds", 60)

        self.collector = BinanceThCollector(
            pairs=pairs,
            interval=interval,
            multi_timeframe_config=self.config.get("multi_timeframe", {}),
        )
        if self.bot is not None:
            self.bot.collector = self.collector
        logger.info("Data Collector initialized")

        # 8. Initialize Adaptive Strategy Router (auto mode switching)
        self.adaptive_router = AdaptiveStrategyRouter(
            config=self.config,
            db=db,
            api_client=self.api_client,
        )
        self.adaptive_router.set_current_mode(self._current_strategy_mode)
        if self.adaptive_router.enabled:
            logger.info("Adaptive Strategy Router initialized (auto mode switching enabled)")
        else:
            logger.info("Adaptive Strategy Router initialized (auto mode switching disabled in config)")

        logger.info("All components initialized successfully")
        return True

    def _resolve_active_strategies(self) -> list[str]:
        """Return the strategies that the active mode profile will actually use."""
        mode = str(getattr(self, "_current_strategy_mode", "standard") or "standard").lower()
        profiles = self.config.get("mode_indicator_profiles") or {}
        profile = profiles.get(mode) or {}
        active = profile.get("active_strategies")
        if active:
            return [str(s) for s in active]
        enabled = (self.config.get("strategies") or {}).get("enabled") or []
        return [str(s) for s in enabled]

    def start(self, register_signal_handlers: bool = True):
        """Start all components with a clean, phase-based CLI readout."""
        from cli_layout import StartupReporter, SuppressRepeatStateFilter

        self._shutdown_event.clear()
        self._app_started_at = time.time()
        collector = self.collector
        bot = self.bot
        if collector is None or bot is None:
            raise RuntimeError("TradingBotApp.start() called before initialize() completed")

        reporter = StartupReporter(logger=logger)

        # Suppress every-iteration state-based logs on the console handlers.
        # File handlers (StructuredFormatter / JSON) are left untouched.
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                if not any(isinstance(f, SuppressRepeatStateFilter) for f in handler.filters):
                    handler.addFilter(SuppressRepeatStateFilter(ttl_seconds=300.0))

        reporter.banner("CRYPTO TRADING BOT  —  Binance.th", version="2026.04.27")

        with reporter.phase("Initialization"):
            mode = str(self.config.get("trading", {}).get("mode", "semi_auto")).lower()
            pairs = list(self.config.get("data", {}).get("pairs") or [])
            interval = int(self.config.get("data", {}).get("collect_interval_seconds", 60))
            reporter.step("config", "bot_config.yaml validated")
            reporter.step("exchange", "api.binance.th reachable", detail="health-check OK")
            reporter.step("mode", mode, detail=f"poll {interval}s")
            reporter.step("pairs", f"{len(pairs)} active", detail=", ".join(pairs[:6]) or "none")
            collector.start()
            reporter.step("collector", "started", detail="background thread")

        with reporter.phase("Backfill"):
            mtf_cfg = self.config.get("multi_timeframe") or {}
            warmup_tfs = resolve_startup_backfill_timeframes(
                mtf_cfg,
                collector_timeframes=list(collector.multi_timeframes),
            )
            reporter.step("timeframes", ", ".join(warmup_tfs))
            t0 = time.monotonic()
            try:
                stats = collector.backfill_all_sync(timeframes=warmup_tfs)
                total_rows = sum(sum(tf_stats.values()) for tf_stats in stats.values()) if stats else 0
                elapsed = time.monotonic() - t0
                reporter.result(f"{total_rows:,} bars added across {len(warmup_tfs)} timeframe(s) in {elapsed:.1f}s")
            except Exception as exc:
                reporter.result(f"backfill failed: {exc}", ok=False)
                logger.error("Pre-loop backfill failed", exc_info=True)

        with reporter.phase("Strategy registration"):
            active = self._resolve_active_strategies()
            descriptions = {
                "sniper": "ADX-gated dual-EMA + MACD",
                "machete_v8b_lite": "multi-indicator confluence (5/7)",
                "simple_scalp_plus": "Hull/EMA/VWAP/RSI scalper",
                "trend_following": "SMA20/50 crossover + ADX>20",
                "mean_reversion": "Bollinger band re-entry",
                "breakout": "Donchian 20-bar breakout",
                "momentum": "RSI threshold crossover",
                "scalping": "fast EMA + RSI scalper",
            }
            for name in active:
                reporter.strategy(name, descriptions.get(name, "—"))
            if not active:
                reporter.warning("no strategies enabled — bot will idle")

        # Start health endpoint before the trading loop so probes stay available
        # even if startup/backfill work takes longer than expected.
        self._start_health_server()
        bot.start()
        self._start_pair_hot_reload()
        self._start_weekly_review_scheduler()

        if register_signal_handlers:
            setup_signal_handlers(bot, collector, self.telegram_handler)

        reporter.running(
            mode=str(self.config.get("trading", {}).get("mode", "semi_auto")).upper(),
            pairs=len(list(self.config.get("data", {}).get("pairs") or [])),
            poll=f"{int(self.config.get('data', {}).get('collect_interval_seconds', 60))}s",
        )
        logger.info("=" * 50)
        self._emit_terminal_status()

    def stop(self):
        """Stop all components gracefully."""
        logger.info("Stopping Crypto Trading Bot...")
        self._shutdown_event.set()

        self._stop_health_server()

        if self._pair_reload_thread and self._pair_reload_thread.is_alive():
            self._pair_reload_thread.join(timeout=5)
            self._pair_reload_thread = None
        if self._weekly_review_thread and self._weekly_review_thread.is_alive():
            self._weekly_review_thread.join(timeout=5)
            self._weekly_review_thread = None

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

        logger.info("All components stopped")

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
                self._set_cli_chat_status(self._get_default_cli_chat_status())
                logger.info("CLI dashboard enabled — starting Rich Live display")
                command_center.start_log_capture()
                with command_center.create_live() as live:
                    self.start(register_signal_handlers=register_signal_handlers)
                    self._start_cli_command_listener()
                    next_refresh_at = 0.0
                    last_render_signature: Optional[str] = None
                    _render_failure_count = 0
                    _last_chat_input = ""
                    _last_snapshot: Optional[Dict[str, Any]] = None
                    while self.bot and self.bot.running:
                        now = time.time()
                        if now >= next_refresh_at:
                            try:
                                snapshot, render_signature = command_center.capture_render_state()
                                _last_snapshot = snapshot
                                if render_signature != last_render_signature:
                                    render_started = time.perf_counter()
                                    live.update(command_center.render(snapshot), refresh=True)
                                    render_elapsed_ms = (time.perf_counter() - render_started) * 1000.0
                                    if render_elapsed_ms >= 1000.0:
                                        logger.warning("[CLI PERF] live.update took %.1fms", render_elapsed_ms)
                                    last_render_signature = render_signature
                                    _last_chat_input = self._cli_chat_input
                                _render_failure_count = 0
                            except Exception as render_exc:
                                _render_failure_count += 1
                                if _render_failure_count <= 3:
                                    logger.warning("CLI render error (%d): %s", _render_failure_count, render_exc)
                                if _render_failure_count >= 10:
                                    logger.error(
                                        "CLI render failed %d times — disabling Live dashboard", _render_failure_count
                                    )
                                    break
                            next_refresh_at = now + command_center.refresh_interval_seconds
                        else:
                            # Fast-path: between full refreshes, re-render only if chat input changed
                            current_input = self._cli_chat_input
                            if current_input != _last_chat_input and _last_snapshot is not None:
                                try:
                                    _last_snapshot["chat"] = self._get_cli_chat_snapshot()
                                    live.update(command_center.render(_last_snapshot), refresh=True)
                                    _last_chat_input = current_input
                                except Exception:
                                    pass
                        # Auto-restart CLI listener if thread died
                        if self._cli_command_thread and not self._cli_command_thread.is_alive():
                            self._cli_command_thread = None
                            self._start_cli_command_listener()
                        # Check for auto mode switch
                        self._check_adaptive_mode_switch()
                        time.sleep(0.15)
                # Fallback: if Live dashboard broke, continue running without it
                if self.bot and self.bot.running and _render_failure_count >= 10:
                    self._live_dashboard_active = False
                    logger.info("Falling back to plain log mode (no Live dashboard)")
                    while self.bot and self.bot.running:
                        now = time.time()
                        if now - self._last_status_log_at >= self._status_interval_seconds:
                            self._emit_terminal_status()
                            self._last_status_log_at = now
                        self._check_adaptive_mode_switch()
                        time.sleep(1)
            else:
                self.start(register_signal_handlers=register_signal_handlers)
                self._start_cli_command_listener()
                while self.bot and self.bot.running:
                    now = time.time()
                    if now - self._last_status_log_at >= self._status_interval_seconds:
                        self._emit_terminal_status()
                        self._last_status_log_at = now
                    self._check_adaptive_mode_switch()
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
    _configure_faulthandler_logging()

    startup_test_mode = os.environ.get("BOT_STARTUP_TEST_MODE", "").strip().lower() in ("1", "true", "yes", "on")
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
            lock_info.get("pid"),
            lock_info.get("started_at"),
        )
        sys.exit(1)

    # Guarantee lock release even on abnormal exits (uncaught exceptions, os._exit)
    atexit.register(release_bot_lock)

    # Proactive IP check — diagnostic visibility for exchange connectivity.
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
