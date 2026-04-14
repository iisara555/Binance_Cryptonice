"""
Phase 3 Step 3 — H5/H6: Trade executor idempotency and thread-safety tests.

Verifies that:
  1. execute_entry() acquires and releases _in_flight_lock correctly.
  2. A second call with the same signal_id while the first is in-flight is
     immediately rejected (duplicate blocked).
  3. After the first call completes, the key is cleared and a second attempt
     is allowed through.
  4. Different signal_ids (different symbols or different signals) are never
     blocked by each other.
  5. 10+ concurrent threads firing the same signal only produce ONE real order.
  6. _in_flight_entries is cleared via the finally-block even when the inner
     call raises an exception.
  7. The idempotency key falls back to symbol:side:price when signal_id is None.
  8. Direct _open_orders mutations still use _orders_lock (H6 integration).
"""

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from unittest.mock import MagicMock, Mock, patch
import pytest

from trade_executor import (
    TradeExecutor, ExecutionPlan, OrderRequest, OrderResult,
    OrderSide, OrderStatus, PartialFillInfo, PartialFillTracker,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_executor() -> TradeExecutor:
    api = Mock()
    api.is_circuit_open.return_value = False

    db = Mock()
    db.load_all_positions.return_value = []

    with patch.object(threading.Thread, "start"):
        ex = TradeExecutor(
            api_client=api,
            config={
                "retry_attempts": 1,
                "retry_delay_seconds": 0,
                "order_timeout_seconds": 30,
                "order_type": "limit",
            },
            db=db,
        )
    return ex


def _make_plan(
    symbol: str = "THB_BTC",
    side: OrderSide = OrderSide.BUY,
    signal_id: str = "sig-001",
    entry_price: float = 1_500_000.0,
) -> ExecutionPlan:
    return ExecutionPlan(
        symbol=symbol,
        side=side,
        amount=1000.0,
        entry_price=entry_price,
        stop_loss=1_470_000.0,
        take_profit=1_560_000.0,
        risk_reward_ratio=2.0,
        confidence=0.75,
        strategy_votes={"trend": 1},
        signal_timestamp=datetime.now(),
        signal_id=signal_id,
        max_price_drift_pct=5.0,
    )


def _idem_key(signal_id: str) -> str:
    return hashlib.sha256(signal_id.encode()).hexdigest()[:32]


def _idem_key_fallback(symbol: str, side: str, price: float) -> str:
    src = f"{symbol}:{side}:{price}"
    return hashlib.sha256(src.encode()).hexdigest()[:32]


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestInFlightGuardAttributes:

    def test_executor_has_in_flight_entries_set(self):
        ex = _make_executor()
        assert hasattr(ex, "_in_flight_entries")
        assert isinstance(ex._in_flight_entries, set)

    def test_executor_has_in_flight_lock(self):
        ex = _make_executor()
        assert hasattr(ex, "_in_flight_lock")
        assert isinstance(ex._in_flight_lock, type(threading.Lock()))

    def test_in_flight_entries_empty_at_construction(self):
        ex = _make_executor()
        assert len(ex._in_flight_entries) == 0


class TestIdempotencyFenceBlocking:

    def _successful_entry_result(self):
        return OrderResult(
            True, OrderStatus.FILLED,
            order_id="ord-111", filled_amount=0.001,
            filled_price=1_500_000.0, ordered_amount=1000.0,
        )

    def test_duplicate_signal_id_blocked_while_in_flight(self):
        """
        While a first execute_entry is executing (simulated by blocking
        _execute_entry_inner), a second call with the same signal_id must be
        immediately rejected with REJECTED status.
        """
        ex = _make_executor()
        plan = _make_plan(signal_id="sig-dup")
        # Event set by first thread once it's inside _execute_entry_inner
        inside_inner = threading.Event()
        # Event used to release the first thread after the test assertion
        release = threading.Event()
        results = {}

        def _blocking_inner(*args, **kwargs):
            inside_inner.set()         # signal: we are now holding the fence
            release.wait(timeout=5.0)  # hold until main thread releases us
            return self._successful_entry_result()

        with patch.object(ex, "_execute_entry_inner", side_effect=_blocking_inner):
            t1 = threading.Thread(
                target=lambda: results.__setitem__("t1", ex.execute_entry(plan, 10_000.0))
            )
            t1.start()
            inside_inner.wait(timeout=5.0)  # wait until t1 holds the fence

            # Second call — fence is occupied, must be rejected immediately
            results["t2"] = ex.execute_entry(plan, 10_000.0)

            release.set()  # let t1 finish
            t1.join()

        assert results["t2"].status.value == "rejected"
        assert "Duplicate signal" in results["t2"].message or "in-flight" in results["t2"].message

    def test_key_cleared_after_first_call_completes(self):
        """After the first call completes the key must be removed so a retry succeeds."""
        ex = _make_executor()
        plan = _make_plan(signal_id="sig-retry")

        call_count = []

        def _fake_inner(p, pv, defer=False):
            call_count.append(1)
            return self._successful_entry_result()

        with patch.object(ex, "_execute_entry_inner", side_effect=_fake_inner):
            r1 = ex.execute_entry(plan, 10_000.0)
            r2 = ex.execute_entry(plan, 10_000.0)

        assert r1.success
        assert r2.success
        assert len(call_count) == 2, "Both calls should reach _execute_entry_inner"

    def test_key_cleared_even_on_exception(self):
        """The finally-block must clear the key when _execute_entry_inner raises."""
        ex = _make_executor()
        plan = _make_plan(signal_id="sig-exc")
        key = _idem_key("sig-exc")

        def _raise_inner(*args, **kwargs):
            raise RuntimeError("network timeout")

        with patch.object(ex, "_execute_entry_inner", side_effect=_raise_inner):
            with pytest.raises(RuntimeError):
                ex.execute_entry(plan, 10_000.0)

        with ex._in_flight_lock:
            assert key not in ex._in_flight_entries, (
                "Idempotency key not cleared after exception in _execute_entry_inner"
            )

    def test_different_signal_ids_do_not_block_each_other(self):
        """Two different signals for the same symbol must not block each other."""
        ex = _make_executor()
        plan_a = _make_plan(signal_id="sig-a", symbol="THB_BTC")
        plan_b = _make_plan(signal_id="sig-b", symbol="THB_BTC")

        results_a = []
        results_b = []

        def _fake_inner(p, pv, defer=False):
            time.sleep(0.05)  # simulate work
            return OrderResult(True, OrderStatus.FILLED, order_id="ord-x",
                               filled_amount=0.001, filled_price=1_500_000.0)

        with patch.object(ex, "_execute_entry_inner", side_effect=_fake_inner):
            t_a = threading.Thread(target=lambda: results_a.append(ex.execute_entry(plan_a, 10_000.0)))
            t_b = threading.Thread(target=lambda: results_b.append(ex.execute_entry(plan_b, 10_000.0)))
            t_a.start()
            t_b.start()
            t_a.join()
            t_b.join()

        assert results_a[0].success
        assert results_b[0].success

    def test_fallback_key_uses_symbol_side_price_when_no_signal_id(self):
        """A plan with signal_id=None must still derive a stable idempotency key."""
        ex = _make_executor()
        plan = _make_plan(signal_id=None)  # type: ignore[arg-type]
        plan.signal_id = None

        expected_key = _idem_key_fallback(
            plan.symbol,
            plan.side.value,
            plan.entry_price,
        )

        def _fake_inner(p, pv, defer=False):
            # Verify the key ended up in the set while inside the fence
            with ex._in_flight_lock:
                assert expected_key in ex._in_flight_entries, (
                    f"Expected fallback key {expected_key} in _in_flight_entries"
                )
            return OrderResult(True, OrderStatus.FILLED, order_id="ord-y",
                               filled_amount=0.001, filled_price=1_500_000.0)

        with patch.object(ex, "_execute_entry_inner", side_effect=_fake_inner):
            ex.execute_entry(plan, 10_000.0)


class TestConcurrentIdempotency:

    def test_10_threads_same_signal_produce_1_order(self):
        """
        10 threads all fire execute_entry with the same signal simultaneously.
        Only ONE must reach _execute_entry_inner; the other 9 must be rejected.
        """
        ex = _make_executor()
        plan = _make_plan(signal_id="sig-concurrent")
        inner_calls = []

        def _slow_inner(p, pv, defer=False):
            inner_calls.append(1)
            time.sleep(0.05)  # hold the fence long enough
            return OrderResult(True, OrderStatus.FILLED, order_id="ord-c",
                               filled_amount=0.001, filled_price=1_500_000.0)

        with patch.object(ex, "_execute_entry_inner", side_effect=_slow_inner):
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = [pool.submit(ex.execute_entry, plan, 10_000.0) for _ in range(10)]
                results = [f.result() for f in as_completed(futures)]

        # Exactly one success; others are REJECTED duplicates
        successes = [r for r in results if r.success]
        rejections = [r for r in results if not r.success and r.status.value == "rejected"]

        assert len(inner_calls) == 1, (
            f"{len(inner_calls)} calls reached _execute_entry_inner — expected exactly 1"
        )
        assert len(successes) == 1, f"Expected 1 success, got {len(successes)}"
        assert len(rejections) == 9, f"Expected 9 rejections, got {len(rejections)}"


class TestPartialFillArithmetic:

    def test_partial_fill_tracker_maintains_weighted_average_across_repeated_updates(self):
        tracker = PartialFillTracker(max_wait_seconds=60.0)
        tracker.start_tracking(
            "ord-pf",
            PartialFillInfo(
                order_id="ord-pf",
                symbol="THB_BTC",
                side=OrderSide.BUY,
                original_amount=1.0,
            ),
        )

        tracker.update_fill("ord-pf", 0.33333333, 100.01)
        tracker.update_fill("ord-pf", 0.33333333, 100.02)
        updated = tracker.update_fill("ord-pf", 0.33333334, 99.99)
        position = tracker.get_actual_position("ord-pf")

        expected_avg = ((0.33333333 * 100.01) + (0.33333333 * 100.02) + (0.33333334 * 99.99)) / 1.0

        assert updated.filled_amount == pytest.approx(1.0)
        assert updated.avg_fill_price == pytest.approx(expected_avg)
        assert updated.is_complete is True
        assert position is not None
        assert position["filled_amount"] == pytest.approx(1.0)
        assert position["avg_price"] == pytest.approx(expected_avg)
        assert position["remaining"] == pytest.approx(0.0)
        assert position["is_complete"] is True


class TestOpenOrdersLockIntegrity:

    def test_concurrent_register_and_read_open_orders(self):
        """
        Multiple threads write via register_tracked_position() and read via
        get_open_orders() simultaneously.  No exception must be raised and
        every written key must eventually be visible.
        """
        ex = _make_executor()
        errors = []
        written_ids = set()
        write_lock = threading.Lock()

        def _writer(i):
            try:
                oid = f"ord-{i}"
                ex.register_tracked_position(oid, {
                    "order_id": oid, "symbol": "THB_BTC",
                    "side": OrderSide.BUY, "amount": 0.001,
                    "entry_price": 1_500_000.0,
                })
                with write_lock:
                    written_ids.add(oid)
            except Exception as e:
                errors.append(e)

        def _reader():
            try:
                for _ in range(20):
                    _ = ex.get_open_orders()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        threads = (
            [threading.Thread(target=_writer, args=(i,)) for i in range(20)]
            + [threading.Thread(target=_reader) for _ in range(5)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent access errors: {errors}"

        # All written keys must be in _open_orders
        with ex._orders_lock:
            present = set(ex._open_orders.keys())
        missing = written_ids - present
        assert not missing, f"These order IDs were written but not readable: {missing}"

    def test_concurrent_remove_while_iterating_does_not_raise(self):
        """
        OMS-style removal under _orders_lock while another thread reads a
        snapshot must not raise RuntimeError('dictionary changed size...')
        """
        ex = _make_executor()
        for i in range(10):
            ex._open_orders[f"ord-{i}"] = {"order_id": f"ord-{i}"}

        errors = []

        def _remove():
            for i in range(10):
                with ex._orders_lock:
                    ex._open_orders.pop(f"ord-{i}", None)

        def _snapshot():
            for _ in range(30):
                try:
                    with ex._orders_lock:
                        _ = list(ex._open_orders.values())
                    time.sleep(0.0005)
                except Exception as e:
                    errors.append(e)

        t_rem = threading.Thread(target=_remove)
        t_snap = threading.Thread(target=_snapshot)
        t_rem.start()
        t_snap.start()
        t_rem.join()
        t_snap.join()

        assert not errors, f"Dictionary iteration errors: {errors}"
