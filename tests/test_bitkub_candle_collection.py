from concurrent.futures import Future
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import threading

import data_collector
from api_client import BinanceThClient
from data_collector import BinanceThCollector as BitkubCollector
from data_collector import resolve_startup_backfill_timeframes


def _collector_backfill_defaults(
    collector,
    *,
    multi_timeframe_enabled: bool = False,
    paged_backfill_enabled: bool = False,
    backfill_target_bars: int = 200,
) -> None:
    """Attributes normally set in ``BinanceThCollector.__init__`` for bare test instances."""
    collector._backfill_kline_limit_per_request = 500
    collector._paged_backfill_enabled = paged_backfill_enabled
    collector.multi_timeframe_enabled = multi_timeframe_enabled
    collector._backfill_target_bars = backfill_target_bars
    collector._backfill_max_pages = 200
    collector._max_backfill_days_by_tf = {}
    collector._max_backfill_days_default = 365


def test_binance_client_get_candle_uses_v1_klines_and_normalizes_rows():
    payload = [
        [1710000000000, "99.5", "102.0", "99.0", "100.0", "12.0"],
        [1710000060000, "100.5", "103.0", "100.0", "101.0", "13.5"],
    ]

    client = BinanceThClient(api_key="key", api_secret="secret", base_url="https://example.invalid")

    with patch("requests.get") as mock_get:
        mock_response = Mock()
        mock_response.json.return_value = payload
        mock_get.return_value = mock_response
        response = client.get_candle("THB_BTC", "15m", limit=2)

    assert response["error"] == 0
    assert response["status"] == "ok"
    assert response["result"] == [
        [1710000000, 99.5, 102.0, 99.0, 100.0, 12.0],
        [1710000060, 100.5, 103.0, 100.0, 101.0, 13.5],
    ]
    assert mock_get.call_args.args[0] == "https://api.binance.th/api/v1/klines"
    assert mock_get.call_args.kwargs["params"] == {"symbol": "BTCUSDT", "interval": "15m", "limit": 2}


def test_binance_client_cancel_order_uses_v1_order_endpoint():
    client = BinanceThClient(api_key="key", api_secret="secret", base_url="https://example.invalid")

    with patch.object(BinanceThClient, "_request", return_value={"orderId": "order-123"}) as request:
        response = client.cancel_order("THB_BTC", "order-123", "sell")

    assert response["id"] == "order-123"
    request.assert_called_once_with(
        "DELETE",
        "/api/v1/order",
        signed=True,
        params={"symbol": "BTCUSDT", "orderId": "order-123"},
    )


def test_bitkub_collector_collect_ohlc_accepts_tradingview_dict_payload():
    collector = BitkubCollector.__new__(BitkubCollector)
    _collector_backfill_defaults(collector)
    collector.db = Mock()
    collector.db.get_latest_price.return_value = None
    collector.db.insert_prices_batch.side_effect = lambda rows: len(rows)
    collector.get_ohlc = Mock(return_value=[
        [1710000000000, "99.5", "102.0", "99.0", "100.0", "12.0"],
        [1710000060000, "100.5", "103.0", "100.0", "101.0", "13.5"],
    ])

    stored = BitkubCollector.collect_ohlc(collector, "THB_BTC", interval=1, timeframe="1m")

    assert stored == 2
    inserted_rows = collector.db.insert_prices_batch.call_args.args[0]
    assert inserted_rows[0]["pair"] == "THB_BTC"
    assert inserted_rows[0]["timeframe"] == "1m"
    assert inserted_rows[0]["close"] == 100.0
    assert inserted_rows[1]["volume"] == 13.5


def test_bitkub_collector_collect_ohlc_result_reports_up_to_date_for_existing_closed_candle():
    collector = BitkubCollector.__new__(BitkubCollector)
    _collector_backfill_defaults(collector)
    collector.db = Mock()
    collector.db.get_latest_price.return_value = SimpleNamespace(timestamp=datetime(2026, 4, 5, 15, 45, 0))
    closed_candle_ts = int(datetime(2026, 4, 5, 15, 45, 0, tzinfo=timezone.utc).timestamp())
    collector.get_ohlc = Mock(return_value=[
        [closed_candle_ts * 1000, "2.9738", "2.9738", "2.9738", "2.9738", "33.54294169"],
    ])

    detail = BitkubCollector._collect_ohlc_result(collector, "THB_DOGE", interval=15, timeframe="15m")

    assert detail["stored"] == 0
    assert detail["status"] == "up_to_date"
    assert detail["latest_stored"] == datetime(2026, 4, 5, 15, 45, 0)
    assert detail["latest_fetched"] == datetime(2026, 4, 5, 15, 45, tzinfo=timezone.utc)


def test_bitkub_collector_log_result_clarifies_zero_insert_as_up_to_date(caplog):
    collector = BitkubCollector.__new__(BitkubCollector)

    with caplog.at_level("INFO"):
        BitkubCollector._log_ohlc_collection_result(
            collector,
            {
                "pair": "THB_DOGE",
                "timeframe": "15m",
                "stored": 0,
                "status": "up_to_date",
                "latest_stored": datetime(2026, 4, 5, 15, 45, 0),
                "latest_fetched": datetime(2026, 4, 5, 15, 45, 0, tzinfo=timezone.utc),
            },
        )

    assert "no new closed candles, already up to date" in caplog.text


def test_bitkub_collector_collect_multi_timeframe_uses_detail_results(monkeypatch):
    collector = BitkubCollector.__new__(BitkubCollector)

    def fake_collect(pair, interval, timeframe):
        stored_by_tf = {"1m": 1, "5m": 5, "15m": 15}
        return {
            "pair": pair,
            "timeframe": timeframe,
            "stored": stored_by_tf[timeframe],
            "status": "stored",
            "latest_stored": None,
            "latest_fetched": None,
        }

    collector._collect_ohlc_result = fake_collect
    collector._log_ohlc_collection_result = Mock()

    class _ImmediateExecutor:
        @staticmethod
        def submit(func, *args, **kwargs):
            future = Future()
            try:
                future.set_result(func(*args, **kwargs))
            except Exception as exc:  # pragma: no cover
                future.set_exception(exc)
            return future

    monkeypatch.setattr(data_collector, "_executor", _ImmediateExecutor())

    results = BitkubCollector.collect_multi_timeframe(collector, "THB_DOGE", ["1m", "5m", "15m"])

    assert results == {"1m": 1, "5m": 5, "15m": 15}
    assert collector._log_ohlc_collection_result.call_count == 3


def test_bitkub_collector_set_pairs_warms_new_pairs_when_running(monkeypatch):
    collector = BitkubCollector.__new__(BitkubCollector)
    collector._pairs_lock = Mock()
    collector._pairs_lock.__enter__ = Mock(return_value=collector._pairs_lock)
    collector._pairs_lock.__exit__ = Mock(return_value=False)
    collector.pairs = ["THB_BTC"]
    collector.running = True
    collector.multi_timeframe_enabled = True
    collector.multi_timeframes = ["1m", "5m"]
    collector._warm_pairs_backfill = Mock()

    BitkubCollector.set_pairs(collector, ["THB_BTC", "THB_SOL"])

    assert collector.pairs == ["THB_BTC", "THB_SOL"]
    collector._warm_pairs_backfill.assert_called_once_with(["THB_SOL"])


def test_bitkub_collector_start_primes_multi_timeframe_before_background_thread(monkeypatch):
    collector = BitkubCollector.__new__(BitkubCollector)
    collector.running = False
    collector.multi_timeframe_enabled = True
    collector._pairs_lock = threading.Lock()
    collector.pairs = ["BTCUSDT"]
    collector.multi_timeframes = ["1m", "5m"]
    collector._warm_pairs_backfill = Mock()
    collector._collector_loop = Mock()
    collector._thread = None

    started = {}

    class _FakeThread:
        def __init__(self, target=None, daemon=None):
            started["target"] = target
            started["daemon"] = daemon

        def start(self):
            started["started"] = True

    monkeypatch.setattr(data_collector.threading, "Thread", _FakeThread)

    BitkubCollector.start(collector, blocking=False)

    assert collector.running is True
    collector._warm_pairs_backfill.assert_called_once_with()
    assert started["target"] == collector._collector_loop
    assert started["daemon"] is True
    assert started["started"] is True


def test_bitkub_collector_warm_pairs_uses_dedicated_pair_executor(monkeypatch):
    collector = BitkubCollector.__new__(BitkubCollector)
    collector.multi_timeframe_enabled = True
    collector.multi_timeframes = ["1m", "5m"]
    collector._last_multi_timeframe_results = {}
    collector.multi_timeframe_interval = 60

    seen_workers = []

    class _ImmediatePairExecutor:
        def __init__(self, max_workers=None):
            seen_workers.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, func, *args, **kwargs):
            future = Future()
            try:
                future.set_result(func(*args, **kwargs))
            except Exception as exc:  # pragma: no cover
                future.set_exception(exc)
            return future

    monkeypatch.setattr(data_collector, "ThreadPoolExecutor", _ImmediatePairExecutor)
    collector.collect_multi_timeframe = Mock(
        side_effect=lambda pair, timeframes: {tf: len(pair) for tf in timeframes}
    )

    results = BitkubCollector._warm_pairs_multi_timeframe(collector, ["THB_BTC", "THB_SOL"])

    assert seen_workers == [2]
    assert results == {
        "THB_BTC": {"1m": 7, "5m": 7},
        "THB_SOL": {"1m": 7, "5m": 7},
    }
    assert collector._last_multi_timeframe_results == results


def test_resolve_startup_backfill_timeframes_matches_collector_when_mtf_enabled():
    cfg = {"enabled": True, "timeframes": ["1m", "5m", "15m", "1h", "4h", "1d"]}
    out = resolve_startup_backfill_timeframes(
        cfg,
        collector_timeframes=["1m", "5m", "15m", "1h", "4h", "1d"],
    )
    assert out == ["1m", "5m", "15m", "1h", "4h", "1d"]


def test_resolve_startup_backfill_timeframes_startup_override():
    cfg = {
        "enabled": True,
        "timeframes": ["1m", "5m", "15m"],
        "startup_backfill_timeframes": ["4h", "1d"],
    }
    out = resolve_startup_backfill_timeframes(
        cfg,
        collector_timeframes=["1m", "5m", "15m", "1h", "4h", "1d"],
    )
    assert out == ["4h", "1d"]


def test_resolve_startup_backfill_timeframes_filters_invalid_intervals():
    cfg = {"enabled": True, "timeframes": ["5m", "not_an_interval", "15m"]}
    out = resolve_startup_backfill_timeframes(
        cfg,
        collector_timeframes=["5m", "not_an_interval", "15m"],
    )
    assert out == ["5m", "15m"]


def test_resolve_startup_backfill_timeframes_default_when_mtf_disabled():
    assert resolve_startup_backfill_timeframes(
        {"enabled": False},
        collector_timeframes=["1m"],
    ) == ["15m", "1h"]


def test_collect_ohlc_result_wires_limit_into_get_ohlc():
    collector = BitkubCollector.__new__(BitkubCollector)
    _collector_backfill_defaults(collector)
    collector.db = Mock()
    collector.db.get_latest_price.return_value = None
    collector.get_ohlc = Mock(return_value=[])

    BitkubCollector._collect_ohlc_result(collector, "BTCUSDT", "15m", limit=42)

    assert collector.get_ohlc.call_args.kwargs["limit"] == 42


def test_collect_ohlc_result_paged_backfill_requests_older_window():
    collector = BitkubCollector.__new__(BitkubCollector)
    _collector_backfill_defaults(
        collector,
        multi_timeframe_enabled=True,
        paged_backfill_enabled=True,
        backfill_target_bars=3,
    )
    collector.db = Mock()
    collector.db.get_latest_price.return_value = None
    collector.db.insert_prices_batch.side_effect = lambda rows: len(rows)

    t_newer = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
    t_mid = datetime(2026, 1, 10, 11, 0, tzinfo=timezone.utc)
    t_old = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)

    page_calls = [0]

    def get_ohlc_side_effect(_sym, interval="15m", limit=500, **kwargs):
        if kwargs.get("end_time_ms") is None:
            return [
                [int(t_mid.timestamp() * 1000), "1", "1", "1", "1", "1"],
                [int(t_newer.timestamp() * 1000), "1", "1", "1", "1", "1"],
            ]
        page_calls[0] += 1
        if page_calls[0] == 1:
            return [[int(t_old.timestamp() * 1000), "1", "1", "1", "1", "1"]]
        return []

    collector.get_ohlc = Mock(side_effect=get_ohlc_side_effect)

    counts = [2, 2, 3]

    def count_rows(*_a, **_k):
        return counts.pop(0) if counts else 10

    collector.db.count_price_rows.side_effect = count_rows

    collector.db.get_earliest_price.return_value = SimpleNamespace(timestamp=t_mid)

    detail = BitkubCollector._collect_ohlc_result(collector, "BTCUSDT", "15m")

    assert detail["stored"] == 3
    assert collector.get_ohlc.call_count == 2
    second = collector.get_ohlc.call_args_list[1]
    assert second.kwargs["end_time_ms"] == int(t_mid.timestamp() * 1000) - 1