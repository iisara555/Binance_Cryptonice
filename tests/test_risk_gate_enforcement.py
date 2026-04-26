"""
Tests for C3/SRG-1 remediation: RiskManager.can_open_position() enforcement.

Verifies that _process_full_auto() calls risk_manager.can_open_position()
before new BUY entries and correctly blocks trades when limits are breached.
"""

import pytest
import threading
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch

from risk_management import RiskManager, RiskConfig, RiskCheckResult
from signal_generator import SignalGenerator, AggregatedSignal
from trade_executor import (
    TradeExecutor, ExecutionPlan, OrderSide, OrderResult, OrderStatus,
)
from trading_bot import TradingBotOrchestrator, BotMode, SignalSource
from strategy_base import SignalType, MarketCondition
from state_management import TradeLifecycleState, TradeStateSnapshot


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_api_client():
    client = Mock()
    client.get_ticker.return_value = {"last": 45_000.0}
    client.get_balances.return_value = {
        "USDT": {"available": 100_000.0},
        "BTC": {"available": 0.0},
    }
    client.is_circuit_open.return_value = False
    return client


@pytest.fixture
def mock_risk_manager():
    mgr = Mock(spec=RiskManager)
    mgr.calc_sl_tp_from_atr.return_value = (44_100.0, 46_800.0)
    mgr.validate_risk_reward.return_value = RiskCheckResult(True, "R:R OK")
    mgr.calculate_position_size.return_value = RiskCheckResult(True, "OK", 1000.0)
    mgr.check_daily_loss_limit.return_value = RiskCheckResult(True, "OK")
    mgr.check_cooldown.return_value = False
    mgr._get_current_drawdown_pct.return_value = 0.0
    # Default: allow new positions
    mgr.can_open_position.return_value = RiskCheckResult(True, "All checks passed")
    return mgr


@pytest.fixture
def mock_executor():
    ex = Mock(spec=TradeExecutor)
    ex.execute_entry.return_value = OrderResult(
        success=True,
        status=OrderStatus.FILLED,
        order_id="order_1",
        filled_amount=0.001,
        filled_price=45_000.0,
        message="OK",
    )
    ex.get_open_orders.return_value = []
    return ex


@pytest.fixture
def mock_db():
    db = Mock()
    db.get_positions.return_value = []
    db.insert_signal.return_value = None
    db.insert_order.return_value = None
    db.load_all_positions.return_value = []
    db.list_trade_states.return_value = []
    db.get_trade_state.return_value = None
    return db


@pytest.fixture
def mock_signal_generator():
    gen = Mock(spec=SignalGenerator)
    gen.generate_signals.return_value = []
    return gen


def _make_buy_decision(symbol: str = "BTCUSDT"):
    """Helper to create a valid BUY TradeDecision."""
    from trading_bot import TradeDecision

    plan = ExecutionPlan(
        symbol=symbol,
        side=OrderSide.BUY,
        amount=1000.0,
        entry_price=45_000.0,
        stop_loss=44_100.0,
        take_profit=46_800.0,
        risk_reward_ratio=2.0,
        confidence=0.75,
        strategy_votes={"trend_following": 1},
        signal_timestamp=datetime.now(),
        signal_id="test_signal_1",
        max_price_drift_pct=1.5,
    )

    signal = AggregatedSignal(
        symbol=symbol,
        signal_type=SignalType.BUY,
        combined_confidence=0.75,
        avg_price=45_000.0,
        avg_stop_loss=44_100.0,
        avg_take_profit=46_800.0,
        avg_risk_reward=2.0,
        strategy_votes={"trend_following": 1},
        risk_score=30.0,
        market_condition=MarketCondition.TRENDING_UP,
    )

    # Mock a passed risk check from SignalGenerator.check_risk
    risk_check = Mock()
    risk_check.passed = True
    risk_check.reasons = []

    return TradeDecision(
        plan=plan,
        signal=signal,
        risk_check=risk_check,
        signal_source=SignalSource.STRATEGY,
    )


def _make_sell_decision(symbol: str = "BTCUSDT"):
    """Helper to create a valid SELL (position close) TradeDecision."""
    from trading_bot import TradeDecision

    plan = ExecutionPlan(
        symbol=symbol,
        side=OrderSide.SELL,
        amount=0.001,
        entry_price=45_000.0,
        stop_loss=45_900.0,
        take_profit=43_200.0,
        risk_reward_ratio=2.0,
        confidence=0.70,
        strategy_votes={"trend_following": 1},
        signal_timestamp=datetime.now(),
        signal_id="test_signal_2",
        max_price_drift_pct=1.5,
        close_position=True,
    )

    signal = AggregatedSignal(
        symbol=symbol,
        signal_type=SignalType.SELL,
        combined_confidence=0.70,
        avg_price=45_000.0,
        avg_risk_reward=2.0,
        strategy_votes={"trend_following": 1},
        risk_score=25.0,
        market_condition=MarketCondition.TRENDING_DOWN,
    )

    risk_check = Mock()
    risk_check.passed = True
    risk_check.reasons = []

    return TradeDecision(
        plan=plan,
        signal=signal,
        risk_check=risk_check,
        signal_source=SignalSource.STRATEGY,
    )


def _build_bot(config_overrides=None, **kwargs):
    """Build a TradingBotOrchestrator with all mocks wired up."""
    config = {
        "mode": "full_auto",
        "trading_pair": "BTCUSDT",
        "interval_seconds": 1,
        "timeframe": "1h",
        "signal_source": "strategy",
        "strategies": {"enabled": ["trend_following"]},
        "trading": {"max_open_positions": 3},
        "risk": {"max_risk_per_trade_pct": 1.0, "max_daily_loss_pct": 5.0},
        "data": {"pairs": ["BTCUSDT"]},
        "backtesting": {"require_validation_before_live": False},
        "state_management": {"enabled": False},
        # Prevent TradingBotOrchestrator.__init__ from calling get_websocket()
        # which sets bitkub_websocket._global_ws and contaminates
        # test_get_websocket_stats_no_connection in test_strategies.py.
        "websocket": {"enabled": False},
    }
    if config_overrides:
        config.update(config_overrides)

    # Remove 'db' from kwargs — it's not a constructor arg.
    mock_db = kwargs.pop("db", None)
    if mock_db is None:
        mock_db = Mock()
        mock_db.get_positions.return_value = []
        mock_db.load_all_positions.return_value = []
        mock_db.list_trade_states.return_value = []

    with patch("trading_bot.get_database", return_value=mock_db):
        return TradingBotOrchestrator(config=config, **kwargs)


# ── Tests ────────────────────────────────────────────────────────────────────

class TestCanOpenPositionEnforcement:
    """The daily-loss / max-positions / cooldown gate must block new BUY orders."""

    def test_buy_blocked_when_daily_loss_exceeded(
        self, mock_api_client, mock_signal_generator, mock_risk_manager,
        mock_executor, mock_db,
    ):
        """When can_open_position returns False, execute_entry must NOT be called."""
        mock_risk_manager.can_open_position.return_value = RiskCheckResult(
            False, "Daily loss limit reached: 5100.00 / 5000.00"
        )

        bot = _build_bot(
            api_client=mock_api_client,
            signal_generator=mock_signal_generator,
            risk_manager=mock_risk_manager,
            executor=mock_executor,
            db=mock_db,
        )

        decision = _make_buy_decision()
        portfolio = {"balance": 94_000.0, "positions": [], "timestamp": datetime.now()}

        bot._process_full_auto(decision, portfolio)

        # Executor must NOT have been called
        mock_executor.execute_entry.assert_not_called()
        # can_open_position MUST have been called
        mock_risk_manager.can_open_position.assert_called_once()

    def test_buy_blocked_when_max_positions_reached(
        self, mock_api_client, mock_signal_generator, mock_risk_manager,
        mock_executor, mock_db,
    ):
        """Max open positions gate prevents new entries."""
        mock_risk_manager.can_open_position.return_value = RiskCheckResult(
            False, "Max open positions reached (3)"
        )

        bot = _build_bot(
            api_client=mock_api_client,
            signal_generator=mock_signal_generator,
            risk_manager=mock_risk_manager,
            executor=mock_executor,
            db=mock_db,
        )

        decision = _make_buy_decision()
        portfolio = {
            "balance": 100_000.0,
            "positions": [{"id": 1}, {"id": 2}, {"id": 3}],
            "timestamp": datetime.now(),
        }

        bot._process_full_auto(decision, portfolio)

        mock_executor.execute_entry.assert_not_called()

    def test_buy_blocked_when_cooldown_active(
        self, mock_api_client, mock_signal_generator, mock_risk_manager,
        mock_executor, mock_db,
    ):
        """Cooldown gate prevents rapid-fire BUY entries."""
        mock_risk_manager.can_open_position.return_value = RiskCheckResult(
            False, "Cooldown period active"
        )

        bot = _build_bot(
            api_client=mock_api_client,
            signal_generator=mock_signal_generator,
            risk_manager=mock_risk_manager,
            executor=mock_executor,
            db=mock_db,
        )

        decision = _make_buy_decision()
        portfolio = {"balance": 100_000.0, "positions": [], "timestamp": datetime.now()}

        bot._process_full_auto(decision, portfolio)

        mock_executor.execute_entry.assert_not_called()

    def test_buy_proceeds_when_all_limits_ok(
        self, mock_api_client, mock_signal_generator, mock_risk_manager,
        mock_executor, mock_db,
    ):
        """Normal BUY must proceed when can_open_position allows it."""
        mock_risk_manager.can_open_position.return_value = RiskCheckResult(
            True, "All checks passed"
        )

        bot = _build_bot(
            api_client=mock_api_client,
            signal_generator=mock_signal_generator,
            risk_manager=mock_risk_manager,
            executor=mock_executor,
            db=mock_db,
        )

        decision = _make_buy_decision()
        portfolio = {"balance": 100_000.0, "positions": [], "timestamp": datetime.now()}

        bot._process_full_auto(decision, portfolio)

        # Executor MUST have been called
        mock_executor.execute_entry.assert_called_once()

    def test_sell_bypasses_can_open_position(
        self, mock_api_client, mock_signal_generator, mock_risk_manager,
        mock_executor, mock_db,
    ):
        """SELL orders (closing positions) must NOT be blocked by can_open_position."""
        # Even though risk says "no new positions", SELL should go through
        mock_risk_manager.can_open_position.return_value = RiskCheckResult(
            False, "Daily loss limit reached"
        )

        bot = _build_bot(
            api_client=mock_api_client,
            signal_generator=mock_signal_generator,
            risk_manager=mock_risk_manager,
            executor=mock_executor,
            db=mock_db,
        )

        decision = _make_sell_decision()
        portfolio = {"balance": 94_000.0, "positions": [{"id": 1}], "timestamp": datetime.now()}

        bot._process_full_auto(decision, portfolio)

        # can_open_position should NOT be called for SELL
        mock_risk_manager.can_open_position.assert_not_called()
        # Executor SHOULD still be called for exit
        mock_executor.execute_entry.assert_called_once()

    def test_state_machine_buy_blocked_by_risk(
        self, mock_api_client, mock_signal_generator, mock_risk_manager,
        mock_executor, mock_db,
    ):
        """With state machine enabled, BUY must also be blocked by can_open_position."""
        mock_risk_manager.can_open_position.return_value = RiskCheckResult(
            False, "Max daily trades reached (10)"
        )

        bot = _build_bot(
            config_overrides={"state_management": {"enabled": True}},
            api_client=mock_api_client,
            signal_generator=mock_signal_generator,
            risk_manager=mock_risk_manager,
            executor=mock_executor,
            db=mock_db,
        )

        decision = _make_buy_decision()
        portfolio = {"balance": 100_000.0, "positions": [], "timestamp": datetime.now()}

        bot._process_full_auto(decision, portfolio)

        # Must NOT have submitted to executor
        mock_executor.execute_entry.assert_not_called()
        mock_risk_manager.can_open_position.assert_called_once()


def test_risk_manager_blocks_new_entries_when_drawdown_limit_reached():
    rm = RiskManager(RiskConfig(
        max_risk_per_trade_pct=1.0,
        max_daily_loss_pct=50.0,
        max_drawdown_threshold_pct=10.0,
        drawdown_soft_reduce_start_pct=5.0,
        drawdown_block_new_entries=True,
    ))
    rm._peak_portfolio_value = 100_000.0

    result = rm.can_open_position(portfolio_value=89_000.0, open_positions_count=0)

    assert result.allowed is False
    assert "Drawdown limit reached" in result.reason

    def test_state_machine_sell_in_position_routes_to_managed_exit(
        self, mock_api_client, mock_signal_generator, mock_risk_manager,
        mock_executor, mock_db,
    ):
        """SELL with IN_POSITION state should use managed exit path, not execute_entry."""
        bot = _build_bot(
            config_overrides={"state_management": {"enabled": True, "allow_sell_entries_from_idle": True}},
            api_client=mock_api_client,
            signal_generator=mock_signal_generator,
            risk_manager=mock_risk_manager,
            executor=mock_executor,
            db=mock_db,
        )
        bot._state_manager.get_state = Mock(return_value=TradeStateSnapshot(
            symbol="BTCUSDT",
            state=TradeLifecycleState.IN_POSITION,
            entry_order_id="entry_1",
            filled_amount=0.01,
            entry_price=45_000.0,
            total_entry_cost=15_000.0,
        ))
        bot._submit_managed_exit = Mock(return_value=True)
        mock_executor.get_open_orders.return_value = [{
            "order_id": "entry_1",
            "symbol": "BTCUSDT",
            "side": OrderSide.BUY,
            "amount": 0.01,
            "remaining_amount": 0.01,
            "entry_price": 45_000.0,
            "total_entry_cost": 15_000.0,
            "timestamp": datetime.now(),
        }]

        decision = _make_sell_decision()
        portfolio = {"balance": 90_000.0, "positions": [{"id": 1}], "timestamp": datetime.now()}

        bot._process_full_auto(decision, portfolio)

        bot._submit_managed_exit.assert_called_once()
        mock_executor.execute_entry.assert_not_called()

    def test_state_machine_sell_idle_executes_when_enabled(
        self, mock_api_client, mock_signal_generator, mock_risk_manager,
        mock_executor, mock_db,
    ):
        """SELL from IDLE should execute directly when allow_sell_entries_from_idle is enabled."""
        bot = _build_bot(
            config_overrides={"state_management": {"enabled": True, "allow_sell_entries_from_idle": True}},
            api_client=mock_api_client,
            signal_generator=mock_signal_generator,
            risk_manager=mock_risk_manager,
            executor=mock_executor,
            db=mock_db,
        )
        bot._try_submit_managed_signal_sell = Mock(return_value=False)

        decision = _make_sell_decision()
        decision.plan.close_position = False
        portfolio = {"balance": 90_000.0, "positions": [], "timestamp": datetime.now()}

        bot._process_full_auto(decision, portfolio)

        mock_risk_manager.can_open_position.assert_called_once()
        mock_executor.execute_entry.assert_called_once()

    def test_state_machine_sell_idle_blocked_when_disabled(
        self, mock_api_client, mock_signal_generator, mock_risk_manager,
        mock_executor, mock_db,
    ):
        """SELL from IDLE should be blocked when allow_sell_entries_from_idle is disabled."""
        bot = _build_bot(
            config_overrides={"state_management": {"enabled": True, "allow_sell_entries_from_idle": False}},
            api_client=mock_api_client,
            signal_generator=mock_signal_generator,
            risk_manager=mock_risk_manager,
            executor=mock_executor,
            db=mock_db,
        )
        bot._try_submit_managed_signal_sell = Mock(return_value=False)

        decision = _make_sell_decision()
        decision.plan.close_position = False
        portfolio = {"balance": 90_000.0, "positions": [], "timestamp": datetime.now()}

        bot._process_full_auto(decision, portfolio)

        mock_executor.execute_entry.assert_not_called()


def test_get_status_uses_total_balance_for_risk_summary_and_completed_trade_count():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot._ws_client = None
    bot.trading_pairs = ['BTCUSDT']
    bot.trading_pair = 'BTCUSDT'
    bot.running = True
    bot.mode = BotMode.DRY_RUN
    bot.signal_source = SignalSource.STRATEGY
    bot.ml_enabled = False
    bot.paused = False
    bot.pause_reason = ''
    bot._reconciling_orders = False
    bot._pause_state_lock = threading.Lock()
    bot._auth_degraded = False
    bot._auth_degraded_reason = ''
    bot.interval_seconds = 60
    bot._loop_count = 1
    bot._last_loop_time = None
    bot._pending_decisions = []
    bot._pending_decisions_lock = threading.Lock()
    bot._executed_today = [{}, {}]
    bot._ws_enabled = False
    bot._last_mtf_status = {}
    bot._mtf_confirmation_required = False
    bot.mtf_enabled = True
    bot.mtf_timeframes = ['1h']
    bot.timeframe = '1h'
    bot.enabled_strategies = []
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = []
    bot.risk_manager = Mock()
    bot.risk_manager.trade_count_today = 0
    bot.risk_manager.get_risk_summary.return_value = {'ok': True}
    bot._get_portfolio_state = Mock(return_value={'balance': 677.39, 'total_balance': 967.70, 'timestamp': None, 'positions': []})
    bot._get_dashboard_multi_timeframe_status = Mock(return_value={'enabled': True, 'pairs': []})

    status = TradingBotOrchestrator.get_status(bot, lightweight=True)

    bot.risk_manager.get_risk_summary.assert_called_once_with(967.70)
    assert status['executed_today'] == 0


def test_submit_managed_entry_marks_immediate_fill_as_in_position():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    decision = _make_buy_decision()
    snapshot = TradeStateSnapshot(
        symbol='BTCUSDT',
        state=TradeLifecycleState.PENDING_BUY,
        entry_order_id='ord-1',
        active_order_id='ord-1',
        total_entry_cost=1500.0,
        entry_price=45_000.0,
        stop_loss=44_100.0,
        take_profit=46_800.0,
        opened_at=datetime.now(),
    )
    filled_snapshot = TradeStateSnapshot(
        symbol='BTCUSDT',
        state=TradeLifecycleState.IN_POSITION,
        entry_order_id='ord-1',
        active_order_id='ord-1',
        total_entry_cost=1500.0,
        filled_amount=0.001,
        entry_price=45_000.0,
        stop_loss=44_100.0,
        take_profit=46_800.0,
        opened_at=datetime.now(),
    )

    bot.executor = Mock()
    bot.executor.execute_entry.return_value = OrderResult(
        success=True,
        status=OrderStatus.FILLED,
        order_id='ord-1',
        filled_amount=0.001,
        filled_price=45_000.0,
        ordered_amount=1500.0,
        remaining_amount=0.0,
        message='filled',
    )
    bot._state_manager = Mock()
    bot._state_manager.start_pending_buy.return_value = snapshot
    bot._state_manager.mark_entry_filled.return_value = filled_snapshot
    bot._register_filled_position_from_state = Mock()
    bot.risk_manager = Mock()
    bot.db = Mock()
    bot.signal_source = SignalSource.STRATEGY
    bot._executed_today = []
    bot._format_coin_symbol = Mock(return_value='BTC')
    bot._format_alert_block = Mock(return_value='msg')
    bot.send_alerts = False
    bot._send_alert = Mock()

    TradingBotOrchestrator._submit_managed_entry(bot, decision, {'balance': 1000.0, 'total_balance': 1000.0})

    bot._state_manager.start_pending_buy.assert_called_once()
    bot._register_filled_position_from_state.assert_called_once_with(snapshot, 0.001, 45_000.0)
    bot._state_manager.mark_entry_filled.assert_called_once_with('BTCUSDT', 0.001, 45_000.0)
    bot.risk_manager.record_trade.assert_called_once()
    assert decision.status == TradeLifecycleState.IN_POSITION.value


def test_try_submit_managed_signal_sell_routes_in_position_symbol_to_managed_exit():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot._state_machine_enabled = True
    bot._state_manager = Mock()
    bot._state_manager.get_state.return_value = TradeStateSnapshot(
        symbol='BTCUSDT',
        state=TradeLifecycleState.IN_POSITION,
        entry_order_id='entry-1',
        filled_amount=0.01,
        entry_price=45_000.0,
        total_entry_cost=15_000.0,
        opened_at=datetime.now(),
    )
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = [{
        'order_id': 'entry-1',
        'symbol': 'BTCUSDT',
        'side': OrderSide.BUY,
        'amount': 0.01,
        'remaining_amount': 0.01,
        'entry_price': 45_000.0,
        'total_entry_cost': 15_000.0,
        'timestamp': datetime.now(),
    }]
    bot._submit_managed_exit = Mock(return_value=True)

    decision = _make_sell_decision()

    submitted = TradingBotOrchestrator._try_submit_managed_signal_sell(bot, decision)

    assert submitted is True
    assert decision.status == 'pending_sell'
    bot._submit_managed_exit.assert_called_once()
    assert bot._submit_managed_exit.call_args.kwargs['triggered'] == 'SIGSELL'


def test_try_submit_managed_signal_sell_suppresses_sub_threshold_profit_exit():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot._state_machine_enabled = True
    bot._state_manager = Mock()
    bot._state_manager.get_state.return_value = TradeStateSnapshot(
        symbol='BTCUSDT',
        state=TradeLifecycleState.IN_POSITION,
        entry_order_id='entry-1',
        filled_amount=0.01,
        entry_price=45_000.0,
        total_entry_cost=15_000.0,
        opened_at=datetime.now(),
    )
    bot.executor = Mock()
    bot.executor.get_open_orders.return_value = [{
        'order_id': 'entry-1',
        'symbol': 'BTCUSDT',
        'side': OrderSide.BUY,
        'amount': 0.01,
        'remaining_amount': 0.01,
        'entry_price': 45_000.0,
        'total_entry_cost': 15_000.0,
        'timestamp': datetime.now(),
    }]
    bot._submit_managed_exit = Mock(return_value=True)
    bot._enforce_min_profit_gate_for_voluntary_exit = True
    bot._min_voluntary_exit_net_profit_pct = 0.2

    decision = _make_sell_decision()
    decision.plan.entry_price = 1_503_000.0

    submitted = TradingBotOrchestrator._try_submit_managed_signal_sell(bot, decision)

    assert submitted is False
    bot._submit_managed_exit.assert_not_called()


def test_register_filled_position_from_state_persists_live_remaining_amount():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.executor = Mock()
    bot._log_filled_order = Mock()
    snapshot = TradeStateSnapshot(
        symbol='BTCUSDT',
        state=TradeLifecycleState.PENDING_BUY,
        entry_order_id='ord-1',
        total_entry_cost=1500.0,
        stop_loss=44_100.0,
        take_profit=46_800.0,
        opened_at=datetime.now(),
    )

    TradingBotOrchestrator._register_filled_position_from_state(bot, snapshot, 0.001, 45_000.0)

    pos_data = bot.executor.register_tracked_position.call_args.args[1]
    assert pos_data['remaining_amount'] == pytest.approx(0.001)
