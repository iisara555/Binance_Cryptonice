import json
from unittest.mock import Mock

from rich.console import Console

from main import TradingBotApp
from cli_ui import CLICommandCenter
from signal_generator import _LATEST_SIGNAL_FLOW, _SIGNAL_FLOW_LOCK, _diag
from trade_executor import OrderResult, OrderSide, OrderStatus


def _build_app(tmp_path, *, auto_detect_held_pairs: bool = False) -> TradingBotApp:
    whitelist_path = tmp_path / "coin_whitelist.json"
    config_path = tmp_path / "bot_config.yaml"
    config_path.write_text(
        "strategy_mode:\n  active: \"standard\"\n",
        encoding="utf-8",
    )
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
            "strategy_mode": {"active": "standard"},
        },
        config_path=str(config_path),
    )


def _clear_signal_flow() -> None:
    with _SIGNAL_FLOW_LOCK:
        _LATEST_SIGNAL_FLOW.clear()


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


def test_track_manual_position_registers_real_cost_basis_with_sl_tp(tmp_path):
    app = _build_app(tmp_path)
    app.api_client = Mock()
    app.executor = Mock()
    app.executor.get_open_orders.return_value = []

    result = app.track_manual_position("btc", 0.001, 1_500_000.0)

    assert result["symbol"] == "THB_BTC"
    assert result["entry_price"] == 1_500_000.0
    assert result["total_entry_cost"] == 1500.0
    assert result["stop_loss"] == 1_447_500.0
    assert result["take_profit"] == 1_590_000.0
    app.executor.register_tracked_position.assert_called_once()


def test_process_cli_command_track_requires_confirmation_and_registers_position(tmp_path):
    app = _build_app(tmp_path)
    app.api_client = Mock()
    app.executor = Mock()
    app.executor.get_open_orders.return_value = []

    preview = app.process_cli_command("track btc 0.001 1500000")

    assert "Type 'confirm'" in preview
    app.executor.register_tracked_position.assert_not_called()

    confirmed = app.process_cli_command("confirm")

    assert "Tracked position:" in confirmed
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


def test_mode_show_reports_active_mode_and_path(tmp_path):
    app = _build_app(tmp_path)

    result = app.process_cli_command("mode show")

    assert "Strategy mode: standard" in result
    assert "timeframe=" in result
    assert str(tmp_path / "bot_config.yaml") in result


def test_cli_snapshot_and_header_expose_active_strategy_mode(tmp_path):
    app = _build_app(tmp_path)
    app.config["active_strategy_mode"] = "trend_only"

    snapshot = app.get_cli_snapshot()
    console = Console(record=True, width=120)
    console.print(CLICommandCenter(app)._build_header(snapshot))
    header_text = console.export_text()

    assert snapshot["strategy_mode"] == "trend_only"
    assert "trend_only" in header_text


def test_mode_set_requires_confirmation_and_persists_yaml_active_mode(tmp_path):
    app = _build_app(tmp_path)

    preview = app.process_cli_command("mode set scalping")

    assert "Type 'confirm'" in preview
    result = app.process_cli_command("confirm")

    assert "Strategy mode saved: scalping" in result
    config_text = (tmp_path / "bot_config.yaml").read_text(encoding="utf-8")
    assert 'active: "scalping"' in config_text
    assert app.config["active_strategy_mode"] == "scalping"


def test_mode_set_restart_requires_confirmation_and_requests_restart(tmp_path):
    app = _build_app(tmp_path)
    app.stop = Mock()

    preview = app.process_cli_command("mode set trend_only restart")

    assert "Type 'confirm'" in preview
    result = app.process_cli_command("confirm")

    assert "Strategy mode saved: trend_only" in result
    assert "restarting now" in result
    config_text = (tmp_path / "bot_config.yaml").read_text(encoding="utf-8")
    assert 'active: "trend_only"' in config_text
    assert app._restart_requested is True


def test_mode_alias_supports_shortcuts_and_cycle(tmp_path):
    app = _build_app(tmp_path)

    preview = app.process_cli_command("mode trend")
    assert "Type 'confirm'" in preview
    result = app.process_cli_command("confirm")
    assert "Strategy mode saved: trend_only" in result

    preview = app.process_cli_command("mode cycle")
    assert "Type 'confirm'" in preview
    result = app.process_cli_command("confirm")
    assert "Strategy mode saved: scalping" in result


def test_mode_alias_restart_requests_restart(tmp_path):
    app = _build_app(tmp_path)
    app.stop = Mock()

    preview = app.process_cli_command("mode scalp restart")

    assert "Type 'confirm'" in preview
    result = app.process_cli_command("confirm")

    assert "Strategy mode saved: scalping" in result
    assert "restarting now" in result
    assert app._restart_requested is True


def test_help_includes_track_command(tmp_path):
    app = _build_app(tmp_path)

    help_text = app.process_cli_command("help")

    assert "track <PAIR> <COIN_AMOUNT> <ENTRY_PRICE>" in help_text
    assert "mode <standard|trend|scalp>" in help_text
    assert "mode set <standard|trend_only|scalping>" in help_text
    assert "ui log <debug|info|warning|error|critical>" in help_text


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


def test_ui_commands_update_runtime_dashboard_preferences(tmp_path):
    app = _build_app(tmp_path)

    msg1 = app.process_cli_command("ui log warning")
    msg2 = app.process_cli_command("ui footer verbose")
    snapshot = app.get_cli_snapshot()

    assert msg1 == "UI log filter set to WARNING+"
    assert msg2 == "UI footer mode set to verbose"
    assert snapshot["ui"]["log_level_filter"] == "WARNING"
    assert snapshot["ui"]["footer_mode"] == "verbose"


def test_ui_command_rejects_invalid_values(tmp_path):
    app = _build_app(tmp_path)

    bad_log = app.process_cli_command("ui log noisy")
    bad_footer = app.process_cli_command("ui footer tiny")

    assert bad_log == "Usage: ui log <debug|info|warning|error|critical>"
    assert bad_footer == "Usage: ui footer <compact|verbose>"


def test_signal_alignment_reports_waiting_for_market_data(tmp_path):
    _clear_signal_flow()
    try:
        app = _build_app(tmp_path)
        _diag("THB_BTC", "Sniper:DataCheck", "REJECT", "Insufficient data (3/210 bars)")

        rows = app._build_cli_signal_alignment(["THB_BTC"])

        assert rows[0]["action"] == "WAIT"
        assert rows[0]["status"] == "Waiting: Insufficient data (3/210 bars)"
    finally:
        _clear_signal_flow()


def test_signal_alignment_reports_waiting_for_first_signal_flow(tmp_path):
    _clear_signal_flow()
    try:
        app = _build_app(tmp_path)

        rows = app._build_cli_signal_alignment(["THB_BTC"])

        assert rows[0]["action"] == "WAIT"
        assert rows[0]["status"] == "Waiting for first signal flow"
    finally:
        _clear_signal_flow()


def test_signal_alignment_includes_pair_runtime_context(tmp_path):
    _clear_signal_flow()
    try:
        app = _build_app(tmp_path)

        rows = app._build_cli_signal_alignment(
            ["THB_BTC"],
            {
                "pairs": [
                    {
                        "pair": "THB_BTC",
                        "ready": False,
                        "timeframes": [
                            {"timeframe": "1m", "count": 5, "latest": "2026-04-11T15:35:00Z"},
                            {"timeframe": "5m", "count": 0, "latest": None},
                        ],
                    }
                ]
            },
        )

        assert rows[0]["tf_ready"] == "1/2"
        assert rows[0]["pair_state"] == "Collecting"
        assert rows[0]["market_update"] == "22:35:00"
    finally:
        _clear_signal_flow()