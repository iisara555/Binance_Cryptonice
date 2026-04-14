"""
Phase 4 Step 3 — M3/M4: State persistence atomicity tests.

M3 — "Trailing stop peak not persisted atomically":
  * DB write must happen BEFORE _open_orders is updated (DB-first ordering).
  * If the DB write raises, _open_orders must NOT be mutated.
  * On success, both DB and memory must be updated.

M4 — "Risk state file has no file locking":
  * RiskManager must own a threading.Lock (_state_lock).
  * Concurrent save_state() calls must not corrupt the JSON file.
  * save_state() must acquire _state_lock before writing.
  * load_state() must acquire _state_lock before reading + mutating state.
"""

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch, call

import pytest

from risk_management import RiskManager, RiskConfig


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers (mirrors test_m1_m2_order_lifecycle._make_executor pattern)
# ─────────────────────────────────────────────────────────────────────────────

_SENTINEL = object()


def _make_executor(db: object = _SENTINEL):
    """Build a TradeExecutor with the OMS thread NOT started."""
    import threading as _t
    from trade_executor import TradeExecutor, PartialFillTracker

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
        "trailing_stop_pct": 2.0,
        "trailing_activation_pct": 1.0,
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
    ex._fill_tracker = PartialFillTracker(max_wait_seconds=60.0)
    ex._open_orders = {}
    ex._order_history = []
    ex._orders_lock = _t.Lock()
    ex._oms_processing = set()
    ex._exit_in_progress = set()
    ex._exit_in_progress_lock = _t.Lock()
    ex._trailing_stop_pct = 2.0
    ex._trailing_activation_pct = 1.0
    ex._allow_trailing_stop = True
    ex._reconcile_done = _t.Event()
    ex._reconcile_done.set()
    ex._oms_running = False
    ex._oms_thread = None  # type: ignore[assignment]
    return ex


def _make_rm(tmp_path: Path) -> RiskManager:
    """Build a RiskManager whose state file lives in tmp_path (no persistent side effects)."""
    rm = RiskManager(RiskConfig())
    rm._state_file = tmp_path / "risk_state.json"
    return rm


# ─────────────────────────────────────────────────────────────────────────────
# M3 — _apply_trailing_stop DB-first ordering
# ─────────────────────────────────────────────────────────────────────────────

class TestM3TrailingStopAtomicity:
    """_apply_trailing_stop must write to DB before updating _open_orders."""

    def _order_with_profit(self, entry: float, current: float) -> dict:
        """Minimal order dict that will trigger a trailing stop update."""
        from trade_executor import OrderSide
        return {
            "order_id": "ts-ord-1",
            "symbol": "THB_BTC",
            "side": OrderSide.BUY,
            "entry_price": entry,
            "stop_loss": entry * 0.95,
            "trailing_peak": entry,
        }

    def _call_apply_trailing_stop(self, ex, entry: float, current: float):
        """
        Call _apply_trailing_stop with a WebSocket tick mock for `current` price.
        """
        order_info = self._order_with_profit(entry, current)
        ex._open_orders["ts-ord-1"] = dict(order_info)
        with patch(
            "bitkub_websocket.get_latest_ticker",
            return_value=Mock(last=current, symbol="THB_BTC"),
        ):
            ex._apply_trailing_stop(order_info)

    def test_db_written_before_memory_on_trailing_stop(self):
        """DB must be updated BEFORE _open_orders is mutated (M3 DB-first ordering)."""
        db = Mock()
        db.load_all_positions.return_value = []
        memory_state_at_db_write: list = []

        def _check(order_id, new_sl, trailing_peak=None):
            # At this moment _open_orders should still show the OLD stop_loss
            memory_state_at_db_write.append(
                ex._open_orders.get("ts-ord-1", {}).get("stop_loss")
            )

        db.update_position_sl.side_effect = _check
        ex = _make_executor(db=db)

        entry = 1_000_000.0
        current = entry * 1.05  # +5% profit — above 1% activation threshold
        self._call_apply_trailing_stop(ex, entry, current)

        assert memory_state_at_db_write, "update_position_sl must be called"
        assert memory_state_at_db_write[0] == pytest.approx(entry * 0.95, rel=1e-4), (
            "update_position_sl must be called before _open_orders is updated; "
            "memory showed new SL at DB write time — DB-first violated"
        )

    def test_db_failure_does_not_update_memory(self):
        """If DB write raises, _open_orders must NOT be mutated (M3 fix)."""
        db = Mock()
        db.load_all_positions.return_value = []
        db.update_position_sl.side_effect = Exception("DB locked")
        ex = _make_executor(db=db)

        entry = 1_000_000.0
        current = entry * 1.05
        original_sl = entry * 0.95
        ex._open_orders["ts-ord-1"] = self._order_with_profit(entry, current)
        ex._open_orders["ts-ord-1"]["stop_loss"] = original_sl

        with patch(
            "bitkub_websocket.get_latest_ticker",
            return_value=Mock(last=current, symbol="THB_BTC"),
        ):
            order_info = dict(ex._open_orders["ts-ord-1"])
            ex._apply_trailing_stop(order_info)

        assert ex._open_orders["ts-ord-1"]["stop_loss"] == pytest.approx(original_sl, rel=1e-6), (
            "_open_orders stop_loss must NOT change when DB write fails"
        )
        assert "trailing_peak" not in ex._open_orders["ts-ord-1"] or (
            ex._open_orders["ts-ord-1"].get("trailing_peak") == entry
        ), "_open_orders trailing_peak must NOT be updated when DB write fails"

    def test_db_success_updates_both_db_and_memory(self):
        """When DB write succeeds, both DB and memory must reflect the new SL and peak."""
        db = Mock()
        db.load_all_positions.return_value = []
        ex = _make_executor(db=db)

        entry = 1_000_000.0
        current = entry * 1.05

        self._call_apply_trailing_stop(ex, entry, current)

        db.update_position_sl.assert_called_once()
        call_args = db.update_position_sl.call_args
        assert call_args[0][0] == "ts-ord-1"
        assert call_args[1].get("trailing_peak") == pytest.approx(current, rel=1e-6)

        assert ex._open_orders["ts-ord-1"]["trailing_peak"] == pytest.approx(current, rel=1e-6)
        new_sl = current * (1 - ex._trailing_stop_pct / 100)
        assert ex._open_orders["ts-ord-1"]["stop_loss"] == pytest.approx(new_sl, rel=1e-4)

    def test_no_db_still_updates_memory(self):
        """When _db is None, _apply_trailing_stop must still update _open_orders."""
        ex = _make_executor(db=None)

        entry = 1_000_000.0
        current = entry * 1.05
        self._call_apply_trailing_stop(ex, entry, current)

        assert "trailing_peak" in ex._open_orders.get("ts-ord-1", {}), (
            "_open_orders must be updated when _db is None"
        )

    def test_trailing_stop_not_updated_below_activation(self):
        """A trade below the activation threshold must not update DB or memory."""
        db = Mock()
        db.load_all_positions.return_value = []
        ex = _make_executor(db=db)

        entry = 1_000_000.0
        current = entry * 1.005  # only +0.5%, below 1% activation threshold

        self._call_apply_trailing_stop(ex, entry, current)

        db.update_position_sl.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# M4 — RiskManager state file locking
# ─────────────────────────────────────────────────────────────────────────────

class TestM4RiskStateLocking:
    """RiskManager must hold _state_lock during all file I/O to prevent corruption."""

    def test_risk_manager_has_state_lock(self):
        """RiskManager.__init__ must create a _state_lock threading.Lock attribute."""
        rm = RiskManager(RiskConfig())
        assert hasattr(rm, "_state_lock"), "RiskManager must have a _state_lock attribute"
        assert isinstance(rm._state_lock, type(threading.Lock())), (
            "_state_lock must be a threading.Lock instance"
        )

    def test_save_state_acquires_lock(self, tmp_path):
        """save_state() must acquire _state_lock before writing."""
        rm = _make_rm(tmp_path)
        lock_was_held_during_write = []

        original_open = open

        def _spy_open(path, mode="r", **kwargs):
            if mode == "w":
                lock_was_held_during_write.append(rm._state_lock.locked())
            return original_open(path, mode, **kwargs)

        with patch("builtins.open", side_effect=_spy_open):
            rm.save_state(str(tmp_path / "risk_state.json"))

        assert lock_was_held_during_write, "open() was never called in write mode"
        assert all(lock_was_held_during_write), (
            "_state_lock must be held during the file write in save_state()"
        )

    def test_load_state_acquires_lock(self, tmp_path):
        """load_state() must acquire _state_lock before reading."""
        rm = _make_rm(tmp_path)
        # First, save some state so the file exists
        rm._trade_count_today = 3
        rm.save_state(str(tmp_path / "risk_state.json"))
        rm._state_file = tmp_path / "risk_state.json"

        lock_was_held_during_read = []
        original_open = open

        def _spy_open(path, mode="r", **kwargs):
            if mode == "r":
                lock_was_held_during_read.append(rm._state_lock.locked())
            return original_open(path, mode, **kwargs)

        with patch("builtins.open", side_effect=_spy_open):
            rm.load_state(str(tmp_path / "risk_state.json"))

        assert lock_was_held_during_read, "open() was never called in read mode"
        assert all(lock_was_held_during_read), (
            "_state_lock must be held during the file read in load_state()"
        )

    def test_concurrent_save_state_produces_valid_json(self, tmp_path):
        """Two threads calling save_state() concurrently must not corrupt the JSON."""
        rm = _make_rm(tmp_path)
        state_file = tmp_path / "risk_state.json"
        rm._state_file = state_file
        errors: list = []

        def _writer(trade_count: int):
            rm._trade_count_today = trade_count
            try:
                rm.save_state(str(state_file))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"save_state() raised in threads: {errors}"

        # File must be valid JSON after all concurrent writes
        with open(state_file) as f:
            data = json.load(f)
        assert "trade_count_today" in data, (
            "risk_state.json must contain valid JSON after concurrent writes"
        )

    def test_save_and_load_round_trip(self, tmp_path):
        """save_state followed by load_state must restore exact state."""
        rm = _make_rm(tmp_path)
        state_file = tmp_path / "risk_state_rt.json"
        rm._trade_count_today = 7
        rm._cooling_down = True
        rm._peak_portfolio_value = 1234.5
        rm.save_state(str(state_file))

        rm2 = _make_rm(tmp_path)
        rm2._state_file = state_file
        result = rm2.load_state(str(state_file))

        assert result is True
        assert rm2._trade_count_today == 7
        assert rm2._cooling_down is True
        assert rm2._peak_portfolio_value == 1234.5

    def test_drawdown_multiplier_reduces_position_size_before_hard_block(self, tmp_path):
        rm = _make_rm(tmp_path)
        rm.config.max_risk_per_trade_pct = 1.0
        rm.config.max_position_per_trade_pct = 100.0
        rm.config.max_drawdown_threshold_pct = 12.0
        rm.config.drawdown_soft_reduce_start_pct = 5.0
        rm.config.min_drawdown_risk_multiplier = 0.35
        rm._peak_portfolio_value = 100_000.0

        result = rm.calculate_position_size(
            portfolio_value=92_000.0,
            entry_price=100_000.0,
            stop_loss_price=99_000.0,
            take_profit_price=102_000.0,
            confidence=0.7,
        )

        assert result.allowed is True
        assert result.suggested_size == pytest.approx(66457.14, rel=0.01)

    def test_load_state_recovers_from_corrupt_file(self, tmp_path):
        """load_state() must reset to safe defaults if the JSON is corrupt."""
        state_file = tmp_path / "risk_state.json"
        state_file.write_text("{INVALID JSON}")  # corrupt

        rm = _make_rm(tmp_path)
        rm._state_file = state_file
        # Pre-set some state to check it gets reset
        rm._trade_count_today = 99

        result = rm.load_state(str(state_file))

        assert result is False
        assert rm._trade_count_today == 0, (
            "Corrupted state file must reset trade_count_today to safe default"
        )
        assert rm._cooling_down is False

    def test_concurrent_save_and_load_no_deadlock(self, tmp_path):
        """A reader and multiple writers running concurrently must not deadlock."""
        rm = _make_rm(tmp_path)
        state_file = tmp_path / "risk_state_dl.json"
        rm._state_file = state_file
        rm.save_state(str(state_file))  # seed a valid file

        finish_flag = threading.Event()
        errors: list = []

        def _save_loop():
            for _ in range(10):
                try:
                    rm.save_state(str(state_file))
                except Exception as e:
                    errors.append(e)
            finish_flag.set()

        def _load_loop():
            for _ in range(10):
                try:
                    rm.load_state(str(state_file))
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=_save_loop),
            threading.Thread(target=_load_loop),
            threading.Thread(target=_load_loop),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent save/load raised: {errors}"
        assert finish_flag.is_set(), "save_loop never finished — possible deadlock"
