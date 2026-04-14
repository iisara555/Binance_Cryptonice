from concurrent.futures import Future
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from api_client import BitkubClient
import data_collector
from data_collector import BitkubCollector


def test_bitkub_client_get_candle_uses_tradingview_history_and_normalizes_rows():
    payload = {
        "c": [100.0, 101.0],
        "h": [102.0, 103.0],
        "l": [99.0, 100.0],
        "o": [99.5, 100.5],
        "s": "ok",
        "t": [1710000000, 1710000060],
        "v": [12.0, 13.5],
    }

    client = BitkubClient(api_key="key", api_secret="secret", base_url="https://example.invalid")

    with patch.object(BitkubClient, "_get_candle_cached", return_value=payload) as cached:
        response = client.get_candle("THB_BTC", "15", limit=2)

    assert response["error"] == 0
    assert response["status"] == "ok"
    assert response["result"] == [
        [1710000000, 99.5, 102.0, 99.0, 100.0, 12.0],
        [1710000060, 100.5, 103.0, 100.0, 101.0, 13.5],
    ]

    called_symbol, called_resolution, called_from, called_to, called_url = cached.call_args.args
    assert called_symbol == "BTC_THB"
    assert called_resolution == "15"
    assert called_to >= called_from
    assert called_url == "https://example.invalid/tradingview/history"


def test_bitkub_client_cancel_order_uses_normalized_market_symbol():
    client = BitkubClient(api_key="key", api_secret="secret", base_url="https://example.invalid")

    with patch.object(BitkubClient, "_request", return_value={"error": 0}) as request:
        response = client.cancel_order("THB_BTC", "order-123", "sell")

    assert response == {"error": 0}
    request.assert_called_once_with(
        "POST",
        "/api/v3/market/cancel-order",
        authenticated=True,
        params={
            "sym": "btc_thb",
            "id": "order-123",
            "sd": "sell",
        },
    )


def test_bitkub_collector_collect_ohlc_accepts_tradingview_dict_payload():
    collector = BitkubCollector.__new__(BitkubCollector)
    collector.db = Mock()
    collector.db.get_latest_price.return_value = None
    collector.db.insert_prices_batch.side_effect = lambda rows: len(rows)
    collector.get_ohlc = Mock(return_value={
        "c": [100.0, 101.0],
        "h": [102.0, 103.0],
        "l": [99.0, 100.0],
        "o": [99.5, 100.5],
        "s": "ok",
        "t": [1710000000, 1710000060],
        "v": [12.0, 13.5],
    })

    stored = BitkubCollector.collect_ohlc(collector, "THB_BTC", interval=1, timeframe="1m")

    assert stored == 2
    inserted_rows = collector.db.insert_prices_batch.call_args.args[0]
    assert inserted_rows[0]["pair"] == "THB_BTC"
    assert inserted_rows[0]["timeframe"] == "1m"
    assert inserted_rows[0]["close"] == 100.0
    assert inserted_rows[1]["volume"] == 13.5


def test_bitkub_collector_collect_ohlc_result_reports_up_to_date_for_existing_closed_candle():
    collector = BitkubCollector.__new__(BitkubCollector)
    collector.db = Mock()
    collector.db.get_latest_price.return_value = SimpleNamespace(timestamp=datetime(2026, 4, 5, 15, 45, 0))
    closed_candle_ts = int(datetime(2026, 4, 5, 15, 45, 0, tzinfo=timezone.utc).timestamp())
    collector.get_ohlc = Mock(return_value={
        "c": [2.9738],
        "h": [2.9738],
        "l": [2.9738],
        "o": [2.9738],
        "s": "ok",
        "t": [closed_candle_ts],
        "v": [33.54294169],
    })

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

    def fake_collect(pair, minutes, timeframe):
        return {
            "pair": pair,
            "timeframe": timeframe,
            "stored": minutes,
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
    collector._warm_pairs_multi_timeframe = Mock()

    BitkubCollector.set_pairs(collector, ["THB_BTC", "THB_SOL"])

    assert collector.pairs == ["THB_BTC", "THB_SOL"]
    collector._warm_pairs_multi_timeframe.assert_called_once_with(["THB_SOL"])


def test_bitkub_collector_start_primes_multi_timeframe_before_background_thread(monkeypatch):
    collector = BitkubCollector.__new__(BitkubCollector)
    collector.running = False
    collector.multi_timeframe_enabled = True
    collector._warm_pairs_multi_timeframe = Mock()
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
    collector._warm_pairs_multi_timeframe.assert_called_once_with()
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