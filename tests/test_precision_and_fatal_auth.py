import logging
from unittest.mock import Mock, patch

import pytest

from alerts import AlertLevel
from api_client import BitkubClient, FatalAuthException
from portfolio_manager import PortfolioManager
from trading_bot import TradingBotOrchestrator


def test_unsuppressed_auth_error_5_raises_fatal_auth_exception_without_global_shutdown():
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
        with pytest.raises(FatalAuthException) as exc_info:
            client.get_balances()

    assert exc_info.value.code == 5
    assert api_module.SHOULD_SHUTDOWN is False
    assert api_module.SHUTDOWN_REASON == ''


def test_main_loop_stops_gracefully_on_fatal_auth_exception():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.running = True
    bot.interval_seconds = 60
    bot._last_loop_time = None
    bot._loop_count = 0
    bot._maybe_run_candle_retention_cleanup = Mock()
    bot._run_iteration = Mock(side_effect=FatalAuthException(5, 'fatal auth failure'))

    with patch('trading_bot.time.sleep'):
        TradingBotOrchestrator._main_loop(bot)

    assert bot.running is False


def test_main_loop_routes_fatal_auth_alert_through_alert_system_not_api_layer():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.running = True
    bot.interval_seconds = 60
    bot._last_loop_time = None
    bot._loop_count = 0
    bot._maybe_run_candle_retention_cleanup = Mock()
    bot._run_iteration = Mock(side_effect=FatalAuthException(5, 'fatal auth failure <bad>'))
    bot.alert_system = Mock()

    with patch('trading_bot.time.sleep'):
        TradingBotOrchestrator._main_loop(bot)

    bot.alert_system.send.assert_called_once()
    assert bot.alert_system.send.call_args.args[0] == AlertLevel.CRITICAL
    assert 'fatal auth failure &lt;bad&gt;' in bot.alert_system.send.call_args.args[1]
    assert 'fatal auth failure <bad>' not in bot.alert_system.send.call_args.args[1]


def test_portfolio_manager_repeated_fee_updates_do_not_accumulate_float_drift():
    pm = PortfolioManager(initial_balance=1000.0)

    for _ in range(10):
        pm.open_position(
            symbol='THB_BTC',
            side='long',
            entry_price=1.0,
            quantity=1.0,
            stop_loss=0.9,
            take_profit=1.1,
        )
        pm.close_position('THB_BTC', exit_price=1.0, reason='manual', entry_fee=0.1, exit_fee=0.2)

    assert pm.current_balance == pytest.approx(997.0)
    assert pm.get_summary_with_formatting()['current_balance_formatted'] == '997.00'