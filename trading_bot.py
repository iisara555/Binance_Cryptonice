"""
Trading Bot Orchestrator
========================
Main orchestrator that:
- Runs main loop every X seconds
- Calls the technical signal generator
- Passes through risk_manager for validation
- Sends Telegram alert before trade (semi-auto)
- Or executes trade automatically (full-auto)

Also includes:
- Dynamic SL/TP based on pair volatility (BTC vs ALT)
- ATR-based stop loss
- MonitoringService for health checks, heartbeats, reconciliation
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from alerts import AlertLevel, AlertSystem, format_fatal_auth_alert  # Unified alert system
from api_client import BinanceAuthException, BinanceThClient
from balance_monitor import BalanceEvent, BalanceMonitor
from database import get_database
from helpers import extract_base_asset, normalize_side_value
from minimal_roi import build_roi_tables
from risk_management import ConfirmationGate, PreTradeGate, RiskManager, SLHoldGuard
from signal_generator import AggregatedSignal, SignalGenerator
from state_facade import TradeStateFacade
from state_management import TradeStateManager, TradeStateSnapshot
from strategy_base import MarketCondition
from trade_executor import ExecutionPlan, OrderResult, OrderSide, TradeExecutor
from trading.execution_runtime import ExecutionRuntimeDeps, ExecutionRuntimeHelper
from trading.managed_lifecycle import ManagedLifecycleHelper

# Import shared enums from modular trading package
from trading.orchestrator import BotMode, SignalSource, TradeDecision
from trading.portfolio_runtime import PortfolioRuntimeHelper
from trading.position_monitor import PositionMonitorHelper
from trading.signal_runtime import ExecutionPlanDeps, MultiTimeframeRuntimeDeps, SignalRuntimeDeps, SignalRuntimeHelper
from trading.candle_retention import (
    build_candle_retention_policy,
    maybe_run_scheduled_candle_retention,
    run_candle_retention_cleanup,
)
from trading.db_maintenance import maybe_run_periodic_db_maintenance
from trading.mtf_readiness import required_candles_for_trading_readiness
from trading.order_history_utils import (
    extract_history_fill_details,
    history_side_value,
    history_status_is_cancelled,
    history_status_is_filled,
    history_status_value,
    order_history_window_limit,
)
from trading.spot_protections import build_pair_loss_streak_guard
from trading.startup_runtime import StartupRuntimeHelper
from trading.status_runtime import StatusRuntimeHelper
from trading.bot_runtime.balance_event_runtime import handle_balance_event
from trading.bot_runtime.candle_readiness_filter_runtime import filter_pairs_by_candle_readiness
from trading.bot_runtime.main_loop_runtime import run_trading_main_loop
from trading.bot_runtime.orchestrator_exit_gates_runtime import (
    coerce_opened_at,
    minimal_roi_exit_signal,
    should_allow_voluntary_exit,
)
from trading.bot_runtime.order_logging_runtime import log_filled_order, lookup_order_history_status
from trading.bot_runtime.orchestrator_runtime_deps import (
    build_execution_plan_deps,
    build_execution_runtime_deps,
    build_multi_timeframe_runtime_deps,
    build_signal_runtime_deps,
)
from trading.bot_runtime.pause_state_runtime import clear_pause_reason, is_paused as orchestrator_is_paused, set_pause_reason
from trading.bot_runtime.pre_trade_gate_runtime import check_pre_trade_gate
from trading.bot_runtime.runtime_pairs_runtime import update_runtime_pairs as sync_runtime_pairs
from trading.bot_runtime.run_iteration_runtime import run_trading_iteration
from trading.bot_runtime.websocket_runtime import ensure_websocket_started, start_or_refresh_websocket
from trading.coercion import coerce_trade_float

# Type-checking imports (Pylance static analysis โ€” not executed at runtime)
if TYPE_CHECKING:
    from monitoring import MonitoringService as _MonitoringServiceType

    _WebSocketClientType = Any
    _PriceTickType = Any

# Runtime monitoring import (graceful fallback if module missing)
try:
    from monitoring import MonitoringService

    _MONITORING_AVAILABLE = True
except ImportError:
    MonitoringService = None
    _MONITORING_AVAILABLE = False

logger = logging.getLogger(__name__)

# Back-compat for tests/tools that imported the private symbol from this module.
_coerce_trade_float = coerce_trade_float

# WebSocket real-time price support โ€” Binance native backend only
_WEBSOCKET_BACKEND = "none"
_WEBSOCKET_CLIENT_INSTALLED = False
try:
    import binance_websocket as _binance_ws_mod
    from binance_websocket import PriceTick, get_latest_ticker, get_websocket, stop_websocket

    _WEBSOCKET_AVAILABLE = True
    _WEBSOCKET_BACKEND = "binance_native"
    _WEBSOCKET_CLIENT_INSTALLED = bool(getattr(_binance_ws_mod, "WEBSOCKET_RUNTIME_OK", False))
except ImportError:
    _WEBSOCKET_AVAILABLE = False
    get_websocket = None
    stop_websocket = None
    PriceTick = None
    get_latest_ticker = None
    logger.warning("No websocket backend available โ€” falling back to REST polling")


class TradingBotOrchestrator:
    """
    Main orchestrator for the crypto trading bot.
    Coordinates signal generation, risk checking, alerts, and execution.

    Runs a pure technical strategy engine.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        api_client: BinanceThClient,
        signal_generator: SignalGenerator,
        risk_manager: RiskManager,
        executor: TradeExecutor,
        alert_sender=None,  # Telegram or console alert function
        alert_system: Optional[AlertSystem] = None,
        trading_disabled_event: Optional[threading.Event] = None,
    ):
        """
        Initialize the Trading Bot Orchestrator.

        Args:
            config: Bot configuration dict from YAML/JSON
            api_client: Binance Thailand API client
            signal_generator: SignalGenerator instance
            risk_manager: RiskManager instance
            executor: TradeExecutor instance
            alert_sender: Function to send alerts (telegram/console)
            alert_system: Shared alert system instance
            trading_disabled_event: threading.Event; if set, all trading is paused
        """
        self.config = config
        self.api_client = api_client
        configured_exchange = str(config.get("exchange") or config.get("exchange_name") or "").strip().lower()
        api_base_url = str(getattr(api_client, "base_url", "") or "").lower()
        self._binance_mode = (
            configured_exchange.startswith("binance") or "binance" in api_base_url or not configured_exchange
        )
        self.signal_generator = signal_generator
        self.risk_manager = risk_manager
        self.executor = executor
        self.alert_system = alert_system or AlertSystem()
        self.alert_sender = alert_sender or self.alert_system.create_trade_sender()
        self._trading_disabled = trading_disabled_event or threading.Event()

        # Bot mode (support nested trading.mode or top-level mode)
        mode_str = (config.get("trading", {}).get("mode") or config.get("mode") or "semi_auto").lower()
        self.mode = (
            BotMode.FULL_AUTO
            if mode_str == "full_auto"
            else (BotMode.DRY_RUN if mode_str == "dry_run" else BotMode.SEMI_AUTO)
        )

        # Trading settings
        trading_cfg = config.get("trading", {})
        self.trading_pair = trading_cfg.get("trading_pair") or config.get("trading_pair") or ""
        self.trading_pairs = self._get_trading_pairs()
        self.interval_seconds = trading_cfg.get("interval_seconds", config.get("interval_seconds", 60))
        self.timeframe = trading_cfg.get("timeframe", config.get("timeframe", "1h"))
        self.read_only = config.get("read_only", False) or os.environ.get("BOT_READ_ONLY", "").lower() in (
            "1",
            "true",
            "yes",
        )
        portfolio_guard_cfg = dict(config.get("data", {}).get("portfolio_guard", {}) or {})
        self._held_coins_only = bool(portfolio_guard_cfg.get("held_coins_only", True))
        self._auth_degraded = bool(config.get("auth_degraded", False))
        self._auth_degraded_reason = str(config.get("auth_degraded_reason") or "")
        self._auth_degraded_logged = False
        candle_retention_config = dict(config.get("candle_retention", {}) or {})
        self._candle_retention_enabled = bool(candle_retention_config.get("enabled", False))
        self._candle_retention_run_on_startup = bool(candle_retention_config.get("run_on_startup", True))
        self._candle_retention_vacuum = bool(candle_retention_config.get("vacuum_after_cleanup", False))
        self._candle_retention_policy = build_candle_retention_policy(candle_retention_config)
        try:
            cleanup_interval_hours = float(candle_retention_config.get("cleanup_interval_hours", 12) or 12)
        except (TypeError, ValueError):
            cleanup_interval_hours = 12.0
        self._candle_retention_interval_seconds = max(int(cleanup_interval_hours * 3600), 3600)
        self._last_candle_retention_cleanup_at = 0.0
        self._last_db_maintenance_at = 0.0
        self._db_maintenance_interval_seconds = 24 * 3600  # once per day
        raw_min_trade_thb = config.get("min_trade_value")
        if raw_min_trade_thb is None:
            raw_min_trade_thb = (config.get("rebalance") or {}).get("min_trade_value")
        if raw_min_trade_thb is None:
            raw_min_trade_thb = 15.0
        try:
            self.min_trade_value_usdt = float(raw_min_trade_thb)
        except (TypeError, ValueError):
            self.min_trade_value_usdt = 15.0

        # Strategy config
        self.strategies_config = config.get("strategies", {})
        self.enabled_strategies = self.strategies_config.get(
            "enabled", ["trend_following", "mean_reversion", "breakout", "scalping"]
        )
        self._active_strategy_mode = str(config.get("active_strategy_mode") or "standard").lower()
        gate_cfg = dict(config.get("pre_trade_gate", {}) or {})
        self._pre_trade_gate_enabled = bool(gate_cfg.get("enabled", True))
        self._pre_trade_gate = PreTradeGate()
        self._pair_loss_guard = build_pair_loss_streak_guard(config)
        self._sl_hold_guard = SLHoldGuard()
        roi_cfg = config.get("minimal_roi")
        if not isinstance(roi_cfg, dict):
            roi_cfg = {
                "enabled": True,
                "scalping": {"0": 0.03, "15": 0.015, "30": 0.008, "60": 0.004},
                "standard": {"0": 0.04, "30": 0.02, "60": 0.01, "120": 0.004},
                "trend_only": {"0": 0.08, "120": 0.03, "360": 0.015, "720": 0.006},
            }
        self._minimal_roi_enabled = bool(roi_cfg.get("enabled", True))
        self._minimal_roi_tables = build_roi_tables(roi_cfg) if self._minimal_roi_enabled else {}
        scalping_cfg = dict(self.strategies_config.get("scalping", {}) or {})
        self._scalping_mode_enabled = self._active_strategy_mode == "scalping"
        self._scalping_position_timeout_minutes = int(scalping_cfg.get("position_timeout_minutes", 30) or 30)
        try:
            bootstrap_timeout_hours = float(scalping_cfg.get("bootstrap_position_timeout_hours", 24) or 24)
        except (TypeError, ValueError):
            bootstrap_timeout_hours = 24.0
        self._bootstrap_position_timeout_minutes = max(int(bootstrap_timeout_hours * 60), 0)
        execution_cfg = dict(config.get("execution", {}) or {})
        self._enforce_min_profit_gate_for_voluntary_exit = bool(
            execution_cfg.get("enforce_min_profit_gate_for_voluntary_exit", True)
        )
        try:
            self._min_voluntary_exit_net_profit_pct = float(
                execution_cfg.get("min_voluntary_exit_net_profit_pct", 0.2) or 0.2
            )
        except (TypeError, ValueError):
            self._min_voluntary_exit_net_profit_pct = 0.2

        # Notification config
        self.notif_config = config.get("notifications", {})
        self.alert_channel = self.notif_config.get("alert_channel", "telegram")
        self.send_alerts = self.notif_config.get("send_alerts", True)

        # Strategy-only runtime: hard-force technical signals after AI purge.
        configured_signal_source = str(config.get("signal_source", "strategy") or "strategy").lower()
        if configured_signal_source != SignalSource.STRATEGY.value:
            logger.warning(
                "signal_source=%s is no longer supported after AI purge; forcing strategy-only mode",
                configured_signal_source,
            )
        self.signal_source = SignalSource.STRATEGY
        self.multi_timeframe_config = dict(config.get("multi_timeframe", {}) or {})
        self.mtf_enabled = bool(self.multi_timeframe_config.get("enabled", False))
        self.mtf_timeframes = [
            str(timeframe).strip()
            for timeframe in (self.multi_timeframe_config.get("timeframes") or ["1m", "5m", "15m", "1h"])
            if str(timeframe).strip()
        ]
        self._mtf_confirmation_required = bool(self.multi_timeframe_config.get("require_htf_confirmation", False))
        self._last_mtf_status: Dict[str, Dict[str, Any]] = {}

        # Validate: higher_timeframes must be a subset of collected timeframes
        if self.mtf_enabled:
            htf_list = [
                str(tf).strip()
                for tf in (self.multi_timeframe_config.get("higher_timeframes") or [])
                if str(tf).strip()
            ]
            missing_htf = [tf for tf in htf_list if tf not in self.mtf_timeframes]
            if missing_htf:
                logger.warning(
                    "โ ๏ธ [MTF Config] higher_timeframes %s are NOT in collected timeframes %s โ€” "
                    "these will never have data. Add them to 'timeframes' or remove from 'higher_timeframes'.",
                    missing_htf,
                    self.mtf_timeframes,
                )

        # Database and state management
        self.db = get_database()
        self.signal_generator.set_database(self.db)
        self._state_manager = TradeStateManager(self.db, config.get("state_management", {}))
        self._state_facade = TradeStateFacade(self._state_manager)
        self._state_machine_enabled = self._state_manager.enabled
        state_cfg = config.get("state_management", {}) or {}
        self._allow_sell_entries_from_idle = bool(state_cfg.get("allow_sell_entries_from_idle", False))
        self.executor._allow_trailing_stop = self._state_manager.allow_trailing_stop

        # Wire trailing stop Telegram notification to executor
        self.executor._on_trailing_stop = self._on_trailing_stop_callback

        self._balance_monitor: Optional[BalanceMonitor] = None
        self._init_balance_monitor(config)

        # State
        self.running = False
        self._loop_thread = None
        self._pending_decisions: List[TradeDecision] = []
        self._pending_decisions_lock = threading.Lock()
        self._executed_today: List[Dict] = []
        self._executed_today_max = 200
        self._last_loop_time: Optional[datetime] = None
        self._loop_count = 0
        self._last_state_gate_logged: Dict[str, str] = {}
        self._last_consumed_signal_triggers: Dict[str, str] = {}

        # === Monitoring Service ===
        self._monitoring: Optional[_MonitoringServiceType] = None
        self._monitoring_start_time = datetime.now()
        self._init_monitoring(config)

        # === Reconciliation pause flag ===
        self._pause_state_lock = threading.Lock()
        self._trading_paused = False
        self._pause_reason = ""
        self._pause_reasons: Dict[str, str] = {}

        # === WebSocket Real-time Prices ===
        self._ws_client: Optional[_WebSocketClientType] = None
        ws_cfg = config.get("websocket", {}) or {}
        self._ws_import_ok: bool = bool(_WEBSOCKET_AVAILABLE and _WEBSOCKET_CLIENT_INSTALLED)
        self._ws_enabled: bool = bool(ws_cfg.get("enabled", True))
        websocket_cfg = dict(ws_cfg)
        self._last_ws_start_attempt_at: float = 0.0
        try:
            self._ws_start_retry_interval_seconds = max(
                5.0,
                float(websocket_cfg.get("startup_retry_interval_seconds", 20.0) or 20.0),
            )
        except (TypeError, ValueError):
            self._ws_start_retry_interval_seconds = 20.0
        if self._ws_enabled and self._binance_mode and _WEBSOCKET_BACKEND != "binance_native":
            logger.warning(
                "Binance mode active but native Binance websocket backend is unavailable. "
                "Runtime price checks will use Binance REST. "
                "Ensure `binance_websocket.py` is on PYTHONPATH and run: pip install websocket-client"
            )
            self._ws_enabled = False
        if self._ws_enabled and not self._ws_import_ok:
            if _WEBSOCKET_AVAILABLE and not _WEBSOCKET_CLIENT_INSTALLED:
                logger.warning(
                    "WebSocket enabled but Python package `websocket-client` is missing for %s. "
                    'Install: "%s" -m pip install websocket-client',
                    sys.executable,
                    sys.executable,
                )
            else:
                logger.warning(
                    "WebSocket enabled in config but no websocket backend could be imported. "
                    "For Binance TH install: pip install websocket-client"
                )
            self._ws_enabled = False
        elif self._ws_enabled and not self.trading_pairs:
            logger.info(
                "WebSocket enabled but there are no trading pairs yet; "
                "streams will start after pairs exist (hybrid resolver / pair refresh)."
            )
        elif not self._ws_enabled:
            logger.info("WebSocket disabled in config or due to exchange/backend compatibility")

        if self._ws_enabled and self._ws_import_ok and self.trading_pairs:
            started_ok = self._start_or_refresh_websocket(self.trading_pairs, reason="startup")
            if not started_ok:
                logger.warning(
                    "WebSocket did not attach a client at startup (empty symbols or backend error); "
                    "main loop will retry if enabled."
                )

        logger.info(
            f"TradingBotOrchestrator initialized | "
            f"Mode: {self.mode.value} | "
            f"Pairs: {self.trading_pairs} | "
            f"Signal Source: {self.signal_source.value} | "
            f"WebSocket: {'enabled' if self._ws_enabled else 'disabled'} ({_WEBSOCKET_BACKEND})"
        )

        # M6 fix: per-position deduplication guard for WS SL/TP exit threads.
        # Prevents unbounded thread spawning during flash crashes where rapid
        # ticks would otherwise create hundreds of threads for the same position.
        self._ws_sltp_inflight: set = set()
        self._ws_sltp_inflight_lock = threading.Lock()
        self._last_candle_backfill_attempt_at = 0.0

        # === Performance caches ===
        # Cache TTLs (seconds)
        self._cache_ttl = {
            "portfolio": 10,  # Portfolio state: 10s
            "market_data": 10,  # Market data from DB: 10s
            "atr": 60,  # ATR calculation: 60s
        }
        self._portfolio_cache = {"data": None, "timestamp": 0.0}
        self._portfolio_cache_lock = threading.Lock()
        self._lightweight_mtm_cache: Optional[Dict[str, Any]] = None
        self._market_data_cache = {"data": None, "timestamp": 0.0}
        self._atr_cache = {"value": None, "timestamp": 0.0}
        self._multi_timeframe_status_cache = {"data": None, "timestamp": 0.0}
        self._symbol_market_cache: Dict[str, Dict] = {}
        self._last_portfolio_guard_skipped: Optional[tuple[str, ...]] = None
        self._last_candle_readiness_skipped: Optional[tuple[str, ...]] = None

        self._prune_invalid_btc_positions()
        # Important startup order:
        # 1) prune DB ghosts
        # 2) reconcile against the active exchange (in start())
        # 3) sync OMS in-memory tracking from DB (after reconciliation)

    def apply_runtime_strategy_refresh(
        self,
        config: Dict[str, Any],
        signal_generator: SignalGenerator,
        *,
        risk_manager: Optional[RiskManager] = None,
    ) -> None:
        """Sync orchestrator with new runtime config after strategy mode switch or hot reload."""
        self.config = config
        self.signal_generator = signal_generator
        if risk_manager is not None:
            self.risk_manager = risk_manager

        mode = str(config.get("active_strategy_mode") or "standard").lower()
        self._active_strategy_mode = mode

        self.strategies_config = dict(config.get("strategies", {}) or {})
        enabled = self.strategies_config.get("enabled", [])
        if isinstance(enabled, list) and enabled:
            self.enabled_strategies = list(enabled)
        self._scalping_mode_enabled = mode == "scalping"

        trading_cfg = dict(config.get("trading", {}) or {})
        self.timeframe = trading_cfg.get("timeframe", self.timeframe)
        self.interval_seconds = trading_cfg.get("interval_seconds", self.interval_seconds)
        self.trading_pair = trading_cfg.get("trading_pair") or config.get("trading_pair") or self.trading_pair
        self.trading_pairs = self._get_trading_pairs()

        mtf = dict(config.get("multi_timeframe", {}) or {})
        self.multi_timeframe_config = mtf
        self.mtf_enabled = bool(mtf.get("enabled", False))
        self.mtf_timeframes = [
            str(timeframe).strip()
            for timeframe in (mtf.get("timeframes") or ["1m", "5m", "15m", "1h"])
            if str(timeframe).strip()
        ]
        self._mtf_confirmation_required = bool(mtf.get("require_htf_confirmation", False))

        self.signal_generator.set_database(self.db)

        self._portfolio_cache = {"data": None, "timestamp": 0.0}
        self._multi_timeframe_status_cache = {"data": None, "timestamp": 0.0}

    def _required_candles_for_trading_readiness(self) -> int:
        """Minimum stored OHLC rows per gated timeframe before a pair is MTF-ready.

        Default matches ``trading.mtf_readiness.MIN_CANDLES_FOR_TRADING_READINESS``.
        Override with ``multi_timeframe.required_candles_for_readiness`` in YAML.
        """
        return required_candles_for_trading_readiness(dict(getattr(self, "multi_timeframe_config", None) or {}))

    def _prune_invalid_btc_positions(self) -> None:
        """Drop BTC rows with impossible base size (any side) and zero-remaining ghosts.

        Invalid amount rows are not present in OMS memory (sync skips them), so pruning
        must hit SQLite directly, then clear zero-remaining rows.
        """
        n_invalid = 0
        n_zero = 0
        try:
            n_invalid = self.db.delete_invalid_btc_amount_positions(1.0)
        except Exception as ex:
            logger.error("[Startup] Failed to prune invalid BTC amount positions: %s", ex)
        try:
            n_zero = self.db.delete_positions_zero_remaining()
        except Exception as ex:
            logger.error("[Startup] Failed to prune closed/ghost positions: %s", ex)
        logger.info(
            "[Startup] Prune complete: removed %d ghost + %d zero-remaining",
            n_invalid,
            n_zero,
        )

    def _init_monitoring(self, config: Dict[str, Any]):
        """Initialize the monitoring service (health checks, heartbeats, reconciliation)."""
        if not _MONITORING_AVAILABLE or not MonitoringService:
            logger.info("MonitoringService not available (monitoring.py not found)")
            return

        monitoring_config = config.get("monitoring", {})
        if not monitoring_config.get("enabled", True):
            logger.info("MonitoringService disabled in config")
            return

        try:
            self._monitoring = MonitoringService(
                bot_ref=self,
                api_client=self.api_client,
                executor=self.executor,
                config=config,
                alert_sender=self.alert_sender,
                start_time=self._monitoring_start_time,
            )
            self._monitoring.start()
            logger.info("MonitoringService started")
        except Exception as e:
            logger.error(f"Failed to initialize MonitoringService: {e}")

    def _is_paused(self) -> tuple:
        """Return (is_paused, reason) checking both reconciliation and manual pause."""
        return orchestrator_is_paused(self)

    def _set_pause_reason(self, key: str, reason: str) -> None:
        set_pause_reason(self, key, reason)

    def _clear_pause_reason(self, key: str) -> None:
        clear_pause_reason(self, key)

    def _invalidate_portfolio_cache(self) -> None:
        _lock = getattr(self, "_portfolio_cache_lock", None)
        if _lock:
            with _lock:
                self._portfolio_cache = {"data": None, "timestamp": 0.0}
        else:
            self._portfolio_cache = {"data": None, "timestamp": 0.0}
        self._lightweight_mtm_cache = None

    def _find_tracked_position_by_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        if not self.executor:
            return None

        target_symbol = str(symbol or "").upper()
        for position in self.executor.get_open_orders() or []:
            if str(position.get("symbol") or "").upper() != target_symbol:
                continue

            side = normalize_side_value(position.get("side"))
            if side == "buy":
                return position

        return None

    # Helper accessors and thin facade wrappers for extracted runtime modules.

    @staticmethod
    def _extract_total_balance(balance_state: Optional[Dict[str, Any]], asset: str) -> float:
        return PortfolioRuntimeHelper.extract_total_balance(balance_state, asset)

    def _get_portfolio_runtime_helper(self) -> PortfolioRuntimeHelper:
        helper = getattr(self, "_portfolio_runtime_helper", None)
        if helper is None:
            helper = PortfolioRuntimeHelper(
                self,
                websocket_available=bool(_WEBSOCKET_AVAILABLE and getattr(self, "_ws_enabled", False)),
                latest_ticker_getter=(
                    get_latest_ticker if bool(_WEBSOCKET_AVAILABLE and getattr(self, "_ws_enabled", False)) else None
                ),
            )
            self._portfolio_runtime_helper = helper
        return helper

    def _get_portfolio_mark_price(self, symbol: str) -> float:
        return self._get_portfolio_runtime_helper().get_portfolio_mark_price(symbol)

    def _estimate_total_portfolio_balance(self, balances: Optional[Dict[str, Any]]) -> float:
        return self._get_portfolio_runtime_helper().estimate_total_portfolio_balance(balances)

    def _get_market_data_for_symbol(self, symbol: str):
        return self._get_portfolio_runtime_helper().get_market_data_for_symbol(symbol)

    def _get_latest_atr(self, symbol: Optional[str] = None, period: int = 14) -> Optional[float]:
        return self._get_portfolio_runtime_helper().get_latest_atr(symbol=symbol, period=period)

    @staticmethod
    def _get_risk_portfolio_value(portfolio_state: Optional[Dict[str, Any]]) -> float:
        return PortfolioRuntimeHelper.get_risk_portfolio_value(portfolio_state)

    def _get_position_bootstrap_helper(self):  # lazily constructs โ€” keeps import graph light
        helper = getattr(self, "_position_bootstrap_helper", None)
        if helper is None:
            from trading.position_bootstrap import PositionBootstrapHelper

            helper = PositionBootstrapHelper(self)
            self._position_bootstrap_helper = helper
        return helper

    def _reconcile_tracked_positions_with_balance_state(
        self,
        balance_state: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Drop filled tracked positions whose base-asset balance is gone on the exchange."""
        return self._get_position_bootstrap_helper().reconcile_tracked_positions_with_balance_state(balance_state)

    def _bootstrap_missing_positions_from_balance_state(
        self,
        balance_state: Optional[Dict[str, Any]] = None,
        target_pairs: Optional[List[str]] = None,
    ) -> List[str]:
        return self._get_position_bootstrap_helper().bootstrap_missing_positions_from_balance_state(
            balance_state,
            target_pairs,
        )

    def _preserve_bootstrap_position_from_balances(
        self,
        order_id: str,
        local_pos: Dict[str, Any],
        balances: Optional[Dict[str, Any]] = None,
    ) -> bool:
        return self._get_position_bootstrap_helper().preserve_bootstrap_position_from_balances(
            order_id,
            local_pos,
            balances,
        )

    def _build_bootstrap_position_sl_tp(self, symbol: str, entry_price: float) -> tuple[Optional[float], Optional[float]]:
        return self._get_position_bootstrap_helper().build_bootstrap_position_sl_tp(symbol, entry_price)

    def _resolve_bootstrap_position_context(self, symbol: str, quantity: float) -> Dict[str, Any]:
        """Recover bootstrap entry context from persisted position/state before estimating from market price."""
        return self._get_position_bootstrap_helper().resolve_bootstrap_position_context(symbol, quantity)

    @staticmethod
    def _bootstrap_quantity_tolerance(quantity: float) -> float:
        from trading.position_bootstrap import bootstrap_quantity_tolerance as _tol

        return _tol(quantity)

    def _build_weighted_inventory_context(
        self,
        events: List[Dict[str, Any]],
        quantity: float,
        *,
        source: str,
    ) -> Dict[str, Any]:
        return self._get_position_bootstrap_helper().build_weighted_inventory_context(
            events,
            quantity,
            source=source,
        )

    def _resolve_bootstrap_exchange_history_context(self, symbol: str, quantity: float) -> Dict[str, Any]:
        return self._get_position_bootstrap_helper().resolve_bootstrap_exchange_history_context(symbol, quantity)

    def _run_candle_retention_cleanup(self, reason: str) -> None:
        run_candle_retention_cleanup(self, reason)

    def _maybe_run_db_maintenance(self) -> None:
        """Periodic DB maintenance: cleanup old data + WAL checkpoint."""
        maybe_run_periodic_db_maintenance(self)

    def _maybe_run_candle_retention_cleanup(self) -> None:
        maybe_run_scheduled_candle_retention(self)

    def _init_balance_monitor(self, config: Dict[str, Any]):
        """Initialize the balance monitor without starting it yet."""
        if self._auth_degraded:
            logger.info("BalanceMonitor skipped in auth degraded mode")
            return

        balance_config = config.get("balance_monitor", {})
        if not balance_config.get("enabled", True):
            logger.info("BalanceMonitor disabled in config")
            return

        try:
            balance_config = dict(balance_config)
            quote_asset = (
                balance_config.get("quote_asset")
                or (config.get("data", {}) or {}).get("hybrid_dynamic_coin_config", {}).get("quote_asset")
                or "USDT"
            )
            balance_config.setdefault("quote_asset", str(quote_asset).upper())
            self._balance_monitor = BalanceMonitor(
                api_client=self.api_client,
                config=balance_config,
                alert_system=self.alert_system,
                on_event=self._handle_balance_event,
            )
            logger.info(
                "BalanceMonitor initialized | interval=%ss",
                self._balance_monitor.poll_interval_seconds,
            )
        except Exception as e:
            logger.error(f"Failed to initialize BalanceMonitor: {e}")
            self._balance_monitor = None

    def _handle_balance_event(self, event: BalanceEvent, balance_state: Dict[str, Any]) -> None:
        """React to deposit and withdrawal events from the balance monitor."""
        handle_balance_event(self, event, balance_state)

    def get_balance_state(self) -> Dict[str, Any]:
        """Expose the latest balance-monitor snapshot to other modules."""
        if self._balance_monitor:
            return self._balance_monitor.get_state()
        if self._auth_degraded:
            return {"updated_at": None, "balances": {}, "api_health": {}, "last_events": []}

        try:
            balances = self.api_client.get_balances()
        except Exception:
            balances = {}

        normalized: Dict[str, Dict[str, float]] = {}
        for symbol, payload in (balances or {}).items():
            if isinstance(payload, dict):
                available = float(payload.get("available", 0.0) or 0.0)
                reserved = float(payload.get("reserved", 0.0) or 0.0)
            else:
                available = float(payload or 0.0)
                reserved = 0.0
            normalized[str(symbol).upper()] = {
                "available": available,
                "reserved": reserved,
                "total": available + reserved,
            }

        return {
            "updated_at": datetime.now().isoformat(),
            "balances": normalized,
            "api_health": {},
            "last_events": [],
        }

    def start(self):
        """Start the trading bot main loop in a background thread."""
        if self.running:
            logger.warning("Bot is already running")
            return

        if self._candle_retention_enabled and self._candle_retention_run_on_startup:
            self._run_candle_retention_cleanup("startup")
        else:
            self._last_candle_retention_cleanup_at = time.time()

        # โ”€โ”€ HOTFIX FATAL-01: Ghost Orders Reconciliation โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€
        # Before starting the main loop, forcefully query the exchange API for
        # real open orders and balances. Use the authoritative remote data to
        # overwrite local SQLite state, preventing ghost orders after crash.
        if self._auth_degraded:
            logger.warning(
                "[Startup] Auth degraded mode active โ€” skipping private exchange startup sync: %s",
                self._auth_degraded_reason or "private API unavailable",
            )
        else:
            self._reconcile_on_startup()

        # Sync OMS state from DB after reconciliation or directly in degraded mode.
        self.executor.sync_open_orders_from_db()
        if bool((self.config.get("trading") or {}).get("startup_order_reconcile", True)) and not self._auth_degraded:
            self._reconcile_open_orders_with_exchange(source="startup")
        if not self._auth_degraded:
            self._bootstrap_held_coin_history()
            self._bootstrap_held_positions()
        if self._state_machine_enabled:
            self._state_manager.sync_in_position_states(self.executor.get_open_orders())

        # โ”€โ”€ H3/H4: Unblock OMS monitor after reconciliation is fully done โ”€โ”€
        # This MUST come after both _reconcile_on_startup() and
        # sync_open_orders_from_db() so the OMS always starts from a
        # Exchange-authoritative, DB-consistent state.
        self.executor.set_reconcile_complete()

        balance_monitor = getattr(self, "_balance_monitor", None)
        if balance_monitor and not balance_monitor.running:
            balance_monitor.start()

        self.running = True
        self._loop_thread = threading.Thread(target=self._main_loop, daemon=True)
        self._loop_thread.start()
        logger.info("Trading bot orchestrator starting")

    def _bootstrap_held_coin_history(self) -> None:
        self._get_startup_runtime_helper().bootstrap_held_coin_history()

    def _bootstrap_held_positions(
        self,
        balances: Optional[Dict[str, Any]] = None,
        target_pairs: Optional[List[str]] = None,
    ) -> List[str]:
        return self._get_startup_runtime_helper().bootstrap_held_positions(
            balances=balances,
            target_pairs=target_pairs,
        )

    def _reconcile_on_startup(self):
        self._get_startup_runtime_helper().reconcile_on_startup()

    def stop(self):
        """Stop the trading bot gracefully."""
        logger.info("Stopping trading bot...")
        self.running = False

        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=30)

        # Stop WebSocket
        if _WEBSOCKET_AVAILABLE and stop_websocket:
            try:
                stop_websocket()
                logger.info("WebSocket stopped")
            except Exception as e:
                logger.warning(f"Error stopping WebSocket: {e}")

        balance_monitor = getattr(self, "_balance_monitor", None)
        if balance_monitor:
            balance_monitor.stop()

        if self.executor:
            try:
                self.executor.stop()
            except Exception as e:
                logger.warning(f"Error stopping OMS executor: {e}")

        logger.info("Trading bot stopped")

    def _start_or_refresh_websocket(self, symbols: List[str], reason: str = "runtime") -> bool:
        return start_or_refresh_websocket(self, symbols, reason)

    def _ensure_websocket_started(self) -> None:
        # Use getattr in ensure_websocket_started so unit tests and partial construction (e.g. __new__
        # without __init__) do not raise AttributeError and mask real control flow in _main_loop.
        ensure_websocket_started(self)

    def _main_loop(self):
        """Main trading loop (interval_seconds between cycles).

        Each cycle: `_run_iteration` (global gates โ’ SL/TP / lifecycle โ’ per symbol
        `SignalRuntimeHelper.process_pair_iteration`) โ’ sleep. OHLCV for strategies is
        read mainly from SQLite (`PortfolioRuntimeHelper.get_market_data_for_symbol`),
        filled by background `BinanceThCollector` from `main.TradingBotApp`.
        Successful plans run through `ExecutionRuntimeHelper.process_full_auto` (or semi/dry).
        """
        run_trading_main_loop(self)

    def _get_trading_pairs(self) -> List[str]:
        """Get list of trading pairs from config.

        Returns pairs from:
        1. data.pairs (for data collection) - used for multi-pair trading
        2. Falls back to trading_pair if data.pairs is not set
        """
        data_config = self.config.get("data", {})
        if "pairs" in data_config:
            return [str(pair).upper() for pair in (data_config.get("pairs") or []) if str(pair).strip()]
        if self.trading_pair:
            return [str(self.trading_pair).upper()]
        return []

    def update_runtime_pairs(self, pairs: List[str], reason: str = "runtime refresh") -> List[str]:
        """Safely apply a new runtime pair set and refresh market-data subscriptions."""
        return sync_runtime_pairs(self, pairs, reason)

    def _filter_pairs_by_candle_readiness(self, pairs: List[str], allow_refresh: bool = True) -> List[str]:
        return filter_pairs_by_candle_readiness(self, pairs, allow_refresh)

    def _order_history_window_limit(self) -> int:
        return order_history_window_limit(getattr(self, "config", {}) or {})

    def _lookup_order_history_status(self, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        """Fallback history lookup for an order when the info endpoint is inconclusive."""
        return lookup_order_history_status(self, symbol, order_id)

    def _extract_history_fill_details(
        self,
        row: Optional[Dict[str, Any]],
        *,
        fallback_amount: float = 0.0,
        fallback_price: float = 0.0,
        fallback_cost: float = 0.0,
    ) -> tuple[float, float]:
        return extract_history_fill_details(
            row,
            fallback_amount=fallback_amount,
            fallback_price=fallback_price,
            fallback_cost=fallback_cost,
        )

    # Back-compat: tests/monkeypatch still reference TradingBotOrchestrator._history_* class attributes.
    _history_status_value = staticmethod(history_status_value)
    _history_side_value = staticmethod(history_side_value)
    _history_status_is_filled = staticmethod(history_status_is_filled)
    _history_status_is_cancelled = staticmethod(history_status_is_cancelled)

    def _log_filled_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        *,
        fee: float = 0.0,
        timestamp: Optional[datetime] = None,
        order_type: str = "limit",
    ) -> None:
        log_filled_order(
            self, symbol, side, quantity, price, fee=fee, timestamp=timestamp, order_type=order_type
        )

    # Startup and managed lifecycle facade wrappers.

    def _reconcile_pending_trade_states(self, remote_order_ids: set[str]) -> set[str]:
        return self._get_startup_runtime_helper().reconcile_pending_trade_states(remote_order_ids)

    def _reconcile_open_orders_with_exchange(self, source: str = "runtime") -> int:
        return self._get_startup_runtime_helper().reconcile_open_orders_with_exchange(source=source)

    def _get_startup_runtime_helper(self) -> StartupRuntimeHelper:
        helper = getattr(self, "_startup_runtime_helper", None)
        if helper is None:
            helper = StartupRuntimeHelper(self)
            self._startup_runtime_helper = helper
        return helper

    def _resolve_fill_amount(
        self,
        snapshot: TradeStateSnapshot,
        result: OrderResult,
        fallback_price: float,
    ) -> tuple[float, float]:
        return ManagedLifecycleHelper.resolve_fill_amount(snapshot, result, fallback_price)

    def _register_filled_position_from_state(
        self,
        snapshot: TradeStateSnapshot,
        filled_amount: float,
        filled_price: float,
    ) -> None:
        self._get_managed_lifecycle_helper().register_filled_position_from_state(snapshot, filled_amount, filled_price)

    def _report_completed_exit(
        self,
        snapshot: TradeStateSnapshot,
        exit_price: float,
        price_source: str,
    ) -> None:
        self._get_managed_lifecycle_helper().report_completed_exit(snapshot, exit_price, price_source)

    def _submit_managed_entry(self, decision: TradeDecision, portfolio: Dict[str, Any]) -> None:
        self._get_managed_lifecycle_helper().submit_managed_entry(decision, portfolio)

    @staticmethod
    def _signal_trigger_cache_key(symbol: str, signal_type: str) -> str:
        return ManagedLifecycleHelper.signal_trigger_cache_key(symbol, signal_type)

    def _get_signal_trigger_token(self, signal: Optional[AggregatedSignal]) -> str:
        return ManagedLifecycleHelper.get_signal_trigger_token(signal)

    def _is_reused_signal_trigger(self, signal: Optional[AggregatedSignal]) -> bool:
        return self._get_managed_lifecycle_helper().is_reused_signal_trigger(signal)

    def _remember_consumed_signal_trigger(self, signal: Optional[AggregatedSignal]) -> None:
        self._get_managed_lifecycle_helper().remember_consumed_signal_trigger(signal)

    def _get_managed_lifecycle_helper(self) -> ManagedLifecycleHelper:
        helper = getattr(self, "_managed_lifecycle_helper", None)
        if helper is None:
            helper = ManagedLifecycleHelper(self)
            self._managed_lifecycle_helper = helper
        return helper

    def _get_position_monitor_helper(self) -> PositionMonitorHelper:
        helper = getattr(self, "_position_monitor_helper", None)
        if helper is None:
            helper = PositionMonitorHelper(
                self,
                websocket_available=bool(_WEBSOCKET_AVAILABLE and getattr(self, "_ws_enabled", False)),
                price_tick_available=bool(PriceTick is not None and getattr(self, "_ws_enabled", False)),
                latest_ticker_getter=(
                    get_latest_ticker if bool(_WEBSOCKET_AVAILABLE and getattr(self, "_ws_enabled", False)) else None
                ),
            )
            self._position_monitor_helper = helper
        return helper

    def _submit_managed_exit(
        self,
        position_id: str,
        pos_symbol: str,
        side: OrderSide,
        amount: float,
        exit_price: float,
        triggered: str,
        entry_price: float,
        total_entry_cost: float,
        price_source: str,
        opened_at: Optional[datetime],
    ) -> bool:
        return self._get_managed_lifecycle_helper().submit_managed_exit(
            position_id=position_id,
            pos_symbol=pos_symbol,
            side=side,
            amount=amount,
            exit_price=exit_price,
            triggered=triggered,
            entry_price=entry_price,
            total_entry_cost=total_entry_cost,
            price_source=price_source,
            opened_at=opened_at,
        )

    def _advance_managed_trade_states(self) -> None:
        self._get_managed_lifecycle_helper().advance_managed_trade_states()

    def _resolve_active_strategies_for_mode(self, mode: str) -> List[str]:
        generator = getattr(self, "signal_generator", None)
        if generator is None:
            return []
        try:
            return list(generator.get_active_strategies_for_mode(mode))
        except Exception as exc:
            logger.debug("Failed to resolve active strategies for mode=%s: %s", mode, exc)
            return []

    def _is_entry_signal_confirmed(self, data: Any, signal_type: str, mode: str) -> bool:
        if str(signal_type or "").lower() != "buy":
            return True
        if data is None or getattr(data, "empty", True):
            return False
        candles_required = ConfirmationGate.CONFIRMATION_CANDLES.get(
            str(mode or "standard").strip().lower() or "standard",
            1,
        )
        min_rows = candles_required + 1
        if len(data) < min_rows:
            return False
        try:
            closes = data["close"].astype(float).tail(min_rows).tolist()
        except Exception:
            return False
        candles = [{"close": close_val} for close_val in closes]
        return bool(ConfirmationGate.is_confirmed(candles, "BUY", mode=mode))

    def _build_signal_runtime_deps(self) -> SignalRuntimeDeps:
        return build_signal_runtime_deps(self)

    def _build_multi_timeframe_runtime_deps(self) -> MultiTimeframeRuntimeDeps:
        return build_multi_timeframe_runtime_deps(self)

    def _build_execution_plan_deps(self) -> ExecutionPlanDeps:
        return build_execution_plan_deps(self)

    def _build_execution_runtime_deps(self) -> ExecutionRuntimeDeps:
        return build_execution_runtime_deps(self)

    def _check_pre_trade_gate(self, decision: TradeDecision, portfolio: Dict[str, Any]) -> bool:
        return check_pre_trade_gate(self, decision, portfolio)

    def _run_iteration(self):
        """One iteration: auth/circuit/clock/pause/kill-switch โ’ position checks โ’ each ready pair.

        Per pair: `SignalRuntimeHelper.process_pair_iteration` (see `trading/signal_runtime.py`).
        """
        run_trading_iteration(self)

    def _process_pair_iteration(self, symbol: str):
        return SignalRuntimeHelper.process_pair_iteration(self._build_signal_runtime_deps(), symbol)

    def _maybe_trigger_sideways_rebalance(self, market_condition: Optional[MarketCondition] = None) -> None:
        """Compatibility hook for sideways-market rebalance logic.

        Runtime rebalance logic is currently handled elsewhere; this hook remains
        to keep existing tests and extension points stable.
        """
        return None

    def _serialize_mtf_signals_detail(self, mtf_result) -> Dict[str, Dict[str, Any]]:
        return SignalRuntimeHelper.serialize_mtf_signals_detail(mtf_result)

    def _merge_mtf_signals_detail(
        self,
        base_details: Optional[Dict[str, Dict[str, Any]]],
        override_details: Optional[Dict[str, Dict[str, Any]]],
    ) -> Dict[str, Dict[str, Any]]:
        return SignalRuntimeHelper.merge_mtf_signals_detail(base_details, override_details)

    def _get_mtf_signal_for_symbol(
        self,
        symbol: str,
        portfolio: Dict[str, Any],
    ):
        return SignalRuntimeHelper.get_mtf_signal_for_symbol(
            self._build_multi_timeframe_runtime_deps(), symbol, portfolio
        )

    def _apply_multi_timeframe_confirmation(
        self,
        symbol: str,
        signals: List[AggregatedSignal],
        mtf_signal,
    ) -> List[AggregatedSignal]:
        return SignalRuntimeHelper.apply_multi_timeframe_confirmation(
            self._build_multi_timeframe_runtime_deps(), symbol, signals, mtf_signal
        )

    def _create_execution_plan_for_symbol(self, signal: AggregatedSignal, symbol: str) -> Optional[ExecutionPlan]:
        return SignalRuntimeHelper.create_execution_plan_for_symbol(self._build_execution_plan_deps(), signal, symbol)

    # โ”€โ”€ WebSocket Real-time Price Handler โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

    def _on_ws_tick(self, tick: _PriceTickType):
        self._get_position_monitor_helper().on_ws_tick(tick)

    def _check_sl_tp_immediate(self, tick: _PriceTickType):
        self._get_position_monitor_helper().check_sl_tp_immediate(tick)

    def _ws_sltp_exit_wrapper(
        self,
        position_id: str,
        symbol: str,
        side: OrderSide,
        amount: float,
        current_price: float,
        triggered: str,
        entry_price: float,
        total_entry_cost: float = 0.0,
    ):
        self._get_position_monitor_helper().ws_sltp_exit_wrapper(
            position_id,
            symbol,
            side,
            amount,
            current_price,
            triggered,
            entry_price,
            total_entry_cost,
        )

    def _execute_ws_sl_tp_exit(
        self,
        position_id: str,
        symbol: str,
        side: OrderSide,
        amount: float,
        current_price: float,
        triggered: str,
        entry_price: float,
        total_entry_cost: float = 0.0,
    ):
        self._get_position_monitor_helper().execute_ws_sl_tp_exit(
            position_id,
            symbol,
            side,
            amount,
            current_price,
            triggered,
            entry_price,
            total_entry_cost,
        )

    def _get_portfolio_state(self, allow_refresh: bool = True) -> Dict[str, Any]:
        return self._get_portfolio_runtime_helper().get_portfolio_state(allow_refresh=allow_refresh)

    def _get_dashboard_multi_timeframe_status(self, allow_refresh: bool = True) -> Dict[str, Any]:
        now = time.time()
        cache = getattr(self, "_multi_timeframe_status_cache", {"data": None, "timestamp": 0.0})
        cache_ttl_config = getattr(self, "_cache_ttl", {}) or {}
        cache_ttl = max(float(cache_ttl_config.get("market_data", 10) or 10), 15.0)
        if cache.get("data") is not None and (
            (now - float(cache.get("timestamp", 0.0) or 0.0)) < cache_ttl or not allow_refresh
        ):
            return cache["data"]

        if not allow_refresh:
            return {
                "enabled": bool(getattr(self, "mtf_enabled", False)),
                "mode": "confirmation",
                "timeframes": list(getattr(self, "mtf_timeframes", []) or []),
                "require_htf_confirmation": bool(getattr(self, "_mtf_confirmation_required", False)),
                "primary_timeframe": getattr(self, "timeframe", "1h"),
                "pairs": [],
                "last_signals": dict(getattr(self, "_last_mtf_status", {}) or {}),
            }

        status = self._build_multi_timeframe_status()
        self._multi_timeframe_status_cache = {"data": status, "timestamp": now}
        return status

    def _process_full_auto(self, decision: TradeDecision, portfolio: Dict[str, Any]):
        ExecutionRuntimeHelper.process_full_auto(self._build_execution_runtime_deps(), decision, portfolio)

    def _process_semi_auto(self, decision: TradeDecision, portfolio: Dict[str, Any]):
        ExecutionRuntimeHelper.process_semi_auto(self._build_execution_runtime_deps(), decision, portfolio)

    def _process_dry_run(self, decision: TradeDecision, portfolio: Dict[str, Any]):
        ExecutionRuntimeHelper.process_dry_run(self._build_execution_runtime_deps(), decision, portfolio)

    def approve_trade(self, decision_id: int) -> bool:
        return ExecutionRuntimeHelper.approve_trade(self._build_execution_runtime_deps(), decision_id)

    def _try_submit_managed_signal_sell(self, decision: TradeDecision) -> bool:
        return self._get_managed_lifecycle_helper().try_submit_managed_signal_sell(decision)

    def reject_trade(self, decision_id: int) -> bool:
        return ExecutionRuntimeHelper.reject_trade(self._build_execution_runtime_deps(), decision_id)

    def _send_trade_alert(self, decision: TradeDecision, result: OrderResult):
        """Log executed entries and notify Telegram."""
        if not self.send_alerts:
            return

        message = self._format_trade_alert(decision, result)
        self._send_alert(message, to_telegram=True)

    def _send_pending_alert(self, decision: TradeDecision, portfolio: Dict[str, Any]):
        """Send alert about pending trade requiring approval."""
        if not self.send_alerts:
            return

        message = self._format_pending_alert(decision, portfolio)
        self._send_alert(message, to_telegram=True)

    def _send_dry_run_alert(self, decision: TradeDecision, portfolio: Dict[str, Any]):
        """Send alert about dry run simulation."""
        if not self.send_alerts:
            return

        message = self._format_dry_run_alert(decision, portfolio)
        self._send_alert(message)

    def _get_status_runtime_helper(self) -> StatusRuntimeHelper:
        helper = getattr(self, "_status_runtime_helper", None)
        if helper is None:
            helper = StatusRuntimeHelper(
                self,
                required_candles=self._required_candles_for_trading_readiness(),
                websocket_available=bool(_WEBSOCKET_AVAILABLE and getattr(self, "_ws_enabled", False)),
                latest_ticker_getter=(
                    get_latest_ticker if bool(_WEBSOCKET_AVAILABLE and getattr(self, "_ws_enabled", False)) else None
                ),
            )
            self._status_runtime_helper = helper
        return helper

    @staticmethod
    def _format_alert_block(header: str, lines: List[str], now: Optional[datetime] = None) -> str:
        return StatusRuntimeHelper.format_alert_block(header, lines, now=now)

    @staticmethod
    def _format_coin_symbol(symbol: str) -> str:
        return StatusRuntimeHelper.format_coin_symbol(symbol)

    def _get_trailing_trace_context(self) -> Dict[str, Any]:
        return self._get_status_runtime_helper().get_trailing_trace_context()

    def _log_position_trace(
        self,
        event: str,
        symbol: str,
        *,
        entry_order_id: str = "",
        exit_order_id: str = "",
        amount: float = 0.0,
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
        previous_stop_loss: float = 0.0,
        current_price: float = 0.0,
        profit_pct: Optional[float] = None,
        trigger: str = "",
        notes: str = "",
    ) -> None:
        self._get_status_runtime_helper().log_position_trace(
            event,
            symbol,
            entry_order_id=entry_order_id,
            exit_order_id=exit_order_id,
            amount=amount,
            entry_price=entry_price,
            exit_price=exit_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            previous_stop_loss=previous_stop_loss,
            current_price=current_price,
            profit_pct=profit_pct,
            trigger=trigger,
            notes=notes,
        )

    def _format_exit_alert(
        self,
        symbol: str,
        trigger_label: str,
        amount: float,
        entry_price: float,
        exit_price: float,
        entry_cost: float,
        gross_exit: float,
        net_pnl: float,
        net_pnl_pct: float,
        total_fees: float,
        now: Optional[datetime] = None,
    ) -> str:
        return self._get_status_runtime_helper().format_exit_alert(
            symbol,
            trigger_label,
            amount,
            entry_price,
            exit_price,
            entry_cost,
            gross_exit,
            net_pnl,
            net_pnl_pct,
            total_fees,
            now=now,
        )

    def _format_trade_alert(self, decision: TradeDecision, result: OrderResult) -> str:
        return self._get_status_runtime_helper().format_trade_alert(decision, result)

    def _format_skip_alert(self, decision: TradeDecision) -> str:
        return self._get_status_runtime_helper().format_skip_alert(decision)

    def _format_pending_alert(self, decision: TradeDecision, portfolio: Dict[str, Any]) -> str:
        return self._get_status_runtime_helper().format_pending_alert(decision, portfolio)

    def _format_dry_run_alert(self, decision: TradeDecision, portfolio: Dict[str, Any]) -> str:
        return self._get_status_runtime_helper().format_dry_run_alert(decision, portfolio)

    def _send_alert(self, message: str, to_telegram: bool = False):
        """Send alert via unified alert system.

        Args:
            message: Alert message text
            to_telegram: If True, send to Telegram. If False, log to console only.
                         Only Trade Executed alerts should set this to True.
        """
        level = AlertLevel.TRADE if to_telegram else AlertLevel.INFO
        self.alert_system.send(level, message)

    def _on_trailing_stop_callback(
        self, symbol: str, old_sl: float, new_sl: float, current_price: float, profit_pct: float
    ):
        """Log trailing stop ratchet (not sent to Telegram)."""
        if not self.send_alerts:
            return
        self._log_position_trace(
            "TRAILING_RATCHET",
            symbol,
            previous_stop_loss=old_sl,
            stop_loss=new_sl,
            current_price=current_price,
            profit_pct=profit_pct,
        )
        coin = extract_base_asset(symbol)
        msg = (
            f"Trailing SL  {coin}  {old_sl:,.0f} โ’ {new_sl:,.0f}  "
            f"profit +{profit_pct:.2f}%  price {current_price:,.0f}"
        )
        self._send_alert(msg, to_telegram=False)

    def _build_multi_timeframe_status(self) -> Dict[str, Any]:
        return self._get_status_runtime_helper().build_multi_timeframe_status()

    def get_status(self, lightweight: bool = False) -> Dict[str, Any]:
        return self._get_status_runtime_helper().get_status(lightweight=lightweight)

    def trigger_rebalance(self) -> Dict[str, Any]:
        """Rebalance is permanently disabled in sniper mode."""
        return {
            "status": "skipped",
            "reason": "Rebalance is disabled in sniper mode",
            "trigger": "manual",
        }

    # Status and monitoring facade wrappers.

    def _safe_pending_count(self) -> int:
        return self._get_status_runtime_helper().safe_pending_count()

    def get_pending_decisions(self) -> List[Dict[str, Any]]:
        return self._get_status_runtime_helper().get_pending_decisions()

    def _register_sl_hold_entry(self, position_id: str) -> None:
        guard = getattr(self, "_sl_hold_guard", None)
        if guard is not None and position_id:
            guard.register_entry(
                str(position_id), str(getattr(self, "_active_strategy_mode", "standard") or "standard")
            )

    def _cleanup_sl_hold_entry(self, position_id: str) -> None:
        guard = getattr(self, "_sl_hold_guard", None)
        if guard is not None and position_id:
            guard.cleanup(str(position_id))

    def _is_sl_hold_locked(self, position_id: str) -> bool:
        guard = getattr(self, "_sl_hold_guard", None)
        return bool(guard is not None and position_id and guard.is_sl_locked(str(position_id)))

    @staticmethod
    def _coerce_opened_at(value: Any) -> Optional[datetime]:
        return coerce_opened_at(value)

    def _minimal_roi_exit_signal(
        self,
        *,
        symbol: str,
        side: Any,
        entry_price: float,
        current_price: float,
        opened_at: Any,
    ) -> tuple[bool, str]:
        return minimal_roi_exit_signal(
            self,
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            current_price=current_price,
            opened_at=opened_at,
        )

    def _check_positions_for_sl_tp(self):
        self._get_position_monitor_helper().check_positions_for_sl_tp()

    def _should_allow_voluntary_exit(
        self,
        symbol: str,
        trigger: str,
        entry_price: float,
        exit_price: float,
        amount: float,
        total_entry_cost: float = 0.0,
        side: Any = "buy",
    ) -> bool:
        return should_allow_voluntary_exit(
            self,
            symbol,
            trigger,
            entry_price,
            exit_price,
            amount,
            total_entry_cost=total_entry_cost,
            side=side,
        )
