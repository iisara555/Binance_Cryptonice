from datetime import datetime, timedelta

import pytest

from database import Database


def test_has_ever_held_returns_false_for_never_held(temp_db):
    db = Database(temp_db)

    assert db.has_ever_held("BTC") is False
    assert db.has_ever_held("eth") is False


def test_has_ever_held_returns_true_after_recording_buy(temp_db):
    db = Database(temp_db)

    db.record_held_coin("BTC", 0.123)

    assert db.has_ever_held("BTC") is True
    assert db.has_ever_held("btc") is True


def test_has_ever_held_remains_true_after_position_closed(temp_db):
    db = Database(temp_db)

    db.record_held_coin("ETH", 1.5)
    position = db.save_position({
        "order_id": "test_eth_buy_1",
        "symbol": "ETH",
        "side": "buy",
        "amount": 1.5,
        "entry_price": 100.0,
        "remaining_amount": 1.5,
    })

    assert position is not None
    assert db.delete_position(position.order_id) is True
    assert db.has_ever_held("ETH") is True


def test_cleanup_price_history_by_timeframe_prunes_each_timeframe_independently(temp_db):
    db = Database(temp_db)
    now = datetime.utcnow()

    db.insert_price("THB_BTC", now - timedelta(days=10), 100, 101, 99, 100, 10, timeframe="1m")
    db.insert_price("THB_BTC", now - timedelta(days=5), 100, 101, 99, 100, 10, timeframe="1m")
    db.insert_price("THB_BTC", now - timedelta(days=20), 100, 101, 99, 100, 10, timeframe="5m")
    db.insert_price("THB_BTC", now - timedelta(days=10), 100, 101, 99, 100, 10, timeframe="5m")
    db.insert_price("THB_BTC", now - timedelta(days=70), 100, 101, 99, 100, 10, timeframe="1h")
    db.insert_price("THB_BTC", now - timedelta(days=20), 100, 101, 99, 100, 10, timeframe="1h")

    deleted = db.cleanup_price_history_by_timeframe({"1m": 7, "5m": 14, "1h": 60})

    assert deleted["1m"] == 1
    assert deleted["5m"] == 1
    assert deleted["1h"] == 1
    assert deleted["total"] == 3

    candles_1m = db.get_candles("THB_BTC", interval="1m")
    candles_5m = db.get_candles("THB_BTC", interval="5m")
    candles_1h = db.get_candles("THB_BTC", interval="1h")

    assert len(candles_1m) == 1
    assert len(candles_5m) == 1
    assert len(candles_1h) == 1


def test_cleanup_price_history_by_timeframe_ignores_invalid_policy_values(temp_db):
    db = Database(temp_db)
    now = datetime.utcnow()

    db.insert_price("THB_ETH", now - timedelta(days=3), 100, 101, 99, 100, 10, timeframe="1m")

    deleted = db.cleanup_price_history_by_timeframe({"1m": 0, "5m": "bad", "": 10})

    assert deleted == {"total": 0}
    candles = db.get_candles("THB_ETH", interval="1m")
    assert len(candles) == 1


def test_sqlite_connection_uses_temp_store_memory(temp_db):
    db = Database(temp_db)

    conn = db.engine.raw_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA temp_store")
        temp_store_mode = cursor.fetchone()[0]
    finally:
        conn.close()

    assert temp_store_mode == 2
