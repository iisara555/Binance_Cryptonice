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
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Dict, Any, List

from alerts import AlertSystem, AlertLevel  # Unified alert system
from enum import Enum
import os

from signal_generator import SignalGenerator, AggregatedSignal, SignalRiskCheck
from trade_executor import TradeExecutor, ExecutionPlan, OrderSide, OrderResult, OrderStatus
from risk_management import RiskManager, RiskConfig, calculate_atr, get_default_sl_tp, check_pair_correlation
from api_client import BitkubClient
from balance_monitor import BalanceEvent, BalanceMonitor
from database import get_database
from backtesting_validation import BacktestingValidator
from state_management import TradeLifecycleState, TradeStateManager, TradeStateSnapshot, normalize_buy_quantity
from strategy_base import MarketCondition, detect_market_condition

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
    _MONITORING_AVAILABLE = False

logger = logging.getLogger(__name__)


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
    from bitkub_websocket import BitkubWebSocket, get_websocket, stop_websocket, PriceTick, get_latest_ticker
    _WEBSOCKET_AVAILABLE = True
except ImportError:
    _WEBSOCKET_AVAILABLE = False
    BitkubWebSocket = None
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
        self.trading_pair = (
            config.get("trading", {}).get("trading_pair")
            or config.get("trading_pair")
            or ""
        )
        self.trading_pairs = self._get_trading_pairs()
        self.interval_seconds = config.get("interval_seconds", 60)
        self.timeframe = config.get("timeframe", "1h")
        self.read_only = config.get("read_only", False) or os.environ.get("BOT_READ_ONLY", "").lower() in ("1", "true", "yes")
        self._auth_degraded = bool(config.get("auth_degraded", False))
        self._auth_degraded_reason = str(config.get("auth_degraded_reason") or "")
        self._auth_degraded_logged = False
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
        self._last_loop_time: Optional[datetime] = None
        self._loop_count = 0
        self._last_state_gate_logged: Dict[str, str] = {}

        # === Monitoring Service ===
        self._monitoring: Optional[_MonitoringServiceType] = None
        self._monitoring_start_time = datetime.now()
        self._init_monitoring(config)

        # === Reconciliation pause flag ===
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
        self._market_data_cache = {"data": None, "timestamp": 0.0}
        self._atr_cache = {"value": None, "timestamp": 0.0}
        self._symbol_market_cache: Dict[str, Dict] = {}
        self._last_portfolio_guard_skipped: Optional[tuple[str, ...]] = None

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
        if getattr(self, "_trading_paused", False):
            return True, getattr(self, "_pause_reason", "")
        monitoring = getattr(self, "_monitoring", None)
        if monitoring and hasattr(monitoring, "_reconciler"):
            return monitoring._reconciler.is_paused()
        return False, ""

    def _set_pause_reason(self, key: str, reason: str) -> None:
        self._pause_reasons[str(key)] = str(reason)
        self._trading_paused = True
        self._pause_reason = " | ".join(self._pause_reasons.values())
        logger.warning("Trading PAUSED: %s", self._pause_reason)

    def _clear_pause_reason(self, key: str) -> None:
        self._pause_reasons.pop(str(key), None)
        self._trading_paused = bool(self._pause_reasons)
        self._pause_reason = " | ".join(self._pause_reasons.values())
        if not self._trading_paused:
            logger.info("Trading RESUMED - auto pause cleared")

    def _invalidate_portfolio_cache(self) -> None:
        self._portfolio_cache = {"data": None, "timestamp": 0.0}

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
        """Backfill held-coin history from live balances and tracked open orders."""
        tracked_pairs = [pair.upper() for pair in self._get_trading_pairs()]
        if not tracked_pairs:
            return

        try:
            balances = self.api_client.get_balances()
        except Exception as e:
            logger.debug("[Portfolio Guard] balance bootstrap skipped: %s", e)
            balances = {}

        tracked_open_orders = {
            str(order.get("symbol", "")).upper()
            for order in self.executor.get_open_orders()
            if order.get("symbol")
        }
        backfilled: list[str] = []

        for pair in tracked_pairs:
            if self.db.has_ever_held(pair):
                continue

            base_asset = pair.split("_", 1)[1] if "_" in pair else pair
            balance_data = balances.get(base_asset.upper(), {}) if isinstance(balances, dict) else {}
            available_qty = float(balance_data.get("available", 0) or 0)

            if available_qty > 0 or pair in tracked_open_orders:
                self.db.record_held_coin(pair, available_qty if available_qty > 0 else 0.0)
                backfilled.append(pair)

        if backfilled:
            logger.info(
                "🛡️  [Portfolio Guard] Backfilled held-coin history from live state: %s",
                backfilled,
            )

    def _reconcile_on_startup(self):
        """
        HOTFIX FATAL-01: Ghost Orders Reconciliation on boot.

        Scenario this fixes:
          1. Bot places a BUY order on Bitkub → order fills.
          2. Bot crashes BEFORE persisting the fill to SQLite.
          3. On reboot, local DB thinks the position is "pending" or missing.
          4. Without reconciliation, bot may try to place the same order again
             or miss an existing open position.

        Steps:
          1. Query Bitkub GET /api/v3/market/my-open-orders for ALL symbols.
          2. Query Bitkub GET /api/v3/market/balances for real wallet state.
          3. Compare against local SQLite positions.
             - Remote order with no local record → add to local tracking
             - Local record with no remote order (filled while bot was down)
               → mark as needing re-lookup via order history or mark closed
          4. Overwrite in-memory _open_orders with Bitkub-authoritative state.

        This runs BEFORE the main loop begins, so all downstream logic
        (SL/TP checks, risk manager, etc.) works on correct state.
        """
        logger.info("╔══════════════════════════════════════════════════════╗")
        logger.info("║  🔍 RECONCILIATION: Querying Bitkub for true state  ║")
        logger.info("╚══════════════════════════════════════════════════════╝")

        reconciled_count = 0
        ghost_orders = []

        try:
            # ── Step 1: Get ALL open orders from Bitkub ──────────────────
            # We query each tracked symbol; the API doesn't support wildcard.
            symbols_to_check = [p.upper() for p in self._get_trading_pairs()]

            all_remote_orders = []
            for sym in symbols_to_check:
                try:
                    orders = self.api_client.get_open_orders(sym)
                    if orders:
                        for o in orders:
                            o["_checked_symbol"] = sym  # tag for debugging
                        all_remote_orders.extend(orders)
                    time.sleep(0.2)  # rate-limit between queries
                except Exception as e:
                    logger.warning(f"[Reconcile] Failed to get open orders for {sym}: {e}", exc_info=True)

            logger.info(f"[Reconcile] Bitkub reported {len(all_remote_orders)} open order(s)")

            # ── Step 2: Convert remote orders to local tracking format ───
            remote_order_ids = set()
            for o in all_remote_orders:
                oid = str(o.get("id", ""))
                if not oid:
                    continue
                remote_order_ids.add(oid)

                # Decode side from order type field
                typ = o.get("typ", o.get("side", "")).lower()
                side_enum = OrderSide.BUY if typ in ("bid", "buy") else OrderSide.SELL

                # Normalise symbol back to THB_X format
                raw_sym = str(o.get("sym") or "")
                if "_" in raw_sym:
                    parts = raw_sym.lower().split("_")
                    # parts[0]=btc, parts[1]=thb → THB_BTC
                    local_sym = f"THB_{parts[0].upper()}"
                else:
                    local_sym = str(o.get("_checked_symbol") or self.trading_pair).upper()

                # Determine entry price, amount, SL, TP from order data
                entry_price = float(o.get("rate") or o.get("rat", 0)) or 0.0
                amount      = float(o.get("amount") or o.get("amt", 0)) or 0.0
                remaining   = float(o.get("unfilled") or o.get("rem", 0)) or amount  # remaining amount

                # Check if we already track this order locally
                existing = self.executor._open_orders.get(oid)
                if existing:
                    # Local record exists — just sync the remaining amount
                    with self.executor._orders_lock:
                        self.executor._open_orders[oid]["remaining_amount"] = remaining
                    logger.debug(f"[Reconcile] Order {oid} synced (local ↔ remote)")
                else:
                    # ═══ GHOST ORDER DETECTED ═══
                    # Remote order exists but NOT in local tracking.
                    # This means the bot crashed after placing the order but
                    # before persisting to SQLite.
                    ghost_orders.append({
                        "order_id": oid,
                        "symbol": local_sym,
                        "side": side_enum,
                        "amount": amount,
                        "entry_price": entry_price,
                        "remaining": remaining,
                        "type": typ,
                    })

            # ── Step 3: Add ghost orders to local tracking ───────────────
            imported_ghost_counts: Counter[tuple[str, str]] = Counter()
            skipped_ghost_counts: Counter[tuple[str, str]] = Counter()
            for ghost in ghost_orders:
                local_sym = str(ghost.get("symbol", "")).upper()
                side_value = ghost.get("side")
                side_str = side_value.value if isinstance(side_value, Enum) else str(side_value).lower()
                amount = float(ghost.get("amount", 0.0) or 0.0)
                # Sanity check: only SELL orders represent BTC quantity here.
                if local_sym in ("THB_BTC", "BTC_THB") and side_str == "sell" and amount > 1.0:
                    skipped_ghost_counts[(local_sym, side_str)] += 1
                    logger.warning(
                        "[Reconcile] Skipping ghost order %s — "
                        "amount=%.8f looks wrong for %s sell order (expected BTC < 1.0)",
                        ghost.get("order_id"),
                        amount,
                        local_sym,
                    )
                    continue

                oid = ghost["order_id"]
                logger.warning(
                    f"👻 [Ghost Order] {oid} found on Bitkub but NOT in local DB! "
                    f"Adding to tracking: {ghost['side'].value.upper()} "
                    f"{ghost['symbol']} {ghost['amount']:.8f} @ {ghost['entry_price']:,.2f}"
                )

                # If order is partially filled on Bitkub side, it won't show as
                # a "pending" order — it'll show as "partial". We still track it.
                with self.executor._orders_lock:
                    self.executor._open_orders[oid] = {
                        "symbol": ghost["symbol"],
                        "side": ghost["side"],
                        "amount": ghost["amount"],
                        "entry_price": ghost["entry_price"],
                        "stop_loss": None,     # Unknown — set conservatively
                        "take_profit": None,   # Unknown — will be computed on next signal
                        "order_id": oid,
                        "timestamp": datetime.now(),
                        "is_partial_fill": ghost["remaining"] < ghost["amount"],
                        "remaining_amount": ghost["remaining"],
                        "total_entry_cost": ghost["entry_price"] * ghost["amount"],
                        "filled": ghost["remaining"] < ghost["amount"],
                    }
                # Persist to DB so it survives another crash
                try:
                    self.db.save_position(self.executor._open_orders[oid])
                except Exception as e:
                    logger.error(f"[Reconcile] Failed to persist ghost order {oid}: {e}")
                imported_ghost_counts[(local_sym, side_str)] += 1
                reconciled_count += 1

            if imported_ghost_counts:
                summary = ", ".join(
                    f"{side.upper()} {symbol} x{count}"
                    for (symbol, side), count in sorted(imported_ghost_counts.items())
                )
                logger.warning(f"[Reconcile] Ghost orders imported summary: {summary}")
            if skipped_ghost_counts:
                summary = ", ".join(
                    f"{side.upper()} {symbol} x{count}"
                    for (symbol, side), count in sorted(skipped_ghost_counts.items())
                )
                logger.warning(f"[Reconcile] Ghost orders skipped by sanity check: {summary}")

            handled_order_ids = self._reconcile_pending_trade_states(remote_order_ids)

            # ── Step 4: Check local orders that are NOT on Bitkub anymore ──
            # These were either: (a) filled while bot was down, or (b) cancelled
            local_order_ids = set(self.executor._open_orders.keys()) - handled_order_ids
            vanished_ids = local_order_ids - remote_order_ids

            if vanished_ids:
                logger.info(f"[Reconcile] {len(vanished_ids)} local order(s) not on Bitkub "
                            f"— checking if they were filled while bot was down")

            for missing_oid in vanished_ids:
                local_pos = self.executor._open_orders.get(missing_oid)
                if not local_pos:
                    continue

                sym = local_pos.get("symbol", self.trading_pair)
                side_enum = local_pos.get("side", OrderSide.BUY)

                # Query order history to check final status
                try:
                    history = self.api_client.get_order_history(sym, limit=50)
                    matched = None
                    for h in history:
                        hist_id = str(h.get("id", ""))
                        if hist_id == missing_oid:
                            matched = h
                            break

                    if matched:
                        status_str = self._history_status_value(matched)
                        if self._history_status_is_filled(matched):
                            logger.info(
                                f"✅ [Reconcile] Order {missing_oid} was FILLED while bot was down. "
                                f"Status: {status_str}"
                            )
                            side_val = side_enum.value if isinstance(side_enum, Enum) else str(side_enum).lower()
                            if side_val == "buy":
                                fallback_cost = _coerce_trade_float(local_pos.get("total_entry_cost"))
                                filled_amount, filled_price = self._extract_history_fill_details(
                                    matched,
                                    fallback_amount=_coerce_trade_float(local_pos.get("filled_amount")) or _coerce_trade_float(local_pos.get("amount")),
                                    fallback_price=_coerce_trade_float(local_pos.get("filled_price")) or _coerce_trade_float(local_pos.get("entry_price")),
                                    fallback_cost=fallback_cost,
                                )
                                if filled_amount > 0 and filled_price > 0:
                                    restored_position = dict(local_pos)
                                    restored_position.update({
                                        "symbol": sym,
                                        "side": OrderSide.BUY,
                                        "amount": filled_amount,
                                        "entry_price": filled_price,
                                        "timestamp": local_pos.get("timestamp") or datetime.now(),
                                        "is_partial_fill": False,
                                        "remaining_amount": 0.0,
                                        "total_entry_cost": fallback_cost or (filled_amount * filled_price),
                                        "filled": True,
                                        "filled_amount": filled_amount,
                                        "filled_price": filled_price,
                                    })
                                    self.executor.register_tracked_position(missing_oid, restored_position)
                                    self._log_filled_order(
                                        sym,
                                        "buy",
                                        filled_amount,
                                        filled_price,
                                        timestamp=local_pos.get("timestamp") or datetime.utcnow(),
                                    )
                                    logger.info(
                                        "[Reconcile] Restored filled BUY %s as tracked position %.8f @ %.2f",
                                        missing_oid,
                                        filled_amount,
                                        filled_price,
                                    )
                                else:
                                    logger.warning("[Reconcile] Filled BUY %s unresolved; leaving local tracking unchanged", missing_oid)
                            else:
                                with self.executor._orders_lock:
                                    self.executor._open_orders.pop(missing_oid, None)
                                try:
                                    self.db.delete_position(missing_oid)
                                except Exception:
                                    pass
                        elif self._history_status_is_cancelled(matched):
                            logger.info(
                                f"🗑️ [Reconcile] Order {missing_oid} was CANCELLED on Bitkub. "
                                f"Removing from local tracking"
                            )
                            with self.executor._orders_lock:
                                self.executor._open_orders.pop(missing_oid, None)
                            try:
                                self.db.delete_position(missing_oid)
                            except Exception:
                                pass
                        else:
                            logger.warning(
                                f"[Reconcile] Order {missing_oid} has unusual status '{status_str}' "
                                f"— keeping in local tracking for now"
                            )
                    else:
                        # Not in order history either — could be very old or expired
                        logger.warning(
                            f"[Reconcile] Order {missing_oid} not found in Bitkub or history. "
                            f"Removing from local tracking (likely stale)"
                        )
                        with self.executor._orders_lock:
                            self.executor._open_orders.pop(missing_oid, None)
                        try:
                            self.db.delete_position(missing_oid)
                        except Exception:
                            pass

                except Exception as e:
                    logger.error(f"[Reconcile] Failed to check history for {missing_oid}: {e}")

            # ── Step 5: Final state summary ──────────────────────────────
            final_count = len(self.executor._open_orders)
            logger.info(
                f"╔══════════════════════════════════════════════════════╗\n"
                f"║  ✅ RECONCILIATION COMPLETE                        ║\n"
                f"║     Ghost orders added:  {reconciled_count}\n"
                f"║     Orders removed:      {len(vanished_ids) if vanished_ids else 0}\n"
                f"║     Active positions:    {final_count}\n"
                f"╚══════════════════════════════════════════════════════╝"
            )

        except Exception as e:
            logger.error(f"[Reconcile] Reconciliation failed: {e}", exc_info=True)
            logger.warning("[Reconcile] Proceeding with local state only — may have stale data!")
    
    def stop(self):
        """Stop the trading bot gracefully."""
        logger.info("กำลังหยุดการทำงานของเทรดบอท...")
        self.running = False

        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=10)

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

        logger.info("เทรดบอทหยุดการทำงานโดยสมบูรณ์")
    
    def _main_loop(self):
        """Main trading loop - runs every interval_seconds."""
        while self.running:
            # ── Fatal Shutdown Gate ────────────────────────────────────────
            # Check global shutdown flag (set by api_client on Error 5 etc.)
            import api_client as _ac
            if _ac.SHOULD_SHUTDOWN:
                logger.critical(
                    f"🚨 GRACEFUL SHUTDOWN: {_ac.SHUTDOWN_REASON}"
                )
                logger.critical(
                    "หยุดการทำงานอย่างปลอดภัย — "
                    "กรุณาตรวจสอบ API Key/Secret ใน .env แล้วรีสตาร์ทบอท"
                )
                self.running = False
                break

            try:
                self._last_loop_time = datetime.now()
                self._loop_count += 1
                
                logger.debug(f"Loop #{self._loop_count} started at {self._last_loop_time}")
                
                # Run one iteration
                self._run_iteration()
                
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

        logger.info("[Pairs] Runtime pairs updated via %s: %s -> %s", reason, current_pairs, normalized)
        return normalized

    def _lookup_order_history_status(self, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        """Fallback history lookup for an order when the info endpoint is inconclusive."""
        try:
            history = self.api_client.get_order_history(symbol, limit=50)
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
        fill_amount = (
            _coerce_trade_float(row.get("filled"))
            or _coerce_trade_float(row.get("filled_amount"))
            or _coerce_trade_float(row.get("executed_amount"))
            or _coerce_trade_float(row.get("executed"))
            or _coerce_trade_float(row.get("amount"))
            or _coerce_trade_float(row.get("amt"))
            or fallback_amount
        )
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
        try:
            self.db.insert_order(
                pair=symbol,
                side=side,
                quantity=quantity,
                price=price,
                status="filled",
                order_type=order_type,
                fee=fee,
                timestamp=timestamp or datetime.utcnow(),
            )
        except Exception as exc:
            logger.error("[State] Failed to log filled %s order for %s: %s", side, symbol, exc, exc_info=True)

    def _reconcile_pending_trade_states(self, remote_order_ids: set[str]) -> set[str]:
        handled_order_ids: set[str] = set()
        if not getattr(self, "_state_machine_enabled", False):
            return handled_order_ids
        state_manager = getattr(self, "_state_manager", None)
        if state_manager is None:
            return handled_order_ids

        for snapshot in list(state_manager.list_active_states()):
            if snapshot.state == TradeLifecycleState.PENDING_BUY:
                tracked_order_id = snapshot.entry_order_id
            elif snapshot.state == TradeLifecycleState.PENDING_SELL:
                tracked_order_id = snapshot.exit_order_id
            else:
                continue

            if not tracked_order_id or tracked_order_id in remote_order_ids:
                continue

            hist = self._lookup_order_history_status(snapshot.symbol, tracked_order_id)
            if self._history_status_is_filled(hist):
                if snapshot.state == TradeLifecycleState.PENDING_BUY:
                    filled_amount, filled_price = self._extract_history_fill_details(
                        hist,
                        fallback_amount=snapshot.filled_amount,
                        fallback_price=snapshot.entry_price,
                        fallback_cost=snapshot.total_entry_cost,
                    )
                    if filled_amount <= 0 or filled_price <= 0:
                        logger.warning("[Reconcile] Filled BUY for %s detected but amount/price unresolved", snapshot.symbol)
                        continue
                    self._register_filled_position_from_state(snapshot, filled_amount, filled_price)
                    state_manager.mark_entry_filled(snapshot.symbol, filled_amount, filled_price)
                    self.db.record_held_coin(snapshot.symbol, filled_amount)
                    if self.risk_manager:
                        self.risk_manager.record_trade()
                    logger.info(
                        "[Reconcile] Pending BUY %s filled while offline -> restored in_position %.8f @ %.2f",
                        snapshot.symbol,
                        filled_amount,
                        filled_price,
                    )
                else:
                    _, exit_price = self._extract_history_fill_details(
                        hist,
                        fallback_amount=snapshot.filled_amount,
                        fallback_price=snapshot.exit_price or snapshot.entry_price,
                    )
                    completed = state_manager.complete_exit(snapshot.symbol, exit_price or snapshot.exit_price or snapshot.entry_price)
                    self._report_completed_exit(completed, exit_price or snapshot.exit_price or snapshot.entry_price, "reconcile")
                    logger.info("[Reconcile] Pending SELL %s filled while offline -> closed trade logged", snapshot.symbol)
                handled_order_ids.add(tracked_order_id)
                continue

            if self._history_status_is_cancelled(hist):
                if snapshot.state == TradeLifecycleState.PENDING_BUY:
                    state_manager.cancel_pending_buy(snapshot.symbol, "buy cancelled during downtime")
                    logger.info("[Reconcile] Pending BUY %s cancelled while offline", snapshot.symbol)
                else:
                    restore_position = {
                        "symbol": snapshot.symbol,
                        "side": OrderSide.BUY,
                        "amount": snapshot.filled_amount,
                        "entry_price": snapshot.entry_price,
                        "stop_loss": snapshot.stop_loss,
                        "take_profit": snapshot.take_profit,
                        "timestamp": snapshot.opened_at or datetime.now(),
                        "is_partial_fill": False,
                        "remaining_amount": 0.0,
                        "total_entry_cost": snapshot.total_entry_cost,
                        "filled": True,
                        "filled_amount": snapshot.filled_amount,
                        "filled_price": snapshot.entry_price,
                        "state_managed": True,
                    }
                    self.executor.register_tracked_position(snapshot.entry_order_id, restore_position)
                    state_manager.restore_in_position(snapshot.symbol, "sell cancelled during downtime")
                    logger.info("[Reconcile] Pending SELL %s cancelled while offline -> restored in_position", snapshot.symbol)
                handled_order_ids.add(tracked_order_id)

        return handled_order_ids

    def _resolve_fill_amount(
        self,
        snapshot: TradeStateSnapshot,
        result: OrderResult,
        fallback_price: float,
    ) -> tuple[float, float]:
        """Best-effort fill normalization for Bitkub responses with missing filled quantity."""
        fill_price = float(result.filled_price or fallback_price or snapshot.entry_price or snapshot.exit_price or 0.0)
        fill_amount = float(result.filled_amount or 0.0)
        if fill_amount > 0 and snapshot.state == TradeLifecycleState.PENDING_BUY:
            total_entry_cost = float(snapshot.total_entry_cost or result.ordered_amount or 0.0)
            normalized_fill_amount = normalize_buy_quantity(fill_amount, fill_price, total_entry_cost)
            if normalized_fill_amount != fill_amount:
                return normalized_fill_amount, fill_price

        if fill_amount > 0:
            return fill_amount, fill_price

        if snapshot.state == TradeLifecycleState.PENDING_BUY:
            if fill_price > 0 and snapshot.total_entry_cost > 0:
                return snapshot.total_entry_cost / fill_price, fill_price
            return 0.0, fill_price

        return float(snapshot.filled_amount or 0.0), fill_price

    def _register_filled_position_from_state(
        self,
        snapshot: TradeStateSnapshot,
        filled_amount: float,
        filled_price: float,
    ) -> None:
        """Create a tracked IN_POSITION row once a pending BUY is confirmed filled."""
        pos_data = {
            "symbol": snapshot.symbol,
            "side": OrderSide.BUY,
            "amount": filled_amount,
            "entry_price": filled_price,
            "stop_loss": snapshot.stop_loss,
            "take_profit": snapshot.take_profit,
            "timestamp": snapshot.opened_at or datetime.now(),
            "is_partial_fill": False,
            "remaining_amount": 0.0,
            "total_entry_cost": snapshot.total_entry_cost,
            "filled": True,
            "filled_amount": filled_amount,
            "filled_price": filled_price,
            "state_managed": True,
        }
        self.executor.register_tracked_position(snapshot.entry_order_id, pos_data)
        self._log_filled_order(
            snapshot.symbol,
            "buy",
            filled_amount,
            filled_price,
            timestamp=snapshot.opened_at or datetime.utcnow(),
        )

    def _report_completed_exit(
        self,
        snapshot: TradeStateSnapshot,
        exit_price: float,
        price_source: str,
    ) -> None:
        """Persist and notify net PnL only after the exit order is fully matched."""
        from trade_executor import BITKUB_FEE_PCT

        amount = float(snapshot.filled_amount or 0.0)
        if amount <= 0:
            logger.warning("[State] Exit report skipped for %s: filled amount is 0", snapshot.symbol)
            return

        entry_cost = float(snapshot.total_entry_cost or (snapshot.entry_price * amount) or 0.0)
        entry_fee = entry_cost * BITKUB_FEE_PCT
        gross_exit = exit_price * amount
        exit_fee = gross_exit * BITKUB_FEE_PCT
        net_exit = gross_exit - exit_fee
        total_fees = entry_fee + exit_fee
        net_pnl = net_exit - entry_cost
        net_pnl_pct = (net_pnl / entry_cost * 100) if entry_cost > 0 else 0.0
        now = datetime.now()

        self._log_filled_order(
            snapshot.symbol,
            "sell",
            amount,
            exit_price,
            fee=exit_fee,
            timestamp=now,
        )

        try:
            self.db.log_closed_trade({
                "symbol": snapshot.symbol,
                "side": "buy",
                "amount": amount,
                "entry_price": snapshot.entry_price,
                "exit_price": exit_price,
                "entry_cost": entry_cost,
                "gross_exit": gross_exit,
                "entry_fee": entry_fee,
                "exit_fee": exit_fee,
                "total_fees": total_fees,
                "net_pnl": net_pnl,
                "net_pnl_pct": net_pnl_pct,
                "trigger": snapshot.trigger,
                "price_source": price_source,
                "opened_at": snapshot.opened_at,
                "closed_at": now,
            })
        except Exception as e:
            logger.error("[State] Failed to log closed trade for %s: %s", snapshot.symbol, e, exc_info=True)

        trigger_label = "Take Profit" if snapshot.trigger == "TP" else ("Stop Loss" if snapshot.trigger == "SL" else (snapshot.trigger or "Exit"))
        msg = self._format_exit_alert(
            snapshot.symbol,
            trigger_label,
            snapshot.entry_price,
            exit_price,
            net_pnl,
            net_pnl_pct,
            total_fees,
            now=now,
        )
        self._send_alert(msg, to_telegram=True)

    def _submit_managed_entry(self, decision: TradeDecision, portfolio: Dict[str, Any]) -> None:
        """Submit a BUY order into PENDING_BUY instead of opening a position immediately."""
        result = self.executor.execute_entry(
            decision.plan,
            portfolio["balance"],
            defer_position_tracking=True,
        )
        if not result.success or not result.order_id:
            decision.status = "failed"
            logger.error("Trade execution failed: %s", result.message)
            return

        snapshot = self._state_manager.start_pending_buy(
            decision.plan.symbol,
            decision.plan,
            result,
            signal_source=self.signal_source.value,
        )
        decision.status = snapshot.state.value
        self._executed_today.append({
            "decision": decision,
            "result": result,
            "timestamp": datetime.now(),
        })
        coin = self._format_coin_symbol(decision.plan.symbol)
        msg = self._format_alert_block(
            f"📥 <b>ส่งคำสั่งซื้อ</b>  {coin}",
            [
                f"ราคา  <code>{decision.plan.entry_price:,.0f}</code> THB  ({decision.plan.confidence:.0%})",
                f"SL <code>{decision.plan.stop_loss:,.0f}</code>  TP <code>{decision.plan.take_profit:,.0f}</code>",
            ],
        )
        self._send_alert(msg, to_telegram=False)

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
        """Submit an exit order and move the symbol into PENDING_SELL."""
        if not self._state_machine_enabled:
            return False

        snapshot = self._state_manager.get_state(pos_symbol)
        if snapshot.state != TradeLifecycleState.IN_POSITION:
            logger.debug(
                "[State] Skip managed exit for %s because lifecycle=%s",
                pos_symbol,
                snapshot.state.value,
            )
            return False

        exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
        result = self.executor.execute_exit(
            position_id=position_id,
            order_id=position_id,
            side=exit_side,
            amount=amount,
            price=exit_price,
            defer_cleanup=True,
        )
        if not result.success or not result.order_id:
            logger.error("Failed to submit %s exit for %s: %s", triggered, pos_symbol, result.message)
            return False

        position_data = {
            "order_id": position_id,
            "symbol": pos_symbol,
            "amount": amount,
            "entry_price": entry_price,
            "stop_loss": snapshot.stop_loss,
            "take_profit": snapshot.take_profit,
            "timestamp": opened_at or datetime.now(),
            "total_entry_cost": total_entry_cost,
        }
        self.executor.remove_tracked_position(position_id)
        pending = self._state_manager.start_pending_sell(
            pos_symbol,
            position_data,
            exit_order_id=result.order_id,
            trigger=triggered,
            exit_price=exit_price,
            notes=f"price_source={price_source}",
        )

        if result.status == OrderStatus.FILLED:
            completed = self._state_manager.complete_exit(
                pos_symbol,
                exit_price=float(result.filled_price or exit_price or 0.0),
            )
            self._report_completed_exit(completed, float(result.filled_price or exit_price or 0.0), price_source)
            return True

        logger.info(
            "[State] %s -> %s | exit order submitted %s for %s",
            pos_symbol,
            pending.state.value,
            result.order_id,
            triggered,
        )
        return True

    def _advance_managed_trade_states(self) -> None:
        """Advance PENDING_BUY/PENDING_SELL via Bitkub order polling."""
        if not self._state_machine_enabled:
            return

        self._state_manager.sync_in_position_states(self.executor.get_open_orders())
        for snapshot in list(self._state_manager.list_active_states()):
            try:
                if snapshot.state == TradeLifecycleState.PENDING_BUY:
                    status = self.executor.check_order_status(
                        snapshot.entry_order_id,
                        symbol=snapshot.symbol,
                        side="buy",
                    )
                    if status.status == OrderStatus.ERROR:
                        hist = self._lookup_order_history_status(snapshot.symbol, snapshot.entry_order_id)
                        hist_status = str((hist or {}).get("status") or "").lower()
                        if hist_status in ("filled", "match", "done", "complete"):
                            status = OrderResult(
                                success=True,
                                status=OrderStatus.FILLED,
                                order_id=snapshot.entry_order_id,
                                filled_price=(hist or {}).get("rate"),
                            )

                    if status.status == OrderStatus.FILLED:
                        filled_amount, filled_price = self._resolve_fill_amount(snapshot, status, snapshot.entry_price)
                        if filled_amount <= 0 or filled_price <= 0:
                            logger.warning("[State] Filled BUY for %s but amount/price unresolved", snapshot.symbol)
                            continue
                        self._register_filled_position_from_state(snapshot, filled_amount, filled_price)
                        self._state_manager.mark_entry_filled(snapshot.symbol, filled_amount, filled_price)
                        self.db.record_held_coin(snapshot.symbol, filled_amount)
                        if self.risk_manager:
                            self.risk_manager.record_trade()
                        logger.info(
                            "[State] %s -> in_position | order=%s amount=%.8f @ %.2f",
                            snapshot.symbol,
                            snapshot.entry_order_id,
                            filled_amount,
                            filled_price,
                        )
                        continue

                    if self._state_manager.is_timed_out(snapshot):
                        cancelled = self.executor.cancel_order(snapshot.entry_order_id, symbol=snapshot.symbol, side="buy")
                        stale_fill = getattr(self.executor, "_oms_cancel_was_error_21", False)
                        self.executor._oms_cancel_was_error_21 = False
                        if stale_fill:
                            fallback = OrderResult(
                                success=True,
                                status=OrderStatus.FILLED,
                                order_id=snapshot.entry_order_id,
                                filled_price=snapshot.entry_price,
                            )
                            filled_amount, filled_price = self._resolve_fill_amount(snapshot, fallback, snapshot.entry_price)
                            if filled_amount > 0 and filled_price > 0:
                                self._register_filled_position_from_state(snapshot, filled_amount, filled_price)
                                self._state_manager.mark_entry_filled(snapshot.symbol, filled_amount, filled_price)
                                self.db.record_held_coin(snapshot.symbol, filled_amount)
                                if self.risk_manager:
                                    self.risk_manager.record_trade()
                            continue
                        if cancelled:
                            self._state_manager.cancel_pending_buy(snapshot.symbol, "buy timeout cancel")
                            logger.info("[State] %s -> idle | pending buy timed out", snapshot.symbol)
                        else:
                            logger.warning("[State] Failed to cancel timed-out buy for %s", snapshot.symbol)

                elif snapshot.state == TradeLifecycleState.PENDING_SELL:
                    status = self.executor.check_order_status(
                        snapshot.exit_order_id,
                        symbol=snapshot.symbol,
                        side="sell",
                    )
                    if status.status == OrderStatus.ERROR:
                        hist = self._lookup_order_history_status(snapshot.symbol, snapshot.exit_order_id)
                        hist_status = str((hist or {}).get("status") or "").lower()
                        if hist_status in ("filled", "match", "done", "complete"):
                            status = OrderResult(
                                success=True,
                                status=OrderStatus.FILLED,
                                order_id=snapshot.exit_order_id,
                                filled_price=(hist or {}).get("rate"),
                            )

                    if status.status == OrderStatus.FILLED:
                        _, exit_price = self._resolve_fill_amount(snapshot, status, snapshot.exit_price)
                        completed = self._state_manager.complete_exit(snapshot.symbol, exit_price)
                        self._report_completed_exit(completed, exit_price, "order")
                        logger.info("[State] %s -> idle | exit filled", snapshot.symbol)
                        continue

                    if self._state_manager.is_timed_out(snapshot):
                        cancelled = self.executor.cancel_order(snapshot.exit_order_id, symbol=snapshot.symbol, side="sell")
                        stale_fill = getattr(self.executor, "_oms_cancel_was_error_21", False)
                        self.executor._oms_cancel_was_error_21 = False
                        if stale_fill:
                            completed = self._state_manager.complete_exit(snapshot.symbol, snapshot.exit_price or snapshot.entry_price)
                            self._report_completed_exit(completed, snapshot.exit_price or snapshot.entry_price, "stale_cancel")
                            continue

                        if cancelled:
                            restore_position = {
                                "symbol": snapshot.symbol,
                                "side": OrderSide.BUY,
                                "amount": snapshot.filled_amount,
                                "entry_price": snapshot.entry_price,
                                "stop_loss": snapshot.stop_loss,
                                "take_profit": snapshot.take_profit,
                                "timestamp": snapshot.opened_at or datetime.now(),
                                "is_partial_fill": False,
                                "remaining_amount": 0.0,
                                "total_entry_cost": snapshot.total_entry_cost,
                                "filled": True,
                                "filled_amount": snapshot.filled_amount,
                                "filled_price": snapshot.entry_price,
                                "state_managed": True,
                            }
                            self.executor.register_tracked_position(snapshot.entry_order_id, restore_position)
                            self._state_manager.restore_in_position(snapshot.symbol, "sell timeout cancel")
                            logger.info("[State] %s restored to in_position after sell timeout", snapshot.symbol)
                        else:
                            logger.warning("[State] Failed to cancel timed-out sell for %s", snapshot.symbol)
            except Exception as e:
                logger.error("[State] Advance error for %s: %s", snapshot.symbol, e, exc_info=True)
    
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
        
        # Portfolio Guard: Filter to only trade coins that have been held
        held_pairs = [
            pair for pair in trading_pairs
            if self.db.has_ever_held(pair)
        ]
        
        # Log filtered pairs
        if len(held_pairs) < len(trading_pairs):
            skipped = [p for p in trading_pairs if p not in held_pairs]
            skipped_key = tuple(skipped)
            if skipped_key != self._last_portfolio_guard_skipped:
                logger.info(f"🛡️  [Portfolio Guard] Skipping never-held pairs: {skipped}")
                self._last_portfolio_guard_skipped = skipped_key
        else:
            self._last_portfolio_guard_skipped = ()
        
        logger.debug(f"Actual pairs to process: {held_pairs}")
        
        for current_pair in held_pairs:
            self._process_pair_iteration(current_pair)
    
    def _process_pair_iteration(self, symbol: str):
        """Process trading iteration for a single symbol.
        
        Args:
            symbol: Trading pair symbol (e.g., 'THB_BTC', 'THB_DOGE')
        """
        portfolio = self._get_portfolio_state()
        mtf_signal = self._get_mtf_signal_for_symbol(symbol, portfolio)

        if self._state_machine_enabled:
            snapshot = self._state_manager.get_state(symbol)
            if snapshot.state != TradeLifecycleState.IDLE:
                last_logged = self._last_state_gate_logged.get(symbol)
                if last_logged != snapshot.state.value:
                    logger.info("[State] %s gated by execution state: %s", symbol, snapshot.state.value)
                    self._last_state_gate_logged[symbol] = snapshot.state.value
                return
            self._last_state_gate_logged.pop(symbol, None)

        # 1. Get current market data for this symbol
        data = self._get_market_data_for_symbol(symbol)
        
        if data is None or data.empty:
            logger.debug(f"No market data for {symbol}, skipping")
            return
        
        # 2a. H5/SRG-2: Sync SignalGenerator with live portfolio state so
        #     check_risk() sees real position count and daily trade count.
        if self._state_machine_enabled:
            _open_count = len(self._state_manager.list_active_states())
        else:
            _open_count = len(portfolio.get("positions", []))
        _daily_count = (
            self.risk_manager.trade_count_today
            if self.risk_manager is not None
            else len(self._executed_today)
        )
        self.signal_generator.sync_state(
            open_positions_count=_open_count,
            daily_trades_count=_daily_count,
        )

        # 2. Sniper: Dual EMA + MACD "Full Bull Alignment"
        signals = self.signal_generator.generate_sniper_signal(
            data=data,
            symbol=symbol,
        )
        if not isinstance(signals, list):
            signals = self.signal_generator.generate_signals(
                data=data,
                symbol=symbol,
            )

        current_market_condition = None
        for generated_signal in signals:
            if isinstance(generated_signal, AggregatedSignal):
                current_market_condition = generated_signal.market_condition
                break
        if current_market_condition is None:
            current_market_condition = detect_market_condition(data["close"].tolist())
        
        if not signals:
            logger.debug(f"No signals generated for {symbol}")
            return
        
        # 4. Log signals to database
        for signal in signals:
            try:
                if isinstance(signal, AggregatedSignal):
                    sig_type = signal.signal_type.value.upper() if hasattr(signal.signal_type, 'value') else str(signal.signal_type).upper()
                    strategy_names = ",".join(sorted(signal.strategy_votes.keys())) if signal.strategy_votes else ""
                    self.db.insert_signal(
                        pair=signal.symbol,
                        signal_type=sig_type,
                        confidence=signal.combined_confidence,
                        result='pending',
                        strategy=strategy_names,
                    )
            except Exception as e:
                logger.error(f"Failed to log signal to database: {e}")
        
        # 5. Process signals based on mode
        for signal in signals:
            # Strategy generator returns AggregatedSignal instances.
            if not isinstance(signal, AggregatedSignal):
                continue

            signal_type = signal.signal_type.value.lower() if hasattr(signal.signal_type, 'value') else str(signal.signal_type).lower()
                
            risk_check = self.signal_generator.check_risk(signal, portfolio)

            if self._state_machine_enabled:
                if signal_type == "buy":
                    approved, gate_reason = self._state_manager.confirm_entry_signal(
                        symbol=symbol,
                        signal_type=signal_type,
                        confidence=signal.combined_confidence,
                        risk_passed=risk_check.passed,
                        signal_time=signal.timestamp,
                    )
                    if not approved:
                        if gate_reason.startswith("awaiting confirmation"):
                            logger.debug("[State] %s BUY signal queued: %s", symbol, gate_reason)
                        else:
                            logger.debug("[State] %s signal ignored: %s", symbol, gate_reason)
                        continue
                elif signal_type == "sell":
                    snapshot = self._state_manager.get_state(symbol)
                    if snapshot.state == TradeLifecycleState.IN_POSITION:
                        pass
                    elif not self._allow_sell_entries_from_idle:
                        logger.debug(
                            "[State] %s SELL signal ignored: lifecycle=%s and idle SELL is disabled",
                            symbol,
                            snapshot.state.value,
                        )
                        continue
                    else:
                        approved, gate_reason = self._state_manager.confirm_idle_sell_signal(
                            symbol=symbol,
                            confidence=signal.combined_confidence,
                            risk_passed=risk_check.passed,
                            signal_time=signal.timestamp,
                        )
                        if not approved:
                            if gate_reason.startswith("awaiting confirmation"):
                                logger.debug("[State] %s SELL signal queued: %s", symbol, gate_reason)
                            else:
                                logger.debug("[State] %s SELL signal ignored: %s", symbol, gate_reason)
                            continue
                else:
                    logger.debug("[State] %s signal ignored: unsupported type '%s'", symbol, signal_type)
                    continue
            
            # Create execution plan for this symbol
            plan = self._create_execution_plan_for_symbol(signal, symbol)
            if not plan:
                continue

            # Create trade decision
            decision = TradeDecision(
                plan=plan,
                signal=signal,
                risk_check=risk_check,
                signal_source=self.signal_source
            )
            
            # Process based on mode
            if self.mode == BotMode.FULL_AUTO:
                self._process_full_auto(decision, portfolio)
            elif self.mode == BotMode.SEMI_AUTO:
                self._process_semi_auto(decision, portfolio)
            else:  # DRY_RUN
                self._process_dry_run(decision, portfolio)

    def _maybe_trigger_sideways_rebalance(self, market_condition: Optional[MarketCondition] = None) -> None:
        """Compatibility hook for sideways-market rebalance logic.

        Runtime rebalance logic is currently handled elsewhere; this hook remains
        to keep existing tests and extension points stable.
        """
        return None

    def _serialize_mtf_signals_detail(self, mtf_result) -> Dict[str, Dict[str, Any]]:
        details: Dict[str, Dict[str, Any]] = {}
        for timeframe, tf_signal in (getattr(mtf_result, "signals", {}) or {}).items():
            signal_type = getattr(getattr(tf_signal, "signal_type", None), "value", getattr(tf_signal, "signal_type", None))
            indicators = getattr(tf_signal, "indicators", {}) or {}
            details[str(timeframe)] = {
                "type": str(signal_type or "HOLD").upper(),
                "confidence": float(getattr(tf_signal, "confidence", 0.0) or 0.0),
                "trend_strength": float(getattr(tf_signal, "trend_strength", 0.0) or 0.0),
                "rsi": float(indicators.get("rsi", 0.0) or 0.0),
                "adx": float(indicators.get("adx", 0.0) or 0.0),
                "macd_hist": float(indicators.get("macd_hist", 0.0) or 0.0),
                "volume_ratio": float(indicators.get("volume_ratio", 0.0) or 0.0),
                "reason": str(getattr(tf_signal, "reason", "") or ""),
            }
        return details

    def _merge_mtf_signals_detail(
        self,
        base_details: Optional[Dict[str, Dict[str, Any]]],
        override_details: Optional[Dict[str, Dict[str, Any]]],
    ) -> Dict[str, Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        ordered_timeframes: List[str] = []

        for source in (base_details or {}, override_details or {}):
            for timeframe in source.keys():
                normalized = str(timeframe)
                if normalized not in ordered_timeframes:
                    ordered_timeframes.append(normalized)

        for timeframe in ordered_timeframes:
            base_row = dict((base_details or {}).get(timeframe) or {})
            override_row = dict((override_details or {}).get(timeframe) or {})
            clean_override = {
                key: value
                for key, value in override_row.items()
                if value is not None and (not isinstance(value, str) or value.strip())
            }
            merged[timeframe] = {
                **base_row,
                **clean_override,
            }

        return merged

    def _get_mtf_signal_for_symbol(
        self,
        symbol: str,
        portfolio: Dict[str, Any],
    ):
        if not self.mtf_enabled:
            return None

        recorded_at = datetime.now().isoformat()
        try:
            mtf_result = self.signal_generator.generate_mtf_signals(
                pair=symbol,
                timeframes=self.mtf_timeframes,
                db=self.db,
            )
        except Exception as exc:
            logger.warning("MTF signal generation failed for %s: %s", symbol, exc)
            self._last_mtf_status[symbol] = {
                "updated_at": recorded_at,
                "status": "error",
                "reason": str(exc),
                "timeframes": list(self.mtf_timeframes),
            }
            return None

        if mtf_result is None:
            self._last_mtf_status[symbol] = {
                "updated_at": recorded_at,
                "status": "waiting",
                "reason": "No multi-timeframe data available yet",
                "timeframes": list(self.mtf_timeframes),
            }
            return None

        snapshot = {
            "updated_at": recorded_at,
            "timeframes": list(getattr(mtf_result, "timeframes", {}).keys() or self.mtf_timeframes),
            "signals_detail": self._serialize_mtf_signals_detail(mtf_result),
            "trend_alignment": float(getattr(mtf_result, "trend_alignment", 0.0) or 0.0),
            "consensus_strength": float(getattr(mtf_result, "consensus_strength", 0.0) or 0.0),
            "higher_timeframe_trend": getattr(getattr(mtf_result, "higher_timeframe_trend", None), "value", getattr(mtf_result, "higher_timeframe_trend", None)),
            "higher_timeframe_confidence": float(getattr(mtf_result, "higher_timeframe_confidence", 0.0) or 0.0),
        }

        try:
            signal = self.signal_generator.get_mtf_signal(
                pair=symbol,
                timeframes=self.mtf_timeframes,
                portfolio=portfolio,
                db=self.db,
                mtf_result=mtf_result,
            )
        except Exception as exc:
            logger.warning("MTF signal build failed for %s: %s", symbol, exc)
            self._last_mtf_status[symbol] = {
                **snapshot,
                "status": "error",
                "reason": str(exc),
            }
            return None

        if signal is None:
            self._last_mtf_status[symbol] = {
                **snapshot,
                "status": "waiting",
                "reason": "No aligned multi-timeframe signal yet",
            }
            return None

        metadata = getattr(signal, "metadata", {}) or {}
        self._last_mtf_status[symbol] = {
            **snapshot,
            "status": "ready",
            "signal_type": signal.signal_type.value,
            "confidence": signal.confidence,
            "timeframes": list(metadata.get("timeframes_used") or snapshot["timeframes"]),
            "higher_timeframe_trend": metadata.get("higher_timeframe_trend") or snapshot["higher_timeframe_trend"],
            "signals_detail": self._merge_mtf_signals_detail(
                snapshot["signals_detail"],
                metadata.get("signals_detail"),
            ),
        }
        return signal

    def _apply_multi_timeframe_confirmation(
        self,
        symbol: str,
        signals: List[AggregatedSignal],
        mtf_signal,
    ) -> List[AggregatedSignal]:
        if not self.mtf_enabled or not signals:
            return signals

        if mtf_signal is None:
            if self._mtf_confirmation_required:
                logger.debug("[MTF] %s skipped: higher-timeframe confirmation not ready", symbol)
                return []
            return signals

        mtf_type = str(getattr(mtf_signal.signal_type, "value", mtf_signal.signal_type)).upper()
        confirmed: List[AggregatedSignal] = []
        for signal in signals:
            signal_type = str(getattr(signal.signal_type, "value", signal.signal_type)).upper()
            if signal_type == mtf_type:
                signal.combined_confidence = min(
                    0.99,
                    max(signal.combined_confidence, (signal.combined_confidence + float(mtf_signal.confidence)) / 2),
                )
                rationale_suffix = f" | MTF confirmed {mtf_type} ({float(mtf_signal.confidence):.0%})"
                signal.trade_rationale = f"{signal.trade_rationale or '[Trade Triggered]'}{rationale_suffix}"
                confirmed.append(signal)
                continue

            if self._mtf_confirmation_required:
                logger.debug("[MTF] %s filtered %s due to conflicting %s confirmation", symbol, signal_type, mtf_type)
                continue

            signal.combined_confidence = max(0.0, signal.combined_confidence * 0.75)
            rationale_suffix = f" | MTF conflict {mtf_type} ({float(mtf_signal.confidence):.0%})"
            signal.trade_rationale = f"{signal.trade_rationale or '[Trade Triggered]'}{rationale_suffix}"
            confirmed.append(signal)

        return confirmed
    
    def _get_market_data_for_symbol(self, symbol: str):
        """Fetch latest market data from database for a specific symbol (10-second cache).
        
        Args:
            symbol: Trading pair symbol (e.g., 'THB_BTC', 'THB_DOGE')
        """
        now = time.time()
        cache_key = f"market_data_{symbol}_{self.timeframe}"
        
        # Check cache
        if (cache_key in self._symbol_market_cache and
            (now - self._symbol_market_cache[cache_key]["timestamp"]) < self._cache_ttl['market_data']):
            return self._symbol_market_cache[cache_key]["data"]
        
        rows = None
        try:
            conn = self.db.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT timestamp, open, high, low, close, volume
                    FROM prices
                    WHERE pair = ?
                      AND COALESCE(timeframe, '1h') = ?
                    ORDER BY timestamp DESC
                    LIMIT 250
                """, (symbol, self.timeframe))
                rows = cursor.fetchall()
            finally:
                conn.close()
        except Exception as e:
            logger.debug(f"Error fetching market data for {symbol}: {e}")
        
        if not rows:
            # Try API fallback
            try:
                timeframe_map = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "1d": "1440"}
                tf_param = timeframe_map.get(self.timeframe, "60")
                
                response = self.api_client.get_candle(symbol, tf_param)
                if response.get("error") == 0:
                    data = response.get("result", [])
                    import pandas as pd
                    df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    df = df.sort_values("timestamp").reset_index(drop=True)
                    df.attrs["_data_source"] = "api"
                    
                    self._symbol_market_cache[cache_key] = {"data": df, "timestamp": now}
                    return df
                else:
                    # VULN-06 FIX: Log non-zero API error at warning level
                    logger.warning(
                        "API candle request for %s returned error=%s: %s",
                        symbol, response.get("error"), response.get("message", ""),
                    )
            except Exception as e:
                # VULN-06 FIX: Promote from debug to warning so operators notice
                logger.warning("API fallback failed for %s: %s", symbol, e)
            return None
        
        import pandas as pd
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        df.attrs["_data_source"] = "db"
        
        self._symbol_market_cache[cache_key] = {"data": df, "timestamp": now}
        return df
    
    def _create_execution_plan_for_symbol(self, signal: AggregatedSignal, symbol: str) -> Optional[ExecutionPlan]:
        """Create an ExecutionPlan from an AggregatedSignal for a specific symbol.

        Uses STRICT dynamic SL/TP based structurally on ATR.
        Enforces 'No ATR = No Trade' rule.
        ✓ NEW: Only allows BUY signals for coins ever held by this bot.
        """
        signal_type_value = str(getattr(signal.signal_type, "value", signal.signal_type) or "").lower()
        side = OrderSide.BUY if signal_type_value == "buy" else OrderSide.SELL
        plan_close_flag = False  # Default: opening new position
        
        # Check existing positions
        try:
            if self._state_machine_enabled:
                open_positions = []
            else:
                session = self.db.get_session()
                try:
                    from sqlalchemy import text
                    open_positions = session.execute(
                        text("SELECT side, amount FROM positions WHERE symbol = :symbol AND remaining_amount > 0"),
                        {"symbol": symbol},
                    ).fetchall()
                finally:
                    session.close()
            
            if open_positions:
                current_side = open_positions[0][0]
                if current_side == 'sell' and side == OrderSide.BUY:
                    plan_close_flag = True
                elif current_side == 'buy' and side == OrderSide.SELL:
                    plan_close_flag = True
        except Exception as e:
            logger.debug(f"[Position Check] Could not check positions: {e}")
        
        # ✓ NEW: Restrict BUY signals to coins ever held by this bot
        if side == OrderSide.BUY and plan_close_flag is False:
            has_history = False
            try:
                if self.db:
                    has_history = self.db.has_ever_held(symbol)
            except Exception as e:
                logger.debug(f"[Portfolio Guard] history lookup failed for {symbol}: {e}")

            if not has_history:
                logger.debug(
                    f"[Portfolio Guard] BUY rejected for {symbol}: never held"
                )
                return None
        
        # SELL signal requires base asset balance
        if side == OrderSide.SELL and plan_close_flag is False:
            try:
                base_asset = symbol.split('_')[1] if '_' in symbol else symbol
                balances = self.api_client.get_balances()
                base_data = balances.get(base_asset.upper()) or balances.get(base_asset.lower()) or {}
                available_base = float(base_data.get('available', 0))
                base_value_thb = available_base * signal.avg_price
                if base_value_thb < self.min_trade_value_thb:
                    logger.debug(
                        f"[Strategy Bypass] {base_asset.upper()} value {base_value_thb:.2f} THB "
                        f"< MIN {self.min_trade_value_thb:.2f} THB"
                    )
                    return None
            except Exception as e:
                logger.debug(f"[Balance Check] ไม่สามารถตรวจสอบ balance: {e}")
        
        direction = "long" if side == OrderSide.BUY else "short"
        entry_price = signal.avg_price
        
        # Must have ATR to proceed
        atr_value = self._get_latest_atr(symbol)
        if not atr_value or atr_value <= 0:
            logger.debug(f"Trade rejected for {symbol}: ATR unavailable")
            return None
        
        # Use signal's SL/TP if provided (sniper strategy computes its own),
        # otherwise fall back to risk manager ATR calculation.
        if signal.avg_stop_loss and signal.avg_take_profit and signal.avg_stop_loss > 0 and signal.avg_take_profit > 0:
            sl = signal.avg_stop_loss
            tp = signal.avg_take_profit
            logger.debug(f"Using signal SL/TP for {symbol}: SL={sl:.4f} TP={tp:.4f}")
        else:
            rr_ratio = signal.avg_risk_reward if signal.avg_risk_reward > 0 else 2.0
            sl, tp = self.risk_manager.calc_sl_tp_from_atr(
                entry_price=entry_price,
                atr_value=atr_value,
                direction=direction,
                risk_reward_ratio=rr_ratio,
            )
            logger.debug(f"ATR-based SL/TP for {symbol}: SL={sl:.4f} TP={tp:.4f} (ATR={atr_value:.4f})")
        
        return ExecutionPlan(
            symbol=symbol,
            side=side,
            amount=0,  # Will be calculated by executor
            entry_price=entry_price,
            stop_loss=sl,
            take_profit=tp,
            risk_reward_ratio=signal.avg_risk_reward,
            confidence=signal.combined_confidence,
            strategy_votes=signal.strategy_votes,
            notes=[
                f"Confidence: {signal.combined_confidence:.2%}",
                f"Strategies: {', '.join(signal.strategy_votes.keys())}",
                f"Risk Score: {signal.risk_score:.0f}/100",
                f"ATR: {atr_value:.4f}" if atr_value else "ATR: N/A",
                f"Dynamic SL/TP: pair={symbol}",
            ],
            signal_timestamp=signal.timestamp,
            signal_id=f"{signal.symbol}_{signal.signal_type.value}_{int(signal.timestamp.timestamp())}",
            max_price_drift_pct=1.5,
            close_position=plan_close_flag,
        )
    
    # ── WebSocket Real-time Price Handler ───────────────────────────────────

    def _on_ws_tick(self, tick: _PriceTickType):
        """
        Called on every real-time price tick from the WebSocket.
        This runs on the WebSocket's callback dispatcher thread.

        Logs significant price movements and can trigger immediate
        SL/TP checks without waiting for the next main-loop iteration.
        """
        logger.debug(
            f"[WS] {tick.symbol} last={tick.last:,.0f} "
            f"bid={tick.bid:,.0f} ask={tick.ask:,.0f} "
            f"change={tick.percent_change_24h:+.2f}%"
        )

        # Immediately check if this price hits any open position SL/TP
        # This gives us <100ms reaction time vs the old 1-5s polling cycle
        self._check_sl_tp_immediate(tick)

    def _check_sl_tp_immediate(self, tick: _PriceTickType):
        """
        Check if a real-time price tick hits any open position SL/TP.
        Called from the WebSocket callback thread — fire-and-forget,
        exceptions are caught to avoid crashing the callback thread.
        """
        if not _WEBSOCKET_AVAILABLE or not PriceTick:
            return

        try:
            open_orders = self.executor.get_open_orders()
        except Exception:
            return

        for pos in open_orders:
            pos_symbol  = pos.get("symbol", self.trading_pair)
            if pos_symbol.upper() != tick.symbol.upper():
                continue

            if self._state_machine_enabled:
                lifecycle = self._state_manager.get_state(pos_symbol)
                if lifecycle.state != TradeLifecycleState.IN_POSITION:
                    continue

            position_id = pos.get("order_id", "")
            entry_price = _coerce_trade_float(pos.get("entry_price"), 0.0)
            stop_loss = _coerce_trade_float(pos.get("stop_loss"), 0.0)
            take_profit = _coerce_trade_float(pos.get("take_profit"), 0.0)
            side        = pos.get("side", OrderSide.BUY)
            amount      = _coerce_trade_float(pos.get("amount"), 0.0)

            if not entry_price or entry_price == 0:
                continue

            triggered = None
            current_price = tick.last

            if side == OrderSide.BUY:
                if stop_loss > 0 and current_price <= stop_loss:
                    triggered = "SL"
                elif take_profit > 0 and current_price >= take_profit:
                    triggered = "TP"
            else:
                if stop_loss > 0 and current_price >= stop_loss:
                    triggered = "SL"
                elif take_profit > 0 and current_price <= take_profit:
                    triggered = "TP"

            if triggered:
                logger.info(
                    f"[WS-SLTP] {triggered} triggered for position {position_id} | "
                    f"Entry={entry_price:,.0f} Current={current_price:,.0f} | "
                    f"SL={stop_loss:,.0f} TP={take_profit:,.0f}"
                )

                # M6 fix: per-position deduplication guard.  If an exit thread
                # is already in-flight for this position we skip spawning
                # another one, preventing the unbounded thread explosion that
                # would otherwise occur during a flash crash with rapid ticks.
                with self._ws_sltp_inflight_lock:
                    if position_id in self._ws_sltp_inflight:
                        logger.debug(
                            f"[WS-SLTP] Exit thread already in-flight for {position_id} — skipping duplicate"
                        )
                        continue
                    self._ws_sltp_inflight.add(position_id)

                # Fire SL/TP exit on a separate thread to avoid
                # blocking the WS callback thread
                total_entry_cost = pos.get("total_entry_cost", entry_price * amount)
                try:
                    threading.Thread(
                        target=self._ws_sltp_exit_wrapper,
                        args=(position_id, pos_symbol, side, amount, current_price, triggered, entry_price, total_entry_cost),
                        daemon=True,
                    ).start()
                except Exception as e:
                    logger.error(f"Failed to fire SL/TP exit thread: {e}", exc_info=True)
                    # Release the inflight guard so retries are possible
                    with self._ws_sltp_inflight_lock:
                        self._ws_sltp_inflight.discard(position_id)

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
        """M6 fix: thin wrapper around _execute_ws_sl_tp_exit that guarantees
        the per-position inflight guard is released when the exit completes
        (or fails), allowing future ticks to retry if needed."""
        try:
            self._execute_ws_sl_tp_exit(
                position_id, symbol, side, amount,
                current_price, triggered, entry_price, total_entry_cost,
            )
        finally:
            with self._ws_sltp_inflight_lock:
                self._ws_sltp_inflight.discard(position_id)

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
        """Execute a SL/TP exit triggered by a WebSocket price tick."""
        # FIX HIGH-01: Check circuit breaker before executing any trade
        # Prevents rate limit death spiral during WebSocket-triggered exits
        if hasattr(self.api_client, 'is_circuit_open') and self.api_client.is_circuit_open():
            logger.warning(
                f"[WS-SLTP] Circuit breaker OPEN — blocking {triggered} exit for "
                f"position {position_id} ({symbol}) to prevent rate limit death spiral"
            )
            return
        
        try:
            if self._state_machine_enabled:
                self._submit_managed_exit(
                    position_id=position_id,
                    pos_symbol=symbol,
                    side=side,
                    amount=amount,
                    exit_price=current_price,
                    triggered=triggered,
                    entry_price=entry_price,
                    total_entry_cost=total_entry_cost,
                    price_source="ws",
                    opened_at=None,
                )
                return

            exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            result = self.executor.execute_exit(
                position_id=position_id,
                order_id=position_id,
                side=exit_side,
                amount=amount,
                price=current_price,
            )
            if result.success:
                # ── Net P/L with Bitkub 0.25% fees ────────────────
                from trade_executor import BITKUB_FEE_PCT
                now = datetime.now()
                
                entry_cost = total_entry_cost if total_entry_cost > 0 else (entry_price * amount)
                entry_fee = entry_cost * BITKUB_FEE_PCT
                
                gross_exit = current_price * amount
                exit_fee = gross_exit * BITKUB_FEE_PCT
                net_exit = gross_exit - exit_fee
                
                total_fees = entry_fee + exit_fee
                net_pnl = net_exit - entry_cost
                net_pnl_pct = (net_pnl / entry_cost * 100) if entry_cost > 0 else 0
                
                pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"
                coin = symbol.replace("THB_", "")
                trigger_label = "Take Profit" if triggered == "TP" else ("Stop Loss" if triggered == "SL" else (triggered or "Exit"))
                
                # ── Log closed trade to DB ─────────────────────────
                try:
                    self.db.log_closed_trade({
                        "symbol": symbol, "side": side,
                        "amount": amount, "entry_price": entry_price,
                        "exit_price": current_price, "entry_cost": entry_cost,
                        "gross_exit": gross_exit, "entry_fee": entry_fee,
                        "exit_fee": exit_fee, "total_fees": total_fees,
                        "net_pnl": net_pnl, "net_pnl_pct": net_pnl_pct,
                        "trigger": triggered, "price_source": "ws",
                        "closed_at": now,
                    })
                except Exception as e:
                    logger.error(f"[WS-SLTP] Failed to log closed trade: {e}", exc_info=True)
                
                msg = self._format_exit_alert(
                    symbol,
                    trigger_label,
                    entry_price,
                    current_price,
                    net_pnl,
                    net_pnl_pct,
                    total_fees,
                    now=now,
                )
                self._send_alert(msg, to_telegram=True)
            else:
                logger.error(f"[WS-SLTP] Exit failed: {result.message}")
        except Exception as e:
            logger.error(f"[WS-SLTP] Exit error: {e}", exc_info=True)

    def _get_latest_atr(self, symbol: Optional[str] = None, period: int = 14) -> Optional[float]:
        """Calculate latest ATR value from market data for a specific symbol.

        Uses the RiskManager's ATR calculation with Wilder's smoothing.

        Returns:
            ATR value (float) or None if market data unavailable.
        """
        symbol = symbol or self.trading_pair or ""
        if not symbol:
            return None
        now = time.time()
        cache_key = f"atr_{symbol}_{period}"
        if (self._atr_cache.get(cache_key) is not None
                and (now - self._atr_cache[cache_key]["timestamp"]) < self._cache_ttl['atr']):
            return self._atr_cache[cache_key]["value"]

        try:
            data = self._get_market_data_for_symbol(symbol)
            if data is None or len(data) < period + 1:
                return None

            # Ensure we have high/low/close columns
            if not all(k in data.columns for k in ["high", "low", "close"]):
                return None

            highs = data["high"].tolist()
            lows = data["low"].tolist()
            closes = data["close"].tolist()

            atr_values = calculate_atr(highs, lows, closes, period=period)
            atr = atr_values[-1]

            if atr <= 0:
                return None

            result = float(atr)
            self._atr_cache[cache_key] = {"value": result, "timestamp": now}
            return result
        except Exception as e:
            logger.debug(f"Could not calculate ATR for {symbol}: {e}")
            return None

    def _get_portfolio_state(self) -> Dict[str, Any]:
        """Get current portfolio state with 10-second caching.

        Always fetches the real THB balance from Bitkub API (get_balances).
        Cached for 10 seconds to avoid hammering the API.
        """
        now = time.time()
        if (self._portfolio_cache["data"] is not None
                and (now - self._portfolio_cache["timestamp"]) < self._cache_ttl['portfolio']):
            return self._portfolio_cache["data"]

        if self._auth_degraded:
            result = {
                "balance": 0.0,
                "positions": self.executor.get_open_orders(),
                "timestamp": datetime.now(),
            }
            self._portfolio_cache = {"data": result, "timestamp": now}
            return result

        try:
            # Always get real balance from Bitkub API (even in read_only mode)
            # This ensures we get the ACTUAL available THB, not a config default.
            response = self.api_client.get_balances()

            if isinstance(response, dict):
                thb_info = response.get("THB", {})
                thb_balance = float(thb_info.get("available", 0))
            elif isinstance(response, list):
                # Fallback: try get_balance (flat dict)
                resp2 = self.api_client.get_balance()
                if isinstance(resp2, dict) and resp2.get("error") == 0:
                    result_data = resp2.get("result", {})
                    thb_balance = float(result_data.get("THB", 0)) if isinstance(result_data, dict) else 0.0
                else:
                    thb_balance = 0.0
            else:
                thb_balance = 0.0

            result = {
                "balance": thb_balance,
                "positions": self.executor.get_open_orders(),
                "timestamp": datetime.now()
            }
            self._portfolio_cache = {"data": result, "timestamp": now}
            return result

        except Exception as e:
            logger.error(f"Error getting portfolio state from Bitkub: {e}", exc_info=True)
            return {
                "balance": 0.0,
                "positions": self.executor.get_open_orders(),
                "timestamp": datetime.now()
            }

    def _process_full_auto(self, decision: TradeDecision, portfolio: Dict[str, Any]):
        """
        Full auto mode: execute trade automatically after risk check passes.
        """
        if self.read_only:
            logger.info("READ_ONLY mode — skipping trade execution")
            return
        
        if not decision.risk_check.passed:
            reason = getattr(decision.risk_check, 'reason', getattr(decision.risk_check, 'reasons', 'Unknown reasons'))
            logger.info(f"🛡️ Risk Manager: ปฏิเสธสัญญาณเทรด ({reason})")
            if self.send_alerts:
                self._send_alert(self._format_skip_alert(decision))
            return
        
        # Log trade rationale
        rationale = getattr(decision.signal, 'trade_rationale', 'N/A')
        logger.info(f"[Trade Decision] {rationale}")
        side_thai = "ซื้อ" if decision.plan.side.value == "buy" else "ขาย"
        logger.info(f"🤖 [FULL_AUTO] กำลังส่งคำสั่งเทรด | {side_thai} ({decision.plan.side.value.upper()}) @ {decision.plan.entry_price:,.2f} THB")

        # ── C3/SRG-1: Global risk gate – block NEW entries (BUY + idle SELL) ────
        is_new_entry_buy = decision.plan.side.value == "buy"
        is_new_entry_idle_sell = (
            decision.plan.side.value == "sell"
            and not decision.plan.close_position
            and (not self._state_machine_enabled or self._allow_sell_entries_from_idle)
        )
        if (is_new_entry_buy or is_new_entry_idle_sell) and self.risk_manager:
            if self._state_machine_enabled:
                open_count = len(self._state_manager.list_active_states())
            else:
                open_count = len(portfolio.get("positions", []))
            gate = self.risk_manager.can_open_position(
                portfolio["balance"], open_count,
            )
            if not gate.allowed:
                logger.warning("🚫 [RiskGate] Trade blocked for %s: %s",
                               decision.plan.symbol, gate.reason)
                return

            # ── Correlation guard – block if too correlated with open positions ──
            corr_threshold = float(self.config.get("risk", {}).get("correlation_threshold", 0.75))
            if corr_threshold < 1.0:
                open_symbols = [
                    str(pos.get("symbol", "")).upper()
                    for pos in self.executor.get_open_orders()
                    if pos.get("symbol")
                ]
                if open_symbols:
                    corr_gate = check_pair_correlation(
                        candidate_symbol=decision.plan.symbol,
                        open_symbols=open_symbols,
                        db=self.db,
                        threshold=corr_threshold,
                        timeframe=self.timeframe,
                    )
                    if not corr_gate.allowed:
                        logger.warning("🔗 [CorrelationGate] Trade blocked for %s: %s",
                                       decision.plan.symbol, corr_gate.reason)
                        return

        if self._state_machine_enabled:
            if decision.plan.side == OrderSide.BUY:
                self._submit_managed_entry(decision, portfolio)
                return
            if self._try_submit_managed_signal_sell(decision):
                return
            if not self._allow_sell_entries_from_idle:
                logger.debug(
                    "[State] SELL signal skipped for %s: no in-position state and idle SELL is disabled",
                    decision.plan.symbol,
                )
                return
        
        # Execute trade
        result = self.executor.execute_entry(decision.plan, portfolio["balance"])
        
        if result.success:
            decision.status = "executed"
            self._executed_today.append({
                "decision": decision,
                "result": result,
                "timestamp": datetime.now()
            })
            
            # Log order to database
            try:
                ts = datetime.utcnow()
                self.db.insert_order(
                    pair=decision.plan.symbol,
                    side=decision.plan.side.value,
                    quantity=result.filled_amount,
                    price=result.filled_price or 0.0,
                    status="filled",
                    order_type="limit",
                    timestamp=ts,
                )
            except Exception as e:
                logger.error(f"Failed to log order to database: {e}", exc_info=True)
            
            # Send alert
            self._send_trade_alert(decision, result)
        else:
            decision.status = "failed"
            if "Skipping order" in result.message:
                logger.info(result.message)
            else:
                logger.error(f"Trade execution failed: {result.message}")
    
    def _process_semi_auto(self, decision: TradeDecision, portfolio: Dict[str, Any]):
        """
        Semi-auto mode: send alert and wait for confirmation.
        """
        if not decision.risk_check.passed:
            return
        
        # Add to pending decisions
        with self._pending_decisions_lock:
            self._pending_decisions.append(decision)
        
        # Send alert
        self._send_pending_alert(decision, portfolio)
        
        logger.info(f"SEMI_AUTO: Alert sent for {decision.plan.side.value} @ {decision.plan.entry_price}")
    
    def _process_dry_run(self, decision: TradeDecision, portfolio: Dict[str, Any]):
        """
        Dry run mode: log what would happen without executing.
        """
        if not decision.risk_check.passed:
            return
        
        logger.info(f"DRY_RUN: Would execute {decision.plan.side.value} @ {decision.plan.entry_price}")
        logger.info(f"  Confidence: {decision.plan.confidence:.2%}")
        logger.info(f"  Strategy votes: {decision.plan.strategy_votes}")
        logger.info(f"  Risk-reward: {decision.plan.risk_reward_ratio:.2f}")
        
        self._send_dry_run_alert(decision, portfolio)
    
    def approve_trade(self, decision_id: int) -> bool:
        """
        Approve a pending trade decision (for semi-auto mode).
        
        Args:
            decision_id: Index of the pending decision to approve
            
        Returns:
            True if approved and executed successfully
        """
        if self._auth_degraded:
            logger.warning("approve_trade blocked: auth degraded mode is active")
            return False

        if self.mode != BotMode.SEMI_AUTO:
            logger.warning("approve_trade called but mode is not semi_auto")
            return False
        
        with self._pending_decisions_lock:
            if decision_id < 0 or decision_id >= len(self._pending_decisions):
                logger.error(f"Invalid decision_id: {decision_id}")
                return False
            
            decision = self._pending_decisions[decision_id]
        portfolio = self._get_portfolio_state()
        
        logger.info(f"Manual approval: executing trade #{decision_id}")

        if self._state_machine_enabled:
            if decision.plan.side == OrderSide.BUY:
                self._submit_managed_entry(decision, portfolio)
                with self._pending_decisions_lock:
                    self._pending_decisions.pop(decision_id)
                return True
            if self._try_submit_managed_signal_sell(decision):
                with self._pending_decisions_lock:
                    self._pending_decisions.pop(decision_id)
                return True
            if not self._allow_sell_entries_from_idle:
                logger.debug(
                    "approve_trade skipped SELL for %s: idle SELL is disabled",
                    decision.plan.symbol,
                )
                return False
        
        result = self.executor.execute_entry(decision.plan, portfolio["balance"])
        
        if result.success:
            decision.status = "executed"
            self._executed_today.append({
                "decision": decision,
                "result": result,
                "timestamp": datetime.now()
            })
            with self._pending_decisions_lock:
                self._pending_decisions.pop(decision_id)
            return True
        
        return False

    def _try_submit_managed_signal_sell(self, decision: TradeDecision) -> bool:
        """Route SELL signal through managed exit flow when symbol is in IN_POSITION state."""
        if not self._state_machine_enabled or decision.plan.side != OrderSide.SELL:
            return False

        symbol = str(decision.plan.symbol or "").upper()
        snapshot = self._state_manager.get_state(symbol)
        if snapshot.state != TradeLifecycleState.IN_POSITION:
            return False

        open_orders = self.executor.get_open_orders() or []
        position = None
        for row in open_orders:
            row_symbol = str(row.get("symbol") or "").upper()
            if row_symbol != symbol:
                continue
            side_value = str(getattr(row.get("side"), "value", row.get("side")) or "").lower()
            if side_value and side_value != "buy":
                continue
            position = row
            break

        if not position:
            logger.warning("[State] SELL signal for %s ignored: no open position found for managed exit", symbol)
            return False

        position_id = str(position.get("order_id") or snapshot.entry_order_id or "")
        amount = float(position.get("remaining_amount") or position.get("amount") or snapshot.filled_amount or 0.0)
        entry_price = float(position.get("entry_price") or snapshot.entry_price or decision.plan.entry_price or 0.0)
        total_entry_cost = float(position.get("total_entry_cost") or snapshot.total_entry_cost or 0.0)
        opened_at = position.get("timestamp") or snapshot.opened_at
        if not position_id or amount <= 0:
            logger.warning("[State] SELL signal for %s ignored: incomplete managed exit payload", symbol)
            return False

        submitted = self._submit_managed_exit(
            position_id=position_id,
            pos_symbol=symbol,
            side=OrderSide.BUY,
            amount=amount,
            exit_price=float(decision.plan.entry_price or entry_price),
            triggered="SIGSELL",
            entry_price=entry_price,
            total_entry_cost=total_entry_cost,
            price_source="signal",
            opened_at=opened_at,
        )
        if submitted:
            decision.status = "pending_sell"
        return bool(submitted)
    
    def reject_trade(self, decision_id: int) -> bool:
        """
        Reject a pending trade decision (for semi-auto mode).
        
        Args:
            decision_id: Index of the pending decision to reject
        """
        with self._pending_decisions_lock:
            if decision_id < 0 or decision_id >= len(self._pending_decisions):
                return False
            
            decision = self._pending_decisions[decision_id]
            decision.status = "rejected"
            
            self._pending_decisions.pop(decision_id)
        
        logger.info(f"Trade #{decision_id} rejected")
        return True
    
    def _send_trade_alert(self, decision: TradeDecision, result: OrderResult):
        """Log executed entries without notifying Telegram."""
        if not self.send_alerts:
            return
        
        message = self._format_trade_alert(decision, result)
        self._send_alert(message, to_telegram=False)
    
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

    @staticmethod
    def _format_alert_block(header: str, lines: List[str], now: Optional[datetime] = None) -> str:
        timestamp = (now or datetime.now()).strftime('%H:%M:%S')
        return "\n".join([header, '-' * 22, *lines, f"Time: <code>{timestamp}</code>"])

    @staticmethod
    def _format_coin_symbol(symbol: str) -> str:
        return str(symbol or "").replace("THB_", "")

    def _format_exit_alert(
        self,
        symbol: str,
        trigger_label: str,
        entry_price: float,
        exit_price: float,
        net_pnl: float,
        net_pnl_pct: float,
        total_fees: float,
        now: Optional[datetime] = None,
    ) -> str:
        pnl_emoji = "✅" if net_pnl >= 0 else "🔻"
        coin = self._format_coin_symbol(symbol)
        return self._format_alert_block(
            f"📤 <b>Position Closed</b>  {coin}  ({trigger_label})",
            [
                f"Entry <code>{entry_price:,.0f}</code> -> Exit <code>{exit_price:,.0f}</code>",
                f"{pnl_emoji} PnL <code>{net_pnl:+,.0f}</code> THB ({net_pnl_pct:+.2f}%)",
                f"Fees <code>{total_fees:,.0f}</code> THB",
            ],
            now=now,
        )
    
    def _format_trade_alert(self, decision: TradeDecision, result: OrderResult) -> str:
        """Format trade execution alert message — premium mobile-friendly style."""
        plan = decision.plan
        now = datetime.now()

        side_val   = plan.side.value.upper() if plan else "N/A"
        entry      = plan.entry_price if plan else 0
        fill_price = result.filled_price or entry
        size_thb   = result.filled_amount * fill_price
        sl         = plan.stop_loss if plan else 0
        tp         = plan.take_profit if plan else 0
        conf       = plan.confidence if plan else 0
        coin       = self._format_coin_symbol(plan.symbol) if plan else ""

        return self._format_alert_block(
            f"📥 <b>Position Opened</b>  {coin}",
            [
                f"Fill Price <code>{fill_price:,.0f}</code> THB",
                f"Notional <code>{size_thb:,.0f}</code> THB  |  Confidence {conf:.0%}",
                f"SL <code>{sl:,.0f}</code>  |  TP <code>{tp:,.0f}</code>",
            ],
            now=now,
        )

    def _format_skip_alert(self, decision: TradeDecision) -> str:
        """Format skipped trade alert message (log only, not sent to Telegram)."""
        plan = decision.plan
        reason = getattr(decision.risk_check, 'reason', getattr(decision.risk_check, 'reasons', 'Unknown reason'))
        coin = plan.symbol.replace("THB_", "") if plan else ""
        side = plan.side.value.upper() if plan else "N/A"

        message = (
            f"🛡️ <b>Risk Control</b>  {coin} {side}\n"
            f"Reason: {reason}"
        )
        return message
    
    def _format_pending_alert(self, decision: TradeDecision, portfolio: Dict[str, Any]) -> str:
        """Format pending trade alert message."""
        plan = decision.plan
        coin = self._format_coin_symbol(plan.symbol) if plan else ""
        with self._pending_decisions_lock:
            decision_id = len(self._pending_decisions) - 1

        return self._format_alert_block(
            f"⏳ <b>Approval Required</b>  {coin}",
            [
                f"Price <code>{plan.entry_price:,.0f}</code> THB  |  Confidence {plan.confidence:.0%}",
                f"Balance <code>{portfolio['balance']:,.0f}</code> THB",
                f"ID: <code>{decision_id}</code>  /approve {decision_id}",
            ],
        )
    
    def _format_dry_run_alert(self, decision: TradeDecision, portfolio: Dict[str, Any]) -> str:
        """Format dry run alert message (log only)."""
        plan = decision.plan
        coin = plan.symbol.replace("THB_", "") if plan else ""

        message = (
            f"🧪 <b>Dry Run</b>  {coin} {plan.side.value.upper()} @ {plan.entry_price:,.0f} THB  ({plan.confidence:.0%})"
        )

        return message
    
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
        coin = symbol.replace("THB_", "")
        msg = (
            f"Trailing SL  {coin}  {old_sl:,.0f} → {new_sl:,.0f}  "
            f"profit +{profit_pct:.2f}%  price {current_price:,.0f}"
        )
        self._send_alert(msg, to_telegram=False)

    def _build_multi_timeframe_status(self) -> Dict[str, Any]:
        timeframes = list(getattr(self, "mtf_timeframes", []) or [])
        status = {
            "enabled": bool(getattr(self, "mtf_enabled", False)),
            "mode": "confirmation",
            "timeframes": timeframes,
            "require_htf_confirmation": bool(getattr(self, "_mtf_confirmation_required", False)),
            "primary_timeframe": getattr(self, "timeframe", "1h"),
            "pairs": [],
            "last_signals": dict(getattr(self, "_last_mtf_status", {}) or {}),
        }
        if not status["enabled"] or not timeframes:
            return status

        db = getattr(self, "db", None)
        if db is None:
            return status

        try:
            conn = db.get_connection()
            cursor = conn.cursor()
            pair_summaries = []
            for pair in list(getattr(self, "trading_pairs", []) or []):
                timeframe_rows = []
                for timeframe in timeframes:
                    cursor.execute(
                        "SELECT COUNT(*), MAX(timestamp) FROM prices WHERE pair = ? AND COALESCE(timeframe, '1h') = ?",
                        (pair, timeframe),
                    )
                    count, latest = cursor.fetchone()
                    timeframe_rows.append({
                        "timeframe": timeframe,
                        "count": int(count or 0),
                        "latest": latest.isoformat() if hasattr(latest, "isoformat") else (str(latest) if latest else None),
                    })

                pair_summaries.append({
                    "pair": pair,
                    "timeframes": timeframe_rows,
                    "ready": all((row["count"] or 0) > 0 for row in timeframe_rows),
                })

            status["pairs"] = pair_summaries
        except Exception as exc:
            logger.debug("Failed to build multi-timeframe status: %s", exc)
            status["error"] = str(exc)
        finally:
            try:
                cursor.close()
                conn.close()
            except Exception:
                pass

        return status
    
    def get_status(self) -> Dict[str, Any]:
        """Get current bot status."""
        paused, pause_reason = self._is_paused()
        trading_pairs = getattr(self, "trading_pairs", [])
        trading_pair = getattr(self, "trading_pair", "")
        portfolio_state_getter = getattr(self, "_get_portfolio_state", lambda: {})
        portfolio_state = portfolio_state_getter() or {}
        risk_manager = getattr(self, "risk_manager", None)

        # WebSocket status
        if self._ws_client:
            ws_state = "connected" if self._ws_client.is_connected() else "disconnected"
            live_symbol = trading_pairs[0] if trading_pairs else trading_pair
            live_tick = get_latest_ticker(live_symbol) if _WEBSOCKET_AVAILABLE and callable(get_latest_ticker) else None
            live_price = live_tick.last if live_tick else None
        else:
            ws_state = "not_started"
            live_price = None

        balance_monitor = getattr(self, "_balance_monitor", None)
        balance_state = balance_monitor.get_state() if balance_monitor else None
        balance_events: List[Dict[str, str]] = []
        for row in list((balance_state or {}).get("last_events") or [])[:5]:
            event_type = str(row.get("event_type") or "BAL")
            coin = str(row.get("coin") or "").upper()
            amount = float(row.get("amount") or 0.0)
            message = f"{event_type} {coin} {amount:,.4f}".strip()
            balance_events.append(
                {
                    "timestamp": str(row.get("occurred_at") or "-"),
                    "type": event_type,
                    "message": message,
                }
            )

        recent_trades: List[Dict[str, str]] = []
        for row in list(getattr(self, "_executed_today", []) or [])[-5:]:
            decision = row.get("decision") if isinstance(row, dict) else None
            result = row.get("result") if isinstance(row, dict) else None
            side = "-"
            symbol = "-"
            if decision and getattr(decision, "plan", None):
                side = str(getattr(getattr(decision.plan, "side", None), "value", "-") or "-")
                symbol = str(getattr(decision.plan, "symbol", "-") or "-")
            status = str(getattr(result, "status", "filled") or "filled")
            timestamp = row.get("timestamp") if isinstance(row, dict) else None
            recent_trades.append(
                {
                    "timestamp": timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp or "-"),
                    "symbol": symbol,
                    "side": side,
                    "status": status,
                }
            )

        mtf_status = self._build_multi_timeframe_status()
        executor = getattr(self, "executor", None)
        open_positions = 0
        if executor and hasattr(executor, "get_open_orders"):
            try:
                open_positions = len(executor.get_open_orders())
            except Exception:
                open_positions = 0

        return {
            "running": getattr(self, "running", False),
            "mode": self.mode.value,
            "signal_source": self.signal_source.value,
            "strategy": "sniper_directional",
            "strategy_engine": {
                "enabled": True,
                "strategies": list(getattr(self, "enabled_strategies", [])),
            },
            "auth_degraded": {
                "active": getattr(self, "_auth_degraded", False),
                "reason": getattr(self, "_auth_degraded_reason", ""),
                "public_only": getattr(self, "_auth_degraded", False),
            },
            "trading_pairs": trading_pairs,
            "trading_paused": {
                "active": paused,
                "reason": pause_reason,
            },
            "timeframe": getattr(self, "timeframe", None),
            "interval_seconds": getattr(self, "interval_seconds", 0),
            "loop_count": getattr(self, "_loop_count", 0),
            "last_loop": self._last_loop_time.isoformat() if self._last_loop_time is not None else None,
            "pending_decisions": self._safe_pending_count(),
            "executed_today": len(getattr(self, "_executed_today", [])),
            "open_positions": open_positions,
            "balance_monitor": {
                "enabled": bool(balance_monitor),
                "running": bool(balance_monitor and balance_monitor.running),
                "updated_at": balance_state.get("updated_at") if balance_state else None,
                "last_event_count": len(balance_state.get("last_events", [])) if balance_state else 0,
            },
            "balance_events": balance_events,
            "recent_trades": recent_trades,
            "multi_timeframe": mtf_status,
            "websocket": {
                "enabled": getattr(self, "_ws_enabled", False),
                "state": ws_state,
                "live_price": live_price,
            },
            "risk_summary": risk_manager.get_risk_summary(portfolio_state.get("balance", 0)) if risk_manager else {}
        }

    def trigger_rebalance(self) -> Dict[str, Any]:
        """Rebalance is permanently disabled in sniper mode."""
        return {
            "status": "skipped",
            "reason": "Rebalance is disabled in sniper mode",
            "trigger": "manual",
        }
    
    def _safe_pending_count(self) -> int:
        with self._pending_decisions_lock:
            return len(self._pending_decisions)
    
    def get_pending_decisions(self) -> List[Dict[str, Any]]:
        """Get list of pending decisions for approval."""
        with self._pending_decisions_lock:
            decisions_copy = list(self._pending_decisions)
        return [
            {
                "id": i,
                "side": d.plan.side.value,
                "entry_price": d.plan.entry_price,
                "confidence": d.plan.confidence,
                "strategy_votes": d.plan.strategy_votes,
                "signal_source": d.signal_source.value,
                "risk_check_passed": d.risk_check.passed,
                "decision_time": d.decision_time.isoformat()
            }
            for i, d in enumerate(decisions_copy)
        ]

    def _check_positions_for_sl_tp(self):
        """
        Monitor open positions and execute SL/TP exits.
        Multi-pair: fetches price per position symbol (not just self.trading_pair).
        """
        open_orders = self.executor.get_open_orders()
        if not open_orders:
            return

        # Cache prices per symbol to avoid redundant API calls
        _price_cache: Dict[str, tuple] = {}

        def _get_price(symbol: str):
            if symbol in _price_cache:
                return _price_cache[symbol]
            price, source = None, "none"
            if self._ws_client and self._ws_client.is_connected() and _WEBSOCKET_AVAILABLE and callable(get_latest_ticker):
                tick = get_latest_ticker(symbol)
                if tick:
                    price, source = tick.last, "ws"
            if price is None:
                try:
                    ticker = self.api_client.get_ticker(symbol)
                    if isinstance(ticker, dict):
                        price = float(ticker.get("last", ticker.get("close", 0)))
                    source = "rest"
                except Exception as e:
                    logger.warning(f"Could not get price for {symbol}: {e}")
            _price_cache[symbol] = (price, source)
            return price, source

        for pos in open_orders:
            position_id = pos.get("order_id", "")
            entry_price = _coerce_trade_float(pos.get("entry_price"), 0.0)
            stop_loss = _coerce_trade_float(pos.get("stop_loss"), 0.0)
            take_profit = _coerce_trade_float(pos.get("take_profit"), 0.0)
            side = pos.get("side", OrderSide.BUY)
            amount = _coerce_trade_float(pos.get("amount"), 0.0)
            pos_symbol = pos.get("symbol", self.trading_pair)

            if self._state_machine_enabled:
                lifecycle = self._state_manager.get_state(pos_symbol)
                if lifecycle.state != TradeLifecycleState.IN_POSITION:
                    continue

            if not entry_price or entry_price == 0:
                continue

            # Get price for THIS position's symbol
            current_price, price_source = _get_price(pos_symbol)
            if current_price is None or current_price == 0:
                continue

            triggered = None

            if side == OrderSide.BUY:
                # For long positions: SL below entry, TP above entry
                if stop_loss > 0 and current_price <= stop_loss:
                    triggered = "SL"
                elif take_profit > 0 and current_price >= take_profit:
                    triggered = "TP"
            else:
                # For short positions (if supported): SL above entry, TP below entry
                if stop_loss > 0 and current_price >= stop_loss:
                    triggered = "SL"
                elif take_profit > 0 and current_price <= take_profit:
                    triggered = "TP"

            if triggered:
                logger.info(
                    f"[{price_source.upper()}-SLTP] {triggered} triggered for "
                    f"position {position_id} ({pos_symbol}) | Entry: {entry_price:,.0f} | "
                    f"Current: {current_price:,.0f} | SL: {stop_loss:,.0f} | "
                    f"TP: {take_profit:,.0f}"
                )

                exit_price = current_price

                if self._state_machine_enabled:
                    self._submit_managed_exit(
                        position_id=position_id,
                        pos_symbol=pos_symbol,
                        side=side,
                        amount=amount,
                        exit_price=exit_price,
                        triggered=triggered,
                        entry_price=entry_price,
                        total_entry_cost=pos.get("total_entry_cost", entry_price * amount),
                        price_source=price_source,
                        opened_at=pos.get("timestamp"),
                    )
                    continue

                # Determine exit side (opposite of entry)
                exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY

                result = self.executor.execute_exit(
                    position_id=position_id,
                    order_id=position_id,
                    side=exit_side,
                    amount=amount,
                    price=exit_price
                )

                if result.success:
                    # ── Net P/L with Bitkub 0.25% fees ────────────────
                    from trade_executor import BITKUB_FEE_PCT
                    now = datetime.now()
                    
                    entry_cost = pos.get("total_entry_cost", entry_price * amount)
                    entry_fee = entry_cost * BITKUB_FEE_PCT
                    
                    gross_exit = exit_price * amount
                    exit_fee = gross_exit * BITKUB_FEE_PCT
                    net_exit = gross_exit - exit_fee
                    
                    total_fees = entry_fee + exit_fee
                    net_pnl = net_exit - entry_cost
                    net_pnl_pct = (net_pnl / entry_cost * 100) if entry_cost > 0 else 0
                    
                    pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"
                    trigger_emoji = "🎯" if triggered == "TP" else "🛑"
                    
                    # ── Log closed trade to DB ─────────────────────────
                    try:
                        self.db.log_closed_trade({
                            "symbol": pos_symbol, "side": side,
                            "amount": amount, "entry_price": entry_price,
                            "exit_price": exit_price, "entry_cost": entry_cost,
                            "gross_exit": gross_exit, "entry_fee": entry_fee,
                            "exit_fee": exit_fee, "total_fees": total_fees,
                            "net_pnl": net_pnl, "net_pnl_pct": net_pnl_pct,
                            "trigger": triggered, "price_source": price_source,
                            "opened_at": pos.get("timestamp"),
                            "closed_at": now,
                        })
                    except Exception as e:
                        logger.error(f"Failed to log closed trade: {e}", exc_info=True)
                    
                    trigger_label = "Take Profit" if triggered == "TP" else ("Stop Loss" if triggered == "SL" else (triggered or "Exit"))
                    msg = self._format_exit_alert(
                        pos_symbol,
                        trigger_label,
                        entry_price,
                        exit_price,
                        net_pnl,
                        net_pnl_pct,
                        total_fees,
                        now=now,
                    )
                    self._send_alert(msg, to_telegram=True)
                else:
                    logger.error(f"Failed to execute {triggered} exit: {result.message}")
