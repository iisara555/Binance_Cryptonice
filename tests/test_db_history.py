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
