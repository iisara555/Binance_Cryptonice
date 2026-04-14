from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from trading.position_manager import PositionManager


class _DummyExecutor:
    def __init__(self, db_rows=None, open_orders=None):
        self._db = SimpleNamespace(load_all_positions=lambda: list(db_rows or []))
        self._open_orders = list(open_orders or [])

    def get_open_orders(self):
        return list(self._open_orders)


class _DummyApiClient:
    def __init__(self, balances=None, open_orders_by_symbol=None):
        self._balances = balances or {}
        self._open_orders_by_symbol = dict(open_orders_by_symbol or {})

    def get_balances(self, force_refresh=True, allow_stale=False):
        return dict(self._balances)

    def get_open_orders(self, symbol=None):
        return list(self._open_orders_by_symbol.get(symbol, []))


def test_sync_from_database_loads_open_positions():
    executor = _DummyExecutor(
        db_rows=[
            {
                "order_id": "oid-1",
                "symbol": "THB_BTC",
                "side": "buy",
                "amount": 0.1,
                "remaining_amount": 0.1,
                "entry_price": 100000.0,
                "stop_loss": 95000.0,
                "take_profit": 110000.0,
                "timestamp": "2026-04-13T10:00:00",
            },
            {
                "order_id": "oid-closed",
                "symbol": "THB_ETH",
                "side": "buy",
                "amount": 1.0,
                "remaining_amount": 0.0,
                "entry_price": 5000.0,
                "timestamp": "2026-04-13T10:00:00",
            },
        ]
    )
    manager = PositionManager(cast(Any, _DummyApiClient()), cast(Any, executor), config={})

    manager.sync_from_database()

    positions = manager.get_open_positions()
    assert len(positions) == 1
    assert positions[0].order_id == "oid-1"
    assert positions[0].symbol == "THB_BTC"


def test_reconcile_with_exchange_removes_position_missing_from_exchange_and_balance():
    executor = _DummyExecutor(
        db_rows=[
            {
                "order_id": "stale-local",
                "symbol": "THB_BTC",
                "side": "buy",
                "amount": 0.1,
                "remaining_amount": 0.1,
                "entry_price": 100000.0,
                "timestamp": "2026-04-13T10:00:00",
            }
        ]
    )
    api_client = _DummyApiClient(
        balances={"BTC": {"available": 0.0, "reserved": 0.0, "total": 0.0}},
        open_orders_by_symbol={"THB_BTC": []},
    )
    manager = PositionManager(cast(Any, api_client), cast(Any, executor), config={})
    manager.sync_from_database()

    manager.reconcile_with_exchange()

    positions = manager.get_open_positions()
    assert positions == []


def test_reconcile_with_exchange_keeps_filled_coin_when_balance_remains():
    executor = _DummyExecutor(
        db_rows=[
            {
                "order_id": "held-btc",
                "symbol": "THB_BTC",
                "side": "buy",
                "amount": 0.1,
                "remaining_amount": 0.1,
                "entry_price": 100000.0,
                "timestamp": "2026-04-13T10:00:00",
            }
        ]
    )
    api_client = _DummyApiClient(
        balances={"BTC": {"available": 0.099, "reserved": 0.0, "total": 0.099}},
        open_orders_by_symbol={"THB_BTC": []},
    )
    manager = PositionManager(cast(Any, api_client), cast(Any, executor), config={})
    manager.sync_from_database()

    manager.reconcile_with_exchange()

    positions = manager.get_open_positions()
    assert len(positions) == 1
    assert positions[0].order_id == "held-btc"


def test_reconcile_with_exchange_adds_live_open_order_from_exchange():
    executor = _DummyExecutor(db_rows=[])
    api_client = _DummyApiClient(
        balances={"ETH": {"available": 0.0, "reserved": 0.0, "total": 0.0}},
        open_orders_by_symbol={
            "THB_ETH": [
                {
                    "id": "live-exchange-order",
                    "_checked_symbol": "THB_ETH",
                    "side": "buy",
                    "amt": 2.0,
                    "rec": 2.0,
                    "rat": 8000.0,
                }
            ]
        },
    )
    manager = PositionManager(cast(Any, api_client), cast(Any, executor), config={})
    local_anchor = manager._position_from_row(
        {
            "order_id": "local-anchor",
            "symbol": "THB_ETH",
            "side": "buy",
            "amount": 1.0,
            "remaining_amount": 1.0,
            "entry_price": 7900.0,
        }
    )
    assert local_anchor is not None
    manager.add_position(local_anchor)

    manager.reconcile_with_exchange()

    positions = manager.get_open_positions()
    assert len(positions) == 1
    assert positions[0].order_id == "live-exchange-order"
    assert positions[0].symbol == "THB_ETH"