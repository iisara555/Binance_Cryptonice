import json
import logging
from unittest.mock import Mock, patch

import pytest
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
    _, tracked_payload = app.executor.register_tracked_position.call_args.args
    assert tracked_payload["amount"] == pytest.approx(0.00025)
    assert tracked_payload["filled"] is True
    assert tracked_payload["filled_amount"] == pytest.approx(0.00025)
    assert tracked_payload["remaining_amount"] == pytest.approx(0.0)


def test_submit_manual_market_buy_rounds_down_request_amount(tmp_path):
    app = _build_app(tmp_path)
    app.api_client = Mock()
    app.executor = Mock()
    app.executor.execute_order.return_value = OrderResult(
        success=True,
        status=OrderStatus.FILLED,
        order_id="buy-2",
        filled_amount=0.00025,
        filled_price=2_000_000.0,
        remaining_amount=0.0,
    )

    app.submit_manual_market_buy("btc", 500.129)

    order_request = app.executor.execute_order.call_args.args[0]
    assert order_request.amount == pytest.approx(500.12)


def test_submit_manual_market_buy_normalizes_pending_market_fill_into_coin_quantity(tmp_path):
    app = _build_app(tmp_path)
    app.api_client = Mock()
    app.executor = Mock()
    app._get_cli_price = Mock(return_value=2_000_000.0)
    app.executor.execute_order.return_value = OrderResult(
        success=True,
        status=OrderStatus.PENDING,
        order_id="buy-pending-1",
        filled_amount=0.0,
        filled_price=None,
        remaining_amount=500.0,
    )

    result = app.submit_manual_market_buy("btc", 500.0)

    assert result["order_id"] == "buy-pending-1"
    _, tracked_payload = app.executor.register_tracked_position.call_args.args
    assert tracked_payload["amount"] == pytest.approx(0.00025)
    assert tracked_payload["filled"] is True
    assert tracked_payload["filled_amount"] == pytest.approx(0.00025)
    assert tracked_payload["remaining_amount"] == pytest.approx(0.0)
    assert tracked_payload["filled_price"] == pytest.approx(2_000_000.0)


def test_track_manual_position_registers_real_cost_basis_with_sl_tp(tmp_path):
    app = _build_app(tmp_path)
    app.api_client = Mock()
    app.executor = Mock()
    app.executor.get_open_orders.return_value = []

    result = app.track_manual_position("btc", 0.001, 1_500_000.0)

    assert result["symbol"] == "THB_BTC"
    assert result["entry_price"] == 1_500_000.0
    assert result["total_entry_cost"] == 1500.0
    assert result["stop_loss"] == 1_470_000.0
    assert result["take_profit"] == 1_560_000.0
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
            "remaining_amount": 0.0,
            "filled_amount": 0.001,
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


def test_submit_manual_market_sell_by_unknown_order_id_fails_loudly(tmp_path):
    app = _build_app(tmp_path)
    app.api_client = Mock()
    app.executor = Mock()
    app.executor.get_open_orders.return_value = []

    with pytest.raises(ValueError, match="Active order not found: ghost-order"):
        app.submit_manual_market_sell("ghost-order")


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


def test_render_exposes_btop_style_dashboard_panels(tmp_path):
    app = _build_app(tmp_path)
    console = Console(record=True, width=180, height=40)
    command_center = CLICommandCenter(app, console=console)

    console.print(command_center.render(app.get_cli_snapshot()))
    rendered = console.export_text()

    assert "Trading Matrix" in rendered
    assert "Risk Rails" in rendered
    assert "Event Tape" in rendered
    assert "Position Book" in rendered
    assert "Signal Radar" in rendered


def test_render_records_real_snapshot_metric_history(tmp_path):
    app = _build_app(tmp_path)
    command_center = CLICommandCenter(app, console=Console(record=True, width=180, height=40))

    snapshot_1 = app.get_cli_snapshot()
    snapshot_1["system"]["available_balance"] = "500.00 THB"
    snapshot_1["system"]["total_balance"] = "20,000.00 THB"
    snapshot_1["system"]["trade_count"] = "1"
    snapshot_1["system"]["daily_loss"] = "5.00 / 100.00 THB"
    snapshot_1["signal_alignment"] = [{"action": "BUY"}, {"action": "WAIT"}]
    command_center.render(snapshot_1)

    snapshot_2 = json.loads(json.dumps(snapshot_1))
    snapshot_2["system"]["available_balance"] = "650.00 THB"
    snapshot_2["system"]["total_balance"] = "20,250.00 THB"
    snapshot_2["system"]["trade_count"] = "3"
    snapshot_2["system"]["daily_loss"] = "12.00 / 100.00 THB"
    snapshot_2["positions"] = [{"symbol": "THB_BTC"}]
    snapshot_2["signal_alignment"] = [{"action": "BUY"}, {"action": "BUY"}, {"action": "SELL"}]
    command_center.render(snapshot_2)

    assert list(command_center._trend_history["available_balance"])[-2:] == [500.0, 650.0]
    assert list(command_center._trend_history["total_balance"])[-2:] == [20000.0, 20250.0]
    assert list(command_center._trend_history["trade_count"])[-2:] == [1.0, 3.0]
    assert list(command_center._trend_history["open_positions"])[-2:] == [0.0, 1.0]


def test_positions_panel_shows_book_summary_and_pnl_trend(tmp_path):
    app = _build_app(tmp_path)
    command_center = CLICommandCenter(app, console=Console(record=True, width=160, height=40))

    snapshot_1 = app.get_cli_snapshot()
    snapshot_1["positions"] = [
        {"symbol": "THB_BTC", "side": "buy", "entry_price": 100.0, "current_price": 105.0, "pnl_pct": 5.0, "sl_distance_pct": -1.5, "tp_distance_pct": 2.0},
    ]
    command_center.render(snapshot_1)

    snapshot_2 = json.loads(json.dumps(snapshot_1))
    snapshot_2["positions"] = [
        {"symbol": "THB_BTC", "side": "buy", "entry_price": 100.0, "current_price": 104.0, "pnl_pct": 4.0, "sl_distance_pct": -1.0, "tp_distance_pct": 2.5},
        {"symbol": "THB_ETH", "side": "buy", "entry_price": 200.0, "current_price": 196.0, "pnl_pct": -2.0, "sl_distance_pct": -1.2, "tp_distance_pct": 3.0},
    ]
    command_center.render(snapshot_2)

    panel = command_center._build_positions_table(snapshot_2, compact=False)
    console = Console(record=True, width=160, height=40)
    console.print(panel)
    rendered = console.export_text()

    assert "Position Book" in rendered
    assert "BOOK PNL" in rendered
    assert "PNL TREND" in rendered
    assert "OPEN TREND" in rendered
    assert list(command_center._trend_history["avg_pnl_pct"])[-2:] == [5.0, 1.0]


def test_signal_and_portfolio_panels_show_history_based_quality_and_mix(tmp_path):
    app = _build_app(tmp_path)
    command_center = CLICommandCenter(app, console=Console(record=True, width=170, height=40))

    snapshot_1 = app.get_cli_snapshot()
    snapshot_1["signal_alignment"] = [
        {"action": "BUY", "trend": "UP", "status": "Ready"},
        {"action": "WAIT", "trend": "MIXED", "status": "Waiting"},
    ]
    snapshot_1["system"]["balance_breakdown"] = [
        "BTC 0.01000000 = 20,000.00 THB (80.00%)",
        "THB 500.00 = 500.00 THB (20.00%)",
    ]
    snapshot_1["system"]["total_balance"] = "20,500.00 THB"
    command_center.render(snapshot_1)

    snapshot_2 = json.loads(json.dumps(snapshot_1))
    snapshot_2["signal_alignment"] = [
        {"action": "SELL", "trend": "DOWN", "status": "Ready"},
        {"action": "WAIT", "trend": "MIXED", "status": "Waiting for data"},
    ]
    snapshot_2["system"]["balance_breakdown"] = [
        "BTC 0.00800000 = 16,000.00 THB (64.00%)",
        "THB 9,000.00 = 9,000.00 THB (36.00%)",
    ]
    snapshot_2["system"]["total_balance"] = "25,000.00 THB"
    command_center.render(snapshot_2)

    signal_console = Console(record=True, width=170, height=40)
    signal_console.print(command_center._build_signal_alignment_panel(snapshot_2))
    signal_rendered = signal_console.export_text()

    portfolio_console = Console(record=True, width=170, height=40)
    portfolio_console.print(command_center._build_balance_breakdown_panel(snapshot_2))
    portfolio_rendered = portfolio_console.export_text()

    assert "QUALITY" in signal_rendered
    assert "SIGNAL TREND" in signal_rendered
    assert "CASH MIX" in portfolio_rendered
    assert "CONCENTRATION" in portfolio_rendered
    assert list(command_center._trend_history["signal_score"])[-2:] == [2.0, -4.0]
    assert list(command_center._trend_history["top_allocation_pct"])[-2:] == [80.0, 64.0]
    assert list(command_center._trend_history["cash_allocation_pct"])[-2:] == [20.0, 36.0]


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


def test_cli_snapshot_uses_lightweight_status_during_live_dashboard(tmp_path):
    app = _build_app(tmp_path)
    app._live_dashboard_active = True
    app.executor = Mock()
    app.executor.get_open_orders.return_value = []
    app.bot = Mock()
    app.bot.get_status.return_value = {
        "mode": "full_auto",
        "trading_pairs": ["THB_BTC"],
        "strategy_engine": {"strategies": []},
        "risk_summary": {},
        "last_loop": None,
        "multi_timeframe": {},
    }
    app.bot._get_portfolio_state.return_value = {"balance": 500.0, "timestamp": None}
    app._sample_api_latency = Mock(return_value=None)

    app.get_cli_snapshot()

    app.bot.get_status.assert_called_once_with(lightweight=True)


def test_cli_snapshot_live_dashboard_uses_cache_fallback_for_position_prices(tmp_path):
    """In live mode, REST fallback is disabled. Instead, stale cache is used."""
    app = _build_app(tmp_path)
    app._live_dashboard_active = True
    # Seed price cache so the stale-cache fallback kicks in
    app._cli_price_cache["THB_BTC"] = (105.0, 0.0)
    app.executor = Mock()
    app.executor.get_open_orders.return_value = [
        {
            "symbol": "THB_BTC",
            "side": "buy",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 104.0,
        }
    ]
    app.bot = Mock()
    app.bot.get_status.return_value = {
        "mode": "full_auto",
        "trading_pairs": ["THB_BTC"],
        "strategy_engine": {"strategies": []},
        "risk_summary": {},
        "last_loop": None,
        "multi_timeframe": {},
    }
    app.bot._get_portfolio_state.return_value = {"balance": 500.0, "timestamp": None}
    app._sample_api_latency = Mock(return_value=None)
    app._get_cli_price = Mock(return_value=None)

    snapshot = app.get_cli_snapshot()

    position = snapshot["positions"][0]
    assert position["current_price"] == 105.0
    assert position["pnl_pct"] == pytest.approx(5.0)


def test_get_cli_price_accepts_stale_ws_tick_in_live_mode(tmp_path):
    """Stale WS prices are accepted for dashboard display when REST fallback is off."""
    app = _build_app(tmp_path)
    app.bot = Mock()
    app.bot._ws_client = Mock()
    app.api_client = Mock()

    with patch("main.get_current_price", return_value=(105.0, "ws_stale")):
        price = app._get_cli_price("THB_BTC", allow_rest_fallback=False)

    assert price == 105.0
    assert app._cli_price_cache["THB_BTC"][0] == 105.0


def test_cli_snapshot_uses_blank_pnl_when_position_price_unavailable(tmp_path):
    app = _build_app(tmp_path)
    app._live_dashboard_active = True
    app.executor = Mock()
    app.executor.get_open_orders.return_value = [
        {
            "symbol": "THB_BTC",
            "side": "buy",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 104.0,
        }
    ]
    app.bot = Mock()
    app.bot.get_status.return_value = {
        "mode": "full_auto",
        "trading_pairs": ["THB_BTC"],
        "strategy_engine": {"strategies": []},
        "risk_summary": {},
        "last_loop": None,
        "multi_timeframe": {},
    }
    app.bot._get_portfolio_state.return_value = {"balance": 500.0, "timestamp": None}
    app._sample_api_latency = Mock(return_value=None)
    app._get_cli_price = Mock(side_effect=[None, None])
    app._get_cli_position_price_hint = Mock(return_value=None)

    snapshot = app.get_cli_snapshot()

    position = snapshot["positions"][0]
    assert position["current_price"] is None
    assert position["pnl_pct"] is None


def test_help_includes_track_command(tmp_path):
    app = _build_app(tmp_path)

    help_text = app.process_cli_command("help")

    assert "track <PAIR> <COIN_AMOUNT> <ENTRY_PRICE>" in help_text
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


def test_footer_render_height_stays_stable_across_content_variants(tmp_path):
    app = _build_app(tmp_path)
    console = Console(record=True, width=80, height=32)
    command_center = CLICommandCenter(app, console=console)

    compact_snapshot = app.get_cli_snapshot()
    compact_snapshot["ui"] = {"footer_mode": "verbose", "log_level_filter": "INFO"}
    compact_snapshot["chat"] = {
        "status": "Ready",
        "history": [],
        "pending_confirmation": None,
        "suggestions": [],
        "input": "",
    }

    busy_snapshot = app.get_cli_snapshot()
    busy_snapshot["ui"] = {"footer_mode": "verbose", "log_level_filter": "INFO"}
    busy_snapshot["chat"] = {
        "status": "Completed: buy btc 500 and inspect managed state",
        "history": [
            {"role": "user", "message": "buy btc 500 then confirm and inspect tracked position details"},
            {"role": "bot", "message": "Preview ready. Type confirm to execute the pending BUY request."},
            {"role": "user", "message": "confirm and show the resulting order plus latest runtime status"},
            {"role": "bot", "message": "Order submitted and awaiting fill confirmation from the exchange."},
        ],
        "pending_confirmation": {
            "summary": "BUY THB_BTC 500 THB with live confirmation required before execution",
            "command_text": "buy btc 500",
        },
        "suggestions": [
            "confirm",
            "cancel",
            "status",
            "positions",
            "help",
        ],
        "input": "ui footer verbose and keep the dashboard stable while typing long commands",
    }

    compact_console = Console(record=True, width=80, height=32)
    compact_console.print(CLICommandCenter(app, console=compact_console)._build_footer(compact_snapshot))
    compact_lines = compact_console.export_text().splitlines()

    busy_console = Console(record=True, width=80, height=32)
    busy_console.print(CLICommandCenter(app, console=busy_console)._build_footer(busy_snapshot))
    busy_lines = busy_console.export_text().splitlines()

    assert len(compact_lines) == len(busy_lines)


def test_render_signature_ignores_timestamp_only_changes(tmp_path):
    app = _build_app(tmp_path)
    command_center = CLICommandCenter(app, console=Console(record=True, width=80, height=32))

    snapshot = app.get_cli_snapshot()
    snapshot["ui"] = {"footer_mode": "compact", "log_level_filter": "INFO"}
    snapshot["system"]["market_age_seconds"] = 3
    sig1 = command_center._build_render_signature(snapshot)

    snapshot_changed = json.loads(json.dumps(snapshot))
    snapshot_changed["updated_at"] = "23:59:59"
    snapshot_changed["system"]["market_age_seconds"] = 999
    sig2 = command_center._build_render_signature(snapshot_changed)

    assert sig1 == sig2


def test_render_signature_changes_when_visible_logs_change(tmp_path):
    app = _build_app(tmp_path)
    command_center = CLICommandCenter(app, console=Console(record=True, width=80, height=32))

    snapshot = app.get_cli_snapshot()
    snapshot["ui"] = {"footer_mode": "compact", "log_level_filter": "INFO"}
    sig_before = command_center._build_render_signature(snapshot)

    record = logging.LogRecord(
        name="crypto-bot.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="Runtime warning changed visible log rows",
        args=(),
        exc_info=None,
    )
    command_center._append_log_record(record)

    sig_after = command_center._build_render_signature(snapshot)

    assert sig_before != sig_after


def test_footer_size_handles_zero_terminal_height(tmp_path):
    app = _build_app(tmp_path)
    command_center = CLICommandCenter(app, console=Console(record=True, width=80, height=32))

    compact_size = command_center._resolve_footer_size(80, 0, "compact")
    verbose_size = command_center._resolve_footer_size(80, 0, "verbose")

    assert compact_size >= 9
    assert verbose_size >= 9


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
                        "waiting_summary": "1m:30",
                        "timeframes": [
                            {"timeframe": "1m", "count": 5, "waiting_candles": 30, "latest": "2026-04-11T15:35:00Z"},
                            {"timeframe": "5m", "count": 40, "waiting_candles": 0, "latest": None},
                        ],
                    }
                ]
            },
        )

        assert rows[0]["tf_ready"] == "1/2"
        assert rows[0]["pair_state"] == "Collecting 1m:30"
        assert rows[0]["wait_detail"] == "1m:30"
        assert rows[0]["market_update"] == "22:35:00"
    finally:
        _clear_signal_flow()


def test_signal_alignment_prioritizes_waiting_pairs_first(tmp_path):
    _clear_signal_flow()
    try:
        app = _build_app(tmp_path)

        rows = app._build_cli_signal_alignment(
            ["THB_BTC", "THB_SOL"],
            {
                "pairs": [
                    {
                        "pair": "THB_BTC",
                        "ready": True,
                        "waiting_summary": "ready",
                        "timeframes": [{"timeframe": "1m", "count": 40, "waiting_candles": 0, "latest": "2026-04-11T15:35:00Z"}],
                    },
                    {
                        "pair": "THB_SOL",
                        "ready": False,
                        "waiting_summary": "1m:12",
                        "timeframes": [{"timeframe": "1m", "count": 23, "waiting_candles": 12, "latest": "2026-04-11T15:34:00Z"}],
                    },
                ]
            },
        )

        assert rows[0]["symbol"] == "THB_SOL"
        assert rows[0]["wait_detail"] == "1m:12"
        assert rows[1]["symbol"] == "THB_BTC"
    finally:
        _clear_signal_flow()