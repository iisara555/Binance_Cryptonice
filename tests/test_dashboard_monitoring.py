import logging
import socket
from urllib.request import urlopen
from unittest.mock import Mock

from health_server import BotHealthServer
from monitoring import MonitoringService, ReconciliationState


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_health_server_serves_metrics_on_base_and_global_paths():
    port = _get_free_port()
    server = BotHealthServer(
        host="127.0.0.1",
        port=port,
        path="/api/health",
        status_provider=lambda: {"healthy": True, "status": "ok"},
    )

    try:
        assert server.start() is True
        global_response = urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5)
        base_response = urlopen(f"http://127.0.0.1:{port}/api/health/metrics", timeout=5)

        assert global_response.status == 200
        assert base_response.status == 200
        assert global_response.headers.get_content_type() == "text/plain"
        assert base_response.headers.get_content_type() == "text/plain"
        assert global_response.read() == base_response.read()
    finally:
        server.stop()


def test_monitoring_api_health_logs_warning_on_failure(caplog):
    bot_ref = Mock()
    api_client = Mock()
    executor = Mock()
    api_client.get_ticker.side_effect = RuntimeError("offline")
    service = MonitoringService(bot_ref, api_client, executor, config={})

    with caplog.at_level(logging.WARNING):
        assert service._check_api_health() is False

    assert "API health check failed: offline" in caplog.text


def test_monitoring_get_status_logs_circuit_breaker_read_failure(caplog):
    bot_ref = Mock()
    api_client = Mock()
    executor = Mock()

    class BrokenCircuitBreaker:
        @property
        def state(self):
            raise RuntimeError("cb unavailable")

    api_client.circuit_breaker = BrokenCircuitBreaker()
    service = MonitoringService(bot_ref, api_client, executor, config={})

    with caplog.at_level(logging.WARNING):
        status = service.get_status()

    assert status["circuit_breaker"]["state"] == "unknown"
    assert "Failed to read circuit breaker status: cb unavailable" in caplog.text


def test_reconciliation_detects_exchange_only_orders_and_pauses():
    reconciler = ReconciliationState()
    executor = Mock()
    api_client = Mock()
    executor.get_open_orders.return_value = [{"order_id": "bot-1", "symbol": "THB_BTC"}]
    api_client.get_open_orders.return_value = [
        {"id": "bot-1", "sym": "btc_thb"},
        {"id": "ghost-2", "_checked_symbol": "THB_BTC"},
    ]

    reconciler.check_positions(executor, api_client)

    paused, reason = reconciler.is_paused()
    assert paused is True
    assert "Exchange-only open orders detected" in reason
    assert any("ghost-2" in issue for issue in reconciler.get_issues())


def test_reconciliation_resumes_after_discrepancy_clears():
    reconciler = ReconciliationState()
    executor = Mock()
    api_client = Mock()
    executor.get_open_orders.return_value = [{"order_id": "bot-1", "symbol": "THB_BTC"}]
    api_client.get_open_orders.return_value = [{"id": "ghost-2", "_checked_symbol": "THB_BTC"}]

    reconciler.check_positions(executor, api_client)
    assert reconciler.is_paused()[0] is True

    api_client.get_open_orders.return_value = [{"id": "bot-1", "sym": "btc_thb"}]
    reconciler.check_positions(executor, api_client)

    assert reconciler.is_paused() == (False, "")
    assert reconciler.get_issues() == []