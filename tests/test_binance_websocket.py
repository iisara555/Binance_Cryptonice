import json
import time

from binance_websocket import BinanceWebSocket, get_latest_ticker


def test_binance_ws_on_message_updates_cache_and_callback():
    received = []
    ws = BinanceWebSocket(["BTCUSDT"], on_tick=lambda tick: received.append(tick))
    payload = {
        "stream": "btcusdt@ticker",
        "data": {
            "s": "BTCUSDT",
            "c": "102345.12",
            "b": "102344.90",
            "a": "102345.50",
            "P": "1.25",
        },
    }

    ws._on_message(None, json.dumps(payload))

    assert len(received) == 1
    tick = received[0]
    assert tick.symbol == "BTCUSDT"
    assert tick.last == 102345.12
    assert tick.bid == 102344.90
    assert tick.ask == 102345.50
    assert tick.percent_change_24h == 1.25

    cached = get_latest_ticker("btcusdt")
    assert cached is not None
    assert cached.last == 102345.12
    assert cached.timestamp <= time.time()
