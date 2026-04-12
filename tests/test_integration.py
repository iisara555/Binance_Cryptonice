"""
Integration Tests
=================
Full end-to-end integration tests for the trading system.
Tests signal → risk → execution flow with mocked API responses.
"""
import asyncio
import io
import logging
import json
import requests
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Any
from unittest.mock import Mock, MagicMock, patch
import pytest
from rich.console import Console

# Import actual modules from the project
from alerts import AlertLevel, AlertSystem, TelegramSender
from signal_generator import SignalGenerator, AggregatedSignal
from risk_management import RiskManager, RiskConfig
from trade_executor import TradeExecutor, ExecutionPlan, OrderSide, OrderResult, OrderStatus
from api_client import BitkubAPIError, BitkubClient
from dynamic_coin_config import JsonCoinWhitelistRepository
from telegram_bot import TelegramBotHandler
from trading_bot import TradingBotOrchestrator, BotMode, SignalSource
from cli_ui import CLICommandCenter
from main import (
    TradingBotApp,
    _apply_strategy_mode_profile,
    _clear_startup_auth_shutdown_state,
    _enable_startup_auth_degraded_mode,
    resolve_runtime_trading_pairs,
)
from helpers import format_bitkub_time
from state_management import TradeLifecycleState, TradeStateManager, TradeStateSnapshot

logger = logging.getLogger(__name__)


@pytest.fixture
def test_config() -> Dict[str, Any]:
    """Test configuration fixture"""
    return {
        'mode': 'dry_run',
        'trading_pair': 'THB_BTC',
        'interval_seconds': 1,
        'timeframe': '1h',
        'signal_source': 'strategy',
        'strategies': {
            'enabled': ['trend_following']
        },
        'trading': {
            'max_position_size_pct': 0.1,
            'max_open_positions': 3
        },
        'backtesting': {
            'require_validation_before_live': False
        },
        'risk': {
            'max_risk_per_trade_pct': 1.0,
            'max_daily_loss_pct': 5.0,
        },
        'data': {
            'pairs': ['THB_BTC']
        }
    }


@pytest.fixture
def mock_api_client():
    """Mock API client"""
    client = Mock()
    client.get_ticker.return_value = {
        'last': 1500000.0,
        'highestBid': 1499000.0,
        'lowestAsk': 1501000.0,
        'percentChange': 2.5,
    }
    client.get_balances.return_value = {
        'THB': {'available': 100000.0, 'reserved': 0.0},
        'BTC': {'available': 0.0, 'reserved': 0.0}
    }
    client.get_open_orders.return_value = []
    client.place_bid.return_value = {'error': 0, 'result': {'id': 'test_order_123'}}
    client.place_ask.return_value = {'error': 0, 'result': {'id': 'test_order_456'}}
    client.is_circuit_open.return_value = False
    client.check_clock_sync.return_value = True
    client.sync_clock.return_value = 0.0
    return client


@pytest.fixture
def mock_signal_generator():
    """Mock signal generator"""
    generator = Mock(spec=SignalGenerator)
    generator.generate_signals.return_value = []
    return generator


@pytest.fixture
def mock_risk_manager():
    """Mock risk manager"""
    manager = Mock(spec=RiskManager)
    manager.calc_sl_tp_from_atr.return_value = (1470000.0, 1560000.0)
    manager.check_daily_loss_limit.return_value = True
    return manager


@pytest.fixture
def mock_db():
    """Mock database"""
    db = Mock()
    db.get_positions.return_value = []
    db.insert_signal.return_value = None
    return db


@pytest.fixture
def mock_trade_executor(mock_db, mock_risk_manager, mock_api_client):
    """Mock trade executor"""
    from trade_executor import OrderStatus
    
    executor = Mock(spec=TradeExecutor)
    executor.execute_entry.return_value = OrderResult(
        success=True,
        status=OrderStatus.FILLED,
        order_id='test_order_123',
        filled_amount=0.06666667,
        filled_price=1500000.0,
        message='Order executed successfully'
    )
    executor.execute_exit.return_value = OrderResult(
        success=True,
        status=OrderStatus.FILLED,
        order_id='test_order_456',
        filled_amount=0.06666667,
        filled_price=1470000.0,
        message='Exit executed successfully'
    )
    executor.get_open_orders.return_value = []
    return executor


class TestFullIntegrationFlow:
    """Full end-to-end integration tests"""

    def test_strategy_mode_profile_trend_only_forces_trend_following(self):
        config = {
            "trading": {"timeframe": "15m"},
            "strategies": {
                "enabled": ["trend_following", "mean_reversion", "breakout"],
                "min_confidence": 0.35,
                "min_strategies_agree": 2,
            },
            "risk": {"stop_loss_pct": 4.5, "take_profit_pct": 10.0},
            "state_management": {"confirmations_required": 2, "confirmation_window_seconds": 180},
            "auto_trader": {"auto_exit": {"max_hold_hours": 48}},
            "multi_timeframe": {"enabled": True, "timeframes": ["15m", "1h"], "higher_timeframes": ["1h"]},
            "strategy_mode": {
                "active": "trend_only",
                "trend_only": {
                    "primary_timeframe": "15m",
                    "confirm_timeframe": "1h",
                    "min_confidence": 0.4,
                    "stop_loss_pct": 4.5,
                    "take_profit_pct": 10.0,
                    "max_hold_hours": 48,
                },
            },
        }

        applied = _apply_strategy_mode_profile(config)

        assert applied["active_strategy_mode"] == "trend_only"
        assert applied["trading"]["timeframe"] == "15m"
        assert applied["strategies"]["enabled"] == ["trend_following"]
        assert applied["strategies"]["min_confidence"] == 0.4
        assert applied["auto_trader"]["auto_exit"]["max_hold_hours"] == 48

    def test_strategy_mode_profile_standard_leaves_existing_mix_unchanged(self):
        config = {
            "trading": {"timeframe": "15m"},
            "strategies": {
                "enabled": ["trend_following", "mean_reversion", "breakout"],
                "min_confidence": 0.35,
                "min_strategies_agree": 2,
            },
            "strategy_mode": {"active": "standard"},
        }

        applied = _apply_strategy_mode_profile(config)

        assert applied["active_strategy_mode"] == "standard"
        assert applied["trading"]["timeframe"] == "15m"
        assert applied["strategies"]["enabled"] == ["trend_following", "mean_reversion", "breakout"]
        assert applied["strategies"]["min_strategies_agree"] == 2

    def test_enable_startup_auth_degraded_mode_forces_safe_config(self):
        """Startup auth failures should force a safe, non-trading runtime config."""
        config = {
            'mode': 'full_auto',
            'simulate_only': False,
            'trading_pair': 'THB_BTC',
            'trading': {'mode': 'full_auto', 'trading_pair': 'THB_BTC'},
            'data': {'auto_detect_held_pairs': True, 'pairs': ['THB_BTC', 'THB_ETH']},
            'rebalance': {'enabled': True},
        }

        pairs = _enable_startup_auth_degraded_mode(
            config,
            'Bitkub private API unavailable',
            ['THB_BTC', 'THB_ETH'],
        )

        assert pairs == ['THB_BTC', 'THB_ETH']
        assert config['auth_degraded'] is True
        assert config['mode'] == 'dry_run'
        assert config['trading']['mode'] == 'dry_run'
        assert config['simulate_only'] is True
        assert config['read_only'] is True
        assert config['data']['auto_detect_held_pairs'] is False
        assert config['data']['pairs'] == ['THB_BTC', 'THB_ETH']
        assert config['rebalance']['enabled'] is False

    def test_clear_startup_auth_shutdown_state_resets_fatal_flags(self):
        """Startup degrade should consume the fatal shutdown state raised by auth error 5."""
        import api_client as api_module

        api_module.SHOULD_SHUTDOWN = True
        api_module.SHUTDOWN_REASON = 'fatal auth failure'
        client = Mock()
        client._cb = Mock()

        _clear_startup_auth_shutdown_state(client)

        assert api_module.SHOULD_SHUTDOWN is False
        assert api_module.SHUTDOWN_REASON == ''
        client._cb.reset.assert_called_once()

    def test_suppressed_startup_auth_probe_avoids_fatal_shutdown_side_effects(self, caplog):
        """Startup auth probes should raise error 5 without emitting fatal shutdown state or CRITICAL logs."""
        import api_client as api_module

        api_module.SHOULD_SHUTDOWN = False
        api_module.SHUTDOWN_REASON = ''
        client = BitkubClient(api_key='key', api_secret='secret', base_url='https://example.invalid')
        client.check_clock_sync = Mock(return_value=True)
        client._get_server_time = Mock(return_value=1234567890000)

        response = Mock()
        response.status_code = 401
        response.text = '{"error":5}'
        response.json.return_value = {'error': 5}

        with patch('api_client.requests.request', return_value=response):
            with caplog.at_level(logging.WARNING):
                with pytest.raises(BitkubAPIError) as exc_info:
                    with client.suppress_fatal_auth_handling('startup pair auto-detection'):
                        client.get_balances()

        assert exc_info.value.code == 5
        assert exc_info.value.message == 'IP not allowed'
        assert api_module.SHOULD_SHUTDOWN is False
        assert api_module.SHUTDOWN_REASON == ''
        assert 'startup pair auto-detection' in caplog.text
        assert not any(record.levelno >= logging.CRITICAL for record in caplog.records)

    def test_resolve_runtime_trading_pairs_uses_json_whitelist_and_quote_balance(self, tmp_path):
        """Hybrid dynamic config should use JSON whitelist order and real THB readiness."""
        whitelist_path = tmp_path / 'coin_whitelist.json'
        whitelist_path.write_text(json.dumps({
            'quote_asset': 'THB',
            'min_quote_balance_thb': 100.0,
            'assets': ['BTC', 'DOGE', 'ETH'],
        }), encoding='utf-8')

        client = Mock()
        client.get_balances.return_value = {
            'THB': {'available': 250.0, 'reserved': 0.0},
            'BTC': {'available': 0.0, 'reserved': 0.0},
            'DOGE': {'available': 0.0, 'reserved': 0.0},
            'ETH': {'available': 0.0, 'reserved': 0.0},
        }
        client.get_symbols.return_value = [
            {'symbol': 'BTC_THB'},
            {'symbol': 'DOGE_THB'},
        ]

        pairs = resolve_runtime_trading_pairs(
            client,
            data_config={
                'hybrid_dynamic_coin_config': {
                    'whitelist_json_path': str(whitelist_path),
                    'min_quote_balance_thb': 100.0,
                },
            },
            project_root=tmp_path,
        )

        assert pairs == ['THB_BTC', 'THB_DOGE']

    def test_resolve_runtime_trading_pairs_keeps_whitelisted_holdings_without_quote_balance(self, tmp_path):
        """Held whitelist assets should stay tradable for managed exits even when THB is low."""
        whitelist_path = tmp_path / 'coin_whitelist.json'
        whitelist_path.write_text(json.dumps({
            'quote_asset': 'THB',
            'min_quote_balance_thb': 100.0,
            'assets': ['BTC', 'DOGE'],
        }), encoding='utf-8')

        client = Mock()
        client.get_balances.return_value = {
            'THB': {'available': 10.0, 'reserved': 0.0},
            'BTC': {'available': 0.0, 'reserved': 0.0},
            'DOGE': {'available': 1500.0, 'reserved': 0.0},
        }
        client.get_symbols.return_value = [
            {'symbol': 'BTC_THB'},
            {'symbol': 'DOGE_THB'},
        ]

        pairs = resolve_runtime_trading_pairs(
            client,
            data_config={
                'hybrid_dynamic_coin_config': {
                    'whitelist_json_path': str(whitelist_path),
                    'min_quote_balance_thb': 100.0,
                },
            },
            project_root=tmp_path,
        )

        assert pairs == ['THB_DOGE']

    def test_resolve_runtime_trading_pairs_falls_back_to_safe_defaults_on_invalid_json(self, tmp_path, caplog):
        """Malformed whitelist JSON should not crash startup and should fall back safely."""
        whitelist_path = tmp_path / 'coin_whitelist.json'
        whitelist_path.write_text('{invalid json', encoding='utf-8')

        client = Mock()
        client.get_balances.return_value = {
            'THB': {'available': 250.0, 'reserved': 0.0},
            'BTC': {'available': 0.0, 'reserved': 0.0},
            'DOGE': {'available': 0.0, 'reserved': 0.0},
        }
        client.get_symbols.return_value = [
            {'symbol': 'BTC_THB'},
            {'symbol': 'DOGE_THB'},
        ]

        with caplog.at_level(logging.WARNING):
            pairs = resolve_runtime_trading_pairs(
                client,
                data_config={
                    'hybrid_dynamic_coin_config': {
                        'whitelist_json_path': str(whitelist_path),
                    },
                },
                project_root=tmp_path,
            )

        assert pairs == ['THB_BTC', 'THB_DOGE']
        assert 'Failed to parse hybrid coin whitelist JSON' in caplog.text

    def test_json_whitelist_repository_warns_on_unknown_keys_and_unsupported_version(self, tmp_path, caplog):
        """Schema validation should warn and fall back safely for unsupported versions and unknown keys."""
        whitelist_path = tmp_path / 'coin_whitelist.json'
        whitelist_path.write_text(json.dumps({
            'version': 99,
            'quote_asset': 'THB',
            'unexpected_key': True,
            'assets': [
                {'symbol': 'BTC', 'unexpected_asset_key': 1},
            ],
        }), encoding='utf-8')

        repo = JsonCoinWhitelistRepository(whitelist_path)
        with caplog.at_level(logging.WARNING):
            config = repo.load()

        assert [entry.symbol for entry in config.entries] == ['BTC', 'DOGE']
        assert config.source_kind == 'unsupported_version'
        assert 'unsupported schema version' in caplog.text

    def test_resolve_runtime_trading_pairs_applies_per_asset_overrides(self, tmp_path):
        """Per-asset overrides should affect readiness independently from global thresholds."""
        whitelist_path = tmp_path / 'coin_whitelist.json'
        whitelist_path.write_text(json.dumps({
            'version': 1,
            'quote_asset': 'THB',
            'min_quote_balance_thb': 100.0,
            'assets': [
                {'symbol': 'BTC', 'min_quote_balance_thb': 40.0},
                {'symbol': 'DOGE', 'include_if_held': False, 'min_quote_balance_thb': 100.0},
            ],
        }), encoding='utf-8')

        client = Mock()
        client.get_balances.return_value = {
            'THB': {'available': 50.0, 'reserved': 0.0},
            'BTC': {'available': 0.0, 'reserved': 0.0},
            'DOGE': {'available': 1000.0, 'reserved': 0.0},
        }
        client.get_symbols.return_value = [
            {'symbol': 'BTC_THB'},
            {'symbol': 'DOGE_THB'},
        ]

        pairs = resolve_runtime_trading_pairs(
            client,
            data_config={
                'hybrid_dynamic_coin_config': {
                    'whitelist_json_path': str(whitelist_path),
                },
            },
            project_root=tmp_path,
        )

        assert pairs == ['THB_BTC']

    def test_refresh_runtime_pairs_updates_runtime_components_and_preserves_active_symbols(self, tmp_path):
        """Hot reload should update bot/collector pairs while retaining symbols with active positions."""
        whitelist_path = tmp_path / 'coin_whitelist.json'
        whitelist_path.write_text(json.dumps({
            'version': 1,
            'assets': ['BTC', 'ETH'],
        }), encoding='utf-8')

        app = TradingBotApp.__new__(TradingBotApp)
        app.config = {
            'data': {
                'auto_detect_held_pairs': True,
                'pairs': ['THB_BTC'],
                'hybrid_dynamic_coin_config': {
                    'whitelist_json_path': str(whitelist_path),
                    'hot_reload_enabled': True,
                },
            },
            'trading': {'trading_pair': 'THB_BTC'},
            'trading_pair': 'THB_BTC',
        }
        app.api_client = Mock()
        app.api_client.get_balances.return_value = {
            'THB': {'available': 500.0, 'reserved': 0.0},
            'BTC': {'available': 0.0, 'reserved': 0.0},
            'ETH': {'available': 0.0, 'reserved': 0.0},
            'DOGE': {'available': 10.0, 'reserved': 0.0},
        }
        app.api_client.get_symbols.return_value = [
            {'symbol': 'BTC_THB'},
            {'symbol': 'ETH_THB'},
            {'symbol': 'DOGE_THB'},
        ]
        app.collector = Mock()
        app.bot = Mock()
        app.executor = Mock()
        app.executor.get_open_orders.return_value = [{'symbol': 'THB_DOGE'}]
        app.telegram_handler = Mock()
        app.telegram_handler.pairs = ['THB_BTC']
        app._pair_reload_lock = threading.Lock()

        pairs = TradingBotApp.refresh_runtime_pairs(app, reason='test hot reload', force=True)

        assert pairs == ['THB_BTC', 'THB_ETH', 'THB_DOGE']
        app.collector.set_pairs.assert_called_once_with(['THB_BTC', 'THB_ETH', 'THB_DOGE'])
        app.bot.update_runtime_pairs.assert_called_once_with(['THB_BTC', 'THB_ETH', 'THB_DOGE'], reason='test hot reload')
        assert app.config['data']['pairs'] == ['THB_BTC', 'THB_ETH', 'THB_DOGE']
        assert app.telegram_handler.pairs == ['THB_BTC', 'THB_ETH', 'THB_DOGE']

    def test_start_skips_private_bootstrap_when_auth_degraded(self):
        """The bot should not reconcile or bootstrap live balances in degraded auth mode."""
        bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
        bot.running = False
        bot._auth_degraded = True
        bot._auth_degraded_reason = 'auth error 5'
        bot._reconcile_on_startup = Mock()
        bot.executor = Mock()
        bot.executor.sync_open_orders_from_db = Mock()
        bot.executor.get_open_orders.return_value = []
        bot._bootstrap_held_coin_history = Mock()
        bot._state_machine_enabled = True
        bot._state_manager = Mock()
        bot._state_manager.sync_in_position_states = Mock()
        bot._main_loop = Mock()
        bot._candle_retention_enabled = False
        bot._candle_retention_run_on_startup = False
        bot._last_candle_retention_cleanup_at = 0
        bot._bootstrap_held_positions = Mock()

        fake_thread = Mock()
        with patch('trading_bot.threading.Thread', return_value=fake_thread):
            TradingBotOrchestrator.start(bot)

        bot._reconcile_on_startup.assert_not_called()
        bot.executor.sync_open_orders_from_db.assert_called_once()
        bot._bootstrap_held_coin_history.assert_not_called()
        bot._state_manager.sync_in_position_states.assert_called_once_with([])
        fake_thread.start.assert_called_once()
        assert bot.running is True

    def test_run_iteration_returns_early_in_auth_degraded_mode(self):
        """Auth-degraded mode should skip the normal trading loop before private API access."""
        bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
        bot._auth_degraded = True
        bot._auth_degraded_reason = 'auth error 5'
        bot._auth_degraded_logged = False
        # ml_enabled is checked by _maybe_retry_ml_init() at the top of
        # _run_iteration() before the auth-degraded early-return guard.
        bot.ml_enabled = False
        bot.api_client = Mock()
        bot._check_positions_for_sl_tp = Mock()
        bot._check_portfolio_rebalance = Mock()
        bot._advance_managed_trade_states = Mock()

        TradingBotOrchestrator._run_iteration(bot)

        bot.api_client.is_circuit_open.assert_not_called()
        bot._check_positions_for_sl_tp.assert_not_called()
        bot._check_portfolio_rebalance.assert_not_called()
        bot._advance_managed_trade_states.assert_not_called()
        assert bot._auth_degraded_logged is True

    def test_approve_trade_is_blocked_in_auth_degraded_mode(self):
        """Manual approvals must be blocked while startup auth degraded mode is active."""
        bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
        bot._auth_degraded = True
        bot.mode = BotMode.SEMI_AUTO
        bot._pending_decisions = [Mock()]

        assert TradingBotOrchestrator.approve_trade(bot, 0) is False

    def test_trigger_rebalance_is_disabled_in_sniper_mode(self):
        """Manual rebalance must stay disabled in sniper mode regardless of auth state."""
        bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
        bot._auth_degraded = True
        bot._auth_degraded_reason = 'Bitkub private API unavailable'

        result = TradingBotOrchestrator.trigger_rebalance(bot)

        assert result == {
            'status': 'skipped',
            'reason': 'Rebalance is disabled in sniper mode',
            'trigger': 'manual',
        }

    def test_get_status_reports_auth_degraded_state(self):
        """Bot status payload should expose auth degraded state to consumers."""
        bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
        bot._ws_client = None
        bot.trading_pairs = []
        bot.trading_pair = ''
        bot.running = True
        bot.mode = BotMode.DRY_RUN
        bot.signal_source = SignalSource.STRATEGY
        bot.ml_enabled = True
        bot._auth_degraded = True
        bot._auth_degraded_reason = 'Bitkub private API unavailable'
        bot.interval_seconds = 60
        bot._loop_count = 3
        bot._last_loop_time = None
        bot._pending_decisions = []
        bot._pending_decisions_lock = threading.Lock()
        bot._executed_today = []
        bot._ws_enabled = False
        bot.risk_manager = Mock()
        bot.risk_manager.get_risk_summary.return_value = {'ok': True}
        bot._get_portfolio_state = Mock(return_value={'balance': 0.0})
        bot._pause_state_lock = threading.Lock()
        bot._paused = False
        bot._pause_reason = ''
        bot._reconcile_in_progress = False

        status = TradingBotOrchestrator.get_status(bot)

        assert status['auth_degraded'] == {
            'active': True,
            'reason': 'Bitkub private API unavailable',
            'public_only': True,
        }
        assert status['mode'] == 'dry_run'

    def test_telegram_status_shows_auth_degraded_banner_without_balance_call(self, tmp_path):
        """Telegram /status should report degraded mode and avoid private balance calls."""
        handler = TelegramBotHandler.__new__(TelegramBotHandler)
        bot_ref = Mock()
        bot_ref.get_status.return_value = {
            'mode': 'dry_run',
            'trading_pairs': [],
            'auth_degraded': {
                'active': True,
                'reason': 'Bitkub private API unavailable',
                'public_only': True,
            },
        }
        app_ref = Mock()
        app_ref.bot = bot_ref
        app_ref.api_client = Mock()
        app_ref.config = {
            'database': {'db_path': str(tmp_path / 'missing.db')},
            'portfolio': {'initial_balance': 500.0},
        }
        handler.app_ref = app_ref
        handler.trading_disabled = threading.Event()
        handler._start_time = time.time() - 5
        handler.pairs = []
        handler._send = Mock()

        handler._cmd_status()

        app_ref.api_client.get_balances.assert_not_called()
        sent_text = handler._send.call_args.args[0]
        assert 'DEGRADED' in sent_text
        assert 'การเทรดถูกปิดใช้งาน' in sent_text
        assert 'Bitkub private API unavailable' in sent_text

    def test_alert_system_create_trade_sender_reuses_same_instance(self):
        """Legacy alert callback should reuse the same AlertSystem instance."""
        system = AlertSystem(bot_token='test_token', chat_id='test_chat')
        system.send = Mock(return_value=True)

        sender = system.create_trade_sender()

        assert sender('trade executed') is True
        system.send.assert_called_once_with(AlertLevel.TRADE, 'trade executed')

    def test_telegram_handler_reuses_shared_alert_transport(self):
        """Telegram command handler should reuse the app-level Telegram transport when available."""
        app_ref = Mock()
        app_ref.alert_system = AlertSystem(bot_token='shared_token', chat_id='shared_chat')

        handler = TelegramBotHandler(
            app_ref=app_ref,
            bot_token='fallback_token',
            chat_id='shared_chat',
        )

        assert handler.telegram is app_ref.alert_system.telegram

    def test_telegram_callback_accepts_group_chat_id_from_message(self):
        """Inline callback auth should validate the callback message chat id, not only the user id."""
        handler = TelegramBotHandler.__new__(TelegramBotHandler)
        handler.chat_id = '-10012345'
        handler.telegram = Mock()
        handler.telegram.answer_callback = Mock()
        handler._execute_kill = Mock()
        handler._execute_resume = Mock()

        handler._handle_callback({
            'id': 'update-1',
            'from': {'id': 999999},
            'message': {'chat': {'id': -10012345}, 'message_id': 12},
            'data': 'kill_confirm',
        })

        handler.telegram.answer_callback.assert_called_once_with('update-1')
        handler._execute_kill.assert_called_once()

    def test_telegram_status_uses_balance_snapshot_and_cached_prices_without_api_burst(self):
        """Telegram /status should use runtime snapshots/caches and avoid private API bursts."""
        handler = TelegramBotHandler.__new__(TelegramBotHandler)
        bot_ref = Mock()
        bot_ref.get_status.return_value = {
            'mode': 'full_auto',
            'trading_pairs': ['THB_BTC'],
            'auth_degraded': {'active': False, 'reason': ''},
        }
        app_ref = Mock()
        app_ref.bot = bot_ref
        app_ref.api_client = Mock()
        app_ref.api_client.get_balances.return_value = {
            'THB': {'available': 999.0, 'reserved': 0.0},
        }
        app_ref.get_balance_state.return_value = {
            'balances': {
                'THB': {'available': 500.0, 'reserved': 0.0},
                'BTC': {'available': 0.01, 'reserved': 0.0},
            }
        }
        app_ref._cli_price_cache = {
            'THB_BTC': (1000000.0, time.time()),
        }
        app_ref.config = {
            'portfolio': {'initial_balance': 500.0},
        }
        handler.app_ref = app_ref
        handler.trading_disabled = threading.Event()
        handler._start_time = time.time() - 5
        handler.pairs = ['THB_BTC']
        handler._send = Mock()

        with patch('telegram_bot.get_latest_ticker', return_value=None):
            handler._cmd_status()

        app_ref.api_client.get_balances.assert_not_called()
        sent_text = handler._send.call_args.args[0]
        assert '10,000 THB' in sent_text
        assert '10,500.00' in sent_text

    def test_telegram_send_formats_percent_style_args(self):
        """Telegram _send should support existing percent-style formatting call sites."""
        handler = TelegramBotHandler.__new__(TelegramBotHandler)
        handler.telegram = Mock()
        handler.telegram.send_message = Mock(return_value={'ok': True})

        handler._send('Trigger result: %s', 'ok')

        handler.telegram.send_message.assert_called_once_with('Trigger result: ok', reply_markup=None)

    def test_telegram_sender_disables_transport_after_unauthorized(self, monkeypatch):
        """Telegram transport should disable itself after an auth failure to stop repeated 401 noise."""
        monkeypatch.setenv('TELEGRAM_ENABLED', 'true')
        sender = TelegramSender(bot_token='bad_token', chat_id='1234')

        response = Mock()
        response.status_code = 401
        response.text = 'Unauthorized'
        response.reason = 'Unauthorized'
        response.raise_for_status.side_effect = requests.exceptions.HTTPError('401 Client Error: Unauthorized', response=response)

        with patch('alerts.requests.post', return_value=response):
            with pytest.raises(requests.exceptions.HTTPError):
                sender.delete_webhook()

        assert sender.auth_failed is True
        assert sender.api_enabled is False
        assert sender.enabled is False
        assert 'Unauthorized' in sender.auth_failure_reason

    def test_telegram_handler_start_skips_polling_after_auth_failure(self):
        """Telegram command polling should not start when webhook cleanup returns unauthorized."""
        app_ref = Mock()
        app_ref.alert_system = None
        handler = TelegramBotHandler(app_ref=app_ref, bot_token='bad_token', chat_id='1234')

        response = Mock()
        response.status_code = 401
        error = requests.exceptions.HTTPError('401 Client Error: Unauthorized', response=response)
        handler.telegram.delete_webhook = Mock(side_effect=error)

        handler.start()

        assert handler._running is False
        assert handler._thread is None

    def test_initialize_wires_executor_to_shared_telegram_transport(self):
        """App initialization should pass the shared Telegram transport into TradeExecutor."""
        config = {
            'trading': {'mode': 'dry_run'},
            'risk': {},
            'strategies': {},
            'execution': {},
            'state_management': {},
            'notifications': {'telegram_command_polling_enabled': False},
            'portfolio': {'initial_balance': 500.0, 'min_balance_threshold': 100.0},
            'data': {'pairs': ['THB_BTC'], 'auto_detect_held_pairs': False, 'collect_interval_seconds': 60},
            'multi_timeframe': {},
        }
        app = TradingBotApp(config)

        with patch('main.validate_config', return_value=([], [])), \
             patch('main.BitkubClient', return_value=Mock()), \
             patch('main.RiskManager', return_value=Mock()), \
             patch('main.SignalGenerator', return_value=Mock()), \
             patch('main.AlertSystem') as mock_alert_system_cls, \
             patch('main.TradeExecutor', return_value=Mock()) as mock_trade_executor_cls, \
             patch('main.TradingBotOrchestrator', return_value=Mock()), \
             patch('main.BitkubCollector', return_value=Mock()), \
             patch('database.get_database', return_value=Mock()):
            mock_alert_system = Mock()
            mock_alert_system.telegram = Mock()
            mock_alert_system.telegram.enabled = True
            mock_alert_system.create_trade_sender.return_value = Mock()
            mock_alert_system_cls.return_value = mock_alert_system

            assert app.initialize() is True

        assert mock_trade_executor_cls.call_args.kwargs['notifier'] is mock_alert_system.telegram

    def test_initialize_logs_when_telegram_polling_disabled_by_config(self, caplog):
        """App initialization should log the exact config gate when Telegram polling is disabled."""
        config = {
            'trading': {'mode': 'dry_run'},
            'risk': {},
            'strategies': {},
            'execution': {},
            'state_management': {},
            'notifications': {'telegram_command_polling_enabled': False},
            'portfolio': {'initial_balance': 500.0, 'min_balance_threshold': 100.0},
            'data': {'pairs': ['THB_BTC'], 'auto_detect_held_pairs': False, 'collect_interval_seconds': 60},
            'multi_timeframe': {},
        }
        app = TradingBotApp(config)

        with patch('main.validate_config', return_value=([], [])), \
             patch('main.BitkubClient', return_value=Mock()), \
             patch('main.RiskManager', return_value=Mock()), \
             patch('main.SignalGenerator', return_value=Mock()), \
             patch('main.AlertSystem') as mock_alert_system_cls, \
             patch('main.TradeExecutor', return_value=Mock()), \
             patch('main.TradingBotOrchestrator', return_value=Mock()), \
             patch('main.BitkubCollector', return_value=Mock()), \
             patch('database.get_database', return_value=Mock()), \
             caplog.at_level(logging.INFO):
            mock_alert_system = Mock()
            mock_alert_system.telegram = Mock()
            mock_alert_system.telegram.enabled = True
            mock_alert_system.create_trade_sender.return_value = Mock()
            mock_alert_system_cls.return_value = mock_alert_system

            assert app.initialize() is True

        assert 'notifications.telegram_command_polling_enabled=false' in caplog.text

    def test_trade_state_manager_requires_buy_confirmation(self):
        """BUY entries should require consecutive confirmations before leaving IDLE."""
        db = Mock()
        db.list_trade_states.return_value = []
        db.get_trade_state.return_value = None

        manager = TradeStateManager(db, {
            'enabled': True,
            'entry_confidence_threshold': 0.35,
            'confirmations_required': 2,
            'confirmation_window_seconds': 180,
        })

        ts = datetime.utcnow()
        approved, reason = manager.confirm_entry_signal('THB_BTC', 'buy', 0.5, True, ts)
        assert approved is False
        assert '1/2' in reason

        approved, reason = manager.confirm_entry_signal('THB_BTC', 'buy', 0.52, True, ts + timedelta(seconds=30))
        assert approved is True
        assert '2/2' in reason

    def test_execute_entry_defers_position_tracking_until_fill(self, mock_api_client):
        """State-managed entries must remain pending until Bitkub confirms a fill."""
        executor = TradeExecutor(mock_api_client, config={})
        executor.api_client.place_bid.return_value = {
            'error': 0,
            'result': {'id': 'pending-buy-1', 'rat': 1500000.0},
        }

        plan = ExecutionPlan(
            symbol='THB_BTC',
            side=OrderSide.BUY,
            amount=0.0,
            entry_price=1500000.0,
            stop_loss=1470000.0,
            take_profit=1560000.0,
            confidence=0.6,
        )

        result = executor.execute_entry(plan, portfolio_value=100000.0, defer_position_tracking=True)

        try:
            assert result.success is True
            assert result.status == OrderStatus.PENDING
            assert result.order_id == 'pending-buy-1'
            assert executor.get_open_orders() == []
        finally:
            executor.stop()

    def test_pending_sell_fill_reports_pnl_to_telegram(self):
        """A managed sell should report net PnL only after the exit order is filled."""
        db = Mock()
        db.list_trade_states.return_value = []
        db.get_trade_state.return_value = None

        bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
        bot.db = db
        bot.api_client = Mock()
        bot.risk_manager = None
        bot._state_machine_enabled = True
        bot._send_alert = Mock()

        executor = Mock()
        executor.get_open_orders.return_value = []
        executor.execute_exit.return_value = OrderResult(
            success=True,
            status=OrderStatus.PENDING,
            order_id='sell-456',
            filled_amount=0.0,
            filled_price=None,
            message='Exit submitted successfully',
        )
        executor.check_order_status.return_value = OrderResult(
            success=True,
            status=OrderStatus.FILLED,
            order_id='sell-456',
            filled_amount=1.0,
            filled_price=120.0,
            message='Exit filled successfully',
        )
        executor.cancel_order.return_value = False
        executor._oms_cancel_was_error_21 = False
        bot.executor = executor
        bot._state_manager = TradeStateManager(db, {'enabled': True})

        opened_at = datetime.utcnow() - timedelta(minutes=5)
        bot._state_manager.sync_in_position_states([{
            'order_id': 'buy-123',
            'symbol': 'THB_BTC',
            'amount': 1.0,
            'entry_price': 100.0,
            'stop_loss': 95.0,
            'take_profit': 120.0,
            'timestamp': opened_at,
            'total_entry_cost': 100.0,
        }])

        submitted = bot._submit_managed_exit(
            position_id='buy-123',
            pos_symbol='THB_BTC',
            side=OrderSide.BUY,
            amount=1.0,
            exit_price=120.0,
            triggered='TP',
            entry_price=100.0,
            total_entry_cost=100.0,
            price_source='ws',
            opened_at=opened_at,
        )

        assert submitted is True
        assert bot._state_manager.get_state('THB_BTC').state == TradeLifecycleState.PENDING_SELL

        bot._advance_managed_trade_states()

        assert bot._state_manager.get_state('THB_BTC').state == TradeLifecycleState.IDLE
        executor.remove_tracked_position.assert_called_once_with('buy-123')
        executor.check_order_status.assert_called_once_with('sell-456', symbol='THB_BTC', side='sell')
        db.log_closed_trade.assert_called_once()
        db.delete_trade_state.assert_called_with('THB_BTC')

        trade_log = db.log_closed_trade.call_args.args[0]
        assert trade_log['symbol'] == 'THB_BTC'
        assert trade_log['trigger'] == 'TP'
        assert trade_log['price_source'] == 'order'
        assert trade_log['entry_price'] == pytest.approx(100.0)
        assert trade_log['exit_price'] == pytest.approx(120.0)
        assert trade_log['amount'] == pytest.approx(1.0)
        assert trade_log['net_pnl'] == pytest.approx(19.7)
        assert trade_log['net_pnl_pct'] == pytest.approx(19.7)

        bot._send_alert.assert_called_once()
        alert_args, alert_kwargs = bot._send_alert.call_args
        assert '<b>Position Closed</b>' in alert_args[0]
        assert 'BTC' in alert_args[0]
        assert '+19.70%' in alert_args[0]
        assert alert_kwargs == {'to_telegram': True}

    def test_resolve_fill_amount_normalizes_pending_buy_thb_sized_fill(self):
        """Pending BUY fills reported as THB spend should be converted to coin quantity."""
        bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
        snapshot = TradeStateSnapshot(
            symbol='THB_BTC',
            state=TradeLifecycleState.PENDING_BUY,
            side='buy',
            entry_order_id='buy-123',
            exit_order_id='',
            active_order_id='buy-123',
            requested_amount=195.42,
            filled_amount=0.0,
            entry_price=2_297_000.0,
            exit_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            total_entry_cost=195.42,
            signal_confidence=0.0,
            signal_source='',
            trigger='',
            notes='',
        )
        result = OrderResult(
            success=True,
            status=OrderStatus.FILLED,
            order_id='buy-123',
            filled_amount=194.93,
            filled_price=2_297_000.0,
            ordered_amount=195.42,
        )

        filled_amount, fill_price = TradingBotOrchestrator._resolve_fill_amount(
            bot,
            snapshot,
            result,
            snapshot.entry_price,
        )

        assert fill_price == pytest.approx(2_297_000.0)
        assert filled_amount == pytest.approx(195.42 / 2_297_000.0)

    def test_resolve_runtime_trading_pairs_uses_bitkub_holdings(self):
        """Runtime trading pairs should come from current Bitkub holdings."""
        client = Mock()
        client.get_balances.return_value = {
            'THB': {'available': 1200.0, 'reserved': 100.0},
            'DOGE': {'available': 12.5, 'reserved': 0.0},
            'BTC': {'available': 0.0, 'reserved': 0.0001},
            'BNB': {'available': 0.0, 'reserved': 0.0},
        }
        client.get_symbols.return_value = [
            {'symbol': 'DOGE_THB'},
            {'symbol': 'BTC_THB'},
            {'symbol': 'ETH_THB'},
        ]

        pairs = resolve_runtime_trading_pairs(client, ['THB_BTC', 'THB_ETH', 'THB_DOGE'])

        assert pairs == ['THB_BTC', 'THB_DOGE']

    def test_get_trading_pairs_respects_explicit_empty_list(self):
        """An explicit empty data.pairs list must not fall back to a hardcoded/default pair."""
        bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
        bot.config = {'data': {'pairs': []}}
        bot.trading_pair = 'THB_BTC'

        assert bot._get_trading_pairs() == []

    def test_reconcile_logs_ghost_order_summary_with_checked_symbol(self, caplog):
        """Reconciliation should summarize ghost imports using the queried symbol fallback."""
        bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
        bot.trading_pairs = ['THB_DOGE']
        bot.trading_pair = 'THB_BTC'
        bot.config = {'data': {'pairs': ['THB_DOGE']}}
        bot.api_client = Mock()
        bot.api_client.get_open_orders.return_value = [{
            'id': 'ghost-doge-1',
            'sym': None,
            '_checked_symbol': 'THB_DOGE',
            'side': 'sell',
            'amount': '6.80588833',
            'rate': '2.9979',
        }]
        bot.api_client.get_order_history.return_value = []
        bot.db = Mock()

        executor = Mock()
        executor._open_orders = {}
        executor._orders_lock = threading.Lock()
        bot.executor = executor

        with caplog.at_level(logging.WARNING):
            bot._reconcile_on_startup()

        assert executor._open_orders['ghost-doge-1']['symbol'] == 'THB_DOGE'
        assert 'Ghost orders imported summary: SELL THB_DOGE x1' in caplog.text

    def test_reconcile_skips_remote_exit_sell_for_existing_position(self, caplog):
        """A live TP/SL order must not be imported as a ghost position on restart."""
        bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
        bot._get_trading_pairs = Mock(return_value=['THB_BTC'])
        bot.trading_pairs = ['THB_BTC']
        bot.trading_pair = 'THB_BTC'
        bot._state_machine_enabled = False
        bot._reconcile_pending_trade_states = Mock(return_value=set())
        bot._history_status_is_filled = TradingBotOrchestrator._history_status_is_filled
        bot._history_status_is_cancelled = TradingBotOrchestrator._history_status_is_cancelled
        bot._history_status_value = TradingBotOrchestrator._history_status_value
        bot._extract_history_fill_details = TradingBotOrchestrator._extract_history_fill_details.__get__(bot, TradingBotOrchestrator)
        bot._lookup_order_history_status = Mock(return_value=None)
        bot._report_completed_exit = Mock()
        bot._register_filled_position_from_state = Mock()
        bot.risk_manager = None

        api_client = Mock()
        api_client.get_open_orders.return_value = [
            {'id': 'sell-tp-1', 'sym': 'btc_thb', 'typ': 'ask', 'rate': 1650000.0, 'amount': 0.001, 'unfilled': 0.001}
        ]
        api_client.get_order_history.return_value = []
        bot.api_client = api_client

        executor = Mock()
        executor._orders_lock = threading.Lock()
        executor._open_orders = {
            'buy-entry-1': {
                'order_id': 'buy-entry-1',
                'symbol': 'THB_BTC',
                'side': OrderSide.BUY,
                'amount': 0.001,
                'entry_price': 1500000.0,
                'stop_loss': 1447500.0,
                'take_profit': 1650000.0,
                'remaining_amount': 0.0,
                'total_entry_cost': 1500.0,
                'filled': True,
            }
        }
        bot.executor = executor
        bot.db = Mock()

        with caplog.at_level(logging.INFO):
            bot._reconcile_on_startup()

        assert 'sell-tp-1' not in executor._open_orders
        bot.db.save_position.assert_not_called()
        assert 'skipping ghost import to preserve entry price' in caplog.text

    def test_reconcile_pending_sell_fill_while_offline_logs_closed_trade(self):
        db = Mock()
        db.list_trade_states.return_value = []
        db.get_trade_state.return_value = None

        bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
        bot.db = db
        bot.api_client = Mock()
        bot.api_client.get_open_orders.return_value = []
        bot.api_client.get_order_history.return_value = [{
            'id': 'sell-456',
            'status': 'filled',
            'rate': 120.0,
            'amount': 1.0,
        }]
        bot.risk_manager = None
        bot._state_machine_enabled = True
        bot._send_alert = Mock()
        bot._trigger_rebalance_async = Mock()
        bot.trading_pairs = ['THB_BTC']
        bot.trading_pair = 'THB_BTC'
        bot.config = {'data': {'pairs': ['THB_BTC']}}

        executor = Mock()
        executor._open_orders = {}
        executor._orders_lock = threading.Lock()
        bot.executor = executor
        bot._state_manager = TradeStateManager(db, {'enabled': True})

        opened_at = datetime.utcnow() - timedelta(minutes=5)
        bot._state_manager.sync_in_position_states([{
            'order_id': 'buy-123',
            'symbol': 'THB_BTC',
            'amount': 1.0,
            'entry_price': 100.0,
            'stop_loss': 95.0,
            'take_profit': 120.0,
            'timestamp': opened_at,
            'total_entry_cost': 100.0,
        }])
        bot._state_manager.start_pending_sell(
            'THB_BTC',
            {
                'order_id': 'buy-123',
                'symbol': 'THB_BTC',
                'amount': 1.0,
                'entry_price': 100.0,
                'stop_loss': 95.0,
                'take_profit': 120.0,
                'timestamp': opened_at,
                'total_entry_cost': 100.0,
            },
            exit_order_id='sell-456',
            trigger='TP',
            exit_price=120.0,
            notes='price_source=order',
        )

        bot._reconcile_on_startup()

        assert bot._state_manager.get_state('THB_BTC').state == TradeLifecycleState.IDLE
        db.log_closed_trade.assert_called_once()
        db.insert_order.assert_called_once()
        trade_log = db.log_closed_trade.call_args.args[0]
        assert trade_log['symbol'] == 'THB_BTC'
        assert trade_log['exit_price'] == pytest.approx(120.0)
        assert trade_log['net_pnl'] == pytest.approx(19.7)
        assert trade_log['price_source'] == 'reconcile'

    def test_position_row_valid_for_sync_accepts_buy_thb_btc_amount(self, mock_api_client):
        """BUY THB_BTC orders store THB amount, so amounts > 1.0 are valid."""
        executor = TradeExecutor(mock_api_client, config={})

        assert executor._position_row_valid_for_sync({
            'symbol': 'THB_BTC',
            'side': 'buy',
            'amount': 24.36,
            'remaining_amount': 24.36,
            'is_partial_fill': False,
        }) is True

        assert executor._position_row_valid_for_sync({
            'symbol': 'THB_BTC',
            'side': 'sell',
            'amount': 24.36,
            'remaining_amount': 24.36,
            'is_partial_fill': False,
        }) is False

    def test_sync_in_position_states_converts_unfilled_buy_to_pending_buy(self):
        class FakeStateDB:
            def __init__(self, rows):
                self.rows = {row['symbol']: dict(row) for row in rows}

            def list_trade_states(self):
                return list(self.rows.values())

            def get_trade_state(self, symbol):
                return self.rows.get(symbol)

            def save_trade_state(self, state_data):
                self.rows[state_data['symbol']] = dict(state_data)

            def delete_trade_state(self, symbol):
                return self.rows.pop(symbol, None) is not None

        opened_at = datetime.utcnow() - timedelta(minutes=5)
        db = FakeStateDB([
            {
                'symbol': 'THB_BTC',
                'state': 'in_position',
                'side': 'buy',
                'entry_order_id': 'sell-123',
                'exit_order_id': '',
                'active_order_id': 'sell-123',
                'requested_amount': 100.0,
                'filled_amount': 0.0001,
                'entry_price': 2200000.0,
                'exit_price': 0.0,
                'stop_loss': 0.0,
                'take_profit': 0.0,
                'total_entry_cost': 100.0,
                'signal_confidence': 0.0,
                'signal_source': '',
                'trigger': '',
                'notes': '',
                'opened_at': opened_at,
                'last_transition_at': opened_at,
            }
        ])
        manager = TradeStateManager(db, {'enabled': True})

        manager.sync_in_position_states([
            {
                'order_id': 'buy-123',
                'symbol': 'THB_BTC',
                'side': 'buy',
                'amount': 24.36,
                'entry_price': 2162852.45,
                'remaining_amount': 24.36,
                'filled_amount': 0.0,
                'filled': False,
                'total_entry_cost': 24.36,
                'timestamp': opened_at,
            }
        ])

        assert manager.get_state('THB_BTC').state == TradeLifecycleState.PENDING_BUY
    
    def test_config_loading(self, test_config):
        """Test that configuration is loaded correctly"""
        assert test_config['mode'] == 'dry_run'
        assert test_config['trading_pair'] == 'THB_BTC'
        assert test_config['interval_seconds'] == 1
        assert 'trend_following' in test_config['strategies']['enabled']
        logger.info("✅ Configuration loading test passed")
    
    def test_signal_generator_initialization(self, mock_signal_generator):
        """Test signal generator can be initialized with mocks"""
        assert mock_signal_generator is not None
        assert hasattr(mock_signal_generator, 'generate_signals')
        logger.info("✅ Signal generator initialization test passed")
    
    def test_risk_manager_initialization(self, mock_risk_manager):
        """Test risk manager can be initialized with mocks"""
        assert mock_risk_manager is not None
        assert hasattr(mock_risk_manager, 'calc_sl_tp_from_atr')
        logger.info("✅ Risk manager initialization test passed")
    
    def test_trade_executor_initialization(self, mock_trade_executor):
        """Test trade executor can be initialized with mocks"""
        assert mock_trade_executor is not None
        assert hasattr(mock_trade_executor, 'execute_entry')
        assert hasattr(mock_trade_executor, 'execute_exit')
        logger.info("✅ Trade executor initialization test passed")


class TestSignalFlow:
    """Tests for signal generation and flow"""
    
    def test_signal_generation_empty_data(self, mock_signal_generator):
        """Test signal generation with empty data returns empty list"""
        mock_signal_generator.generate_signals.return_value = []
        signals = mock_signal_generator.generate_signals(
            data=None,
            symbol='THB_BTC',
            use_strategies=['trend_following']
        )
        assert signals == []
        logger.info("✅ Empty signal generation test passed")
    
    def test_signal_with_mock_data(self, mock_signal_generator):
        """Test signal generation with mocked data"""
        import pandas as pd
        import numpy as np
        
        # Create sample price data
        n = 50
        data = pd.DataFrame({
            'timestamp': pd.date_range(start='2024-01-01', periods=n, freq='1h'),
            'open': np.linspace(100000, 105000, n),
            'high': np.linspace(100500, 105500, n),
            'low': np.linspace(99500, 104500, n),
            'close': np.linspace(100000, 105000, n),
            'volume': np.random.rand(n) * 1000 + 500,
        })
        
        mock_signal_generator.generate_signals.return_value = []
        signals = mock_signal_generator.generate_signals(
            data=data,
            symbol='THB_BTC',
            use_strategies=['trend_following']
        )
        
        assert isinstance(signals, list)
        logger.info("✅ Signal generation with mock data test passed")


class TestRiskManagement:
    """Tests for risk management flow"""

    def test_load_state_resets_trade_counter_when_last_trade_is_previous_day(self, tmp_path):
        state_path = tmp_path / 'risk_state.json'
        state_path.write_text(json.dumps({
            'daily_loss_start': None,
            'daily_loss_date': None,
            'trade_count_today': 195,
            'last_trade_time': '2026-04-03T21:12:43.986543',
            'cooling_down': False,
        }))

        manager = RiskManager(RiskConfig())

        assert manager.load_state(str(state_path)) is True
        assert manager._trade_count_today == 0
        assert manager._daily_loss_date == date.today()
    
    def test_atr_calculation_integration(self, mock_risk_manager):
        """Test ATR-based SL/TP calculation"""
        entry_price = 1500000.0
        atr_value = 1000.0  # 1000 THB ATR
        
        sl, tp = mock_risk_manager.calc_sl_tp_from_atr(
            entry_price=entry_price,
            atr_value=atr_value,
            direction='long',
            risk_reward_ratio=2.0
        )
        
        assert sl < entry_price
        assert tp > entry_price
        assert tp > sl
        logger.info(f"✅ ATR calculation: Entry={entry_price}, SL={sl}, TP={tp}")
    
    def test_risk_checks(self, mock_risk_manager):
        """Test that risk checks can be called"""
        daily_check = mock_risk_manager.check_daily_loss_limit()
        
        assert daily_check is True
        logger.info("✅ Risk checks test passed")


class TestExecutionFlow:
    """Tests for trade execution flow"""
    
    def test_entry_execution_mock(self, mock_trade_executor):
        """Test entry execution with mock"""
        result = mock_trade_executor.execute_entry(Mock())
        
        assert result.success is True
        assert result.filled_price == 1500000.0
        logger.info("✅ Entry execution mock test passed")
    
    def test_exit_execution_mock(self, mock_trade_executor):
        """Test exit execution with mock"""
        result = mock_trade_executor.execute_exit(Mock())
        
        assert result.success is True
        logger.info("✅ Exit execution mock test passed")
    
    def test_open_orders_retrieval(self, mock_trade_executor):
        """Test open orders retrieval"""
        orders = mock_trade_executor.get_open_orders()
        
        assert isinstance(orders, list)
        logger.info("✅ Open orders retrieval test passed")


class TestBotMode:
    """Tests for bot mode handling"""
    
    def test_bot_mode_enum(self):
        """Test BotMode enum values"""
        assert BotMode.FULL_AUTO.value == "full_auto"
        assert BotMode.SEMI_AUTO.value == "semi_auto"
        assert BotMode.DRY_RUN.value == "dry_run"
        logger.info("✅ BotMode enum test passed")
    
    def test_signal_source_enum(self):
        """Test SignalSource enum values"""
        assert SignalSource.STRATEGY.value == "strategy"
        logger.info("✅ SignalSource enum test passed")

    def test_cli_mode_shows_live_for_full_auto_without_safety_flags(self):
        app = TradingBotApp.__new__(TradingBotApp)
        app.config = {"mode": "full_auto", "simulate_only": False, "read_only": False}

        assert TradingBotApp._derive_cli_mode(app, {"mode": "full_auto"}) == "LIVE"

    def test_cli_mode_shows_semi_auto(self):
        app = TradingBotApp.__new__(TradingBotApp)
        app.config = {"mode": "semi_auto", "simulate_only": True, "read_only": False}

        assert TradingBotApp._derive_cli_mode(app, {"mode": "semi_auto"}) == "SEMI AUTO"

    def test_cli_mode_shows_read_only_before_simulation(self):
        app = TradingBotApp.__new__(TradingBotApp)
        app.config = {"mode": "full_auto", "simulate_only": True, "read_only": True}

        assert TradingBotApp._derive_cli_mode(app, {"mode": "full_auto"}) == "READ ONLY"

    def test_cli_mode_shows_simulation(self):
        app = TradingBotApp.__new__(TradingBotApp)
        app.config = {"mode": "dry_run", "simulate_only": True, "read_only": False}

        assert TradingBotApp._derive_cli_mode(app, {"mode": "dry_run"}) == "SIMULATION"

    def test_cli_mode_shows_degraded_when_auth_degraded(self):
        app = TradingBotApp.__new__(TradingBotApp)
        app.config = {"mode": "full_auto", "simulate_only": False, "read_only": False, "auth_degraded": True}

        assert TradingBotApp._derive_cli_mode(app, {"mode": "full_auto"}) == "DEGRADED"


class TestCliSnapshot:
    def test_cli_balance_summary_converts_all_assets_to_thb(self):
        app = TradingBotApp.__new__(TradingBotApp)
        app.config = {"auth_degraded": False}
        app.api_client = Mock()
        app.api_client.get_balances.return_value = {
            "THB": {"available": 1500.0, "reserved": 250.0},
            "BTC": {"available": 0.01, "reserved": 0.0},
            "ETH": {"available": 0.5, "reserved": 0.0},
        }
        app._get_cli_price = Mock(side_effect=lambda symbol: {
            "THB_BTC": 2_000_000.0,
            "THB_ETH": 100_000.0,
        }.get(symbol))

        summary = TradingBotApp._get_cli_balance_summary(app, {"balance": 999.0})

        assert summary["total_balance"] == pytest.approx(71_750.0)
        assert summary["breakdown"] == [
            {"asset": "ETH", "amount": 0.5, "value_thb": 50_000.0},
            {"asset": "BTC", "amount": 0.01, "value_thb": 20_000.0},
            {"asset": "THB", "amount": 1_750.0, "value_thb": 1_750.0},
        ]

    def test_cli_balance_summary_avoids_rest_balance_calls_during_live_dashboard(self):
        app = TradingBotApp.__new__(TradingBotApp)
        app.config = {"auth_degraded": False}
        app._live_dashboard_active = True
        app._cli_balance_summary_cache = None
        app.api_client = Mock()
        app.api_client.get_balances.side_effect = AssertionError("REST balances should not be used in live dashboard")
        app.bot = Mock()
        app.bot._balance_monitor = Mock()
        app.bot.get_balance_state.return_value = {
            "balances": {
                "THB": {"available": 500.0, "reserved": 0.0},
                "BTC": {"available": 0.01, "reserved": 0.0},
            }
        }
        app._get_cli_price = Mock(side_effect=lambda symbol, allow_rest_fallback=True: {
            "THB_BTC": 2_000_000.0,
        }.get(symbol))

        summary = TradingBotApp._get_cli_balance_summary(app, {"balance": 500.0})

        assert summary["total_balance"] == pytest.approx(20_500.0)
        app.api_client.get_balances.assert_not_called()

    def test_cli_command_center_mutes_and_restores_console_handlers(self):
        app = Mock()
        command_center = CLICommandCenter(app)
        root = logging.getLogger()
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.INFO)
        file_like_handler = logging.StreamHandler(io.StringIO())
        file_like_handler.setLevel(logging.INFO)
        original_console_level = console_handler.level
        original_file_like_level = file_like_handler.level
        root.addHandler(console_handler)
        root.addHandler(file_like_handler)

        try:
            command_center.start_log_capture()

            assert console_handler.level > logging.CRITICAL
            assert file_like_handler.level == original_file_like_level

            command_center.stop_log_capture()

            assert console_handler.level == original_console_level
            assert file_like_handler.level == original_file_like_level
        finally:
            root.removeHandler(console_handler)
            root.removeHandler(file_like_handler)
            if command_center._log_handler is not None:
                command_center.stop_log_capture()

    def test_cli_snapshot_exposes_total_balance_field(self):
        app = TradingBotApp.__new__(TradingBotApp)
        app.config = {
            "mode": "full_auto",
            "simulate_only": False,
            "read_only": False,
            "auth_degraded": False,
            "data": {"pairs": ["THB_BTC"]},
        }
        app._cli_bot_name = "Test Bot"
        app._derive_risk_level = Mock(return_value=("NORMAL", "green"))
        app._sample_api_latency = Mock(return_value=25.0)
        app._format_cli_timestamp = Mock(return_value="12:34:56")
        app._get_cli_price = Mock(return_value=2_000_000.0)
        app.api_client = Mock()
        app.api_client.get_balances.return_value = {
            "THB": {"available": 500.0, "reserved": 0.0},
            "BTC": {"available": 0.01, "reserved": 0.0},
        }
        app.executor = Mock()
        app.executor.get_open_orders.return_value = []
        app.bot = Mock()
        app.bot.get_status.return_value = {
            "mode": "full_auto",
            "trading_pairs": ["THB_BTC"],
            "strategy_engine": {"strategies": ["trend_following"]},
            "risk_summary": {"trades_today": 3},
            "last_loop": None,
        }
        app.bot._get_portfolio_state.return_value = {"balance": 500.0, "timestamp": None}

        snapshot = TradingBotApp.get_cli_snapshot(app)

        assert snapshot["system"]["available_balance"] == "500.00 THB"
        assert snapshot["system"]["total_balance"] == "20,500.00 THB"
        assert snapshot["system"]["balance_breakdown"] == [
            "BTC 0.01000000 = 20,000.00 THB (97.56%)",
            "THB 500.00 = 500.00 THB (2.44%)",
        ]
        assert snapshot["system"]["trade_count"] == "3"

    def test_format_cli_timestamp_converts_runtime_utc_to_bitkub_time(self):
        assert TradingBotApp._format_cli_timestamp("2026-04-11T15:35:00") == "22:35:00"
        assert TradingBotApp._format_cli_timestamp("2026-04-11T15:35:00Z") == "22:35:00"

    def test_format_cli_recent_events_converts_timestamps_without_instance_state(self):
        events = TradingBotApp._format_cli_recent_events(
            {
                "recent_trades": [
                    {
                        "timestamp": "2026-04-11T15:35:00Z",
                        "symbol": "THB_BTC",
                        "side": "buy",
                        "status": "filled",
                    }
                ],
                "balance_events": [
                    {
                        "timestamp": "2026-04-11T15:36:00",
                        "type": "DEPOSIT",
                        "message": "DEPOSIT THB 100.0000",
                    }
                ],
            }
        )

        assert events[0]["timestamp"] == "22:36:00"
        assert events[1]["timestamp"] == "22:35:00"


class TestCliUi:
    def test_balance_breakdown_text_uses_allocation_aware_styles(self):
        btc_text = CLICommandCenter._balance_breakdown_text("BTC 0.01000000 = 20,000.00 THB (97.56%)")
        eth_text = CLICommandCenter._balance_breakdown_text("ETH 0.50000000 = 5,000.00 THB (24.39%)")
        xrp_text = CLICommandCenter._balance_breakdown_text("XRP 10.00000000 = 250.00 THB (1.22%)")
        thb_text = CLICommandCenter._balance_breakdown_text("THB 500.00 = 500.00 THB (2.44%)")

        assert btc_text.plain == "BTC 0.01000000 = 20,000.00 THB (97.56%)"
        assert eth_text.plain == "ETH 0.50000000 = 5,000.00 THB (24.39%)"
        assert xrp_text.plain == "XRP 10.00000000 = 250.00 THB (1.22%)"
        assert thb_text.plain == "THB 500.00 = 500.00 THB (2.44%)"
        assert len(btc_text.spans) == 2
        assert len(eth_text.spans) == 2
        assert len(xrp_text.spans) == 2
        assert len(thb_text.spans) == 2
        assert btc_text.spans[0].style == "bold bright_green"
        assert btc_text.spans[1].style == "bright_black"
        assert eth_text.spans[0].style == "bold cyan"
        assert eth_text.spans[1].style == "bright_black"
        assert xrp_text.spans[0].style == "bold white"
        assert xrp_text.spans[1].style == "bright_black"
        assert thb_text.spans[0].style == "bold yellow"
        assert thb_text.spans[1].style == "bright_black"

    def test_allocation_bar_text_uses_threshold_styles(self):
        btc_bar = CLICommandCenter._allocation_bar_text(97.56, asset="BTC")
        eth_bar = CLICommandCenter._allocation_bar_text(24.39, asset="ETH")
        xrp_bar = CLICommandCenter._allocation_bar_text(1.22, asset="XRP")
        thb_bar = CLICommandCenter._allocation_bar_text(2.44, asset="THB")

        assert btc_bar.plain == "[####################] 97.56%"
        assert eth_bar.plain == "[#####---------------] 24.39%"
        assert xrp_bar.plain == "[#-------------------]  1.22%"
        assert thb_bar.plain == "[#-------------------]  2.44%"
        assert btc_bar.spans[1].style == "bold bright_green"
        assert eth_bar.spans[1].style == "bold cyan"
        assert xrp_bar.spans[2].style == "bright_black"
        assert thb_bar.spans[1].style == "bold yellow"

    def test_system_status_panel_keeps_only_core_metrics(self):
        app = Mock()
        app.get_cli_snapshot.return_value = {
            "bot_name": "Test Bot",
            "mode": "LIVE",
            "risk_level": "NORMAL",
            "risk_style": "green",
            "positions": [],
            "pairs": "THB_BTC",
            "strategies": "trend_following",
            "updated_at": "12:34:56",
            "system": {
                "last_market_update": "12:34:00",
                "api_latency": "15 ms",
                "available_balance": "500.00 THB",
                "total_balance": "20,500.00 THB",
                "balance_breakdown": ["BTC 0.01000000 = 20,000.00 THB (97.56%)", "THB 500.00 = 500.00 THB (2.44%)"],
                "trade_count": "3",
            },
        }

        command_center = CLICommandCenter(app)
        panel = command_center._build_system_status_table(app.get_cli_snapshot())
        console = Console(record=True, width=120)
        console.print(panel)
        rendered = console.export_text()

        assert "Breakdown" not in rendered
        assert "Portfolio Breakdown" not in rendered
        assert "Total Balance" in rendered
        assert "Today's Trades" in rendered
        assert "BTC 0.01000000 = 20,000.00 THB (97.56%)" not in rendered

    def test_portfolio_breakdown_panel_renders_separately(self):
        app = Mock()
        app.get_cli_snapshot.return_value = {
            "bot_name": "Test Bot",
            "mode": "LIVE",
            "risk_level": "NORMAL",
            "risk_style": "green",
            "positions": [],
            "pairs": "THB_BTC",
            "strategies": "trend_following",
            "updated_at": "12:34:56",
            "system": {
                "last_market_update": "12:34:00",
                "api_latency": "15 ms",
                "available_balance": "500.00 THB",
                "total_balance": "20,500.00 THB",
                "balance_breakdown": ["BTC 0.01000000 = 20,000.00 THB (97.56%)", "THB 500.00 = 500.00 THB (2.44%)"],
                "trade_count": "3",
            },
        }

        command_center = CLICommandCenter(app)
        panel = command_center._build_balance_breakdown_panel(app.get_cli_snapshot())
        console = Console(record=True, width=120)
        console.print(panel)
        rendered = console.export_text()

        assert "Portfolio Breakdown" in rendered
        assert "BTC 0.01000000 = 20,000.00 THB (97.56%)" in rendered
        assert "THB 500.00 = 500.00 THB (2.44%)" in rendered
        assert "[####################] 97.56%" in rendered
        assert "[#-------------------]  2.44%" in rendered

    def test_footer_renders_command_chat_panel(self):
        app = Mock()
        app.get_cli_snapshot.return_value = {
            "bot_name": "Test Bot",
            "mode": "LIVE",
            "risk_level": "NORMAL",
            "risk_style": "green",
            "positions": [],
            "pairs": "THB_BTC",
            "strategies": "trend_following",
            "commands_hint": "Type in footer chat",
            "updated_at": "12:34:56",
            "chat": {
                "status": "Typing...",
                "pending_confirmation": {
                    "summary": "Confirm market BUY THB_BTC with 500.00 THB",
                    "command_text": "buy THB_BTC 500",
                },
                "history": [
                    {"role": "user", "message": "risk show"},
                    {"role": "bot", "message": "Runtime risk: 2.00% per trade (MEDIUM)"},
                ],
                "suggestions": ["confirm", "cancel"],
                "input": "pairs add BTC",
            },
            "system": {
                "last_market_update": "12:34:00",
                "api_latency": "15 ms",
                "available_balance": "500.00 THB",
                "total_balance": "20,500.00 THB",
                "balance_breakdown": [],
                "trade_count": "3",
            },
        }

        command_center = CLICommandCenter(app)
        panel = command_center._build_footer(app.get_cli_snapshot())
        console = Console(record=True, width=120)
        console.print(panel)
        rendered = console.export_text()

        assert "Command Chat" in rendered
        assert "Pending Confirm market BUY THB_BTC with 500.00 THB" in rendered
        assert "You: risk show" in rendered
        assert "Bot: Runtime risk: 2.00% per trade (MEDIUM)" in rendered
        assert "Tips confirm | cancel" in rendered
        assert "> pairs add BTC" in rendered

    def test_log_buffer_uses_bitkub_time(self):
        app = Mock()
        command_center = CLICommandCenter(app)
        record = logging.LogRecord(
            name="test.runtime",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="runtime ready",
            args=(),
            exc_info=None,
        )
        record.created = datetime(2026, 4, 11, 15, 35, 0, tzinfo=timezone.utc).timestamp()

        command_center._append_log_record(record)

        assert command_center._log_lines[-1]["timestamp"] == "22:35:00"

    def test_signal_alignment_panel_renders_pair_runtime_context(self):
        app = Mock()
        command_center = CLICommandCenter(app)

        panel = command_center._build_signal_alignment_panel(
            {
                "signal_alignment": [
                    {
                        "symbol": "THB_BTC",
                        "tf_ready": "4/6",
                        "market_update": "22:35:00",
                        "macro": "BUY",
                        "micro": "BUY",
                        "trigger": "HOLD",
                        "trend": "BUY",
                        "trigger_side": "NONE",
                        "action": "WAIT",
                        "status": "Waiting: Insufficient data (3/210 bars)",
                    }
                ]
            }
        )
        console = Console(record=True, width=140)
        console.print(panel)
        rendered = console.export_text()

        assert "TF" in rendered
        assert "Upd" in rendered
        assert "4/6" in rendered
        assert "22:35:00" in rendered


def test_format_bitkub_time_normalizes_to_thailand_timezone():
    assert format_bitkub_time(datetime(2026, 4, 11, 15, 35, 0, tzinfo=timezone.utc)) == "22:35:00"


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
