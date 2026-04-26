"""Thread-safety checks for the Binance Thailand REST client."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from api_client import BinanceThClient


def _make_client() -> BinanceThClient:
    client = BinanceThClient(
        api_key="test_key",
        api_secret="test_secret",
        base_url="https://api.binance.th",
    )
    client.sync_clock = Mock(return_value=0.0)
    client.check_clock_sync = Mock(return_value=True)
    return client


class TestRateLimiterThreadSafety:
    def test_concurrent_requests_respect_min_interval(self):
        client = _make_client()
        client._min_request_interval = 0.02

        call_times: list[float] = []
        call_lock = threading.Lock()

        def fake_request(*args, **kwargs):
            with call_lock:
                call_times.append(time.time())
            response = Mock()
            response.status_code = 200
            response.json.return_value = {"ok": True}
            return response

        with patch("requests.request", side_effect=fake_request):
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [
                    pool.submit(client._request, "GET", "/api/v1/account", signed=True)
                    for _ in range(8)
                ]
                for future in futures:
                    assert future.result() == {"ok": True}

        assert len(call_times) == 8
        call_times.sort()
        expected_span = (len(call_times) - 1) * client._min_request_interval
        assert call_times[-1] - call_times[0] >= expected_span - 0.03

    def test_last_request_time_monotonically_advances(self):
        client = _make_client()
        client._min_request_interval = 0.01
        start_time = time.time()

        response = Mock()
        response.status_code = 200
        response.json.return_value = {"ok": True}

        with patch("requests.request", return_value=response):
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = [
                    pool.submit(client._request, "GET", "/api/v1/depth", params={"symbol": "BTCUSDT"})
                    for _ in range(10)
                ]
                for future in futures:
                    assert future.result() == {"ok": True}

        min_expected = start_time + 9 * client._min_request_interval
        assert client._last_request_time >= min_expected - 0.01


class TestBalanceCacheThreadSafety:
    def test_fresh_cache_returns_without_api_call(self):
        client = _make_client()
        cached = {"USDT": {"available": 10.0, "reserved": 0.0}}
        client._balances_cache = cached
        client._balances_cache_time = time.time()

        with patch.object(client, "_request", return_value={"balances": []}) as mock_request:
            assert client.get_balances() == cached
            mock_request.assert_not_called()

    def test_stale_cache_is_refreshed(self):
        client = _make_client()
        client._balances_cache = {"USDT": {"available": 1.0, "reserved": 0.0}}
        client._balances_cache_time = time.time() - 100

        payload = {
            "balances": [
                {"asset": "USDT", "free": "20.5", "locked": "1.5"},
                {"asset": "BTC", "free": "0.01", "locked": "0"},
            ]
        }
        with patch.object(client, "_request", return_value=payload) as mock_request:
            balances = client.get_balances(force_refresh=True, allow_stale=False)

        mock_request.assert_called_once_with("GET", "/api/v1/account", signed=True, timeout=None)
        assert balances["USDT"] == {"available": 20.5, "reserved": 1.5}
        assert balances["BTC"] == {"available": 0.01, "reserved": 0.0}

    def test_stale_cache_can_be_returned_on_api_error(self):
        client = _make_client()
        stale = {"USDT": {"available": 1.0, "reserved": 0.0}}
        client._balances_cache = stale
        client._balances_cache_time = time.time() - 100

        with patch.object(client, "_request", side_effect=RuntimeError("network error")):
            assert client.get_balances(force_refresh=True, allow_stale=True) == stale
