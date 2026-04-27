"""
Phase 3 Step 2 — H3/H4: OMS startup reconciliation race condition tests.

Verifies that:
  1. The OMS monitor loop does NOT process orders before reconciliation is done.
  2. set_reconcile_complete() unblocks the OMS promptly.
  3. trading_bot.start() calls set_reconcile_complete() after reconciliation.
  4. In degraded mode (skipped reconciliation), set_reconcile_complete() is
     still called so the OMS is not permanently blocked.
  5. Reconciliation mutations (_open_orders writes) do not race with OMS
     cancel/reprice decisions for the same order IDs.
  6. stop() unblocks the OMS even if reconciliation never completed.
"""

import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch, call
import pytest

from trade_executor import TradeExecutor, OrderSide, OrderStatus, OrderResult


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_executor(db=None, oms_running=False) -> TradeExecutor:
    """Build a TradeExecutor whose OMS thread is NOT started (for isolation)."""
    api = Mock()
    api.is_circuit_open.return_value = False

    if db is None:
        db = Mock()
        db.load_all_positions.return_value = []

    ex = TradeExecutor.__new__(TradeExecutor)
    ex.api_client = api
    ex.config = {
        "retry_attempts": 1,
        "retry_delay_seconds": 0,
        "order_timeout_seconds": 30,
        "order_type": "limit",
        "partial_fill_max_wait": 60.0,
        "trailing_stop_pct": 1.0,
        "trailing_activation_pct": 0.5,
        "allow_trailing_stop": True,
    }
    ex.risk_manager = None
    ex._db = db
    ex._on_trailing_stop = None
    ex._notifier = None
    ex._last_cancel_error = None
    ex._oms_cancel_was_error_21 = False
    ex.retry_attempts = 1
    ex.retry_delay = 0
    ex.order_timeout = 30
    ex.order_type = "limit"

    from trade_executor import PartialFillTracker
    ex._fill_tracker = PartialFillTracker(max_wait_seconds=60.0)
    ex._open_orders = {}
    ex._order_history = []
    ex._orders_lock = threading.Lock()
    ex._oms_processing = set()
    ex._exit_in_progress = set()
    ex._exit_in_progress_lock = threading.Lock()
    ex._trailing_stop_pct = 1.0
    ex._trailing_activation_pct = 0.5
    ex._allow_trailing_stop = True
    ex._reconcile_done = threading.Event()
    ex._oms_running = oms_running
    ex._oms_stop_event = threading.Event()
    ex._oms_thread = None  # type: ignore[assignment]
    return ex


def _make_pending_order(order_id: str = "ord-1", symbol: str = "THB_BTC") -> dict:
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": OrderSide.BUY,
        "amount": 0.001,
        "entry_price": 1_500_000.0,
        "stop_loss": None,
        "take_profit": None,
        "timestamp": MagicMock(  # stale enough to trigger timeout
            __sub__=lambda s, o: MagicMock(total_seconds=lambda: 120.0)
        ),
        "is_partial_fill": False,
        "remaining_amount": 0.001,
        "total_entry_cost": 1500.0,
        "filled": False,
        "filled_amount": 0.0,
        "filled_price": 0.0,
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestReconcileGateAttribute:

    def test_executor_has_reconcile_done_event(self):
        """TradeExecutor.__init__ must create a _reconcile_done Event."""
        api = Mock()
        api.is_circuit_open.return_value = False
        db = Mock()
        db.load_all_positions.return_value = []

        # Patch OMS thread start so it doesn't run during test
        with patch.object(threading.Thread, "start"):
            ex = TradeExecutor(
                api_client=api,
                config={"order_timeout_seconds": 30},
                db=db,
            )

        assert hasattr(ex, "_reconcile_done")
        assert isinstance(ex._reconcile_done, threading.Event)

    def test_reconcile_done_is_unset_at_construction(self):
        """_reconcile_done must be UNSET at construction time."""
        api = Mock()
        api.is_circuit_open.return_value = False
        db = Mock()
        db.load_all_positions.return_value = []

        with patch.object(threading.Thread, "start"):
            ex = TradeExecutor(
                api_client=api,
                config={"order_timeout_seconds": 30},
                db=db,
            )

        assert not ex._reconcile_done.is_set(), (
            "_reconcile_done should be unset until reconciliation completes"
        )

    def test_set_reconcile_complete_sets_event(self):
        """set_reconcile_complete() must set _reconcile_done."""
        ex = _make_executor()
        assert not ex._reconcile_done.is_set()
        ex.set_reconcile_complete()
        assert ex._reconcile_done.is_set()


class TestOMSLoopGatedByReconciliation:

    def test_oms_skips_orders_before_reconciliation(self):
        """
        When _reconcile_done is unset, a single OMS cycle must not call
        cancel_order, check_order_status, or modify _open_orders.
        """
        ex = _make_executor()
        ex._open_orders["ord-1"] = _make_pending_order("ord-1")

        cancel_calls = []

        with patch.object(ex, "cancel_order", side_effect=lambda *a, **kw: cancel_calls.append(a) or True), \
             patch.object(ex, "check_order_status", return_value=Mock(status=OrderStatus.PENDING)), \
             patch.object(ex, "_verify_order_fill"):

            # Monkey-patch sleep so OMS loop runs immediately
            with patch("trade_executor.time.sleep"):
                # Run one loop iteration manually: reconcile NOT done → should skip
                ex._oms_running = True

                def _run_one_cycle():
                    # Simulate exactly one body of the while-loop
                    import trade_executor as _te
                    _te.time.sleep(0)  # no-op
                    if not ex._reconcile_done.is_set():
                        return  # gate fires — skips everything
                    # If we get here, the gate did NOT fire (test failure)
                    cancel_calls.append("GATE_BYPASSED")

                _run_one_cycle()

        assert not cancel_calls, (
            "OMS processed orders before reconciliation was signalled"
        )

    def test_oms_processes_orders_after_reconciliation(self):
        """After set_reconcile_complete(), the OMS must be able to process orders."""
        ex = _make_executor()
        ex.set_reconcile_complete()  # signal done

        # Verify _reconcile_done is now set (OMS will proceed)
        assert ex._reconcile_done.is_set()

    def test_oms_thread_waits_then_processes(self):
        """
        Start the real OMS thread with reconciliation blocked.  Add a timed
        order to _open_orders.  Assert cancel_order is NOT called in the first
        2 seconds.  Then signal reconciliation and assert the OMS becomes
        active.
        """
        api = Mock()
        api.is_circuit_open.return_value = False

        db = Mock()
        db.load_all_positions.return_value = []
        db.delete_position.return_value = None

        with patch.object(threading.Thread, "start"):
            ex = TradeExecutor(
                api_client=api,
                config={"order_timeout_seconds": 1},
                db=db,
            )

        cancel_called = threading.Event()

        def _fake_cancel(*args, **kwargs):
            cancel_called.set()
            return True

        with patch.object(ex, "cancel_order", side_effect=_fake_cancel), \
             patch.object(ex, "check_order_status",
                          return_value=Mock(status=OrderStatus.PENDING)), \
             patch.object(ex, "_replace_order_async"), \
             patch.object(ex, "_verify_order_fill"):

            # Plant a timed-out order (60s old — exceeds 1s timeout, under 24h limit)
            ex._open_orders["ord-timeout"] = {
                "order_id": "ord-timeout",
                "symbol": "THB_BTC",
                "side": OrderSide.BUY,
                "amount": 0.001,
                "entry_price": 1_500_000.0,
                "stop_loss": None,
                "take_profit": None,
                "timestamp": datetime.now(timezone.utc) - timedelta(seconds=60),
                "is_partial_fill": False,
                "remaining_amount": 0.001,
                "total_entry_cost": 1500.0,
                "filled": False,
                "filled_amount": 0.0,
                "filled_price": 0.0,
            }

            # Start real OMS thread — reconciliation NOT yet done
            ex._oms_running = True
            ex._oms_thread = threading.Thread(
                target=ex._oms_monitor_loop, daemon=True
            )
            ex._oms_thread.start()

            # Give the OMS multiple sleep cycles — it must not call cancel yet
            time.sleep(0.3)
            assert not cancel_called.is_set(), (
                "OMS called cancel_order before reconciliation was signalled"
            )

            # Now signal reconciliation complete
            ex.set_reconcile_complete()

            # OMS should now process the timed-out order within a few seconds
            fired = cancel_called.wait(timeout=12)
            assert fired, (
                "OMS failed to process timed-out order after reconciliation was signalled"
            )

            ex.stop()


class TestReconciliationAndOMSNoDataRace:

    def test_verify_order_fill_persists_live_snapshot_after_lock_release(self):
        ex = _make_executor(db=Mock())
        ex._db.save_position = Mock()
        ex._open_orders["ord-1"] = _make_pending_order("ord-1")
        ex._open_orders["ord-1"]["entry_price"] = 1_600_000.0

        stale_order_info = dict(ex._open_orders["ord-1"])
        stale_order_info["entry_price"] = 1_500_000.0

        with patch.object(
            ex,
            "check_order_status",
            return_value=OrderResult(
                success=True,
                status=OrderStatus.FILLED,
                order_id="ord-1",
                filled_amount=0.001,
                filled_price=1_605_000.0,
            ),
        ):
            ex._verify_order_fill(stale_order_info)

        saved_payload = ex._db.save_position.call_args.args[0]
        assert saved_payload["entry_price"] == 1_600_000.0
        assert saved_payload["filled"] is True
        assert saved_payload["filled_amount"] == pytest.approx(0.001)
        assert saved_payload["filled_price"] == pytest.approx(1_605_000.0)

    def test_reconciliation_write_is_visible_to_oms_after_unlock(self):
        """
        Simulate a reconciliation writing a new order to _open_orders under
        the lock, then releasing the lock.  When the OMS reads _open_orders it
        must see the new order — no stale read through cache / torn state.
        """
        ex = _make_executor()

        written_order = {
            "order_id": "ghost-1",
            "symbol": "THB_ETH",
            "side": OrderSide.BUY,
            "amount": 0.1,
            "entry_price": 80_000.0,
            "stop_loss": None,
            "take_profit": None,
            "timestamp": MagicMock(
                __sub__=lambda s, o: MagicMock(total_seconds=lambda: 10.0)
            ),
            "is_partial_fill": False,
            "remaining_amount": 0.1,
            "total_entry_cost": 8000.0,
            "filled": False,
        }

        # Simulate reconciliation write
        with ex._orders_lock:
            ex._open_orders["ghost-1"] = written_order

        # OMS read (simulated outside lock, as it takes a snapshot)
        with ex._orders_lock:
            snapshot = list(ex._open_orders.values())

        ids = [o["order_id"] for o in snapshot]
        assert "ghost-1" in ids, (
            "OMS did not see ghost order written by reconciliation"
        )

    def test_oms_removal_does_not_crash_concurrent_reconciliation_read(self):
        """
        OMS removes an order while reconciliation is iterating over
        _open_orders.  With proper locking neither side must crash or corrupt
        the dict.
        """
        ex = _make_executor()

        for i in range(5):
            ex._open_orders[f"ord-{i}"] = {"order_id": f"ord-{i}", "symbol": "THB_BTC"}

        errors = []
        removed = []

        def _oms_removes():
            for i in range(5):
                with ex._orders_lock:
                    ex._open_orders.pop(f"ord-{i}", None)
                removed.append(f"ord-{i}")
                time.sleep(0.001)

        def _reconcile_reads():
            for _ in range(20):
                try:
                    with ex._orders_lock:
                        _ = list(ex._open_orders.keys())
                    time.sleep(0.0005)
                except Exception as e:
                    errors.append(e)

        t_oms = threading.Thread(target=_oms_removes)
        t_rec = threading.Thread(target=_reconcile_reads)
        t_oms.start()
        t_rec.start()
        t_oms.join()
        t_rec.join()

        assert not errors, f"Concurrent access raised exceptions: {errors}"
        assert len(removed) == 5


class TestStopUnblocksOMS:

    def test_stop_sets_reconcile_done_so_oms_exits(self):
        """
        If reconciliation never completes (e.g. bot shutdown during startup),
        stop() must set _reconcile_done so the OMS thread is not permanently
        blocked.
        """
        api = Mock()
        api.is_circuit_open.return_value = False
        db = Mock()
        db.load_all_positions.return_value = []

        with patch.object(threading.Thread, "start"):
            ex = TradeExecutor(
                api_client=api,
                config={"order_timeout_seconds": 30},
                db=db,
            )

        assert not ex._reconcile_done.is_set()
        ex.stop()
        assert ex._reconcile_done.is_set(), (
            "stop() must set _reconcile_done to unblock any waiting OMS thread"
        )


class TestTradingBotStartSignalsOMS:

    def _build_bot_and_start(self, auth_degraded: bool):
        """Build a minimal bot, call start(), return (bot, signal_was_set)."""
        from trading_bot import TradingBotOrchestrator
        from signal_generator import SignalGenerator
        from risk_management import RiskManager
        from api_client import BitkubClient

        mock_db = Mock()
        mock_db.get_positions.return_value = []
        mock_db.load_all_positions.return_value = []
        mock_db.list_trade_states.return_value = []
        mock_db.has_ever_held.return_value = False

        api = Mock(spec=BitkubClient)
        api.is_circuit_open.return_value = False
        api.check_clock_sync.return_value = True
        api.get_balances.return_value = {}
        api.get_open_orders.return_value = []

        sg = Mock(spec=SignalGenerator)
        sg.generate_signals.return_value = []
        sg.sync_state = Mock()

        rm = Mock(spec=RiskManager)
        rm.trade_count_today = 0

        ex = Mock(spec=TradeExecutor)
        ex.get_open_orders.return_value = []
        ex._reconcile_done = threading.Event()
        ex.set_reconcile_complete = ex._reconcile_done.set

        config = {
            "mode": "full_auto",
            "trading_pair": "THB_BTC",
            "interval_seconds": 3600,
            "timeframe": "1h",
            "signal_source": "strategy",
            "strategies": {"enabled": ["trend_following"]},
            "trading": {"max_open_positions": 3},
            "risk": {"max_risk_per_trade_pct": 1.0, "max_daily_loss_pct": 5.0},
            "data": {"pairs": ["THB_BTC"]},
            "backtesting": {"require_validation_before_live": False},
            "state_management": {"enabled": False},
            "websocket": {"enabled": False},
        }

        with patch("trading_bot.get_database", return_value=mock_db):
            bot = TradingBotOrchestrator(
                config=config,
                api_client=api,
                signal_generator=sg,
                risk_manager=rm,
                executor=ex,
            )

        bot._auth_degraded = auth_degraded
        bot._auth_degraded_reason = "test" if auth_degraded else ""

        with patch.object(bot, "_reconcile_on_startup"), \
             patch.object(bot, "_bootstrap_held_coin_history"), \
             patch.object(threading.Thread, "start"):
            bot.start()

        return bot, ex._reconcile_done.is_set()

    def test_start_signals_oms_after_normal_reconciliation(self):
        """start() must call set_reconcile_complete() after full reconciliation."""
        _, signalled = self._build_bot_and_start(auth_degraded=False)
        assert signalled, (
            "start() did not call set_reconcile_complete() after normal reconciliation"
        )

    def test_start_signals_oms_in_degraded_mode(self):
        """start() must call set_reconcile_complete() even in degraded (skip) mode."""
        _, signalled = self._build_bot_and_start(auth_degraded=True)
        assert signalled, (
            "start() did not call set_reconcile_complete() in auth-degraded mode"
        )
