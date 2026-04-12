import os
import threading
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

os.environ.setdefault("BITKUB_API_KEY", "test-key")
os.environ.setdefault("BITKUB_API_SECRET", "test-secret")

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
    assert "No tracked THB_ETH position exists" in alert_message
    assert "not auto-converted into a managed bot position" in alert_message


def test_bootstrap_held_positions_assigns_sl_tp_to_synthetic_positions():
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
    bot._get_trading_pairs = Mock(return_value=["THB_BTC"])

    with patch("trading_bot.time.sleep"):
        bot._bootstrap_held_positions()

    bot.executor.register_tracked_position.assert_called_once()
    _, pos_data = bot.executor.register_tracked_position.call_args.args
    assert pos_data["entry_price"] == 1_500_000.0
    assert pos_data["stop_loss"] == 1_432_500.0
    assert pos_data["take_profit"] == 1_650_000.0


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


def test_preserve_bootstrap_position_keeps_existing_entry_price_from_db():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.config = {"risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0}}
    bot.executor = Mock()
    bot.executor._orders_lock = threading.Lock()
    bot.executor._open_orders = {}
    bot.db = Mock()

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
    bot.config = {"risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0}}
    bot.executor = Mock()
    bot.executor._orders_lock = threading.Lock()
    bot.executor._open_orders = {}
    bot.db = Mock()

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

    preserved = bot._preserve_bootstrap_position_from_balances(
        "bootstrap_THB_BTC_1",
        {"symbol": "THB_BTC", "side": "buy", "entry_price": 1_500_000.0},
        {"BTC": {"available": 0.0, "reserved": 0.0, "total": 0.0}},
    )

    assert preserved is False
    bot.db.save_position.assert_not_called()
