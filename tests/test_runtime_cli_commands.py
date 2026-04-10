import json
from unittest.mock import Mock

from main import TradingBotApp
from trade_executor import OrderResult, OrderSide, OrderStatus


def _build_app(tmp_path, *, auto_detect_held_pairs: bool = False) -> TradingBotApp:
    whitelist_path = tmp_path / "coin_whitelist.json"
    return TradingBotApp(
        {
            "mode": "full_auto",
            "simulate_only": False,
            "read_only": False,
            "risk": {"max_risk_per_trade_pct": 2.0},
            "data": {
                "pairs": ["THB_BTC"],
                "auto_detect_held_pairs": auto_detect_held_pairs,
                "hybrid_dynamic_coin_config": {
                    "whitelist_json_path": str(whitelist_path),
                    "min_quote_balance_thb": 100.0,
                    "require_supported_market": True,
                    "include_assets_with_balance": True,
                },
            },
            "cli_ui": {"enabled": False, "command_listener_enabled": False},
        }
    )


def test_set_runtime_risk_pct_updates_running_config(tmp_path):
    app = _build_app(tmp_path)
    app.risk_manager = Mock()
    app.risk_manager.config = Mock(max_risk_per_trade_pct=2.0)

    result = app.set_runtime_risk_pct(3.5)

    assert result["status"] == "ok"
    assert result["risk_pct"] == 3.5
    assert app.config["risk"]["max_risk_per_trade_pct"] == 3.5
    assert app.risk_manager.config.max_risk_per_trade_pct == 3.5


def test_pairs_add_and_remove_updates_pairlist_and_runtime_pairs(tmp_path):
    app = _build_app(tmp_path)

    add_result = app.add_runtime_pairs(["btc", "THB_ETH"])

    assert add_result["added_pairs"] == ["THB_BTC", "THB_ETH"]
    assert add_result["active_pairs"] == ["THB_BTC", "THB_ETH"]
    whitelist_path = tmp_path / "coin_whitelist.json"
    assert json.loads(whitelist_path.read_text(encoding="utf-8"))["assets"] == ["BTC", "ETH"]

    remove_result = app.remove_runtime_pairs(["btc"])

    assert remove_result["removed_pairs"] == ["THB_BTC"]
    assert remove_result["active_pairs"] == ["THB_ETH"]
    assert json.loads(whitelist_path.read_text(encoding="utf-8"))["assets"] == ["ETH"]


def test_submit_manual_market_buy_places_market_order_and_tracks_position(tmp_path):
    app = _build_app(tmp_path)
    app.api_client = Mock()
    app.executor = Mock()
    app.executor.execute_order.return_value = OrderResult(
        success=True,
        status=OrderStatus.FILLED,
        order_id="buy-1",
        filled_amount=0.00025,
        filled_price=2_000_000.0,
        remaining_amount=0.0,
    )

    result = app.submit_manual_market_buy("btc", 500.0)

    order_request = app.executor.execute_order.call_args.args[0]
    assert order_request.symbol == "THB_BTC"
    assert order_request.side == OrderSide.BUY
    assert order_request.amount == 500.0
    assert order_request.order_type == "market"
    assert result["order_id"] == "buy-1"
    app.executor.register_tracked_position.assert_called_once()


def test_submit_manual_market_sell_can_close_active_order_by_id(tmp_path):
    app = _build_app(tmp_path)
    app.api_client = Mock()
    app.executor = Mock()
    app.executor.get_open_orders.return_value = [
        {
            "order_id": "pos-1",
            "symbol": "THB_BTC",
            "side": OrderSide.BUY,
            "amount": 0.001,
            "remaining_amount": 0.001,
            "entry_price": 1_500_000.0,
        }
    ]
    app.executor.execute_order.return_value = OrderResult(
        success=True,
        status=OrderStatus.FILLED,
        order_id="sell-1",
        filled_amount=0.001,
        filled_price=1_510_000.0,
    )

    result = app.submit_manual_market_sell("pos-1")

    order_request = app.executor.execute_order.call_args.args[0]
    assert order_request.symbol == "THB_BTC"
    assert order_request.side == OrderSide.SELL
    assert order_request.amount == 0.001
    assert order_request.order_type == "market"
    assert result["closed_order_id"] == "pos-1"
    app.executor.remove_tracked_position.assert_called_once_with("pos-1")


def test_process_cli_command_supports_runtime_commands(tmp_path):
    app = _build_app(tmp_path)
    app.risk_manager = Mock()
    app.risk_manager.config = Mock(max_risk_per_trade_pct=2.0)

    risk_message = app.process_cli_command("risk set 2.5")
    pairs_message = app.process_cli_command("pairs add btc doge")

    assert "Type 'confirm'" in risk_message
    assert "THB_BTC" in pairs_message
    assert "THB_DOGE" in pairs_message


def test_cli_chat_submission_updates_snapshot_history(tmp_path):
    app = _build_app(tmp_path)

    result = app._submit_cli_chat_command("help")
    chat = app.get_cli_snapshot().get("chat", {})

    assert "Commands:" in result
    assert chat["history"][0]["role"] == "user"
    assert chat["history"][0]["message"] == "help"
    assert chat["history"][1]["role"] == "bot"
    assert "Commands:" in chat["history"][1]["message"]
    assert chat["status"] == "Completed: help"


def test_risky_commands_require_confirmation_before_execution(tmp_path):
    app = _build_app(tmp_path)
    app.api_client = Mock()
    app.executor = Mock()
    app.executor.execute_order.return_value = OrderResult(
        success=True,
        status=OrderStatus.FILLED,
        order_id="buy-1",
        filled_amount=0.00025,
        filled_price=2_000_000.0,
        remaining_amount=0.0,
    )

    preview = app.process_cli_command("buy btc 500")

    assert "Type 'confirm'" in preview
    app.executor.execute_order.assert_not_called()

    confirmed = app.process_cli_command("confirm")

    assert "Market BUY submitted" in confirmed
    app.executor.execute_order.assert_called_once()


def test_cancel_clears_pending_confirmation(tmp_path):
    app = _build_app(tmp_path)

    app.process_cli_command("risk set 2.5")
    cancelled = app.process_cli_command("cancel")
    chat = app.get_cli_snapshot().get("chat", {})

    assert cancelled == "Pending confirmation cancelled"
    assert chat["pending_confirmation"] is None
    assert chat["status"] == "Confirmation cancelled"


def test_history_navigation_and_autocomplete_update_chat_input(tmp_path):
    app = _build_app(tmp_path)
    app._submit_cli_chat_command("help")
    app._submit_cli_chat_command("status")

    app._navigate_cli_history(-1)
    assert app.get_cli_snapshot()["chat"]["input"] == "status"

    app._navigate_cli_history(-1)
    assert app.get_cli_snapshot()["chat"]["input"] == "help"

    app._navigate_cli_history(1)
    assert app.get_cli_snapshot()["chat"]["input"] == "status"

    app._cli_chat_input = "ri"
    app._accept_cli_suggestion()
    assert app.get_cli_snapshot()["chat"]["input"].startswith("risk")


def test_snapshot_exposes_pending_confirmation_and_suggestions(tmp_path):
    app = _build_app(tmp_path)

    app.process_cli_command("buy btc 500")
    chat = app.get_cli_snapshot()["chat"]

    assert chat["pending_confirmation"]["command"] == "buy"
    assert any(item == "confirm" for item in chat["suggestions"])