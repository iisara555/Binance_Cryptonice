from unittest.mock import Mock, call

from monitoring import ReconciliationState
from trading.startup_runtime import reconcile_open_orders_with_exchange


def test_reconcile_open_orders_marks_exchange_missing_orders_cancelled():
    bot = Mock()
    bot.config = {"auth_degraded": False}
    bot.api_client.get_open_orders.return_value = []
    bot.executor.get_open_orders.return_value = [
        {"id": 1, "order_id": "buy-1", "symbol": "BTCUSDT", "side": "buy", "filled": False},
        {"id": 2, "order_id": "sell-2", "symbol": "ETHUSDT", "side": "sell", "filled": False},
    ]
    bot.executor._orders_lock.__enter__ = Mock(return_value=None)
    bot.executor._orders_lock.__exit__ = Mock(return_value=None)
    bot.executor._open_orders = {"buy-1": {}, "sell-2": {}}
    bot._state_manager = None

    removed = reconcile_open_orders_with_exchange(bot, source="test")

    assert removed == 2
    bot.db.update_order_status.assert_has_calls([call(1, "cancelled"), call(2, "cancelled")])
    bot.db.delete_position.assert_has_calls([call("buy-1"), call("sell-2")])
    bot.executor.sync_open_orders_from_db.assert_called_once()
    assert bot.executor._open_orders == {}


def test_reconcile_open_orders_ignores_filled_synthetic_positions():
    bot = Mock()
    bot.config = {"auth_degraded": False}
    bot.executor.get_open_orders.return_value = [
        {
            "order_id": "bootstrap_BTCUSDT_1",
            "symbol": "BTCUSDT",
            "side": "buy",
            "filled": True,
            "amount": 0.001,
        }
    ]

    removed = reconcile_open_orders_with_exchange(bot, source="test")

    assert removed == 0
    bot.api_client.get_open_orders.assert_not_called()
    bot.db.delete_position.assert_not_called()


def test_reconciliation_monitor_does_not_pause_for_filled_positions_without_open_exchange_orders():
    monitor = ReconciliationState()
    executor = Mock()
    executor.get_open_orders.return_value = [
        {"order_id": "bootstrap_BTCUSDT_1", "symbol": "BTCUSDT", "side": "buy", "filled": True}
    ]
    api_client = Mock()
    api_client.get_open_orders.return_value = []

    monitor.check_positions(executor, api_client)

    assert monitor.is_paused() == (False, "")
