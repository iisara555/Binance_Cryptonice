import logging
from unittest.mock import Mock, patch

import pytest

from alerts import AlertLevel
from api_client import BinanceAPIError, BinanceAuthException, BinanceThClient
from portfolio_manager import PortfolioManager
from trading_bot import TradingBotOrchestrator


def test_unsuppressed_binance_auth_error_raises_auth_exception_without_global_shutdown():
    import api_client as api_module

    api_module.SHOULD_SHUTDOWN = False
    api_module.SHUTDOWN_REASON = ''

    client = BinanceThClient(api_key='key', api_secret='secret', base_url='https://example.invalid')
    client.check_clock_sync = Mock(return_value=True)
    client.auth_fatal_threshold = 1
    client.signed_transient_2015_http_retries = 1

    response = Mock()
    response.status_code = 401
    response.text = '{"code":-2015,"msg":"Invalid API-key"}'
    response.json.return_value = {'code': -2015, 'msg': 'Invalid API-key'}

    with patch('api_client.requests.request', return_value=response):
        with pytest.raises(BinanceAuthException) as exc_info:
            client.get_balances()

    assert exc_info.value.code == -2015
    assert api_module.SHOULD_SHUTDOWN is False
    assert api_module.SHUTDOWN_REASON == ''


def test_transient_binance_auth_errors_below_threshold_do_not_escalate():
    """Up to threshold-1 consecutive -2015 errors must surface as plain
    BinanceAPIError (not BinanceAuthException) and must not trip the global
    circuit breaker. This tolerates Binance.th IP-whitelist propagation lag."""

    client = BinanceThClient(api_key='key', api_secret='secret', base_url='https://example.invalid')
    client.check_clock_sync = Mock(return_value=True)
    client.auth_fatal_threshold = 5
    client.signed_transient_2015_http_retries = 1

    response = Mock()
    response.status_code = 401
    response.text = '{"code":-2015,"msg":"Invalid API-key"}'
    response.json.return_value = {'code': -2015, 'msg': 'Invalid API-key'}

    with patch('api_client.requests.request', return_value=response):
        for attempt in range(client.auth_fatal_threshold - 1):
            with pytest.raises(BinanceAPIError) as exc_info:
                client.get_balances(force_refresh=True, allow_stale=False)
            assert exc_info.value.code == -2015
            assert not isinstance(exc_info.value, BinanceAuthException), (
                f"attempt {attempt + 1}/{client.auth_fatal_threshold} should be transient"
            )

        assert client._cb.state == 'closed', (
            'Circuit breaker should remain CLOSED while tolerating transient auth errors'
        )

        with pytest.raises(BinanceAuthException) as exc_info:
            client.get_balances(force_refresh=True, allow_stale=False)
        assert exc_info.value.code == -2015


def test_signed_request_retries_on_2015_before_success():
    """HTTP 401 + -2015 on first attempt should transparently retry signed GET."""

    client = BinanceThClient(api_key='key', api_secret='secret', base_url='https://example.invalid')
    client.check_clock_sync = Mock(return_value=True)
    client.signed_transient_2015_http_retries = 3

    fail = Mock()
    fail.status_code = 401
    fail.text = '{"code":-2015,"msg":"Invalid API-key"}'
    fail.json.return_value = {'code': -2015, 'msg': 'Invalid API-key'}

    ok = Mock()
    ok.status_code = 200
    ok.text = '{"balances":[]}'
    ok.json.return_value = {'balances': []}

    with patch('api_client.requests.request', side_effect=[fail, ok]) as req:
        out = client.get_balances(force_refresh=True, allow_stale=False)

    assert req.call_count == 2
    assert out == {}


def test_signed_success_resets_consecutive_auth_failure_counter():
    """A successful signed response must reset the consecutive auth-failure
    counter so subsequent transient errors restart the tolerance window."""

    client = BinanceThClient(api_key='key', api_secret='secret', base_url='https://example.invalid')
    client.check_clock_sync = Mock(return_value=True)
    client.auth_fatal_threshold = 3
    client.signed_transient_2015_http_retries = 1

    fail_response = Mock()
    fail_response.status_code = 401
    fail_response.text = '{"code":-2015,"msg":"Invalid API-key"}'
    fail_response.json.return_value = {'code': -2015, 'msg': 'Invalid API-key'}

    ok_response = Mock()
    ok_response.status_code = 200
    ok_response.text = '{"balances":[]}'
    ok_response.json.return_value = {'balances': []}

    with patch('api_client.requests.request', side_effect=[fail_response, ok_response, fail_response]):
        with pytest.raises(BinanceAPIError):
            client.get_balances(force_refresh=True, allow_stale=False)
        assert client._consecutive_auth_failures == 1

        client.get_balances(force_refresh=True, allow_stale=False)
        assert client._consecutive_auth_failures == 0

        with pytest.raises(BinanceAPIError):
            client.get_balances(force_refresh=True, allow_stale=False)
        assert client._consecutive_auth_failures == 1


def test_main_loop_stops_gracefully_on_fatal_auth_exception():
    bot = TradingBotOrchestrator.__new__(TradingBotOrchestrator)
    bot.running = True
    bot.interval_seconds = 60
    bot._last_loop_time = None
    bot._loop_count = 0
    bot._maybe_run_candle_retention_cleanup = Mock()
    bot._maybe_run_db_maintenance = Mock()
    bot._run_iteration = Mock(side_effect=BinanceAuthException(-2015, 'fatal auth failure'))

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
    bot._maybe_run_db_maintenance = Mock()
    bot._run_iteration = Mock(side_effect=BinanceAuthException(-2015, 'fatal auth failure <bad>'))
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