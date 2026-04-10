import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.skip(reason="Rebalance is permanently disabled in sniper mode")

from portfolio_rebalancer import AllocationTarget
from strategy_base import SignalType
from state_management import TradeLifecycleState
from trade_executor import OrderStatus, OrderSide
from trading_bot import TradingBotOrchestrator


def test_rebalance_target_allocation_prefers_plan_targets():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot._rebalancer = Mock()
    bot._rebalancer.get_target_allocation.return_value = {"THB": 20.0, "BTC": 80.0}
    bot.config = {"rebalance": {"target_allocation": {"BTC": 100.0}}}

    plan = SimpleNamespace(
        allocations=[
            AllocationTarget(symbol="THB", target_pct=30.0),
            AllocationTarget(symbol="BTC", target_pct=70.0),
        ]
    )

    targets = TradingBotOrchestrator._get_rebalance_target_allocation(bot, plan)

    assert targets == {"THB": 30.0, "BTC": 70.0}


def test_rebalance_target_allocation_falls_back_to_runtime_targets():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot._rebalancer = Mock()
    bot._rebalancer.get_target_allocation.return_value = {"THB": 25.0, "BTC": 75.0}
    bot.config = {"rebalance": {"target_allocation": {"BTC": 100.0}}}

    targets = TradingBotOrchestrator._get_rebalance_target_allocation(bot)

    assert targets == {"THB": 25.0, "BTC": 75.0}


def test_trigger_rebalance_returns_direct_result():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot._auth_degraded = False
    bot.rebalance = Mock(return_value={
        "status": "skipped",
        "reason": "Within threshold: max drift 0.05% (threshold: 10.0%)",
        "trigger": "manual",
    })

    result = TradingBotOrchestrator.trigger_rebalance(bot)

    bot.rebalance.assert_called_once_with(trigger_source="manual", reason="Manual trigger")
    assert result == {
        "status": "skipped",
        "reason": "Within threshold: max drift 0.05% (threshold: 10.0%)",
        "trigger": "manual",
    }


def test_build_rebalance_overview_returns_sorted_allocations():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot._auth_degraded = False
    bot._last_rebalance_plan = None
    bot._get_rebalance_target_allocation = Mock(return_value={"BTC": 50.0, "THB": 50.0})
    bot._get_rebalance_price_data = Mock(return_value={})
    bot._create_rebalance_adapter = Mock(return_value=SimpleNamespace(total_portfolio_value=lambda: 1000.0))

    allocations = [
        SimpleNamespace(
            symbol="THB",
            target_pct=50.0,
            current_pct=42.0,
            drift_pct=-8.0,
            current_value=420.0,
            current_price=1.0,
            data_ready=True,
            data_status="ready",
        ),
        SimpleNamespace(
            symbol="BTC",
            target_pct=50.0,
            current_pct=58.0,
            drift_pct=8.0,
            current_value=580.0,
            current_price=100000.0,
            data_ready=True,
            data_status="ready",
        ),
    ]

    bot._rebalancer = SimpleNamespace(
        enabled=True,
        config={},
        threshold_strategy=SimpleNamespace(threshold_pct=10.0, min_rebalance_pct=1.0),
        _build_allocations=Mock(return_value=(allocations, ["DOGE"])),
    )

    overview = TradingBotOrchestrator._build_rebalance_overview(bot)

    assert overview["enabled"] is True
    assert overview["within_threshold"] is True
    assert overview["max_drift_pct"] == 8.0
    assert overview["skipped_assets"] == ["DOGE"]
    assert [item["symbol"] for item in overview["allocations"]] == ["THB", "BTC"]


def test_execute_rebalance_plan_persists_pending_orders(monkeypatch):
    monkeypatch.setattr("trading_bot.time.sleep", lambda *_args, **_kwargs: None)

    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot._rebalancer = SimpleNamespace(min_trade_value=15.0, config={"use_market_orders": False})
    bot.executor = Mock()
    bot.executor.execute_order.return_value = SimpleNamespace(
        success=True,
        order_id="rebalance-order-1",
        status=OrderStatus.PENDING,
        filled_amount=0.0,
        filled_price=0.0,
        remaining_amount=0.0001,
        message="accepted",
    )
    bot.executor.get_open_orders.return_value = [{"symbol": "THB_BTC", "side": "sell"}]
    bot.api_client = Mock()
    bot.api_client.get_balances.return_value = {
        "THB": {"available": 100.0},
        "BTC": {"available": 0.5},
    }
    bot.db = Mock()
    bot.config = {"portfolio": {"initial_balance": 500.0}}
    bot._get_rebalance_target_allocation = Mock(return_value={"THB": 20.0, "BTC": 80.0})
    bot._state_machine_enabled = True
    bot._state_manager = Mock()
    bot._send_alert = Mock()

    plan = SimpleNamespace(
        orders=[
            SimpleNamespace(
                symbol="BTC",
                side="sell",
                quantity=0.0001,
                estimated_value=20.0,
                current_price=200000.0,
            )
        ],
        skipped_assets=[],
        max_drift_pct=0.0,
        allocations=[],
    )

    TradingBotOrchestrator._execute_rebalance_plan(bot, plan)

    bot.executor.register_tracked_position.assert_called_once()
    tracked_order_id, tracked_payload = bot.executor.register_tracked_position.call_args.args
    assert tracked_order_id == "rebalance-order-1"
    assert tracked_payload["symbol"] == "THB_BTC"
    assert tracked_payload["side"] == OrderSide.SELL
    assert tracked_payload["amount"] == pytest.approx(0.0001)
    assert tracked_payload["remaining_amount"] == pytest.approx(0.0001)
    assert tracked_payload["total_entry_cost"] == pytest.approx(20.0)
    bot.db.insert_order.assert_not_called()
    bot._state_manager.sync_in_position_states.assert_called_once_with(
        bot.executor.get_open_orders.return_value
    )


def test_execute_rebalance_plan_logs_filled_orders_without_tracking(monkeypatch):
    monkeypatch.setattr("trading_bot.time.sleep", lambda *_args, **_kwargs: None)

    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot._rebalancer = SimpleNamespace(min_trade_value=15.0, config={"use_market_orders": False})
    bot.executor = Mock()
    bot.executor.execute_order.return_value = SimpleNamespace(
        success=True,
        order_id="rebalance-fill-1",
        status=OrderStatus.FILLED,
        filled_amount=0.0001,
        filled_price=200000.0,
        remaining_amount=0.0,
        message="filled",
    )
    bot.api_client = Mock()
    bot.api_client.get_balances.return_value = {
        "THB": {"available": 100.0},
        "BTC": {"available": 0.5},
    }
    bot.db = Mock()
    bot.config = {"portfolio": {"initial_balance": 500.0}}
    bot._get_rebalance_target_allocation = Mock(return_value={"THB": 20.0, "BTC": 80.0})
    bot._state_machine_enabled = True
    bot._state_manager = Mock()
    bot._send_alert = Mock()

    plan = SimpleNamespace(
        orders=[
            SimpleNamespace(
                symbol="BTC",
                side="sell",
                quantity=0.0001,
                estimated_value=20.0,
                current_price=200000.0,
            )
        ],
        skipped_assets=[],
        max_drift_pct=0.0,
        allocations=[],
    )

    TradingBotOrchestrator._execute_rebalance_plan(bot, plan)

    bot.executor.register_tracked_position.assert_not_called()
    bot.db.insert_order.assert_called_once()
    assert bot.db.insert_order.call_args.kwargs["pair"] == "THB_BTC"
    assert bot.db.insert_order.call_args.kwargs["side"] == "sell"
    assert bot.db.insert_order.call_args.kwargs["quantity"] == pytest.approx(0.0001)
    assert bot.db.insert_order.call_args.kwargs["price"] == pytest.approx(200000.0)
    bot._state_manager.sync_in_position_states.assert_not_called()


def test_build_multi_timeframe_status_reports_pair_coverage(tmp_path):
    db_path = tmp_path / "mtf-status.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE prices (pair TEXT, timeframe TEXT, timestamp TEXT)"
    )
    conn.executemany(
        "INSERT INTO prices(pair, timeframe, timestamp) VALUES (?, ?, ?)",
        [
            ("THB_BTC", "1m", "2026-04-05T10:00:00"),
            ("THB_BTC", "5m", "2026-04-05T10:05:00"),
        ],
    )
    conn.commit()
    conn.close()

    class DummyDb:
        def __init__(self, database_path):
            self.database_path = str(database_path)

        def get_connection(self):
            return sqlite3.connect(self.database_path)

    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.mtf_enabled = True
    bot.mtf_timeframes = ["1m", "5m"]
    bot._mtf_confirmation_required = True
    bot.timeframe = "15m"
    bot._last_mtf_status = {"THB_BTC": {"signal": "BUY"}}
    bot.trading_pairs = ["THB_BTC"]
    bot.db = DummyDb(db_path)

    status = TradingBotOrchestrator._build_multi_timeframe_status(bot)

    assert status["enabled"] is True
    assert status["require_htf_confirmation"] is True
    assert status["primary_timeframe"] == "15m"
    assert status["pairs"][0]["pair"] == "THB_BTC"
    assert status["pairs"][0]["ready"] is True
    assert [row["count"] for row in status["pairs"][0]["timeframes"]] == [1, 1]


def test_get_mtf_signal_for_symbol_keeps_subsignals_when_unaligned():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.mtf_enabled = True
    bot.mtf_timeframes = ["1m", "5m", "15m"]
    bot._last_mtf_status = {}
    bot.db = Mock()

    mtf_result = SimpleNamespace(
        timeframes={"1m": object(), "5m": object(), "15m": object()},
        signals={
            "1m": SimpleNamespace(
                signal_type=SignalType.BUY,
                confidence=0.81,
                trend_strength=0.62,
                indicators={"rsi": 61.0, "adx": 24.0, "macd_hist": 0.0014, "volume_ratio": 1.2},
                reason="short-term breakout",
            ),
            "5m": SimpleNamespace(
                signal_type=SignalType.HOLD,
                confidence=0.45,
                trend_strength=0.08,
                indicators={"rsi": 49.5, "adx": 11.0, "macd_hist": -0.0001, "volume_ratio": 0.9},
                reason="mixed momentum",
            ),
            "15m": SimpleNamespace(
                signal_type=SignalType.SELL,
                confidence=0.73,
                trend_strength=0.55,
                indicators={"rsi": 38.2, "adx": 32.0, "macd_hist": -0.0022, "volume_ratio": 1.4},
                reason="higher-timeframe pressure",
            ),
        },
        trend_alignment=0.33,
        consensus_strength=0.66,
        higher_timeframe_trend=SignalType.SELL,
        higher_timeframe_confidence=0.74,
    )

    bot.signal_generator = Mock()
    bot.signal_generator.generate_mtf_signals.return_value = mtf_result
    bot.signal_generator.get_mtf_signal.return_value = None

    result = TradingBotOrchestrator._get_mtf_signal_for_symbol(bot, "THB_BTC", {"balance": 1000.0})

    assert result is None
    snapshot = bot._last_mtf_status["THB_BTC"]
    assert snapshot["status"] == "waiting"
    assert snapshot["signals_detail"]["1m"]["type"] == "BUY"
    assert snapshot["signals_detail"]["1m"]["rsi"] == 61.0
    assert snapshot["signals_detail"]["1m"]["adx"] == 24.0
    assert snapshot["signals_detail"]["1m"]["macd_hist"] == 0.0014
    assert snapshot["signals_detail"]["1m"]["reason"] == "short-term breakout"
    assert snapshot["signals_detail"]["5m"]["type"] == "HOLD"
    assert snapshot["signals_detail"]["15m"]["type"] == "SELL"
    assert snapshot["higher_timeframe_trend"] == "SELL"
    assert snapshot["consensus_strength"] == 0.66
    assert bot.signal_generator.get_mtf_signal.call_args.kwargs["mtf_result"] is mtf_result


def test_get_mtf_signal_for_symbol_ready_merges_subsignal_reason_details():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.mtf_enabled = True
    bot.mtf_timeframes = ["1m", "5m", "15m"]
    bot._last_mtf_status = {}
    bot.db = Mock()

    mtf_result = SimpleNamespace(
        timeframes={"1m": object(), "5m": object(), "15m": object()},
        signals={
            "1m": SimpleNamespace(
                signal_type=SignalType.BUY,
                confidence=0.81,
                trend_strength=0.62,
                indicators={"rsi": 61.0, "adx": 24.0, "macd_hist": 0.0014, "volume_ratio": 1.2},
                reason="short-term breakout",
            ),
            "5m": SimpleNamespace(
                signal_type=SignalType.HOLD,
                confidence=0.45,
                trend_strength=0.08,
                indicators={"rsi": 49.5, "adx": 11.0, "macd_hist": -0.0001, "volume_ratio": 0.9},
                reason="mixed momentum",
            ),
            "15m": SimpleNamespace(
                signal_type=SignalType.SELL,
                confidence=0.73,
                trend_strength=0.55,
                indicators={"rsi": 38.2, "adx": 32.0, "macd_hist": -0.0022, "volume_ratio": 1.4},
                reason="higher-timeframe pressure",
            ),
        },
        trend_alignment=0.52,
        consensus_strength=0.66,
        higher_timeframe_trend=SignalType.BUY,
        higher_timeframe_confidence=0.74,
    )

    ready_signal = SimpleNamespace(
        signal_type=SignalType.BUY,
        confidence=0.79,
        metadata={
            "timeframes_used": ["1m", "5m", "15m"],
            "higher_timeframe_trend": "BUY",
            "signals_detail": {
                "1m": {"type": "BUY", "confidence": 0.81, "trend_strength": 0.62, "rsi": 61.0, "adx": 24.0, "macd_hist": 0.0014, "volume_ratio": 1.2},
                "5m": {"type": "HOLD", "confidence": 0.45, "trend_strength": 0.08, "rsi": 49.5, "adx": 11.0, "macd_hist": -0.0001, "volume_ratio": 0.9},
                "15m": {"type": "SELL", "confidence": 0.73, "trend_strength": 0.55, "rsi": 38.2, "adx": 32.0, "macd_hist": -0.0022, "volume_ratio": 1.4},
            },
        },
    )

    bot.signal_generator = Mock()
    bot.signal_generator.generate_mtf_signals.return_value = mtf_result
    bot.signal_generator.get_mtf_signal.return_value = ready_signal

    result = TradingBotOrchestrator._get_mtf_signal_for_symbol(bot, "THB_BTC", {"balance": 1000.0})

    assert result is ready_signal
    snapshot = bot._last_mtf_status["THB_BTC"]
    assert snapshot["status"] == "ready"
    assert snapshot["signal_type"] == "BUY"
    assert snapshot["signals_detail"]["1m"]["reason"] == "short-term breakout"
    assert snapshot["signals_detail"]["5m"]["reason"] == "mixed momentum"
    assert snapshot["signals_detail"]["15m"]["reason"] == "higher-timeframe pressure"


def test_process_pair_iteration_refreshes_mtf_even_when_lifecycle_gated():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot._state_machine_enabled = True
    bot._state_manager = Mock()
    bot._state_manager.get_state.return_value = SimpleNamespace(state=TradeLifecycleState.IN_POSITION)
    bot._last_state_gate_logged = {}
    bot._get_portfolio_state = Mock(return_value={"balance": 1000.0})
    bot._get_mtf_signal_for_symbol = Mock(return_value=None)
    bot._get_market_data_for_symbol = Mock()

    TradingBotOrchestrator._process_pair_iteration(bot, "THB_BTC")

    bot._get_portfolio_state.assert_called_once_with()
    bot._get_mtf_signal_for_symbol.assert_called_once_with("THB_BTC", {"balance": 1000.0})
    bot._get_market_data_for_symbol.assert_not_called()
    assert bot._last_state_gate_logged["THB_BTC"] == TradeLifecycleState.IN_POSITION.value
