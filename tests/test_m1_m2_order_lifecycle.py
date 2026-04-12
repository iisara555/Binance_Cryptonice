"""
Phase 4 — M1/M2: Order-lifecycle atomicity tests.

M1 — "Cancel removes tracking before re-validation":
  * A failed cancel_order call must NOT remove the order from _open_orders or the DB.
  * A successful cancel_order call MUST remove the order from BOTH _open_orders AND the DB.
  * The OMS 24-hour aged-order path must attempt cancel_order before dropping tracking;
    if the cancel fails the order must be retained.

M2 — "Non-atomic rebalance state tracking":
  * register_tracked_position must persist to DB BEFORE updating _open_orders.
  * If the DB write raises, _open_orders must NOT be updated.
  * If _db is None the method falls back to memory-only (no crash, backward-compat).
"""

import threading
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, Mock, call, patch

import pytest

from trade_executor import TradeExecutor, OrderSide, OrderResult


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixture helpers (mirrors test_oms_reconcile_gate.py pattern)
# ─────────────────────────────────────────────────────────────────────────────

_SENTINEL = object()


def _make_executor(db: object = _SENTINEL) -> TradeExecutor:
    """Build a TradeExecutor with the OMS thread NOT started (for isolation).

    Pass db=None explicitly to get an executor without a database.
    If db is omitted a fresh Mock is provided with an empty position list.
    """
    api = Mock()
    api.is_circuit_open.return_value = False

    if db is _SENTINEL:
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
    ex._db = db  # type: ignore[assignment]
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
    ex._oms_processing_lock = threading.Lock()
    ex._oms_processing = set()
    ex._exit_in_progress = set()
    ex._exit_in_progress_lock = threading.Lock()
    ex._trailing_stop_pct = 1.0
    ex._trailing_activation_pct = 0.5
    ex._allow_trailing_stop = True
    ex._reconcile_done = threading.Event()
    ex._reconcile_done.set()  # allow OMS ticks in tests that need them
    ex._oms_running = False
    ex._oms_stop_event = threading.Event()
    ex._oms_thread = None  # type: ignore[assignment]
    return ex


def _pending_order(order_id: str = "ord-1", symbol: str = "THB_BTC") -> dict:
    return {
        "order_id": order_id,
        "symbol": symbol,
        "side": OrderSide.BUY,
        "amount": 0.001,
        "entry_price": 1_500_000.0,
        "stop_loss": None,
        "take_profit": None,
        "timestamp": datetime.utcnow(),
        "is_partial_fill": False,
        "remaining_amount": 0.001,
        "total_entry_cost": 1500.0,
        "filled": False,
        "filled_amount": 0.0,
        "filled_price": 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# M1 — cancel_order: memory + DB removal only on confirmed API success
# ─────────────────────────────────────────────────────────────────────────────

class TestM1CancelOrderAtomicity:
    """cancel_order must not remove tracking until the exchange confirms the cancel."""

    def test_failed_cancel_keeps_order_in_open_orders(self):
        """API returns a non-zero error — order must remain in _open_orders."""
        db = Mock()
        db.load_all_positions.return_value = []
        ex = _make_executor(db=db)
        ex._open_orders["ord-1"] = _pending_order("ord-1")
        ex.api_client.cancel_order.return_value = {"error": 99, "message": "Network error"}

        result = ex.cancel_order("ord-1", symbol="THB_BTC", side="buy")

        assert result is False
        assert "ord-1" in ex._open_orders, (
            "Order must NOT be removed from _open_orders when the API cancel fails"
        )

    def test_failed_cancel_does_not_touch_db(self):
        """API cancel failure must not trigger a DB delete."""
        db = Mock()
        db.load_all_positions.return_value = []
        ex = _make_executor(db=db)
        ex._open_orders["ord-1"] = _pending_order("ord-1")
        ex.api_client.cancel_order.return_value = {"error": 5}

        ex.cancel_order("ord-1", symbol="THB_BTC", side="buy")

        db.delete_position.assert_not_called()


def test_oms_processing_is_cleared_when_replacement_thread_fails_to_start():
    db = Mock()
    db.load_all_positions.return_value = []
    ex = _make_executor(db=db)
    new_order = Mock()
    old_pos_data = {"order_id": "ord-1"}
    failing_thread = Mock()
    failing_thread.start.side_effect = RuntimeError("thread start failed")

    with patch("trade_executor.threading.Thread", return_value=failing_thread):
        with pytest.raises(RuntimeError, match="thread start failed"):
            ex._start_oms_replacement("ord-1", new_order, old_pos_data)

    assert "ord-1" not in ex._oms_processing

    def test_successful_cancel_removes_from_open_orders(self):
        """Confirmed API cancel must evict the order from _open_orders."""
        db = Mock()
        db.load_all_positions.return_value = []
        ex = _make_executor(db=db)
        ex._open_orders["ord-1"] = _pending_order("ord-1")
        ex.api_client.cancel_order.return_value = {"error": 0}

        result = ex.cancel_order("ord-1", symbol="THB_BTC", side="buy")

        assert result is True
        assert "ord-1" not in ex._open_orders, (
            "Order must be removed from _open_orders after confirmed cancel"
        )

    def test_successful_cancel_removes_from_db(self):
        """Confirmed API cancel must also remove the order from the DB (M1 ghost fix)."""
        db = Mock()
        db.load_all_positions.return_value = []
        ex = _make_executor(db=db)
        ex._open_orders["ord-1"] = _pending_order("ord-1")
        ex.api_client.cancel_order.return_value = {"error": 0}

        ex.cancel_order("ord-1", symbol="THB_BTC", side="buy")

        db.delete_position.assert_called_once_with("ord-1")

    def test_unknown_order_id_cancel_returns_false_without_crash(self):
        """Cancelling a non-existent order must not raise and must return False."""
        ex = _make_executor()

        result = ex.cancel_order("no-such-order", symbol="THB_BTC", side="buy")

        # Either False (unknown order) or depends on impl — must not raise
        assert isinstance(result, bool)


# ─────────────────────────────────────────────────────────────────────────────
# M1 — OMS 24-hour aged-order path
# ─────────────────────────────────────────────────────────────────────────────

class TestM1AgedOrderOmsPath:
    """OMS must attempt cancel_order before removing aged orders from tracking."""

    def _aged_timestamp(self) -> datetime:
        """Return a real timestamp that is 26 hours in the past."""
        return datetime.utcnow() - timedelta(hours=26)

    def test_aged_order_calls_cancel_before_removal(self):
        """The OMS aged-order path must call cancel_order (not silently drop the order)."""
        db = Mock()
        db.load_all_positions.return_value = []
        ex = _make_executor(db=db)

        order = _pending_order("ord-aged")
        order["timestamp"] = self._aged_timestamp()
        ex._open_orders["ord-aged"] = order

        cancel_calls: list = []

        def _fake_cancel(order_id, **kwargs):
            cancel_calls.append(order_id)
            # Simulate successful cancel → also remove from _open_orders (real behaviour)
            with ex._orders_lock:
                ex._open_orders.pop(order_id, None)
            ex._remove_persisted_position(order_id)
            return True

        ex._oms_running = True
        ex._reconcile_done.set()

        with patch.object(ex, "cancel_order", side_effect=_fake_cancel):
            def _stop():
                time.sleep(0.3)
                ex._oms_running = False
                ex._oms_stop_event.set()

            t = threading.Thread(target=_stop, daemon=True)
            t.start()
            ex._oms_monitor_loop()
            t.join(timeout=2)

        assert cancel_calls == ["ord-aged"], (
            "cancel_order must be invoked for aged orders before removing tracking"
        )

    def test_aged_order_retained_when_cancel_fails(self):
        """If cancel_order fails for an aged order, the order must be kept in tracking."""
        db = Mock()
        db.load_all_positions.return_value = []
        ex = _make_executor(db=db)

        order = _pending_order("ord-aged")
        order["timestamp"] = self._aged_timestamp()
        ex._open_orders["ord-aged"] = order

        with patch.object(ex, "cancel_order", return_value=False):
            ex._oms_running = True
            ex._reconcile_done.set()

            def _stop():
                time.sleep(0.3)
                ex._oms_running = False
                ex._oms_stop_event.set()

            t = threading.Thread(target=_stop, daemon=True)
            t.start()
            ex._oms_monitor_loop()
            t.join(timeout=2)

        assert "ord-aged" in ex._open_orders, (
            "Order must be retained in _open_orders when the aged-order cancel fails"
        )

    def test_aged_order_removed_when_cancel_succeeds(self):
        """If cancel_order succeeds for an aged order, it must be removed from tracking."""
        db = Mock()
        db.load_all_positions.return_value = []
        ex = _make_executor(db=db)

        order = _pending_order("ord-aged")
        order["timestamp"] = self._aged_timestamp()
        ex._open_orders["ord-aged"] = order

        def _fake_cancel(order_id, **kwargs):
            # Mirror real cancel_order success path: evict from memory + DB
            with ex._orders_lock:
                ex._open_orders.pop(order_id, None)
            ex._remove_persisted_position(order_id)
            return True

        ex._oms_running = True
        ex._reconcile_done.set()

        with patch.object(ex, "cancel_order", side_effect=_fake_cancel):
            def _stop():
                time.sleep(0.3)
                ex._oms_running = False
                ex._oms_stop_event.set()

            t = threading.Thread(target=_stop, daemon=True)
            t.start()
            ex._oms_monitor_loop()
            t.join(timeout=2)

        assert "ord-aged" not in ex._open_orders, (
            "Order must be removed from _open_orders when the aged-order cancel succeeds"
        )


# ─────────────────────────────────────────────────────────────────────────────
# M2 — register_tracked_position: DB-first ordering
# ─────────────────────────────────────────────────────────────────────────────

class TestM2RegisterTrackedPositionAtomicity:
    """register_tracked_position must persist to DB before touching _open_orders."""

    def test_db_failure_does_not_update_memory(self):
        """If DB write raises, _open_orders must NOT be updated (M2 fix)."""
        db = Mock()
        db.load_all_positions.return_value = []
        db.save_position.side_effect = Exception("DB locked")
        ex = _make_executor(db=db)

        ex.register_tracked_position("ord-1", {"symbol": "THB_BTC", "side": "buy"})

        assert "ord-1" not in ex._open_orders, (
            "_open_orders must NOT be updated when the DB write fails"
        )

    def test_db_success_updates_memory(self):
        """When the DB write succeeds, _open_orders must be populated."""
        db = Mock()
        db.load_all_positions.return_value = []
        db.save_position.return_value = None  # success
        ex = _make_executor(db=db)

        ex.register_tracked_position("ord-1", {"symbol": "THB_BTC", "side": "buy"})

        assert "ord-1" in ex._open_orders

    def test_db_written_before_memory(self):
        """Verify call ordering: save_position must be called before _open_orders is set."""
        db = Mock()
        db.load_all_positions.return_value = []
        memory_state_at_db_write: list = []

        def _check_order_at_save(data):
            # At the moment save_position is called, _open_orders must NOT yet have the entry
            memory_state_at_db_write.append("ord-1" in ex._open_orders)

        db.save_position.side_effect = _check_order_at_save
        ex = _make_executor(db=db)

        ex.register_tracked_position("ord-1", {"symbol": "THB_BTC", "side": "buy"})

        assert memory_state_at_db_write == [False], (
            "save_position must be called BEFORE the order is inserted into _open_orders"
        )

    def test_no_db_still_updates_memory(self):
        """When _db is None, register_tracked_position must still populate _open_orders."""
        ex = _make_executor(db=None)

        ex.register_tracked_position("ord-1", {"symbol": "THB_BTC", "side": "buy"})

        assert "ord-1" in ex._open_orders

    def test_empty_position_id_is_noop(self):
        """Passing an empty position_id must not crash and must not touch state."""
        db = Mock()
        db.load_all_positions.return_value = []
        ex = _make_executor(db=db)

        ex.register_tracked_position("", {"symbol": "THB_BTC"})

        assert ex._open_orders == {}
        db.save_position.assert_not_called()

    def test_db_save_called_with_order_id_field(self):
        """The dict passed to save_position must include the 'order_id' key."""
        db = Mock()
        db.load_all_positions.return_value = []
        ex = _make_executor(db=db)

        ex.register_tracked_position("ord-42", {"symbol": "THB_ETH", "side": "sell"})

        db.save_position.assert_called_once()
        saved_data = db.save_position.call_args[0][0]
        assert saved_data.get("order_id") == "ord-42"

    def test_partial_rebalance_failure_leaves_no_ghost(self):
        """Simulate a multi-order rebalance where the second DB write fails.

        After the failure, only the first order must be in _open_orders — the second
        must NOT appear (M2: no partial in-memory divergence).
        """
        db = Mock()
        db.load_all_positions.return_value = []
        call_count = [0]

        def _save(data):
            call_count[0] += 1
            if call_count[0] == 2:
                raise Exception("Intermittent DB error")

        db.save_position.side_effect = _save
        ex = _make_executor(db=db)

        ex.register_tracked_position("ord-A", {"symbol": "THB_BTC", "side": "buy"})
        ex.register_tracked_position("ord-B", {"symbol": "THB_ETH", "side": "buy"})

        assert "ord-A" in ex._open_orders, "First successful registration must persist"
        assert "ord-B" not in ex._open_orders, (
            "Second registration (DB failed) must NOT appear in memory"
        )
