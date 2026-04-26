import os
import threading
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

os.environ.setdefault("BINANCE_API_KEY", "test-key")
os.environ.setdefault("BINANCE_API_SECRET", "test-secret")

from balance_monitor import BalanceEvent
from trading_bot import TradingBotOrchestrator
from state_management import TradeLifecycleState


class _FakeReconciler:
    def __init__(self, paused=False, reason=""):
        self._paused = paused
        self._reason = reason

    def is_paused(self):
        return self._paused, self._reason


class _FakeMonitoring:
    def __init__(self, paused=False, reason=""):
        self._reconciler = _FakeReconciler(paused=paused, reason=reason)


def _build_pause_bot():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot._pause_state_lock = threading.Lock()
    bot._trading_paused = False
    bot._pause_reason = ""
    bot._pause_reasons = {}
    bot._monitoring = None
    return bot


def test_manual_pause_state_round_trip():
    bot = _build_pause_bot()

    bot._set_pause_reason("balance", "THB withdrawal detected")
    paused, reason = bot._is_paused()
    assert paused is True
    assert reason == "THB withdrawal detected"

    bot._set_pause_reason("risk", "daily loss limit")
    paused, reason = bot._is_paused()
    assert paused is True
    assert reason == "THB withdrawal detected | daily loss limit"

    bot._clear_pause_reason("balance")
    paused, reason = bot._is_paused()
    assert paused is True
    assert reason == "daily loss limit"

    bot._clear_pause_reason("risk")
    paused, reason = bot._is_paused()
    assert paused is False
    assert reason == ""


def test_monitoring_pause_is_used_when_manual_pause_is_clear():
    bot = _build_pause_bot()
    bot._monitoring = _FakeMonitoring(paused=True, reason="reconciliation in progress")

    paused, reason = bot._is_paused()

    assert paused is True
    assert reason == "reconciliation in progress"


def test_handle_balance_event_crypto_deposit_alerts_for_tracked_position():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot._portfolio_cache = {"data": object(), "timestamp": 1.0}
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = [{
        "order_id": "bootstrap_THB_BTC_1",
        "symbol": "THB_BTC",
        "side": "buy",
        "entry_price": 1_500_000.0,
    }]
    bot.alert_system = Mock()
    bot.send_alerts = True
    bot._reconcile_tracked_positions_with_balance_state = Mock(return_value=[])
    bot._clear_pause_reason = Mock()
    bot._set_pause_reason = Mock()

    bot._handle_balance_event(
        BalanceEvent(
            event_type="DEPOSIT",
            coin="BTC",
            amount=0.0004,
            balance=0.0014,
            occurred_at=None,
            source="crypto",
        ),
        {"balances": {"BTC": {"available": 0.0014, "reserved": 0.0, "total": 0.0014}}},
    )

    alert_message = bot.alert_system.send.call_args.args[1]
    assert "External crypto deposit detected: BTC +0.00040000" in alert_message
    assert "Tracked THB_BTC entry remains 1,500,000.00" in alert_message
    assert "will not average-in this deposit automatically" in alert_message


def test_handle_balance_event_crypto_deposit_alerts_when_untracked():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot._portfolio_cache = {"data": object(), "timestamp": 1.0}
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = []
    bot.alert_system = Mock()
    bot.send_alerts = True
    bot._reconcile_tracked_positions_with_balance_state = Mock(return_value=[])
    bot._clear_pause_reason = Mock()
    bot._set_pause_reason = Mock()
    bot._get_trading_pairs = Mock(return_value=[])

    bot._handle_balance_event(
        BalanceEvent(
            event_type="DEPOSIT",
            coin="ETH",
            amount=0.25,
            balance=0.25,
            occurred_at=None,
            source="crypto",
        ),
        {"balances": {"ETH": {"available": 0.25, "reserved": 0.0, "total": 0.25}}},
    )

    alert_message = bot.alert_system.send.call_args.args[1]
    assert "External crypto deposit detected: ETH +0.25000000" in alert_message
    assert "No tracked ETHUSDT position exists" in alert_message
    assert "not auto-converted into a managed bot position" in alert_message


def test_handle_balance_event_crypto_deposit_bootstraps_active_runtime_pair_into_position_book():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot._portfolio_cache = {"data": object(), "timestamp": 1.0}
    bot.executor = Mock()
    bot.alert_system = Mock()
    bot.send_alerts = True
    bot._reconcile_tracked_positions_with_balance_state = Mock(return_value=[])
    bot._clear_pause_reason = Mock()
    bot._set_pause_reason = Mock()
    bot._get_trading_pairs = Mock(return_value=["THB_SOL"])
    bot._bootstrap_held_positions = Mock()
    bot._find_tracked_position_by_symbol = Mock(side_effect=[None, None, {
        "order_id": "bootstrap_THB_SOL_1",
        "symbol": "THB_SOL",
        "entry_price": 2_750.0,
        "bootstrap_source": "estimated_from_ticker",
    }])

    bot._handle_balance_event(
        BalanceEvent(
            event_type="DEPOSIT",
            coin="SOL",
            amount=0.5,
            balance=1.25,
            occurred_at=None,
            source="crypto",
        ),
        {"balances": {"SOL": {"available": 1.25, "reserved": 0.0, "total": 1.25}}},
    )

    bot._bootstrap_held_positions.assert_called_once()
    alert_message = bot.alert_system.send.call_args.args[1]
    assert "External crypto deposit detected: SOL +0.50000000" in alert_message
    assert "THB_SOL was registered into Position Book" in alert_message


def test_bootstrap_held_positions_assigns_sl_tp_to_synthetic_positions():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.config = {"risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0, "use_dynamic_sl_tp": False}}
    bot.min_trade_value_thb = 15.0
    bot.api_client = Mock()
    bot.api_client.get_balances.return_value = {
        "BTC": {"available": 0.001, "reserved": 0.0}
    }
    bot.api_client.get_ticker.return_value = {"last": 1_500_000.0}
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = []
    bot.db = Mock()
    bot._get_trading_pairs = Mock(return_value=["THB_BTC"])

    with patch("trading_bot.time.sleep"):
        bot._bootstrap_held_positions()

    bot.executor.register_tracked_position.assert_called_once()
    _, pos_data = bot.executor.register_tracked_position.call_args.args
    assert pos_data["entry_price"] == 1_500_000.0
    assert pos_data["stop_loss"] == 1_432_500.0
    assert pos_data["take_profit"] == 1_650_000.0


def test_bootstrap_held_positions_prefers_persisted_position_entry_price_over_current_price():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.config = {"risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0}}
    bot.min_trade_value_thb = 15.0
    bot.api_client = Mock()
    bot.api_client.get_balances.return_value = {
        "BTC": {"available": 0.001, "reserved": 0.0}
    }
    bot.api_client.get_ticker.return_value = {"last": 1_500_000.0}
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = []
    bot.db = Mock()
    bot.db.load_all_positions.return_value = [{
        "order_id": "manual_THB_BTC_1",
        "symbol": "THB_BTC",
        "side": "buy",
        "amount": 0.001,
        "entry_price": 1_200_000.0,
        "stop_loss": 1_146_000.0,
        "take_profit": 1_320_000.0,
        "total_entry_cost": 1200.0,
        "timestamp": datetime(2026, 4, 13, 12, 0, 0),
    }]
    bot.db.get_trade_state.return_value = None
    bot._get_trading_pairs = Mock(return_value=["THB_BTC"])

    with patch("trading_bot.time.sleep"):
        bot._bootstrap_held_positions()

    _, pos_data = bot.executor.register_tracked_position.call_args.args
    assert pos_data["entry_price"] == 1_200_000.0
    assert pos_data["filled_price"] == 1_200_000.0
    assert pos_data["stop_loss"] == 1_146_000.0
    assert pos_data["take_profit"] == 1_320_000.0
    assert pos_data["total_entry_cost"] == 1200.0
    assert pos_data["timestamp"] == datetime(2026, 4, 13, 12, 0, 0)


def test_bootstrap_held_positions_prefers_trade_state_entry_price_over_current_price():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.config = {"risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0}}
    bot.min_trade_value_thb = 15.0
    bot.api_client = Mock()
    bot.api_client.get_balances.return_value = {
        "BTC": {"available": 0.001, "reserved": 0.0}
    }
    bot.api_client.get_ticker.return_value = {"last": 1_500_000.0}
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = []
    bot.db = Mock()
    bot.db.load_all_positions.return_value = []
    bot.db.get_trade_state.return_value = {
        "symbol": "THB_BTC",
        "state": TradeLifecycleState.IN_POSITION.value,
        "entry_price": 1_250_000.0,
        "stop_loss": 1_193_750.0,
        "take_profit": 1_375_000.0,
        "total_entry_cost": 1250.0,
    }
    bot._get_trading_pairs = Mock(return_value=["THB_BTC"])

    with patch("trading_bot.time.sleep"):
        bot._bootstrap_held_positions()

    _, pos_data = bot.executor.register_tracked_position.call_args.args
    assert pos_data["entry_price"] == 1_250_000.0
    assert pos_data["filled_price"] == 1_250_000.0
    assert pos_data["stop_loss"] == 1_193_750.0
    assert pos_data["take_profit"] == 1_375_000.0
    assert pos_data["total_entry_cost"] == 1250.0


def test_bootstrap_held_positions_ignores_newer_bootstrap_row_when_real_position_exists():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.config = {"risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0}}
    bot.min_trade_value_thb = 15.0
    bot.api_client = Mock()
    bot.api_client.get_balances.return_value = {
        "BTC": {"available": 0.0000605, "reserved": 0.0}
    }
    bot.api_client.get_ticker.return_value = {"last": 2_390_512.0}
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = []
    bot.db = Mock()
    bot.db.load_all_positions.return_value = [
        {
            "order_id": "bootstrap_THB_BTC_1713085845",
            "symbol": "THB_BTC",
            "side": "buy",
            "amount": 0.0000605,
            "entry_price": 2_390_512.0,
            "stop_loss": 2_372_583.16,
            "take_profit": 2_432_345.96,
            "total_entry_cost": 144.626,
            "timestamp": datetime(2026, 4, 14, 15, 10, 45),
        },
        {
            "order_id": "ord-rec-1",
            "symbol": "THB_BTC",
            "side": "buy",
            "amount": 0.0000605,
            "entry_price": 2_393_000.0,
            "stop_loss": 2_375_052.5,
            "take_profit": 2_434_877.5,
            "total_entry_cost": 144.7765,
            "timestamp": datetime(2026, 4, 14, 14, 31, 0),
        },
    ]
    bot.db.get_trades.return_value = []
    bot.db.get_trade_state.return_value = {
        "symbol": "THB_BTC",
        "state": TradeLifecycleState.IN_POSITION.value,
        "entry_price": 2_390_512.0,
        "total_entry_cost": 144.626,
    }
    bot._get_trading_pairs = Mock(return_value=["THB_BTC"])

    with patch("trading_bot.time.sleep"):
        bot._bootstrap_held_positions()

    _, pos_data = bot.executor.register_tracked_position.call_args.args
    assert pos_data["entry_price"] == 2_393_000.0
    assert pos_data["filled_price"] == 2_393_000.0
    assert pos_data["total_entry_cost"] == 144.7765


def test_bootstrap_held_positions_uses_trade_history_when_only_bootstrap_context_exists():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.config = {"risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0}}
    bot.min_trade_value_thb = 15.0
    bot.api_client = Mock()
    bot.api_client.get_balances.return_value = {
        "BTC": {"available": 0.0000605, "reserved": 0.0}
    }
    bot.api_client.get_ticker.return_value = {"last": 2_390_512.0}
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = []
    bot.db = Mock()
    bot.db.load_all_positions.return_value = [{
        "order_id": "bootstrap_THB_BTC_1713085845",
        "symbol": "THB_BTC",
        "side": "buy",
        "amount": 0.0000605,
        "entry_price": 2_390_512.0,
        "stop_loss": 2_372_583.16,
        "take_profit": 2_432_345.96,
        "total_entry_cost": 144.626,
        "timestamp": datetime(2026, 4, 14, 15, 10, 45),
    }]
    bot.db.get_trades.return_value = [Mock(
        pair="THB_BTC",
        side="buy",
        quantity=0.0000605,
        price=2_393_000.0,
        timestamp=datetime(2026, 4, 14, 14, 31, 0),
    )]
    bot.db.get_trade_state.return_value = {
        "symbol": "THB_BTC",
        "state": TradeLifecycleState.IN_POSITION.value,
        "entry_price": 2_390_512.0,
        "total_entry_cost": 144.626,
    }
    bot._get_trading_pairs = Mock(return_value=["THB_BTC"])

    with patch("trading_bot.time.sleep"):
        bot._bootstrap_held_positions()

    _, pos_data = bot.executor.register_tracked_position.call_args.args
    assert pos_data["entry_price"] == 2_393_000.0
    assert pos_data["filled_price"] == 2_393_000.0
    assert pos_data["total_entry_cost"] == 0.0000605 * 2_393_000.0


def test_bootstrap_held_positions_uses_exchange_order_history_when_local_state_is_bootstrap_only():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.config = {"risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0}}
    bot.min_trade_value_thb = 15.0
    bot.api_client = Mock()
    bot.api_client.get_balances.return_value = {
        "BTC": {"available": 0.0000605, "reserved": 0.0}
    }
    bot.api_client.get_ticker.return_value = {"last": 2_390_515.0}
    bot.api_client.get_order_history.return_value = [{
        "txn_id": "txn-real-btc-1",
        "order_id": "ord-real-btc-1",
        "side": "buy",
        "rate": "2393000.01",
        "amount": "145.15",
        "fee": "0.37",
        "ts": 1776151909388,
    }]
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = []
    bot.db = Mock()
    bot.db.load_all_positions.return_value = [{
        "order_id": "bootstrap_THB_BTC_1776154258",
        "symbol": "THB_BTC",
        "side": "buy",
        "amount": 0.0000605,
        "entry_price": 2_390_515.0,
        "stop_loss": 2_372_586.14,
        "take_profit": 2_432_349.01,
        "total_entry_cost": 144.6261575,
        "timestamp": datetime(2026, 4, 14, 15, 10, 58),
    }]
    bot.db.get_trades.return_value = [Mock(
        pair="THB_BTC",
        side="buy",
        quantity=0.00000836,
        price=2_293_139.40,
        timestamp=datetime(2026, 4, 14, 14, 20, 0),
    )]
    bot.db.get_trade_state.return_value = {
        "symbol": "THB_BTC",
        "state": TradeLifecycleState.IN_POSITION.value,
        "entry_price": 2_390_515.0,
        "total_entry_cost": 144.6261575,
        "entry_order_id": "bootstrap_THB_BTC_1776154258",
    }
    bot._get_trading_pairs = Mock(return_value=["THB_BTC"])

    with patch("trading_bot.time.sleep"):
        bot._bootstrap_held_positions()

    _, pos_data = bot.executor.register_tracked_position.call_args.args
    assert pos_data["entry_price"] == pytest.approx(2_393_000.01)
    assert pos_data["filled_price"] == pytest.approx(2_393_000.01)
    assert pos_data["total_entry_cost"] == pytest.approx(145.15)


@pytest.mark.parametrize(
    ("symbol", "history_rows", "quantity", "expected_price", "expected_cost"),
    [
        (
            "THB_BTC",
            [
                {"side": "buy", "rate": "10", "amount": "100", "fee": "0", "ts": 1},
                {"side": "buy", "rate": "20", "amount": "100", "fee": "0", "ts": 2},
                {"side": "sell", "rate": "30", "amount": "8", "fee": "0", "ts": 3},
            ],
            7.0,
            120.0 / 7.0,
            120.0,
        ),
        (
            "THB_DOGE",
            [
                {"side": "buy", "rate": "2.5", "amount": "25", "fee": "0", "ts": 1},
                {"side": "buy", "rate": "3.5", "amount": "35", "fee": "0", "ts": 2},
                {"side": "sell", "rate": "4.0", "amount": "4", "fee": "0", "ts": 3},
            ],
            16.0,
            50.0 / 16.0,
            50.0,
        ),
    ],
)
def test_exchange_history_context_uses_weighted_average_inventory(symbol, history_rows, quantity, expected_price, expected_cost):
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.api_client = Mock()
    bot.api_client.get_order_history.return_value = history_rows

    ctx = TradingBotOrchestrator._resolve_bootstrap_exchange_history_context(bot, symbol, quantity)

    assert ctx["source"] == "exchange_history"
    assert ctx["entry_price"] == pytest.approx(expected_price)
    assert ctx["total_entry_cost"] == pytest.approx(expected_cost)


@pytest.mark.parametrize("symbol", ["THB_BTC", "THB_DOGE", "THB_ETH"])
def test_bootstrap_position_context_uses_weighted_average_trade_history(symbol):
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.api_client = Mock()
    bot.api_client.get_order_history.return_value = []
    bot.db = Mock()
    bot.db.load_all_positions.return_value = [{
        "order_id": f"bootstrap_{symbol}_1",
        "symbol": symbol,
        "side": "buy",
        "amount": 7.0,
        "entry_price": 12.0,
        "total_entry_cost": 84.0,
        "timestamp": datetime(2026, 4, 14, 12, 0, 0),
    }]
    bot.db.get_trades.return_value = [
        Mock(pair=symbol, side="buy", quantity=10.0, price=10.0, timestamp=datetime(2026, 4, 14, 11, 0, 0)),
        Mock(pair=symbol, side="buy", quantity=5.0, price=20.0, timestamp=datetime(2026, 4, 14, 11, 5, 0)),
        Mock(pair=symbol, side="sell", quantity=8.0, price=25.0, timestamp=datetime(2026, 4, 14, 11, 10, 0)),
    ]
    bot.db.get_trade_state.return_value = None

    ctx = TradingBotOrchestrator._resolve_bootstrap_position_context(bot, symbol, 7.0)

    assert ctx["source"] == "trade_history"
    assert ctx["entry_price"] == pytest.approx(120.0 / 7.0)
    assert ctx["total_entry_cost"] == pytest.approx(120.0)


def test_exchange_history_context_uses_default_history_window_limit():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.api_client = Mock()
    bot.api_client.get_order_history.return_value = []
    bot.config = {}

    ctx = TradingBotOrchestrator._resolve_bootstrap_exchange_history_context(bot, "THB_BTC", 1.0)

    assert ctx == {}
    bot.api_client.get_order_history.assert_called_once_with("THB_BTC", limit=200)


def test_lookup_order_history_status_uses_configured_history_window_limit():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.api_client = Mock()
    bot.api_client.get_order_history.return_value = [{"id": "ord-123", "status": "filled"}]
    bot.config = {"data": {"order_history_limit": 180}}

    row = TradingBotOrchestrator._lookup_order_history_status(bot, "THB_BTC", "ord-123")

    assert row == {"id": "ord-123", "status": "filled"}
    bot.api_client.get_order_history.assert_called_once_with("THB_BTC", limit=180)


def test_reconcile_tracked_positions_drops_manual_sold_coin_when_balance_is_zero():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = [{
        "order_id": "manual_btc_1",
        "symbol": "THB_BTC",
        "side": "buy",
        "amount": 0.001,
        "remaining_amount": 0.0,
        "filled": True,
        "filled_amount": 0.001,
    }]
    bot.db = Mock()
    bot._state_machine_enabled = False

    removed = bot._reconcile_tracked_positions_with_balance_state({
        "balances": {
            "BTC": {"available": 0.0, "reserved": 0.0, "total": 0.0}
        }
    })

    assert removed == ["THB_BTC"]
    bot.executor.remove_tracked_position.assert_called_once_with("manual_btc_1")
    bot.db.record_held_coin.assert_called_once_with("THB_BTC", 0.0)


def test_reconcile_tracked_positions_keeps_position_when_coin_balance_remains():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = [{
        "order_id": "manual_btc_1",
        "symbol": "THB_BTC",
        "side": "buy",
        "amount": 0.001,
        "remaining_amount": 0.0,
        "filled": True,
        "filled_amount": 0.001,
    }]
    bot.db = Mock()
    bot._state_machine_enabled = False

    removed = bot._reconcile_tracked_positions_with_balance_state({
        "balances": {
            "BTC": {"available": 0.0009, "reserved": 0.0, "total": 0.0009}
        }
    })

    assert removed == []
    bot.executor.remove_tracked_position.assert_not_called()
    bot.db.record_held_coin.assert_not_called()


def test_reconcile_tracked_positions_bootstraps_missing_held_coin_from_balance_state():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.config = {"risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0, "use_dynamic_sl_tp": False}}
    bot.min_trade_value_thb = 15.0
    bot.api_client = Mock()
    bot.api_client.get_ticker.return_value = {"last": 42.7}
    bot.api_client.get_order_history.return_value = []
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = []
    bot.db = Mock()
    bot.db.load_all_positions.return_value = []
    bot.db.get_trades.return_value = []
    bot.db.get_trade_state.return_value = None
    bot._get_trading_pairs = Mock(return_value=["THB_DOT"])
    bot._state_machine_enabled = False

    with patch("trading_bot.time.sleep"):
        removed = bot._reconcile_tracked_positions_with_balance_state({
            "balances": {
                "DOT": {"available": 3.68279069, "reserved": 0.0, "total": 3.68279069}
            }
        })

    assert removed == []
    bot.executor.register_tracked_position.assert_called_once()
    _, pos_data = bot.executor.register_tracked_position.call_args.args
    assert pos_data["symbol"] == "THB_DOT"
    assert pos_data["amount"] == 3.68279069
    assert pos_data["filled_amount"] == 3.68279069
    bot.db.record_held_coin.assert_called_once_with("THB_DOT", 3.68279069)
    bot.executor.remove_tracked_position.assert_not_called()
    bot.api_client.get_balances.assert_not_called()


def test_scalping_mode_forces_time_exit_after_timeout():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = [{
        "order_id": "scalp_btc_1",
        "symbol": "THB_BTC",
        "side": "buy",
        "amount": 0.001,
        "entry_price": 1000000.0,
        "stop_loss": 990000.0,
        "take_profit": 1020000.0,
        "timestamp": datetime.now() - timedelta(minutes=31),
        "total_entry_cost": 1000.0,
    }]
    bot.api_client = Mock()
    bot.api_client.get_ticker.return_value = {"last": 1005000.0}
    bot._ws_client = None
    bot.trading_pair = "THB_BTC"
    bot._state_machine_enabled = True
    bot._state_manager = Mock()
    bot._state_manager.get_state.return_value = Mock(state=TradeLifecycleState.IN_POSITION)
    bot._submit_managed_exit = Mock()
    bot._scalping_mode_enabled = True
    bot._scalping_position_timeout_minutes = 30

    bot._check_positions_for_sl_tp()

    assert bot._submit_managed_exit.call_args.kwargs["triggered"] == "TIME"


def test_bootstrap_position_is_exempt_from_scalping_timeout():
    """Bootstrap positions remain exempt when bootstrap timeout is explicitly disabled."""
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = [{
        "order_id": "bootstrap_THB_BTC_1776306580",
        "symbol": "THB_BTC",
        "side": "buy",
        "amount": 0.001,
        "entry_price": 1000000.0,
        "stop_loss": 0,
        "take_profit": 0,
        "timestamp": datetime.now() - timedelta(minutes=60),
        "total_entry_cost": 1000.0,
    }]
    bot.api_client = Mock()
    # Price is neutral; SL/TP disabled (0) so only TIME could trigger
    bot.api_client.get_ticker.return_value = {"last": 1005000.0}
    bot._ws_client = None
    bot.trading_pair = "THB_BTC"
    bot._state_machine_enabled = True
    bot._state_manager = Mock()
    bot._state_manager.get_state.return_value = Mock(state=TradeLifecycleState.IN_POSITION)
    bot._submit_managed_exit = Mock()
    bot._scalping_mode_enabled = True
    bot._scalping_position_timeout_minutes = 30
    bot._bootstrap_position_timeout_minutes = None

    bot._check_positions_for_sl_tp()

    # Should NOT have been TIME-exited despite being 60min old
    bot._submit_managed_exit.assert_not_called()


def test_bootstrap_position_forces_time_exit_after_bootstrap_timeout():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = [{
        "order_id": "bootstrap_THB_BTC_1776306580",
        "symbol": "THB_BTC",
        "side": "buy",
        "amount": 0.001,
        "entry_price": 1000000.0,
        "stop_loss": 0,
        "take_profit": 0,
        "timestamp": datetime.now() - timedelta(hours=25),
        "total_entry_cost": 1000.0,
    }]
    bot.api_client = Mock()
    bot.api_client.get_ticker.return_value = {"last": 1005000.0}
    bot._ws_client = None
    bot.trading_pair = "THB_BTC"
    bot._state_machine_enabled = True
    bot._state_manager = Mock()
    bot._state_manager.get_state.return_value = Mock(state=TradeLifecycleState.IN_POSITION)
    bot._submit_managed_exit = Mock()
    bot._scalping_mode_enabled = True
    bot._scalping_position_timeout_minutes = 30
    bot._bootstrap_position_timeout_minutes = 24 * 60

    bot._check_positions_for_sl_tp()

    assert bot._submit_managed_exit.call_args.kwargs["triggered"] == "TIME"


def test_scalping_time_exit_is_suppressed_when_net_profit_is_below_fee_gate():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = [{
        "order_id": "scalp_sol_1",
        "symbol": "THB_SOL",
        "side": "buy",
        "amount": 1.0,
        "entry_price": 100.0,
        "stop_loss": 99.0,
        "take_profit": 102.5,
        "timestamp": datetime.now() - timedelta(minutes=31),
        "total_entry_cost": 100.0,
    }]
    bot.api_client = Mock()
    bot.api_client.get_ticker.return_value = {"last": 100.35}
    bot._ws_client = None
    bot.trading_pair = "THB_SOL"
    bot._state_machine_enabled = True
    bot._state_manager = Mock()
    bot._state_manager.get_state.return_value = Mock(state=TradeLifecycleState.IN_POSITION)
    bot._submit_managed_exit = Mock()
    bot._scalping_mode_enabled = True
    bot._scalping_position_timeout_minutes = 30
    bot._enforce_min_profit_gate_for_voluntary_exit = True
    bot._min_voluntary_exit_net_profit_pct = 0.2

    bot._check_positions_for_sl_tp()

    bot._submit_managed_exit.assert_not_called()


def test_voluntary_exit_gate_uses_position_side_for_pnl_math():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot._enforce_min_profit_gate_for_voluntary_exit = True
    bot._min_voluntary_exit_net_profit_pct = 0.2

    # BUY from 100 -> 99 is a loss and should fail the gate.
    allow_buy = bot._should_allow_voluntary_exit(
        symbol="THB_TEST",
        trigger="TIME",
        entry_price=100.0,
        exit_price=99.0,
        amount=1.0,
        total_entry_cost=100.0,
        side="buy",
    )
    assert allow_buy is False

    # SELL/short from 100 -> 99 is profitable after entry fee deduction and should pass.
    allow_short = bot._should_allow_voluntary_exit(
        symbol="THB_TEST",
        trigger="TIME",
        entry_price=100.0,
        exit_price=99.0,
        amount=1.0,
        total_entry_cost=100.0,
        side="sell",
    )
    assert allow_short is True


def test_preserve_bootstrap_position_keeps_existing_entry_price_from_db():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.config = {"risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0}}
    bot.executor = Mock()
    bot.executor._orders_lock = threading.Lock()
    bot.executor._open_orders = {}
    bot.db = Mock()
    bot._resolve_bootstrap_position_context = Mock(return_value={})

    local_pos = {
        "order_id": "bootstrap_THB_BTC_1",
        "symbol": "THB_BTC",
        "side": "buy",
        "amount": 0.001,
        "entry_price": 1_500_000.0,
        "stop_loss": 1_432_500.0,
        "take_profit": 1_650_000.0,
        "remaining_amount": 0.0,
        "filled": True,
        "filled_amount": 0.001,
        "total_entry_cost": 1500.0,
    }

    preserved = bot._preserve_bootstrap_position_from_balances(
        "bootstrap_THB_BTC_1",
        local_pos,
        {"BTC": {"available": 0.0009, "reserved": 0.0, "total": 0.0009}},
    )

    assert preserved is True
    saved_payload = bot.db.save_position.call_args.args[0]
    assert saved_payload["entry_price"] == 1_500_000.0
    assert saved_payload["amount"] == 0.0009
    assert saved_payload["filled_amount"] == 0.0009
    assert saved_payload["total_entry_cost"] == 1350.0
    assert saved_payload["stop_loss"] == 1_432_500.0
    assert saved_payload["take_profit"] == 1_650_000.0


def test_preserve_bootstrap_position_rebuilds_missing_sl_tp_from_existing_entry_price():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.config = {"risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0, "use_dynamic_sl_tp": False}}
    bot.executor = Mock()
    bot.executor._orders_lock = threading.Lock()
    bot.executor._open_orders = {}
    bot.db = Mock()
    bot._resolve_bootstrap_position_context = Mock(return_value={})

    preserved = bot._preserve_bootstrap_position_from_balances(
        "bootstrap_THB_BTC_1",
        {
            "symbol": "THB_BTC",
            "side": "buy",
            "entry_price": 1_500_000.0,
            "stop_loss": None,
            "take_profit": None,
        },
        {"BTC": {"available": 0.0009, "reserved": 0.0, "total": 0.0009}},
    )

    assert preserved is True
    saved_payload = bot.db.save_position.call_args.args[0]
    assert saved_payload["stop_loss"] == 1_432_500.0
    assert saved_payload["take_profit"] == 1_650_000.0


def test_preserve_bootstrap_position_does_not_average_in_external_balance_increase():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.config = {"risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0}}
    bot.executor = Mock()
    bot.executor._orders_lock = threading.Lock()
    bot.executor._open_orders = {}
    bot.db = Mock()
    bot._resolve_bootstrap_position_context = Mock(return_value={})

    preserved = bot._preserve_bootstrap_position_from_balances(
        "bootstrap_THB_BTC_1",
        {
            "order_id": "bootstrap_THB_BTC_1",
            "symbol": "THB_BTC",
            "side": "buy",
            "amount": 0.001,
            "filled_amount": 0.001,
            "entry_price": 1_500_000.0,
            "stop_loss": 1_432_500.0,
            "take_profit": 1_650_000.0,
        },
        {"BTC": {"available": 0.0014, "reserved": 0.0, "total": 0.0014}},
    )

    assert preserved is True
    saved_payload = bot.db.save_position.call_args.args[0]
    assert saved_payload["amount"] == 0.001
    assert saved_payload["filled_amount"] == 0.001
    assert saved_payload["entry_price"] == 1_500_000.0
    assert saved_payload["total_entry_cost"] == 1500.0


def test_preserve_bootstrap_position_returns_false_without_live_balance():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.config = {"risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0}}
    bot.executor = Mock()
    bot.executor._orders_lock = threading.Lock()
    bot.executor._open_orders = {}
    bot.db = Mock()
    bot._resolve_bootstrap_position_context = Mock(return_value={})

    preserved = bot._preserve_bootstrap_position_from_balances(
        "bootstrap_THB_BTC_1",
        {"symbol": "THB_BTC", "side": "buy", "entry_price": 1_500_000.0},
        {"BTC": {"available": 0.0, "reserved": 0.0, "total": 0.0}},
    )

    assert preserved is False
    bot.db.save_position.assert_not_called()


def test_preserve_bootstrap_position_upgrades_entry_from_exchange_history_context():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.config = {"risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0}}
    bot.executor = Mock()
    bot.executor._orders_lock = threading.Lock()
    bot.executor._open_orders = {}
    bot.db = Mock()
    bot._resolve_bootstrap_position_context = Mock(return_value={
        "entry_price": 2_393_000.01,
        "total_entry_cost": 145.15,
        "source": "exchange_history",
    })

    preserved = bot._preserve_bootstrap_position_from_balances(
        "bootstrap_THB_BTC_1",
        {
            "order_id": "bootstrap_THB_BTC_1",
            "symbol": "THB_BTC",
            "side": "buy",
            "amount": 0.0000279,
            "filled_amount": 0.0000279,
            "entry_price": 2_390_515.0,
            "stop_loss": 2_372_586.14,
            "take_profit": 2_432_349.01,
            "total_entry_cost": 144.6261575,
        },
        {"BTC": {"available": 0.0000605, "reserved": 0.0, "total": 0.0000605}},
    )

    assert preserved is True
    saved_payload = bot.db.save_position.call_args.args[0]
    assert saved_payload["amount"] == pytest.approx(0.0000605)
    assert saved_payload["filled_amount"] == pytest.approx(0.0000605)
    assert saved_payload["entry_price"] == pytest.approx(2_393_000.01)
    assert saved_payload["filled_price"] == pytest.approx(2_393_000.01)
    assert saved_payload["total_entry_cost"] == pytest.approx(145.15)


def test_preserve_bootstrap_position_propagates_recovered_acquired_at_timestamp():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.config = {"risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0}}
    bot.executor = Mock()
    bot.executor._orders_lock = threading.Lock()
    bot.executor._open_orders = {}
    bot.db = Mock()
    recovered_at = datetime(2026, 4, 15, 8, 45, 15)
    bot._resolve_bootstrap_position_context = Mock(return_value={
        "entry_price": 2_393_000.01,
        "total_entry_cost": 145.15,
        "acquired_at": recovered_at,
        "source": "exchange_history",
    })

    preserved = bot._preserve_bootstrap_position_from_balances(
        "bootstrap_THB_BTC_1",
        {
            "order_id": "bootstrap_THB_BTC_1",
            "symbol": "THB_BTC",
            "side": "buy",
            "amount": 0.0000279,
            "filled_amount": 0.0000279,
            "entry_price": 2_390_515.0,
            "stop_loss": 2_372_586.14,
            "take_profit": 2_432_349.01,
            "total_entry_cost": 144.6261575,
            "timestamp": datetime(2026, 4, 16, 16, 33, 36),
        },
        {"BTC": {"available": 0.0000605, "reserved": 0.0, "total": 0.0000605}},
    )

    assert preserved is True
    saved_payload = bot.db.save_position.call_args.args[0]
    assert saved_payload["timestamp"] == recovered_at
