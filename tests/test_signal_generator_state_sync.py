"""
Tests for H5/SRG-2 remediation: SignalGenerator state synchronisation.

Before a signal is evaluated, _process_pair_iteration() must call
signal_generator.sync_state() so that check_risk() sees the real
open-position count and today's trade count rather than permanent zeros.
"""

import pytest
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch, call
import pandas as pd
import numpy as np

from signal_generator import SignalGenerator, AggregatedSignal
from strategy_base import SignalType, MarketCondition, TradingSignal
from trade_executor import OrderSide, OrderResult, OrderStatus, ExecutionPlan


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ohlcv(rows: int = 60) -> pd.DataFrame:
    """Minimal OHLCV DataFrame for strategy testing."""
    np.random.seed(42)
    close = 1_500_000.0 + np.cumsum(np.random.randn(rows) * 5_000)
    return pd.DataFrame({
        "open":   close * 0.999,
        "high":   close * 1.001,
        "low":    close * 0.998,
        "close":  close,
        "volume": np.abs(np.random.randn(rows)) * 100 + 50,
        "timestamp": pd.date_range("2026-01-01", periods=rows, freq="1h"),
    })


def _make_aggregated_signal(symbol: str = "THB_BTC", sig_type: SignalType = SignalType.BUY) -> AggregatedSignal:
    return AggregatedSignal(
        symbol=symbol,
        signal_type=sig_type,
        combined_confidence=0.75,
        avg_price=1_500_000.0,
        avg_stop_loss=1_470_000.0,
        avg_take_profit=1_560_000.0,
        avg_risk_reward=2.0,
        strategy_votes={"trend_following": 1, "breakout": 1},
        risk_score=30.0,
        market_condition=MarketCondition.TRENDING_UP,
    )


def _attach_trigger_metadata(signal: AggregatedSignal, trigger_timestamp: str) -> AggregatedSignal:
    raw_signal = Mock()
    raw_signal.metadata = {"macd_cross_timestamp": trigger_timestamp}
    signal.signals = [raw_signal]
    return signal


def _make_sg(config: dict | None = None) -> SignalGenerator:
    cfg = {
        "min_confidence": 0.5,
        "max_risk_score": 70,
        "min_strategies_agree": 2,
        "max_open_positions": 3,
        "max_daily_trades": 10,
    }
    if config:
        cfg.update(config)
    return SignalGenerator(cfg)


def _build_bot(config_overrides=None, **kwargs):
    """Build a minimal TradingBotOrchestrator with mocks."""
    from trading_bot import TradingBotOrchestrator, BotMode, SignalSource

    config = {
        "mode": "full_auto",
        "trading_pair": "THB_BTC",
        "interval_seconds": 1,
        "timeframe": "1h",
        "signal_source": "strategy",
        "strategies": {"enabled": ["trend_following"]},
        "trading": {"max_open_positions": 3},
        "risk": {"max_risk_per_trade_pct": 1.0, "max_daily_loss_pct": 5.0},
        "data": {"pairs": ["THB_BTC"]},
        "backtesting": {"require_validation_before_live": False},
        "state_management": {"enabled": False},
        # Disable WebSocket so __init__ never sets bitkub_websocket._global_ws;
        # leaving _global_ws set would cause test_get_websocket_stats_no_connection
        # (in test_strategies.py) to fail when the full suite is run.
        "websocket": {"enabled": False},
    }
    if config_overrides:
        config.update(config_overrides)

    mock_db = kwargs.pop("db", None) or Mock()
    mock_db.get_positions.return_value = []
    mock_db.load_all_positions.return_value = []
    mock_db.list_trade_states.return_value = []

    with patch("trading_bot.get_database", return_value=mock_db):
        return TradingBotOrchestrator(config=config, **kwargs)


# ══════════════════════════════════════════════════════════════════════════════
# Part 1 – Unit tests for SignalGenerator.sync_state()
# ══════════════════════════════════════════════════════════════════════════════

class TestSyncStateMethod:
    """sync_state() must correctly update internal counters."""

    def test_sync_sets_open_position_count(self):
        """`_open_positions` length reflects the synced count."""
        sg = _make_sg()
        assert len(sg._open_positions) == 0  # starts empty

        sg.sync_state(open_positions_count=3, daily_trades_count=0)

        assert len(sg._open_positions) == 3

    def test_sync_sets_daily_trade_count(self):
        """`_daily_trade_count` reflects the synced count."""
        sg = _make_sg()
        assert sg._daily_trade_count == 0

        sg.sync_state(open_positions_count=0, daily_trades_count=7)

        assert sg._daily_trade_count == 7

    def test_sync_overwrites_previous_state(self):
        """Subsequent calls overwrite earlier values (idempotent)."""
        sg = _make_sg()
        sg.sync_state(open_positions_count=5, daily_trades_count=8)
        sg.sync_state(open_positions_count=2, daily_trades_count=3)

        assert len(sg._open_positions) == 2
        assert sg._daily_trade_count == 3

    def test_sync_zero_clears_state(self):
        """Syncing zeros correctly clears any prior stubs."""
        sg = _make_sg()
        sg.sync_state(open_positions_count=3, daily_trades_count=5)
        sg.sync_state(open_positions_count=0, daily_trades_count=0)

        assert len(sg._open_positions) == 0
        assert sg._daily_trade_count == 0


# ══════════════════════════════════════════════════════════════════════════════
# Part 2 – check_risk() uses synced state
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckRiskUsesRealState:
    """After sync_state(), check_risk() must observe the injected values."""

    def test_check_risk_blocked_at_max_positions_after_sync(self):
        """If synced open positions == max, check_risk must fail with position reason."""
        sg = _make_sg({"max_open_positions": 3})
        sg.sync_state(open_positions_count=3, daily_trades_count=0)

        signal = _make_aggregated_signal()
        portfolio = {"balance": 100_000.0, "positions": []}

        result = sg.check_risk(signal, portfolio)

        assert not result.passed
        assert any("position" in r.lower() for r in result.reasons), result.reasons

    def test_check_risk_passes_when_positions_below_max(self):
        """With 2 of 3 positions open, a new BUY should pass the position gate."""
        sg = _make_sg({"max_open_positions": 3})
        sg.sync_state(open_positions_count=2, daily_trades_count=0)

        signal = _make_aggregated_signal()
        portfolio = {"balance": 100_000.0, "positions": []}

        result = sg.check_risk(signal, portfolio)

        # Position gate should NOT be the reason for failure (if any)
        position_reasons = [r for r in result.reasons if "position" in r.lower()]
        assert len(position_reasons) == 0, f"Unexpected position block: {position_reasons}"

    def test_check_risk_blocked_at_daily_limit_after_sync(self):
        """If synced daily trades == max, check_risk must fail with daily-limit reason."""
        sg = _make_sg({"max_daily_trades": 10})
        sg.sync_state(open_positions_count=0, daily_trades_count=10)

        signal = _make_aggregated_signal()
        portfolio = {"balance": 100_000.0, "positions": []}

        result = sg.check_risk(signal, portfolio)

        assert not result.passed
        assert any("daily" in r.lower() or "limit" in r.lower() for r in result.reasons), result.reasons

    def test_check_risk_without_sync_always_passes_position_gate(self):
        """Proof that WITHOUT sync, position gate is never triggered (the bug)."""
        sg = _make_sg({"max_open_positions": 3})
        # Intentionally do NOT call sync_state — bug scenario

        signal = _make_aggregated_signal()
        portfolio = {"balance": 100_000.0, "positions": [
            {"id": "p1"}, {"id": "p2"}, {"id": "p3"},
        ]}

        result = sg.check_risk(signal, portfolio)

        # Without sync, _open_positions is empty → position gate always passes
        # (This test documents the pre-fix behaviour; it should FAIL after the fix
        # only IF the portfolio dict was made the source of truth — but we verify
        # the sync call is what fixes it, NOT portfolio-dict reading.)
        position_block = [r for r in result.reasons if "position" in r.lower()]
        assert len(position_block) == 0, (
            "Expected 0 position reasons (no sync → always zero) but got: "
            f"{position_block}"
        )


class TestExecutionPlanSignalSideMapping:
    """Execution plan generation must map SignalType enums to OrderSide safely."""

    def test_create_execution_plan_maps_buy_signal_type_to_buy_side(self):
        api_client = Mock()
        api_client.get_balances.return_value = {"THB": {"available": 100_000.0}}
        api_client.is_circuit_open.return_value = False

        bot = _build_bot(
            api_client=api_client,
            signal_generator=_make_sg(),
            risk_manager=Mock(),
            executor=Mock(),
        )
        bot.db.has_ever_held.return_value = True
        bot._get_latest_atr = Mock(return_value=1_000.0)

        signal = _make_aggregated_signal(sig_type=SignalType.BUY)
        plan = bot._create_execution_plan_for_symbol(signal, signal.symbol)

        assert plan is not None
        assert plan.side is OrderSide.BUY

    def test_create_execution_plan_maps_sell_signal_type_to_sell_side(self):
        api_client = Mock()
        api_client.get_balances.return_value = {
            "BTC": {"available": 0.01},
            "THB": {"available": 100_000.0},
        }
        api_client.is_circuit_open.return_value = False

        bot = _build_bot(
            api_client=api_client,
            signal_generator=_make_sg(),
            risk_manager=Mock(),
            executor=Mock(),
        )
        bot._get_latest_atr = Mock(return_value=1_000.0)

        signal = _make_aggregated_signal(sig_type=SignalType.SELL)
        signal.avg_stop_loss = 1_530_000.0
        signal.avg_take_profit = 1_440_000.0
        plan = bot._create_execution_plan_for_symbol(signal, signal.symbol)

        assert plan is not None
        assert plan.side is OrderSide.SELL


# ══════════════════════════════════════════════════════════════════════════════
# Part 3 – Integration: bot calls sync_state before generating signals
# ══════════════════════════════════════════════════════════════════════════════

class TestBotSyncsStateBeforeSignals:
    """_process_pair_iteration() must call sync_state() with live counts."""

    def _make_portfolio(self, positions=None):
        return {
            "balance": 100_000.0,
            "positions": positions or [],
            "timestamp": datetime.now(),
        }

    def test_sync_called_before_generate_signals(self):
        """sync_state must be called with correct counts before signal generation."""
        mock_api = Mock()
        mock_api.get_ticker.return_value = {"last": 1_500_000.0}
        mock_api.get_balances.return_value = {"THB": {"available": 100_000.0}}
        mock_api.is_circuit_open.return_value = False

        mock_sg = Mock(spec=SignalGenerator)
        mock_sg.generate_signals.return_value = []

        mock_rm = Mock()
        mock_rm.trade_count_today = 4

        mock_executor = Mock()
        mock_executor.get_open_orders.return_value = [{"id": "o1"}, {"id": "o2"}]

        bot = _build_bot(
            api_client=mock_api,
            signal_generator=mock_sg,
            risk_manager=mock_rm,
            executor=mock_executor,
        )

        # Patch _get_market_data_for_symbol to return valid data
        data = _make_ohlcv()
        with patch.object(bot, "_get_market_data_for_symbol", return_value=data), \
             patch.object(bot, "_get_mtf_signal_for_symbol", return_value=None), \
             patch.object(bot, "_maybe_trigger_sideways_rebalance"):
            bot._process_pair_iteration("THB_BTC")

        # sync_state must be called exactly once
        mock_sg.sync_state.assert_called_once()
        call_args = mock_sg.sync_state.call_args

        # It must be called BEFORE generate_signals
        # Verify via call_args_list ordering
        all_calls = [c[0] for c in mock_sg.method_calls]
        sync_idx = next(i for i, c in enumerate(mock_sg.method_calls) if c[0] == "sync_state")
        gen_idx  = next(i for i, c in enumerate(mock_sg.method_calls) if c[0] == "generate_signals")
        assert sync_idx < gen_idx, (
            f"sync_state (idx {sync_idx}) must come before generate_signals (idx {gen_idx})"
        )

    def test_sync_passes_correct_position_count_no_state_machine(self):
        """With state machine disabled, open_count = len(portfolio['positions'])."""
        mock_api = Mock()
        mock_api.get_balances.return_value = {"THB": {"available": 100_000.0}}
        mock_api.is_circuit_open.return_value = False

        mock_sg = Mock(spec=SignalGenerator)
        mock_sg.generate_signals.return_value = []

        mock_rm = Mock()
        mock_rm.trade_count_today = 0

        # Open orders acts as position list for portfolio
        three_positions = [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]
        mock_executor = Mock()
        mock_executor.get_open_orders.return_value = three_positions

        bot = _build_bot(
            config_overrides={"state_management": {"enabled": False}},
            api_client=mock_api,
            signal_generator=mock_sg,
            risk_manager=mock_rm,
            executor=mock_executor,
        )

        data = _make_ohlcv()
        with patch.object(bot, "_get_market_data_for_symbol", return_value=data), \
             patch.object(bot, "_get_mtf_signal_for_symbol", return_value=None), \
             patch.object(bot, "_maybe_trigger_sideways_rebalance"):
            bot._process_pair_iteration("THB_BTC")

        mock_sg.sync_state.assert_called_once()
        kwargs = mock_sg.sync_state.call_args.kwargs
        # open_positions_count should be 3 (from portfolio["positions"])
        assert kwargs.get("open_positions_count") == 3, (
            f"Expected open_positions_count=3, got {kwargs}"
        )

    def test_sync_passes_correct_daily_count_from_risk_manager(self):
        """daily_trades_count must come from risk_manager.trade_count_today."""
        mock_api = Mock()
        mock_api.get_balances.return_value = {"THB": {"available": 100_000.0}}
        mock_api.is_circuit_open.return_value = False

        mock_sg = Mock(spec=SignalGenerator)
        mock_sg.generate_signals.return_value = []

        mock_rm = Mock()
        mock_rm.trade_count_today = 7  # authoritative source

        mock_executor = Mock()
        mock_executor.get_open_orders.return_value = []

        bot = _build_bot(
            api_client=mock_api,
            signal_generator=mock_sg,
            risk_manager=mock_rm,
            executor=mock_executor,
        )

        data = _make_ohlcv()
        with patch.object(bot, "_get_market_data_for_symbol", return_value=data), \
             patch.object(bot, "_get_mtf_signal_for_symbol", return_value=None), \
             patch.object(bot, "_maybe_trigger_sideways_rebalance"):
            bot._process_pair_iteration("THB_BTC")

        kwargs = mock_sg.sync_state.call_args.kwargs
        assert kwargs.get("daily_trades_count") == 7, (
            f"Expected daily_trades_count=7, got {kwargs}"
        )

    def test_with_3_open_positions_synced_signal_generator_blocks_new_buy(self):
        """End-to-end: 3 open positions → sync → check_risk blocks a 4th BUY."""
        # Use a REAL SignalGenerator (not mock) — tests the whole path
        real_sg = _make_sg({"max_open_positions": 3, "min_strategies_agree": 1})

        # Manually sync as the bot would
        real_sg.sync_state(open_positions_count=3, daily_trades_count=0)

        signal = _make_aggregated_signal()
        portfolio = {"balance": 100_000.0, "positions": []}

        result = real_sg.check_risk(signal, portfolio)

        assert not result.passed
        assert any("position" in r.lower() for r in result.reasons)

    def test_state_machine_enabled_uses_list_active_states_for_count(self):
        """With state machine ON, open_count comes from list_active_states()."""
        mock_api = Mock()
        mock_api.get_balances.return_value = {"THB": {"available": 100_000.0}}
        mock_api.is_circuit_open.return_value = False

        mock_sg = Mock(spec=SignalGenerator)
        mock_sg.generate_signals.return_value = []

        mock_rm = Mock()
        mock_rm.trade_count_today = 0

        mock_executor = Mock()
        mock_executor.get_open_orders.return_value = []

        bot = _build_bot(
            config_overrides={"state_management": {"enabled": True}},
            api_client=mock_api,
            signal_generator=mock_sg,
            risk_manager=mock_rm,
            executor=mock_executor,
        )

        # Simulate 2 active states from state manager
        active_snapshots = [Mock(), Mock()]
        bot._state_machine_enabled = True
        bot._state_manager.list_active_states = Mock(return_value=active_snapshots)

        # State machine will gate the symbol unless it's IDLE — bypass by setting IDLE
        from state_management import TradeLifecycleState
        idle_snapshot = Mock()
        idle_snapshot.state = TradeLifecycleState.IDLE
        bot._state_manager.get_state = Mock(return_value=idle_snapshot)

        data = _make_ohlcv()
        with patch.object(bot, "_get_market_data_for_symbol", return_value=data), \
             patch.object(bot, "_get_mtf_signal_for_symbol", return_value=None), \
             patch.object(bot, "_maybe_trigger_sideways_rebalance"):
            bot._process_pair_iteration("THB_BTC")

        kwargs = mock_sg.sync_state.call_args.kwargs
        assert kwargs.get("open_positions_count") == 2, (
            f"Expected open_positions_count=2 from list_active_states, got {kwargs}"
        )

    def test_lifecycle_gated_pair_still_refreshes_signal_flow_but_skips_execution(self):
        """In-position pairs should still refresh diagnostics for Rich CLI while skipping trade execution."""
        mock_api = Mock()
        mock_api.get_balances.return_value = {"THB": {"available": 100_000.0}}
        mock_api.is_circuit_open.return_value = False

        mock_sg = Mock(spec=SignalGenerator)
        mock_sg.generate_sniper_signal.return_value = [_make_aggregated_signal()]
        mock_sg.check_risk.return_value = Mock(passed=True, reasons=[])

        mock_rm = Mock()
        mock_rm.trade_count_today = 0

        mock_executor = Mock()
        mock_executor.get_open_orders.return_value = []

        bot = _build_bot(
            config_overrides={"state_management": {"enabled": True}},
            api_client=mock_api,
            signal_generator=mock_sg,
            risk_manager=mock_rm,
            executor=mock_executor,
        )

        from state_management import TradeLifecycleState
        gated_snapshot = Mock()
        gated_snapshot.state = TradeLifecycleState.IN_POSITION
        bot._state_manager.get_state = Mock(return_value=gated_snapshot)
        bot._create_execution_plan_for_symbol = Mock()

        data = _make_ohlcv(rows=250)
        with patch.object(bot, "_get_market_data_for_symbol", return_value=data), \
             patch.object(bot, "_get_mtf_signal_for_symbol", return_value=None), \
             patch.object(bot, "_maybe_trigger_sideways_rebalance"):
            bot._process_pair_iteration("THB_BTC")

        mock_sg.sync_state.assert_called_once()
        mock_sg.generate_sniper_signal.assert_called_once()
        bot._create_execution_plan_for_symbol.assert_not_called()

    def test_idle_sell_requires_confirmation_before_plan_creation(self):
        """When idle SELL is enabled, it must still pass confirmation gate first."""
        mock_api = Mock()
        mock_api.get_balances.return_value = {"THB": {"available": 100_000.0}}
        mock_api.is_circuit_open.return_value = False

        mock_sg = Mock(spec=SignalGenerator)
        sell_signal = _make_aggregated_signal(sig_type=SignalType.SELL)
        mock_sg.generate_sniper_signal.return_value = [sell_signal]
        mock_sg.check_risk.return_value = Mock(passed=True, reasons=[])

        mock_rm = Mock()
        mock_rm.trade_count_today = 0

        mock_executor = Mock()
        mock_executor.get_open_orders.return_value = []

        bot = _build_bot(
            config_overrides={
                "mode": "dry_run",
                "state_management": {
                    "enabled": True,
                    "allow_sell_entries_from_idle": True,
                },
            },
            api_client=mock_api,
            signal_generator=mock_sg,
            risk_manager=mock_rm,
            executor=mock_executor,
        )

        from state_management import TradeLifecycleState
        idle_snapshot = Mock()
        idle_snapshot.state = TradeLifecycleState.IDLE
        bot._state_manager.get_state = Mock(return_value=idle_snapshot)
        bot._state_manager.confirm_idle_sell_signal = Mock(
            return_value=(False, "awaiting confirmation 1/2")
        )
        bot._create_execution_plan_for_symbol = Mock(return_value=None)

        data = _make_ohlcv(rows=250)
        with patch.object(bot, "_get_market_data_for_symbol", return_value=data), \
             patch.object(bot, "_get_mtf_signal_for_symbol", return_value=None), \
             patch.object(bot, "_maybe_trigger_sideways_rebalance"):
            bot._process_pair_iteration("THB_BTC")

        bot._state_manager.confirm_idle_sell_signal.assert_called_once()
        bot._create_execution_plan_for_symbol.assert_not_called()

    def test_idle_sell_after_confirmation_proceeds_to_plan_creation(self):
        """Confirmed idle SELL should proceed to execution plan construction."""
        mock_api = Mock()
        mock_api.get_balances.return_value = {"THB": {"available": 100_000.0}}
        mock_api.is_circuit_open.return_value = False

        mock_sg = Mock(spec=SignalGenerator)
        sell_signal = _make_aggregated_signal(sig_type=SignalType.SELL)
        mock_sg.generate_sniper_signal.return_value = [sell_signal]
        mock_sg.check_risk.return_value = Mock(passed=True, reasons=[])

        mock_rm = Mock()
        mock_rm.trade_count_today = 0

        mock_executor = Mock()
        mock_executor.get_open_orders.return_value = []

        bot = _build_bot(
            config_overrides={
                "mode": "dry_run",
                "state_management": {
                    "enabled": True,
                    "allow_sell_entries_from_idle": True,
                },
            },
            api_client=mock_api,
            signal_generator=mock_sg,
            risk_manager=mock_rm,
            executor=mock_executor,
        )

        from state_management import TradeLifecycleState
        idle_snapshot = Mock()
        idle_snapshot.state = TradeLifecycleState.IDLE
        bot._state_manager.get_state = Mock(return_value=idle_snapshot)
        bot._state_manager.confirm_idle_sell_signal = Mock(
            return_value=(True, "confirmed 2/2")
        )

        from trade_executor import ExecutionPlan, OrderSide
        bot._create_execution_plan_for_symbol = Mock(return_value=ExecutionPlan(
            symbol="THB_BTC",
            side=OrderSide.SELL,
            amount=0.001,
            entry_price=1_500_000.0,
            stop_loss=1_530_000.0,
            take_profit=1_440_000.0,
            risk_reward_ratio=2.0,
            confidence=0.7,
            strategy_votes={"sniper_dual_ema_macd": 1},
            signal_timestamp=datetime.now(),
            signal_id="sell_confirmed",
            max_price_drift_pct=1.5,
            close_position=False,
        ))
        bot._process_dry_run = Mock()

        data = _make_ohlcv(rows=250)
        with patch.object(bot, "_get_market_data_for_symbol", return_value=data), \
             patch.object(bot, "_get_mtf_signal_for_symbol", return_value=None), \
             patch.object(bot, "_maybe_trigger_sideways_rebalance"):
            bot._process_pair_iteration("THB_BTC")

        bot._state_manager.confirm_idle_sell_signal.assert_called_once()
        bot._create_execution_plan_for_symbol.assert_called_once()

    def test_duplicate_trigger_timestamp_is_not_reused_for_second_entry(self):
        """A signal with the same MACD trigger timestamp should not be re-consumed."""
        mock_api = Mock()
        mock_api.get_balances.return_value = {"THB": {"available": 100_000.0}}
        mock_api.is_circuit_open.return_value = False

        mock_sg = Mock(spec=SignalGenerator)
        buy_signal = _attach_trigger_metadata(
            _make_aggregated_signal(sig_type=SignalType.BUY),
            "2026-04-14 10:15:00",
        )
        mock_sg.generate_sniper_signal.return_value = [buy_signal]
        mock_sg.check_risk.return_value = Mock(passed=True, reasons=[])

        mock_rm = Mock()
        mock_rm.trade_count_today = 0

        mock_executor = Mock()
        mock_executor.get_open_orders.return_value = []

        bot = _build_bot(
            config_overrides={"mode": "dry_run", "state_management": {"enabled": False}},
            api_client=mock_api,
            signal_generator=mock_sg,
            risk_manager=mock_rm,
            executor=mock_executor,
        )
        bot._last_consumed_signal_triggers = {"THB_BTC:buy": "2026-04-14 10:15:00"}
        bot._create_execution_plan_for_symbol = Mock()
        bot._process_dry_run = Mock()

        data = _make_ohlcv(rows=250)
        with patch.object(bot, "_get_market_data_for_symbol", return_value=data), \
             patch.object(bot, "_get_mtf_signal_for_symbol", return_value=None), \
             patch.object(bot, "_maybe_trigger_sideways_rebalance"):
            bot._process_pair_iteration("THB_BTC")

        mock_sg.check_risk.assert_not_called()
        bot._create_execution_plan_for_symbol.assert_not_called()


class TestOpposingSignalsDedupe:
    def test_reduce_keeps_higher_confidence_direction(self):
        from trading.signal_runtime import _reduce_opposing_signals_single_direction

        buy = _make_aggregated_signal(sig_type=SignalType.BUY)
        buy.combined_confidence = 0.9
        sell = _make_aggregated_signal(sig_type=SignalType.SELL)
        sell.combined_confidence = 0.4
        out = _reduce_opposing_signals_single_direction([buy, sell], "THB_BTC")
        assert len(out) == 1
        assert out[0].signal_type == SignalType.BUY

    def test_reduce_keeps_sell_when_stronger(self):
        from trading.signal_runtime import _reduce_opposing_signals_single_direction

        buy = _make_aggregated_signal(sig_type=SignalType.BUY)
        buy.combined_confidence = 0.3
        sell = _make_aggregated_signal(sig_type=SignalType.SELL)
        sell.combined_confidence = 0.85
        out = _reduce_opposing_signals_single_direction([buy, sell], "THB_BTC")
        assert len(out) == 1
        assert out[0].signal_type == SignalType.SELL


class TestRefreshRiskConfigForMode:
    def test_profile_overrides_global_thresholds(self):
        sg = SignalGenerator(
            {
                "min_confidence": 0.5,
                "min_strategies_agree": 2,
                "max_open_positions": 3,
                "max_daily_trades": 10,
                "mode_indicator_profiles": {
                    "scalping": {"min_confidence": 0.41, "min_strategies_agree": 1},
                },
                "strategies": {},
                "risk": {"max_open_positions": 5, "max_daily_trades": 8},
            }
        )
        sg.refresh_risk_config_for_mode("scalping")
        assert sg.risk_config["min_confidence"] == 0.41
        assert sg.risk_config["min_strategies_agree"] == 1
        assert sg.risk_config["max_positions"] == 5
        assert sg.risk_config["max_daily_trades"] == 8

    def test_profile_sets_independent_execution(self):
        sg = SignalGenerator(
            {
                "strategies": {"independent_strategy_execution": False},
                "mode_indicator_profiles": {
                    "scalping": {"independent_strategy_execution": True},
                },
            }
        )
        sg.refresh_risk_config_for_mode("scalping")
        assert sg.risk_config["independent_strategy_execution"] is True


class TestIndependentStrategyExecution:
    def test_risk_check_skips_strategy_agreement_when_independent(self):
        sg = SignalGenerator(
            {
                "strategies": {
                    "min_strategies_agree": 9,
                    "independent_strategy_execution": True,
                },
            }
        )
        sg.risk_config["min_confidence"] = 0.1
        sg.risk_config["max_risk_score"] = 100
        sg.sync_state(0, 0)
        agg = AggregatedSignal(
            symbol="BTCUSDT",
            signal_type=SignalType.BUY,
            combined_confidence=0.8,
            signals=[],
            avg_price=100.0,
            avg_stop_loss=98.0,
            avg_take_profit=104.0,
            avg_risk_reward=2.0,
            strategy_votes={"simple_scalp_plus": 1},
            risk_score=35.0,
            market_condition=MarketCondition.TRENDING_UP,
        )
        rc = sg.check_risk(agg, {"balance": 10000})
        assert rc.passed is True

    def test_aggregate_emits_one_candidate_per_raw_when_independent(self):
        sg = SignalGenerator(
            {
                "strategies": {"independent_strategy_execution": True},
            }
        )
        df = _make_ohlcv(80)
        a = TradingSignal(
            strategy_name="simple_scalp_plus",
            symbol="BTCUSDT",
            signal_type=SignalType.BUY,
            confidence=0.7,
            price=100.0,
            stop_loss=98.0,
            take_profit=105.0,
            risk_reward_ratio=2.0,
        )
        b = TradingSignal(
            strategy_name="sniper",
            symbol="BTCUSDT",
            signal_type=SignalType.BUY,
            confidence=0.65,
            price=100.0,
            stop_loss=97.0,
            take_profit=106.0,
            risk_reward_ratio=2.0,
        )
        out = sg._aggregate_signals(
            [a, b],
            MarketCondition.TRENDING_UP,
            "BTCUSDT",
            df,
        )
        assert len(out) == 2
        vote_keys = {tuple(sorted(x.strategy_votes.keys())) for x in out}
        assert vote_keys == {("simple_scalp_plus",), ("sniper",)}


class TestApplyRuntimeStrategyRefresh:
    def test_updates_mode_generator_and_risk_manager(self):
        mock_api = Mock()
        mock_api.get_balances.return_value = {"THB": {"available": 100_000.0}}
        mock_api.is_circuit_open.return_value = False
        initial_sg = MagicMock(spec=SignalGenerator)
        mock_executor = Mock()
        mock_executor.get_open_orders.return_value = []
        bot = _build_bot(
            api_client=mock_api,
            signal_generator=initial_sg,
            risk_manager=Mock(),
            executor=mock_executor,
        )

        new_sg = MagicMock(spec=SignalGenerator)
        new_rm = Mock()
        cfg = dict(bot.config)
        cfg["active_strategy_mode"] = "scalping"
        cfg["strategies"] = {"enabled": ["sniper", "simple_scalp_plus"]}
        cfg.setdefault("multi_timeframe", {})["enabled"] = False

        bot.apply_runtime_strategy_refresh(cfg, new_sg, risk_manager=new_rm)

        assert bot._active_strategy_mode == "scalping"
        assert bot.signal_generator is new_sg
        assert bot.risk_manager is new_rm
        assert bot.enabled_strategies == ["sniper", "simple_scalp_plus"]
        new_sg.set_database.assert_called_once()
