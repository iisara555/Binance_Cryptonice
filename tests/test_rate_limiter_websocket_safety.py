import threading
import time

import pytest

from binance_websocket import BinanceWebSocket
from rate_limiter import TokenBucketRateLimiter


def test_rate_limiter_rejects_invalid_rate_and_capacity():
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(rate=0, capacity=10)

    with pytest.raises(ValueError):
        TokenBucketRateLimiter(rate=1.0, capacity=0)


def test_rate_limiter_prevents_impossible_acquire_request():
    limiter = TokenBucketRateLimiter(rate=1.0, capacity=2, name="test")

    # Requesting more than capacity can never succeed; should fail fast.
    assert limiter.acquire(tokens=3, blocking=True, timeout=None) is False


def test_websocket_sets_circuit_open_time_on_failure_threshold():
    ws = BinanceWebSocket(["BTCUSDT"], on_tick=lambda tick: None)
    ws._running = True
    ws._reconnect_delay = 0.01
    ws.MAX_RECONNECT_ATTEMPTS = 2
    ws._consecutive_failures = 1
    ws._circuit_open_time = 0.0

    ws._handle_disconnection()

    assert ws._consecutive_failures >= ws.MAX_RECONNECT_ATTEMPTS
    assert ws._circuit_open_time > 0.0


def test_websocket_stop_interrupts_reconnect_backoff_sleep():
    ws = BinanceWebSocket(["BTCUSDT"], on_tick=lambda tick: None)
    ws._running = True
    ws._reconnect_delay = 10.0  # intentionally long to verify interruption

    runner = threading.Thread(target=ws._handle_disconnection, daemon=True)
    started_at = time.time()
    runner.start()
    time.sleep(0.05)

    ws.stop()
    runner.join(timeout=1.0)
    elapsed = time.time() - started_at

    assert not runner.is_alive(), "Reconnect handler should exit promptly after stop()"
    assert elapsed < 1.5, "stop() should interrupt reconnect backoff wait"
