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

import time
import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Dict, Any, List

from alerts import AlertSystem, AlertLevel, format_fatal_auth_alert  # Unified alert system
from enum import Enum
import os

from signal_generator import SignalGenerator, AggregatedSignal
from trade_executor import TradeExecutor, ExecutionPlan, OrderSide, OrderResult
from risk_management import RiskManager, resolve_effective_sl_tp_percentages
from api_client import BitkubClient, FatalAuthException
from balance_monitor import BalanceEvent, BalanceMonitor
from database import get_database
from helpers import calc_net_pnl, normalize_side_value
from state_management import TradeLifecycleState, TradeStateManager, TradeStateSnapshot
from strategy_base import MarketCondition
from trading.execution_runtime import ExecutionRuntimeDeps, ExecutionRuntimeHelper
from trading.managed_lifecycle import ManagedLifecycleHelper
from trading.portfolio_runtime import PortfolioRuntimeHelper
from trading.position_monitor import PositionMonitorHelper
from trading.signal_runtime import ExecutionPlanDeps, MultiTimeframeRuntimeDeps, SignalRuntimeDeps, SignalRuntimeHelper
from trading.startup_runtime import StartupRuntimeHelper
from trading.status_runtime import StatusRuntimeHelper

# Import shared enums from modular trading package
from trading.orchestrator import BotMode, SignalSource, TradeDecision

# Type-checking imports (Pylance static analysis — not executed at runtime)
if TYPE_CHECKING:
    from monitoring import MonitoringService as _MonitoringServiceType
    from bitkub_websocket import BitkubWebSocket as _BitkubWebSocketType
    from bitkub_websocket import PriceTick as _PriceTickType

# Runtime monitoring import (graceful fallback if module missing)
try:
    from monitoring import MonitoringService
    _MONITORING_AVAILABLE = True
except ImportError:
    MonitoringService = None
    _MONITORING_AVAILABLE = False

logger = logging.getLogger(__name__)

_MIN_CANDLES_FOR_TRADING_READINESS = 35


def _coerce_trade_float(val, default: float = 0.0) -> float:
    """Normalize DB/API numeric fields; avoids ``None > 0`` TypeErrors in comparisons."""
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

# WebSocket real-time price support
try:
    from bitkub_websocket import get_websocket, stop_websocket, PriceTick, get_latest_ticker
    _WEBSOCKET_AVAILABLE = True
except ImportError:
    _WEBSOCKET_AVAILABLE = False
    get_websocket = None
    stop_websocket = None
    PriceTick = None
    get_latest_ticker = None
    logger.warning("bitkub_websocket not available — falling back to REST polling")


class TradingBotOrchestrator:
    """
    Main orchestrator for the crypto trading bot.
    Coordinates signal generation, risk checking, alerts, and execution.
    
    Runs a pure technical strategy engine.
    """
    
    def __init__(
        self,
        config: Dict[str, Any],
        api_client: BitkubClient,
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
            api_client: Bitkub API client
            signal_generator: SignalGenerator instance
            risk_manager: RiskManager instance
            executor: TradeExecutor instance
            alert_sender: Function to send alerts (telegram/console)
            alert_system: Shared alert system instance
            trading_disabled_event: threading.Event; if set, all trading is paused
        """
        self.config = config
        self.api_client = api_client
        self.signal_generator = signal_generator
        self.risk_manager = risk_manager
        self.executor = executor
        self.alert_system = alert_system or AlertSystem()
        self.alert_sender = alert_sender or self.alert_system.create_trade_sender()
        self._trading_disabled = trading_disabled_event or threading.Event()
        
        # Bot mode (support nested trading.mode or top-level mode)
        mode_str = (config.get("trading", {}).get("mode") or config.get("mode") or "semi_auto").lower()
        self.mode = BotMode.FULL_AUTO if mode_str == "full_auto" else (
            BotMode.DRY_RUN if mode_str == "dry_run" else BotMode.SEMI_AUTO
        )
        
        # Trading settings
        trading_cfg = config.get("trading", {})
        self.trading_pair = (
            trading_cfg.get("trading_pair")
            or config.get("trading_pair")
            or ""
        )
        self.trading_pairs = self._get_trading_pairs()
        self.interval_seconds = trading_cfg.get("interval_seconds", config.get("interval_seconds", 60))
        self.timeframe = trading_cfg.get("timeframe", config.get("timeframe", "1h"))
        self.read_only = config.get("read_only", False) or os.environ.get("BOT_READ_ONLY", "").lower() in ("1", "true", "yes")
        portfolio_guard_cfg = dict(config.get("data", {}).get("portfolio_guard", {}) or {})
        self._held_coins_only = bool(portfolio_guard_cfg.get("held_coins_only", True))
        self._auth_degraded = bool(config.get("auth_degraded", False))
        self._auth_degraded_reason = str(config.get("auth_degraded_reason") or "")
        self._auth_degraded_logged = False
        candle_retention_config = dict(config.get("candle_retention", {}) or {})
        self._candle_retention_enabled = bool(candle_retention_config.get("enabled", False))
        self._candle_retention_run_on_startup = bool(candle_retention_config.get("run_on_startup", True))
        self._candle_retention_vacuum = bool(candle_retention_config.get("vacuum_after_cleanup", False))
        self._candle_retention_policy = self._build_candle_retention_policy(candle_retention_config)
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
            raw_min_trade_thb = 15.0
        try:
            self.min_trade_value_thb = float(raw_min_trade_thb)
        except (TypeError, ValueError):
            self.min_trade_value_thb = 15.0
        
        # Strategy config
        self.strategies_config = config.get("strategies", {})
        self.enabled_strategies = self.strategies_config.get("enabled", [
            "trend_following", "mean_reversion", "breakout", "scalping"
        ])
        self._active_strategy_mode = str(config.get("active_strategy_mode") or "standard").lower()
        scalping_cfg = dict(self.strategies_config.get("scalping", {}) or {})
        self._scalping_mode_enabled = self._active_strategy_mode == "scalping"
        self._scalping_position_timeout_minutes = int(scalping_cfg.get("position_timeout_minutes", 30) or 30)
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
                    "⚠️ [MTF Config] higher_timeframes %s are NOT in collected timeframes %s — "
                    "these will never have data. Add them to 'timeframes' or remove from 'higher_timeframes'.",
                    missing_htf, self.mtf_timeframes,
                )
        
        # Database and state management
        self.db = get_database()
        self.signal_generator.set_database(self.db)
        self._state_manager = TradeStateManager(self.db, config.get("state_management", {}))
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
        self._ws_client: Optional[_BitkubWebSocketType] = None
        self._ws_enabled: bool = config.get("websocket", {}).get("enabled", True)
        if self._ws_enabled and _WEBSOCKET_AVAILABLE and self.trading_pairs:
            try:
                # Collect all symbols from data config
                ws_symbols = [p.upper() for p in self.trading_pairs]
                ws_symbols = list(dict.fromkeys(ws_symbols))  # dedupe, preserve order

                ws = get_websocket(  # type: ignore[misc]
                    symbols=ws_symbols,
                    on_tick=self._on_ws_tick,
                )
                if ws is not None:
                    self._ws_client = ws
                logger.info(
                    f"เชื่อมต่อ WebSocket สำเร็จ | เหรียญที่ติดตาม: {ws_symbols} | "
                    f"url=wss://api.bitkub.com/websocket"
                )
            except Exception as e:
                logger.error(f"เกิดข้อผิดพลาดในการเริ่มต้น WebSocket: {e}")
                self._ws_enabled = False
        else:
            if not _WEBSOCKET_AVAILABLE:
                logger.info("ไม่ได้เปิดใช้งาน WebSocket (ไม่พบโมดูลที่เกี่ยวข้อง)")
            elif not self.trading_pairs:
                logger.info("ไม่ได้เปิดใช้งาน WebSocket เพราะ Bitkub ไม่พบคู่เหรียญที่ถืออยู่")
            else:
                logger.info("ปิดการใช้งาน WebSocket ในไฟล์คอนฟิก (disabled in config)")

        logger.info(
            f"TradingBotOrchestrator initialized | "
            f"Mode: {self.mode.value} | "
            f"Pairs: {self.trading_pairs} | "
            f"Signal Source: {self.signal_source.value} | "
            f"WebSocket: {'enabled' if self._ws_enabled else 'disabled'}"
        )

        # M6 fix: per-position deduplication guard for WS SL/TP exit threads.
        # Prevents unbounded thread spawning during flash crashes where rapid
        # ticks would otherwise create hundreds of threads for the same position.
        self._ws_sltp_inflight: set = set()
        self._ws_sltp_inflight_lock = threading.Lock()

        # === Performance caches ===
        # Cache TTLs (seconds)
        self._cache_ttl = {
            'portfolio': 10,   # Portfolio state: 10s
            'market_data': 10, # Market data from DB: 10s
            'atr': 60,         # ATR calculation: 60s
        }
        self._portfolio_cache = {"data": None, "timestamp": 0.0}
        self._portfolio_cache_lock = threading.Lock()
        self._market_data_cache = {"data": None, "timestamp": 0.0}
        self._atr_cache = {"value": None, "timestamp": 0.0}
        self._multi_timeframe_status_cache = {"data": None, "timestamp": 0.0}
        self._symbol_market_cache: Dict[str, Dict] = {}
        self._last_portfolio_guard_skipped: Optional[tuple[str, ...]] = None
        self._last_candle_readiness_skipped: Optional[tuple[str, ...]] = None

        self._prune_invalid_btc_positions()
        # Important startup order:
        # 1) prune DB ghosts
        # 2) reconcile against Bitkub (in start())
        # 3) sync OMS in-memory tracking from DB (after reconciliation)

    def _prune_invalid_btc_positions(self) -> None:
        """Drop THB_BTC rows with impossible base size (any side) and zero-remaining ghosts.

        Invalid amount rows are not present in OMS memory (sync skips them), so pruning
        must hit SQLite directly, then clear zero-remaining rows.
        """
        n_invalid = 0
        n_zero = 0
        try:
            n_invalid = self.db.delete_positions_thb_btc_amount_over_limit(1.0)
        except Exception as ex:
            logger.error("[Startup] Failed to prune invalid THB_BTC amount positions: %s", ex)
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
        with self._pause_state_lock:
            is_paused = bool(getattr(self, "_trading_paused", False))
            pause_reason = str(getattr(self, "_pause_reason", "") or "")
        if is_paused:
            return True, pause_reason
        monitoring = getattr(self, "_monitoring", None)
        if monitoring and hasattr(monitoring, "_reconciler"):
            return monitoring._reconciler.is_paused()
        return False, ""

    def _set_pause_reason(self, key: str, reason: str) -> None:
        with self._pause_state_lock:
            self._pause_reasons[str(key)] = str(reason)
            self._trading_paused = True
            self._pause_reason = " | ".join(self._pause_reasons.values())
            pause_reason = self._pause_reason
        logger.warning("Trading PAUSED: %s", pause_reason)

    def _clear_pause_reason(self, key: str) -> None:
        with self._pause_state_lock:
            self._pause_reasons.pop(str(key), None)
            self._trading_paused = bool(self._pause_reasons)
            self._pause_reason = " | ".join(self._pause_reasons.values())
            is_paused = self._trading_paused
        if not is_paused:
            logger.info("Trading RESUMED - auto pause cleared")

    def _invalidate_portfolio_cache(self) -> None:
        _lock = getattr(self, "_portfolio_cache_lock", None)
        if _lock:
            with _lock:
                self._portfolio_cache = {"data": None, "timestamp": 0.0}
        else:
            self._portfolio_cache = {"data": None, "timestamp": 0.0}

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
                websocket_available=_WEBSOCKET_AVAILABLE,
                latest_ticker_getter=get_latest_ticker,
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

    def _reconcile_tracked_positions_with_balance_state(
        self,
        balance_state: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Drop filled tracked positions whose base-asset balance is gone on Bitkub."""
        if not self.executor:
            return []

        snapshot = balance_state or self.get_balance_state()
        balances = (snapshot or {}).get("balances") or {}
        if not balances:
            return []

        removed_symbols: List[str] = []
        for position in self.executor.get_open_orders() or []:
            symbol = str(position.get("symbol") or "").upper()
            order_id = str(position.get("order_id") or "")
            if not symbol or not order_id or "_" not in symbol:
                continue

            side = normalize_side_value(position.get("side"))

            filled_amount = float(position.get("filled_amount") or 0.0)
            amount = float(position.get("amount") or 0.0)
            remaining_amount = float(position.get("remaining_amount") or 0.0)
            is_partial_fill = bool(position.get("is_partial_fill"))
            is_filled = bool(position.get("filled"))

            represents_live_coin = False
            if side == "buy":
                represents_live_coin = (
                    is_filled
                    or is_partial_fill
                    or filled_amount > 0.0
                    or (amount > 0.0 and remaining_amount <= 0.0)
                )
            elif side == "sell":
                represents_live_coin = True
            elif not side:
                represents_live_coin = True

            if not represents_live_coin:
                continue

            base_asset = symbol.split("_", 1)[1].upper()
            balance_total = self._extract_total_balance(snapshot, base_asset)
            tracked_amount = max(filled_amount, amount, remaining_amount, 0.0)
            dust_threshold = min(max(tracked_amount * 0.01, 1e-8), 1e-6)
            if balance_total > dust_threshold:
                continue

            self.executor.remove_tracked_position(order_id)
            self.db.record_held_coin(symbol, 0.0)
            removed_symbols.append(symbol)
            logger.warning(
                "[Balance Reconcile] Removed stale tracked position %s (%s) after balance dropped to %.8f",
                symbol,
                order_id,
                balance_total,
            )

        if removed_symbols and self._state_machine_enabled:
            self._state_manager.sync_in_position_states(self.executor.get_open_orders())

        return removed_symbols

    def _preserve_bootstrap_position_from_balances(
        self,
        order_id: str,
        local_pos: Dict[str, Any],
        balances: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not self.executor or not self.db:
            return False

        bootstrap_id = str(order_id or "")
        if not bootstrap_id.startswith("bootstrap_"):
            return False

        symbol = str(local_pos.get("symbol") or "").upper()
        if "_" not in symbol:
            return False

        side_value = local_pos.get("side", OrderSide.BUY)
        side_str = normalize_side_value(side_value)
        if side_str != "buy":
            return False

        base_asset = symbol.split("_", 1)[1].upper()
        balance_payload = (balances or {}).get(base_asset, {}) if isinstance(balances, dict) else {}
        balance_total = self._extract_total_balance({"balances": {base_asset: balance_payload}}, base_asset)
        if balance_total <= 0:
            return False

        preserved = dict(local_pos)
        tracked_amount = max(
            float(local_pos.get("filled_amount") or 0.0),
            float(local_pos.get("amount") or 0.0),
            0.0,
        )
        preserved_amount = float(balance_total)
        external_excess = 0.0
        if tracked_amount > 0:
            preserved_amount = min(float(balance_total), tracked_amount)
            external_excess = max(float(balance_total) - tracked_amount, 0.0)

        preserved_entry = float(local_pos.get("entry_price") or 0.0)
        preserved_sl = local_pos.get("stop_loss")
        preserved_tp = local_pos.get("take_profit")
        restored_context = self._resolve_bootstrap_position_context(symbol, float(balance_total))
        restored_source = str(restored_context.get("source") or "")
        restored_entry = _coerce_trade_float(restored_context.get("entry_price"), 0.0)
        if restored_source and restored_source != "bootstrap_position" and restored_entry > 0:
            preserved_amount = float(balance_total)
            external_excess = 0.0
            preserved_entry = restored_entry
            preserved_sl = restored_context.get("stop_loss")
            preserved_tp = restored_context.get("take_profit")
        if preserved_entry > 0 and (not preserved_sl or not preserved_tp):
            fallback_sl, fallback_tp = self._build_bootstrap_position_sl_tp(symbol, preserved_entry)
            preserved_sl = preserved_sl or fallback_sl
            preserved_tp = preserved_tp or fallback_tp

        preserved.update({
            "amount": preserved_amount,
            "entry_price": preserved_entry,
            "remaining_amount": 0.0,
            "filled": True,
            "filled_amount": preserved_amount,
            "filled_price": preserved_entry,
            "stop_loss": preserved_sl,
            "take_profit": preserved_tp,
        })
        if preserved_entry > 0:
            restored_cost = _coerce_trade_float(restored_context.get("total_entry_cost"), 0.0)
            preserved["total_entry_cost"] = restored_cost if (restored_source and restored_source != "bootstrap_position" and restored_cost > 0) else (preserved_amount * preserved_entry)

        with self.executor._orders_lock:
            self.executor._open_orders[bootstrap_id] = preserved

        try:
            self.db.save_position(preserved)
        except Exception as exc:
            logger.warning(
                "[Reconcile] Failed to persist preserved bootstrap position %s: %s",
                bootstrap_id,
                exc,
            )

            logger.warning(
                "[Reconcile] Live %s balance exceeds tracked bootstrap size by %.8f. "
                "Keeping tracked entry/SL/TP unchanged and not averaging external coins into position %s.",
                base_asset,
                external_excess,
                bootstrap_id,
            )

        logger.info(
            "[Reconcile] Preserved bootstrap position %s using live %s balance %.8f, entry %.2f, SL=%s, TP=%s%s",
            bootstrap_id,
            base_asset,
            preserved_amount,
            preserved_entry,
            f"{float(preserved_sl):.2f}" if preserved_sl else "n/a",
            f"{float(preserved_tp):.2f}" if preserved_tp else "n/a",
            f" via {restored_source}" if restored_source and restored_source != "bootstrap_position" else "",
        )
        return True

    def _build_bootstrap_position_sl_tp(self, symbol: str, entry_price: float) -> tuple[Optional[float], Optional[float]]:
        if entry_price <= 0:
            return None, None

        risk_cfg = dict(self.config.get("risk", {}) or {})
        stop_loss_pct, take_profit_pct = resolve_effective_sl_tp_percentages(symbol, risk_cfg)

        stop_loss = round(entry_price * (1 + (stop_loss_pct / 100.0)), 6)
        take_profit = round(entry_price * (1 + (take_profit_pct / 100.0)), 6)
        return stop_loss, take_profit

    def _resolve_bootstrap_position_context(
        self,
        symbol: str,
        quantity: float,
    ) -> Dict[str, Any]:
        """Recover bootstrap entry context from persisted position/state before estimating from market price."""
        symbol_key = str(symbol or "").upper()
        if not symbol_key:
            return {}

        best: Dict[str, Any] = {}
        bootstrap_best: Dict[str, Any] = {}

        db = getattr(self, "db", None)
        if db is not None and hasattr(db, "load_all_positions"):
            try:
                rows = list(db.load_all_positions() or [])
            except Exception as exc:
                logger.debug("[Bootstrap Positions] Failed to load persisted positions for %s: %s", symbol_key, exc)
                rows = []

            matching_rows = []
            bootstrap_rows = []
            for row in rows:
                row_symbol = str(row.get("symbol") or "").upper()
                row_side = row.get("side", "buy")
                if hasattr(row_side, "value"):
                    row_side = row_side.value
                if row_symbol != symbol_key or str(row_side or "").lower() != "buy":
                    continue

                entry_price = _coerce_trade_float(row.get("entry_price"), 0.0)
                if entry_price <= 0:
                    continue
                order_id = str(row.get("order_id") or "")
                if order_id.startswith("bootstrap_"):
                    bootstrap_rows.append(row)
                else:
                    matching_rows.append(row)

            if matching_rows:
                matching_rows.sort(key=lambda row: row.get("timestamp") or datetime.min, reverse=True)
                row = matching_rows[0]
                entry_price = _coerce_trade_float(row.get("entry_price"), 0.0)
                total_entry_cost = _coerce_trade_float(row.get("total_entry_cost"), 0.0)
                if entry_price > 0:
                    best = {
                        "entry_price": entry_price,
                        "stop_loss": row.get("stop_loss"),
                        "take_profit": row.get("take_profit"),
                        "total_entry_cost": total_entry_cost if total_entry_cost > 0 else (quantity * entry_price),
                        "source": "persisted_position",
                    }
            elif bootstrap_rows:
                bootstrap_rows.sort(key=lambda row: row.get("timestamp") or datetime.min, reverse=True)
                row = bootstrap_rows[0]
                entry_price = _coerce_trade_float(row.get("entry_price"), 0.0)
                total_entry_cost = _coerce_trade_float(row.get("total_entry_cost"), 0.0)
                if entry_price > 0:
                    bootstrap_best = {
                        "entry_price": entry_price,
                        "stop_loss": row.get("stop_loss"),
                        "take_profit": row.get("take_profit"),
                        "total_entry_cost": total_entry_cost if total_entry_cost > 0 else (quantity * entry_price),
                        "source": "bootstrap_position",
                    }

        if not best:
            history_best = self._resolve_bootstrap_exchange_history_context(symbol_key, quantity)
            if history_best:
                best = history_best

        if db is not None and hasattr(db, "get_trades"):
            try:
                recent_trades = list(db.get_trades(pair=symbol_key, limit=20) or [])
            except Exception as exc:
                logger.debug("[Bootstrap Positions] Failed to load trade history for %s: %s", symbol_key, exc)
                recent_trades = []

            trade_events: list[Dict[str, Any]] = []
            for trade in recent_trades:
                trade_side = getattr(trade, "side", "")
                trade_pair = getattr(trade, "pair", "")
                normalized_side = str(trade_side or "").lower()
                if str(trade_pair or "").upper() != symbol_key or normalized_side not in ("buy", "sell"):
                    continue
                trade_price = _coerce_trade_float(getattr(trade, "price", 0.0), 0.0)
                trade_qty = _coerce_trade_float(getattr(trade, "quantity", 0.0), 0.0)
                if trade_price <= 0 or trade_qty <= 0:
                    continue
                trade_events.append({
                    "side": normalized_side,
                    "quantity": trade_qty,
                    "price": trade_price,
                    "total_cost": trade_qty * trade_price if normalized_side == "buy" else 0.0,
                    "timestamp": getattr(trade, "timestamp", None),
                })

            if trade_events and not best:
                trade_history_best = self._build_weighted_inventory_context(
                    trade_events,
                    quantity,
                    source="trade_history",
                )
                if trade_history_best:
                    best = trade_history_best

        if db is not None and hasattr(db, "get_trade_state"):
            try:
                state_row = db.get_trade_state(symbol_key)
            except Exception as exc:
                logger.debug("[Bootstrap Positions] Failed to load trade state for %s: %s", symbol_key, exc)
                state_row = None

            state_value = str((state_row or {}).get("state") or "").lower()
            state_entry = _coerce_trade_float((state_row or {}).get("entry_price"), 0.0)
            if state_entry > 0 and state_value in (
                TradeLifecycleState.IN_POSITION.value,
                TradeLifecycleState.PENDING_SELL.value,
            ) and not best:
                best = {
                    "entry_price": state_entry,
                    "stop_loss": (state_row or {}).get("stop_loss"),
                    "take_profit": (state_row or {}).get("take_profit"),
                    "total_entry_cost": _coerce_trade_float((state_row or {}).get("total_entry_cost"), 0.0) or (quantity * state_entry),
                    "source": "trade_state",
                }

        return best or bootstrap_best

    @staticmethod
    def _bootstrap_quantity_tolerance(quantity: float) -> float:
        return max(abs(float(quantity or 0.0)) * 0.05, 1e-8)

    def _build_weighted_inventory_context(
        self,
        events: List[Dict[str, Any]],
        quantity: float,
        *,
        source: str,
    ) -> Dict[str, Any]:
        if not events:
            return {}

        inventory_qty = 0.0
        inventory_notional = 0.0
        inventory_cost = 0.0
        sorted_events = sorted(events, key=lambda event: event.get("timestamp") or datetime.min)

        for event in sorted_events:
            side = str(event.get("side") or "").lower()
            event_qty = _coerce_trade_float(event.get("quantity"), 0.0)
            event_price = _coerce_trade_float(event.get("price"), 0.0)
            event_cost = _coerce_trade_float(event.get("total_cost"), 0.0)
            if event_qty <= 0 or event_price <= 0:
                continue

            if side in ("buy", "bid"):
                inventory_qty += event_qty
                inventory_notional += event_qty * event_price
                inventory_cost += event_cost if event_cost > 0 else (event_qty * event_price)
                continue

            if side not in ("sell", "ask") or inventory_qty <= 0:
                continue

            matched_qty = min(event_qty, inventory_qty)
            avg_price = inventory_notional / inventory_qty if inventory_qty > 0 else 0.0
            avg_cost = inventory_cost / inventory_qty if inventory_qty > 0 else 0.0
            inventory_qty = max(inventory_qty - matched_qty, 0.0)
            inventory_notional = max(inventory_notional - (avg_price * matched_qty), 0.0)
            inventory_cost = max(inventory_cost - (avg_cost * matched_qty), 0.0)
            if inventory_qty <= 1e-12:
                inventory_qty = 0.0
                inventory_notional = 0.0
                inventory_cost = 0.0

        if inventory_qty <= 0 or inventory_notional <= 0 or inventory_cost <= 0:
            return {}

        qty_tolerance = self._bootstrap_quantity_tolerance(quantity)
        if quantity > 0 and abs(inventory_qty - quantity) > qty_tolerance:
            return {}

        entry_price = inventory_notional / inventory_qty if inventory_qty > 0 else 0.0
        if entry_price <= 0:
            return {}

        return {
            "entry_price": entry_price,
            "stop_loss": None,
            "take_profit": None,
            "total_entry_cost": inventory_cost,
            "source": source,
        }

    def _resolve_bootstrap_exchange_history_context(
        self,
        symbol: str,
        quantity: float,
    ) -> Dict[str, Any]:
        try:
            history = list(self.api_client.get_order_history(symbol, limit=self._order_history_window_limit()) or [])
        except Exception as exc:
            logger.debug("[Bootstrap Positions] Failed to load exchange order history for %s: %s", symbol, exc)
            return {}

        qty_tolerance = self._bootstrap_quantity_tolerance(quantity)
        close_matches: list[Dict[str, Any]] = []
        fallback_matches: list[Dict[str, Any]] = []
        history_events: list[Dict[str, Any]] = []

        for row in history:
            status_value = self._history_status_value(row)
            if status_value and not self._history_status_is_filled(row):
                continue

            side_value = self._history_side_value(row)
            if side_value and side_value not in ("buy", "bid", "sell", "ask"):
                continue

            filled_amount, filled_price = self._extract_history_fill_details(row)
            if filled_amount <= 0 or filled_price <= 0:
                continue

            history_events.append({
                "side": side_value,
                "quantity": filled_amount,
                "price": filled_price,
                "total_cost": 0.0,
                "timestamp": self._history_timestamp_value(row),
            })

            raw_cost = _coerce_trade_float(row.get("amt"), 0.0)
            if raw_cost <= 0:
                raw_cost = _coerce_trade_float(row.get("amount"), 0.0)
            if side_value in ("buy", "bid"):
                history_events[-1]["total_cost"] = raw_cost if raw_cost > 0 else (filled_amount * filled_price)

            candidate = {
                "entry_price": filled_price,
                "stop_loss": None,
                "take_profit": None,
                "total_entry_cost": raw_cost if raw_cost > 0 else (filled_amount * filled_price),
                "source": "exchange_history",
            }

            if side_value in ("buy", "bid"):
                if quantity > 0 and abs(filled_amount - quantity) <= qty_tolerance:
                    close_matches.append(candidate)
                else:
                    fallback_matches.append(candidate)

        weighted_context = self._build_weighted_inventory_context(
            history_events,
            quantity,
            source="exchange_history",
        )
        if weighted_context:
            return weighted_context

        if close_matches:
            return close_matches[0]
        if fallback_matches:
            return fallback_matches[0]
        return {}

    @staticmethod
    def _history_timestamp_value(row: Optional[Dict[str, Any]]) -> datetime:
        if not row:
            return datetime.min

        raw_ts = row.get("ts") or row.get("timestamp") or row.get("created_at") or row.get("updated_at")
        if isinstance(raw_ts, datetime):
            return raw_ts
        if isinstance(raw_ts, (int, float)):
            try:
                ts_value = float(raw_ts)
                if ts_value > 1e12:
                    ts_value /= 1000.0
                return datetime.fromtimestamp(ts_value)
            except (OverflowError, OSError, ValueError):
                return datetime.min
        if isinstance(raw_ts, str) and raw_ts.strip():
            try:
                return datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            except ValueError:
                return datetime.min
        return datetime.min

    @staticmethod
    def _default_candle_retention_policy() -> Dict[str, int]:
        return {
            "1m": 7,
            "5m": 14,
            "15m": 30,
            "1h": 60,
            "4h": 90,
            "1d": 180,
        }

    def _build_candle_retention_policy(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
        policy = self._default_candle_retention_policy()
        raw_policy = dict((config or {}).get("timeframes") or {})
        for timeframe, raw_days in raw_policy.items():
            normalized_timeframe = str(timeframe or "").strip()
            if not normalized_timeframe:
                continue
            try:
                days = int(raw_days)
            except (TypeError, ValueError):
                logger.warning("[Retention] Invalid retention value for %s: %r", normalized_timeframe, raw_days)
                continue
            if days <= 0:
                logger.warning("[Retention] Ignoring non-positive retention for %s: %s", normalized_timeframe, days)
                continue
            policy[normalized_timeframe] = days
        return policy

    def _run_candle_retention_cleanup(self, reason: str) -> None:
        if not self._candle_retention_enabled or not self._candle_retention_policy:
            return

        try:
            deleted = self.db.cleanup_price_history_by_timeframe(self._candle_retention_policy)
            self._last_candle_retention_cleanup_at = time.time()
            total_deleted = int(deleted.get("total", 0) or 0)
            detail = ", ".join(
                f"{timeframe}={int(deleted.get(timeframe, 0) or 0)}"
                for timeframe in self._candle_retention_policy.keys()
            )
            logger.info("[Retention] Candle cleanup (%s) removed %d row(s): %s", reason, total_deleted, detail)

            if total_deleted > 0 and self._candle_retention_vacuum:
                if self.db.vacuum():
                    logger.info("[Retention] SQLite VACUUM completed after %s cleanup", reason)
                else:
                    logger.warning("[Retention] SQLite VACUUM skipped/failed after %s cleanup", reason)
        except Exception as exc:
            logger.warning("[Retention] Candle cleanup (%s) failed: %s", reason, exc)

    def _maybe_run_db_maintenance(self) -> None:
        """Periodic DB maintenance: cleanup old data + WAL checkpoint."""
        now_ts = time.time()
        if self._last_db_maintenance_at <= 0:
            self._last_db_maintenance_at = now_ts
            return
        if (now_ts - self._last_db_maintenance_at) < self._db_maintenance_interval_seconds:
            return
        try:
            deleted = self.db.cleanup_old_data(days=90)
            total = sum(deleted.values())
            logger.info(
                "[Maintenance] DB cleanup removed %d row(s): %s", total,
                ", ".join(f"{k}={v}" for k, v in deleted.items()),
            )
            conn = self.db.get_connection()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
            logger.info("[Maintenance] WAL checkpoint (TRUNCATE) completed")
        except Exception as exc:
            logger.warning("[Maintenance] DB maintenance failed: %s", exc)
        self._last_db_maintenance_at = now_ts

        # Prune _executed_today to prevent unbounded growth
        if len(self._executed_today) > self._executed_today_max:
            self._executed_today = self._executed_today[-self._executed_today_max:]

    def _maybe_run_candle_retention_cleanup(self) -> None:
        if not self._candle_retention_enabled or not self._candle_retention_policy:
            return

        now_ts = time.time()
        if self._last_candle_retention_cleanup_at <= 0:
            self._last_candle_retention_cleanup_at = now_ts
            return

        if (now_ts - self._last_candle_retention_cleanup_at) < self._candle_retention_interval_seconds:
            return

        self._run_candle_retention_cleanup("scheduled")

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
        self._invalidate_portfolio_cache()
        self._reconcile_tracked_positions_with_balance_state(balance_state)

        if event.source == "crypto" and event.event_type == "DEPOSIT":
            asset = str(event.coin or "").upper()
            pair = f"THB_{asset}" if asset else ""
            tracked_position = self._find_tracked_position_by_symbol(pair)
            wallet_balance = self._extract_total_balance(balance_state, asset)

            if not tracked_position and pair:
                try:
                    active_pairs = {str(item).upper() for item in (self._get_trading_pairs() or [])}
                except Exception:
                    active_pairs = set()
                if pair in active_pairs and wallet_balance > 0:
                    try:
                        self._bootstrap_held_positions()
                    except Exception as exc:
                        logger.error("Failed to bootstrap deposited position %s: %s", pair, exc, exc_info=True)
                    tracked_position = self._find_tracked_position_by_symbol(pair)

            if tracked_position:
                entry_price = _coerce_trade_float(tracked_position.get("entry_price"), 0.0)
                if str(tracked_position.get("bootstrap_source") or "").strip():
                    message = (
                        f"External crypto deposit detected: {asset} +{event.amount:.8f} "
                        f"(wallet {wallet_balance:.8f}). {pair} was registered into Position Book at "
                        f"entry {entry_price:,.2f} via bootstrap tracking."
                    )
                else:
                    message = (
                        f"External crypto deposit detected: {asset} +{event.amount:.8f} "
                        f"(wallet {wallet_balance:.8f}). Tracked {pair} entry remains {entry_price:,.2f}; "
                        f"bot will not average-in this deposit automatically."
                    )
            else:
                message = (
                    f"External crypto deposit detected: {asset} +{event.amount:.8f} "
                    f"(wallet {wallet_balance:.8f}). No tracked {pair} position exists, so the deposit "
                    f"was not auto-converted into a managed bot position."
                )

            logger.warning(message)
            if self.send_alerts:
                self._send_alert(message, to_telegram=True)

        if event.coin == "THB":
            if event.event_type == "DEPOSIT":
                self._clear_pause_reason("balance-monitor-thb-withdrawal")
                logger.info("THB deposit detected | amount=%.2f | balance=%.2f", event.amount, event.balance)
            elif event.event_type.startswith("WITHDRAWAL"):
                self._set_pause_reason(
                    "balance-monitor-thb-withdrawal",
                    f"THB withdrawal detected ({event.amount:,.2f} THB)",
                )

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

        # ── HOTFIX FATAL-01: Ghost Orders Reconciliation ───────────────────
        # Before starting the main loop, forcefully query Bitkub API for
        # real open orders and balances. Use the authoritative remote data to
        # overwrite local SQLite state, preventing ghost orders after crash.
        if self._auth_degraded:
            logger.warning(
                "[Startup] Auth degraded mode active — skipping private Bitkub startup sync: %s",
                self._auth_degraded_reason or "private API unavailable",
            )
        else:
            self._reconcile_on_startup()

        # Sync OMS state from DB after reconciliation or directly in degraded mode.
        self.executor.sync_open_orders_from_db()
        if not self._auth_degraded:
            self._bootstrap_held_coin_history()
            self._bootstrap_held_positions()
        if self._state_machine_enabled:
            self._state_manager.sync_in_position_states(self.executor.get_open_orders())

        # ── H3/H4: Unblock OMS monitor after reconciliation is fully done ──
        # This MUST come after both _reconcile_on_startup() and
        # sync_open_orders_from_db() so the OMS always starts from a
        # Bitkub-authoritative, DB-consistent state.
        self.executor.set_reconcile_complete()

        balance_monitor = getattr(self, "_balance_monitor", None)
        if balance_monitor and not balance_monitor.running:
            balance_monitor.start()

        self.running = True
        self._loop_thread = threading.Thread(target=self._main_loop, daemon=True)
        self._loop_thread.start()
        logger.info("ระบบจัดการเทรดบอทกำลังเริ่มต้นการทำงาน")

    def _bootstrap_held_coin_history(self) -> None:
        self._get_startup_runtime_helper().bootstrap_held_coin_history()

    def _bootstrap_held_positions(self) -> None:
        self._get_startup_runtime_helper().bootstrap_held_positions()

    def _reconcile_on_startup(self):
        self._get_startup_runtime_helper().reconcile_on_startup()
    
    def stop(self):
        """Stop the trading bot gracefully."""
        logger.info("กำลังหยุดการทำงานของเทรดบอท...")
        self.running = False

        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=30)

        # Stop WebSocket
        if _WEBSOCKET_AVAILABLE and stop_websocket:
            try:
                stop_websocket()
                logger.info("หยุดการทำงานของ WebSocket สำเร็จ")
            except Exception as e:
                logger.warning(f"เกิดข้อผิดพลาดในการหยุด WebSocket: {e}")

        balance_monitor = getattr(self, "_balance_monitor", None)
        if balance_monitor:
            balance_monitor.stop()

        if self.executor:
            try:
                self.executor.stop()
            except Exception as e:
                logger.warning(f"เกิดข้อผิดพลาดในการหยุด OMS executor: {e}")

        logger.info("เทรดบอทหยุดการทำงานโดยสมบูรณ์")
    
    def _main_loop(self):
        """Main trading loop - runs every interval_seconds."""
        while self.running:
            try:
                self._last_loop_time = datetime.now()
                self._loop_count += 1
                self._maybe_run_candle_retention_cleanup()
                self._maybe_run_db_maintenance()
                
                logger.debug(f"Loop #{self._loop_count} started at {self._last_loop_time}")
                
                # Run one iteration
                self._run_iteration()

            except FatalAuthException as exc:
                logger.critical("🚨 GRACEFUL SHUTDOWN: %s", exc.message)
                alert_system = getattr(self, "alert_system", None)
                if alert_system is not None:
                    try:
                        title = "FATAL: Bitkub Auth Error 5" if getattr(exc, "code", None) == 5 else "FATAL: Bitkub Auth Error"
                        alert_system.send(
                            AlertLevel.CRITICAL,
                            format_fatal_auth_alert(exc.message, title=title),
                        )
                    except Exception as alert_exc:
                        logger.warning("Failed to send fatal auth alert: %s", alert_exc)
                logger.critical(
                    "หยุดการทำงานอย่างปลอดภัย — "
                    "กรุณาตรวจสอบ API Key/Secret ใน .env แล้วรีสตาร์ทบอท"
                )
                self.running = False
                break
                
            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
            
            # Sleep until next iteration
            elapsed = (datetime.now() - (self._last_loop_time or datetime.now())).total_seconds()
            sleep_time = max(1, self.interval_seconds - elapsed)
            time.sleep(sleep_time)
    
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
        normalized: List[str] = []
        seen: set[str] = set()
        for pair in pairs or []:
            value = str(pair or "").strip().upper()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)

        current_pairs = list(self.trading_pairs)
        if normalized == current_pairs:
            return current_pairs

        self.config.setdefault("data", {})["pairs"] = list(normalized)
        self.trading_pairs = list(normalized)
        self.trading_pair = normalized[0] if normalized else ""
        self.config["trading_pair"] = self.trading_pair
        self.config.setdefault("trading", {})["trading_pair"] = self.trading_pair

        if self._ws_enabled and _WEBSOCKET_AVAILABLE:
            try:
                if normalized:
                    ws = get_websocket(symbols=normalized, on_tick=self._on_ws_tick)  # type: ignore[misc]
                    if ws is not None:
                        self._ws_client = ws
                elif stop_websocket:
                    stop_websocket()
                    self._ws_client = None
            except Exception as exc:
                logger.error("Failed to refresh WebSocket subscriptions for %s: %s", normalized, exc)

        self._multi_timeframe_status_cache = {"data": None, "timestamp": 0.0}

        logger.info("[Pairs] Runtime pairs updated via %s: %s -> %s", reason, current_pairs, normalized)
        return normalized

    def _filter_pairs_by_candle_readiness(self, pairs: List[str], allow_refresh: bool = True) -> List[str]:
        if not pairs or not bool(getattr(self, "mtf_enabled", False)):
            self._last_candle_readiness_skipped = ()
            return list(pairs)

        try:
            mtf_status = self._get_dashboard_multi_timeframe_status(allow_refresh=allow_refresh) or {}
        except Exception as exc:
            logger.debug("Failed to evaluate candle readiness filter: %s", exc)
            return list(pairs)

        pair_rows = list(mtf_status.get("pairs") or [])
        if not pair_rows:
            self._last_candle_readiness_skipped = ()
            return list(pairs)

        ready_pairs = {
            str(row.get("pair") or "").upper()
            for row in pair_rows
            if row.get("ready")
        }
        filtered_pairs = [pair for pair in pairs if str(pair).upper() in ready_pairs]
        skipped = tuple(pair for pair in pairs if pair not in filtered_pairs)

        if skipped:
            if skipped != getattr(self, "_last_candle_readiness_skipped", None):
                logger.info("[Candle Guard] Skipping pairs without complete candle readiness: %s", list(skipped))
                self._last_candle_readiness_skipped = skipped
        else:
            self._last_candle_readiness_skipped = ()

        return filtered_pairs

    def _order_history_window_limit(self) -> int:
        config = getattr(self, "config", {}) or {}
        data_config = dict(config.get("data", {}) or {})
        raw_limit = data_config.get("order_history_limit", config.get("order_history_limit", 200))
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 200
        return max(50, min(limit, 500))

    def _lookup_order_history_status(self, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        """Fallback history lookup for an order when the info endpoint is inconclusive."""
        try:
            history = self.api_client.get_order_history(symbol, limit=self._order_history_window_limit())
        except Exception as e:
            logger.debug("[State] History lookup failed for %s: %s", order_id, e)
            return None

        for row in history:
            if str(row.get("id", "")) == str(order_id):
                return row
        return None

    @staticmethod
    def _history_status_value(row: Optional[Dict[str, Any]]) -> str:
        if not row:
            return ""
        return str(row.get("status") or row.get("typ") or "").lower()

    @staticmethod
    def _history_side_value(row: Optional[Dict[str, Any]]) -> str:
        if not row:
            return ""
        return normalize_side_value(row.get("side") or row.get("sd") or "")

    @classmethod
    def _history_status_is_filled(cls, row: Optional[Dict[str, Any]]) -> bool:
        return cls._history_status_value(row) in ("filled", "match", "done", "complete")

    @classmethod
    def _history_status_is_cancelled(cls, row: Optional[Dict[str, Any]]) -> bool:
        return cls._history_status_value(row) in ("cancel", "cancelled")

    def _extract_history_fill_details(
        self,
        row: Optional[Dict[str, Any]],
        *,
        fallback_amount: float = 0.0,
        fallback_price: float = 0.0,
        fallback_cost: float = 0.0,
    ) -> tuple[float, float]:
        if not row:
            return fallback_amount, fallback_price

        fill_price = (
            _coerce_trade_float(row.get("filled_price"))
            or _coerce_trade_float(row.get("avg_price"))
            or _coerce_trade_float(row.get("rate"))
            or _coerce_trade_float(row.get("rat"))
            or _coerce_trade_float(row.get("price"))
            or fallback_price
        )
        history_side = self._history_side_value(row)
        explicit_base_amount = (
            _coerce_trade_float(row.get("filled"))
            or _coerce_trade_float(row.get("filled_amount"))
            or _coerce_trade_float(row.get("executed_amount"))
            or _coerce_trade_float(row.get("executed"))
            or _coerce_trade_float(row.get("rec"))
        )
        if explicit_base_amount > 0:
            fill_amount = explicit_base_amount
        else:
            raw_amount = _coerce_trade_float(row.get("amount"))
            raw_cost = _coerce_trade_float(row.get("amt")) or raw_amount
            fee_value = _coerce_trade_float(row.get("fee"))
            if history_side in ("buy", "bid") and raw_cost > 0 and fill_price > 0:
                net_cost = raw_cost - fee_value if fee_value > 0 and raw_cost > fee_value else raw_cost
                fill_amount = net_cost / fill_price if net_cost > 0 else 0.0
            else:
                fill_amount = raw_amount or raw_cost or fallback_amount
        if fill_amount <= 0 and fill_price > 0 and fallback_cost > 0:
            fill_amount = fallback_cost / fill_price
        return fill_amount, fill_price

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
        if not getattr(self, "db", None) or quantity <= 0 or price <= 0:
            return
        logged_at = timestamp or datetime.utcnow()
        try:
            self.db.insert_order(
                pair=symbol,
                side=side,
                quantity=quantity,
                price=price,
                status="filled",
                order_type=order_type,
                fee=fee,
                timestamp=logged_at,
            )
        except Exception as exc:
            logger.error("[State] Failed to log filled %s order for %s: %s", side, symbol, exc, exc_info=True)
        try:
            self.db.insert_trade(
                pair=symbol,
                side=side,
                quantity=quantity,
                price=price,
                fee=fee,
                timestamp=logged_at,
            )
        except Exception as exc:
            logger.error("[State] Failed to log filled %s trade for %s: %s", side, symbol, exc, exc_info=True)

    # Startup and managed lifecycle facade wrappers.

    def _reconcile_pending_trade_states(self, remote_order_ids: set[str]) -> set[str]:
        return self._get_startup_runtime_helper().reconcile_pending_trade_states(remote_order_ids)

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
                websocket_available=_WEBSOCKET_AVAILABLE,
                price_tick_available=PriceTick is not None,
                latest_ticker_getter=get_latest_ticker,
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

    def _build_signal_runtime_deps(self) -> SignalRuntimeDeps:
        return SignalRuntimeDeps(
            get_portfolio_state=self._get_portfolio_state,
            get_mtf_signal_for_symbol=self._get_mtf_signal_for_symbol,
            state_machine_enabled=bool(getattr(self, "_state_machine_enabled", False)),
            state_manager=getattr(self, "_state_manager", None),
            last_state_gate_logged=self.__dict__.setdefault("_last_state_gate_logged", {}),
            get_market_data_for_symbol=self._get_market_data_for_symbol,
            risk_manager=getattr(self, "risk_manager", None),
            executed_today=self.__dict__.setdefault("_executed_today", []),
            signal_generator=self.signal_generator,
            database=self.db,
            is_reused_signal_trigger=self._is_reused_signal_trigger,
            get_signal_trigger_token=self._get_signal_trigger_token,
            allow_sell_entries_from_idle=bool(getattr(self, "_allow_sell_entries_from_idle", False)),
            create_execution_plan_for_symbol=self._create_execution_plan_for_symbol,
            signal_source=self.signal_source,
            mode=self.mode,
            process_full_auto=self._process_full_auto,
            process_semi_auto=self._process_semi_auto,
            process_dry_run=self._process_dry_run,
        )

    def _build_multi_timeframe_runtime_deps(self) -> MultiTimeframeRuntimeDeps:
        return MultiTimeframeRuntimeDeps(
            mtf_enabled=bool(getattr(self, "mtf_enabled", False)),
            signal_generator=self.signal_generator,
            mtf_timeframes=list(getattr(self, "mtf_timeframes", []) or []),
            database=self.db,
            last_mtf_status=self.__dict__.setdefault("_last_mtf_status", {}),
            serialize_mtf_signals_detail=SignalRuntimeHelper.serialize_mtf_signals_detail,
            merge_mtf_signals_detail=SignalRuntimeHelper.merge_mtf_signals_detail,
            mtf_confirmation_required=bool(getattr(self, "_mtf_confirmation_required", False)),
        )

    def _build_execution_plan_deps(self) -> ExecutionPlanDeps:
        return ExecutionPlanDeps(
            state_machine_enabled=bool(getattr(self, "_state_machine_enabled", False)),
            database=self.db,
            held_coins_only=bool(getattr(self, "_held_coins_only", False)),
            api_client=self.api_client,
            min_trade_value_thb=float(getattr(self, "min_trade_value_thb", 15.0) or 15.0),
            get_latest_atr=self._get_latest_atr,
            risk_manager=self.risk_manager,
            loop_count=int(getattr(self, "_loop_count", 0) or 0),
        )

    def _build_execution_runtime_deps(self) -> ExecutionRuntimeDeps:
        pending_lock = getattr(self, "_pending_decisions_lock", None)
        if pending_lock is None:
            pending_lock = threading.Lock()
            self._pending_decisions_lock = pending_lock
        return ExecutionRuntimeDeps(
            read_only=bool(getattr(self, "read_only", False)),
            send_alerts=bool(getattr(self, "send_alerts", False)),
            format_skip_alert=getattr(self, "_format_skip_alert", lambda *_args, **_kwargs: ""),
            send_alert=getattr(self, "_send_alert", lambda *_args, **_kwargs: None),
            send_pending_alert=getattr(self, "_send_pending_alert", lambda *_args, **_kwargs: None),
            send_dry_run_alert=getattr(self, "_send_dry_run_alert", lambda *_args, **_kwargs: None),
            state_machine_enabled=bool(getattr(self, "_state_machine_enabled", False)),
            allow_sell_entries_from_idle=bool(getattr(self, "_allow_sell_entries_from_idle", False)),
            state_manager=getattr(self, "_state_manager", None),
            risk_manager=getattr(self, "risk_manager", None),
            get_risk_portfolio_value=self._get_risk_portfolio_value,
            config=dict(getattr(self, "config", {}) or {}),
            executor=getattr(self, "executor", None),
            database=getattr(self, "db", None),
            timeframe=str(getattr(self, "timeframe", "1h") or "1h"),
            submit_managed_entry=getattr(self, "_submit_managed_entry", lambda *_args, **_kwargs: None),
            try_submit_managed_signal_sell=getattr(self, "_try_submit_managed_signal_sell", lambda *_args, **_kwargs: False),
            send_trade_alert=getattr(self, "_send_trade_alert", lambda *_args, **_kwargs: None),
            pending_decisions=self.__dict__.setdefault("_pending_decisions", []),
            pending_decisions_lock=pending_lock,
            get_portfolio_state=self._get_portfolio_state,
            auth_degraded=bool(getattr(self, "_auth_degraded", False)),
            mode=self.mode,
            executed_today=self.__dict__.setdefault("_executed_today", []),
        )
    
    def _run_iteration(self):
        """Run one iteration of the trading logic for all pairs."""
        if self._auth_degraded:
            if not self._auth_degraded_logged:
                logger.warning(
                    "Trading loop running in degraded public-only mode — skipping reconciliation, balances, and order execution until Bitkub credentials are fixed"
                )
                self._auth_degraded_logged = True
            return

        # ── Circuit Breaker gate ──────────────────────────────────────────
        if self.api_client.is_circuit_open():
            logger.warning(
                "Circuit breaker is OPEN — skipping iteration. "
                f"State: {self.api_client.circuit_breaker.state}"
            )
            return

        # ── Clock Sync check ──────────────────────────────────────────────
        if not self.api_client.check_clock_sync():
            logger.warning(
                f"Clock offset {self.api_client._clock.offset:+.1f}s > limit — "
                "skipping iteration"
            )
            return

        self._reconcile_tracked_positions_with_balance_state()

        # ── Reconciliation pause gate ─────────────────────────────────────
        paused, reason = self._is_paused()
        if paused:
            logger.warning(f"Trading PAUSED: {reason}")
            # Still run position monitoring but skip new trade entries
            self._check_positions_for_sl_tp()
            return

        # ── Telegram Kill Switch gate ────────────────────────────────────
        if self._trading_disabled.is_set():
            logger.warning("Trading disabled via kill switch — skipping new trades")
            self._check_positions_for_sl_tp()
            return

        if self._state_machine_enabled:
            self._state_manager.sync_in_position_states(self.executor.get_open_orders())
        
        # 0. Check open positions for SL/TP hits
        self._check_positions_for_sl_tp()
        
        self._advance_managed_trade_states()
        
        # Get trading pairs (supports multiple pairs)
        trading_pairs = self._get_trading_pairs()
        logger.debug(f"Processing {len(trading_pairs)} trading pair(s): {trading_pairs}")

        active_pairs = trading_pairs
        if self._held_coins_only:
            active_pairs = [
                pair for pair in trading_pairs
                if self.db.has_ever_held(pair)
            ]

            if len(active_pairs) < len(trading_pairs):
                skipped = [p for p in trading_pairs if p not in active_pairs]
                skipped_key = tuple(skipped)
                if skipped_key != self._last_portfolio_guard_skipped:
                    logger.info(f"🛡️  [Portfolio Guard] Skipping never-held pairs: {skipped}")
                    self._last_portfolio_guard_skipped = skipped_key
            else:
                self._last_portfolio_guard_skipped = ()
        else:
            self._last_portfolio_guard_skipped = ()

        active_pairs = self._filter_pairs_by_candle_readiness(active_pairs, allow_refresh=True)

        logger.debug(f"Actual pairs to process: {active_pairs}")

        for current_pair in active_pairs:
            self._process_pair_iteration(current_pair)
    
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
        return SignalRuntimeHelper.get_mtf_signal_for_symbol(self._build_multi_timeframe_runtime_deps(), symbol, portfolio)

    def _apply_multi_timeframe_confirmation(
        self,
        symbol: str,
        signals: List[AggregatedSignal],
        mtf_signal,
    ) -> List[AggregatedSignal]:
        return SignalRuntimeHelper.apply_multi_timeframe_confirmation(self._build_multi_timeframe_runtime_deps(), symbol, signals, mtf_signal)
    
    def _create_execution_plan_for_symbol(self, signal: AggregatedSignal, symbol: str) -> Optional[ExecutionPlan]:
        return SignalRuntimeHelper.create_execution_plan_for_symbol(self._build_execution_plan_deps(), signal, symbol)
    
    # ── WebSocket Real-time Price Handler ───────────────────────────────────

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
        if cache.get("data") is not None and ((now - float(cache.get("timestamp", 0.0) or 0.0)) < cache_ttl or not allow_refresh):
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
                required_candles=_MIN_CANDLES_FOR_TRADING_READINESS,
                websocket_available=_WEBSOCKET_AVAILABLE,
                latest_ticker_getter=get_latest_ticker,
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
        self, symbol: str, old_sl: float, new_sl: float,
        current_price: float, profit_pct: float
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
        coin = symbol.replace("THB_", "")
        msg = (
            f"Trailing SL  {coin}  {old_sl:,.0f} → {new_sl:,.0f}  "
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
        trigger_value = str(trigger or "").upper()
        if trigger_value not in {"SIGSELL", "TIME"}:
            return True
        if not bool(getattr(self, "_enforce_min_profit_gate_for_voluntary_exit", False)):
            return True

        amount_value = float(amount or 0.0)
        entry_value = float(entry_price or 0.0)
        exit_value = float(exit_price or 0.0)
        entry_cost = float(total_entry_cost or 0.0)
        if amount_value <= 0 or entry_value <= 0 or exit_value <= 0:
            return True
        if entry_cost <= 0:
            entry_cost = entry_value * amount_value
        if entry_cost <= 0:
            return True

        from trade_executor import BITKUB_FEE_PCT
        side_value = str(getattr(side, "value", side) or "buy").lower()

        pnl = calc_net_pnl(
            entry_cost=entry_cost,
            exit_price=exit_value,
            quantity=amount_value,
            side=side_value,
            fee_pct=BITKUB_FEE_PCT,
        )
        net_pnl_pct = float(pnl.get("net_pnl_pct", 0.0) or 0.0)
        min_net_profit_pct = float(getattr(self, "_min_voluntary_exit_net_profit_pct", 0.0) or 0.0)
        if net_pnl_pct >= min_net_profit_pct:
            return True

        logger.info(
            "[ExitGate] Suppressed %s exit for %s | entry=%.2f exit=%.2f amount=%.8f | net_pnl_pct=%.3f < %.3f",
            trigger_value,
            str(symbol or "").upper(),
            entry_value,
            exit_value,
            amount_value,
            net_pnl_pct,
            min_net_profit_pct,
        )
        return False
