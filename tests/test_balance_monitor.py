import threading
from unittest.mock import Mock

import pytest

from alerts import AlertLevel
from balance_monitor import BalanceMonitor, _MonitorStopping


def _build_monitor(tmp_path, alert_system=None, config=None, on_event=None):
    api_client = Mock()
    api_client.get_balances.return_value = {}
    api_client.get_fiat_deposit_history.return_value = []
    api_client.get_fiat_withdraw_history.return_value = []
    api_client.get_crypto_deposit_history.return_value = {"items": []}
    api_client.get_crypto_withdraw_history.return_value = {"items": []}
    monitor_config = {"persist_path": str(tmp_path / "balance-monitor-state.json")}
    monitor_config.update(config or {})
    return BalanceMonitor(api_client, monitor_config, alert_system=alert_system or Mock(), on_event=on_event)


def test_detect_fiat_deposit_and_withdrawal_events(tmp_path):
    alert_system = Mock()
    monitor = _build_monitor(tmp_path, alert_system=alert_system)

    balances = {"USDT": {"available": 12_500.0, "reserved": 0.0, "total": 12_500.0}}
    deposits = [{"txn_id": "dep-1", "status": "complete", "amount": "5000", "time": "2024-01-01T10:00:00Z"}]
    withdrawals = [{"txn_id": "wd-1", "status": "complete", "amount": "1500", "time": "2024-01-01T11:00:00Z"}]

    events = monitor._detect_fiat_events(deposits, withdrawals, balances)

    assert [event.event_type for event in events] == ["DEPOSIT", "WITHDRAWAL"]
    assert [event.coin for event in events] == ["USDT", "USDT"]
    assert [event.amount for event in events] == [5000.0, 1500.0]
    assert all(event.balance == 12_500.0 for event in events)

    repeat_events = monitor._detect_fiat_events(deposits, withdrawals, balances)
    assert repeat_events == []


def test_detect_crypto_withdrawal_lifecycle(tmp_path):
    monitor = _build_monitor(tmp_path)
    balances = {"BTC": {"available": 0.25, "reserved": 0.0, "total": 0.25}}

    pending = {
        "items": [
            {
                "txn_id": "crypto-wd-1",
                "symbol": "btc",
                "status": "processing",
                "amount": "0.05",
                "created_at": "2024-01-01T10:00:00Z",
            }
        ]
    }
    completed = {
        "items": [
            {
                "txn_id": "crypto-wd-1",
                "symbol": "btc",
                "status": "complete",
                "amount": "0.05",
                "created_at": "2024-01-01T10:00:00Z",
                "completed_at": "2024-01-01T10:10:00Z",
            }
        ]
    }

    pending_events = monitor._detect_crypto_events({"items": []}, pending, balances)
    completed_events = monitor._detect_crypto_events({"items": []}, completed, balances)
    repeated_completion = monitor._detect_crypto_events({"items": []}, completed, balances)

    assert [event.event_type for event in pending_events] == ["WITHDRAWAL_INITIATED"]
    assert pending_events[0].coin == "BTC"
    assert pending_events[0].amount == 0.05

    assert [event.event_type for event in completed_events] == ["WITHDRAWAL_COMPLETED"]
    assert completed_events[0].coin == "BTC"
    assert completed_events[0].status == "complete"
    assert repeated_completion == []


def test_first_poll_bootstraps_existing_history_without_emitting_events(tmp_path):
    monitor = _build_monitor(tmp_path)
    monitor.api_client.get_balances.return_value = {
        "USDT": {"available": 100.0, "reserved": 0.0, "total": 100.0},
        "BTC": {"available": 0.25, "reserved": 0.0, "total": 0.25},
    }
    monitor.api_client.get_fiat_deposit_history.return_value = [
        {"txn_id": "dep-old", "status": "complete", "amount": "2000", "time": "2022-01-01T00:00:00Z"}
    ]
    monitor.api_client.get_fiat_withdraw_history.return_value = [
        {"txn_id": "wd-old", "status": "complete", "amount": "500", "time": "2022-01-02T00:00:00Z"}
    ]
    monitor.api_client.get_crypto_deposit_history.return_value = {
        "items": [
            {
                "txn_id": "crypto-dep-old",
                "symbol": "btc",
                "status": "complete",
                "amount": "0.01",
                "completed_at": "2022-01-03T00:00:00Z",
            }
        ]
    }
    monitor.api_client.get_crypto_withdraw_history.return_value = {
        "items": [
            {
                "txn_id": "crypto-wd-old",
                "symbol": "btc",
                "status": "complete",
                "amount": "0.02",
                "completed_at": "2022-01-04T00:00:00Z",
            }
        ]
    }

    events = monitor.poll_once()

    assert events == []
    assert monitor._history_bootstrapped is True
    assert "dep-old" in monitor._seen_fiat_deposits
    assert "wd-old" in monitor._seen_fiat_withdrawals
    assert "crypto-dep-old" in monitor._seen_crypto_deposits
    assert monitor._crypto_withdraw_status["crypto-wd-old"] == "complete"


def test_second_poll_emits_only_new_history_after_bootstrap(tmp_path):
    monitor = _build_monitor(tmp_path)
    monitor.api_client.get_balances.return_value = {
        "USDT": {"available": 500.0, "reserved": 0.0, "total": 500.0}
    }
    monitor.api_client.get_crypto_deposit_history.return_value = {"items": []}
    monitor.api_client.get_crypto_withdraw_history.return_value = {"items": []}

    monitor.api_client.get_fiat_deposit_history.return_value = [
        {"txn_id": "dep-old", "status": "complete", "amount": "1000", "time": "2022-01-01T00:00:00Z"}
    ]
    monitor.api_client.get_fiat_withdraw_history.return_value = [
        {"txn_id": "wd-old", "status": "complete", "amount": "250", "time": "2022-01-02T00:00:00Z"}
    ]

    assert monitor.poll_once() == []

    monitor.api_client.get_fiat_deposit_history.return_value = [
        {"txn_id": "dep-old", "status": "complete", "amount": "1000", "time": "2022-01-01T00:00:00Z"},
        {"txn_id": "dep-new", "status": "complete", "amount": "200", "time": "2024-01-03T00:00:00Z"},
    ]
    monitor.api_client.get_fiat_withdraw_history.return_value = [
        {"txn_id": "wd-old", "status": "complete", "amount": "250", "time": "2022-01-02T00:00:00Z"},
        {"txn_id": "wd-new", "status": "complete", "amount": "50", "time": "2024-01-04T00:00:00Z"},
    ]

    events = monitor.poll_once()

    assert [event.transaction_id for event in events] == ["dep-new", "wd-new"]
    assert [event.event_type for event in events] == ["DEPOSIT", "WITHDRAWAL"]


def test_first_poll_can_emit_existing_history_when_bootstrap_disabled(tmp_path):
    monitor = _build_monitor(tmp_path, config={"bootstrap_history_on_startup": False})
    monitor.api_client.get_balances.return_value = {
        "USDT": {"available": 500.0, "reserved": 0.0, "total": 500.0}
    }
    monitor.api_client.get_fiat_deposit_history.return_value = [
        {"txn_id": "dep-old", "status": "complete", "amount": "1000", "time": "2022-01-01T00:00:00Z"}
    ]
    monitor.api_client.get_fiat_withdraw_history.return_value = [
        {"txn_id": "wd-old", "status": "complete", "amount": "250", "time": "2022-01-02T00:00:00Z"}
    ]
    monitor.api_client.get_crypto_deposit_history.return_value = {"items": []}
    monitor.api_client.get_crypto_withdraw_history.return_value = {"items": []}

    events = monitor.poll_once()

    assert [event.transaction_id for event in events] == ["dep-old", "wd-old"]
    assert monitor._history_bootstrapped is True


def test_threshold_alerts_only_repeat_after_recovery(tmp_path):
    alert_system = Mock()
    monitor = _build_monitor(
        tmp_path,
        alert_system=alert_system,
        config={
            "quote_min_threshold": 500.0,
            "coin_min_thresholds": {"BTC": 0.01},
        },
    )

    low_balances = {
        "USDT": {"available": 100.0, "reserved": 0.0, "total": 100.0},
        "BTC": {"available": 0.001, "reserved": 0.0, "total": 0.001},
    }
    healthy_balances = {
        "USDT": {"available": 1_000.0, "reserved": 0.0, "total": 1_000.0},
        "BTC": {"available": 0.02, "reserved": 0.0, "total": 0.02},
    }

    monitor._check_thresholds(low_balances)
    monitor._check_thresholds(low_balances)
    monitor._check_thresholds(healthy_balances)
    monitor._check_thresholds(low_balances)

    critical_calls = [call for call in alert_system.send.call_args_list if call.args[0] == AlertLevel.CRITICAL]
    assert len(critical_calls) == 4


def test_stop_requested_aborts_retry_without_critical_alert(tmp_path):
    alert_system = Mock()
    monitor = _build_monitor(tmp_path, alert_system=alert_system)

    attempt_count = {"value": 0}

    def fail_and_request_stop():
        attempt_count["value"] += 1
        monitor._stop_event.set()
        raise RuntimeError("simulated api failure")

    with pytest.raises(_MonitorStopping):
        monitor._call_with_retries(fail_and_request_stop, "market.balances")

    assert attempt_count["value"] == 1
    critical_calls = [call for call in alert_system.send.call_args_list if call.args[0] == AlertLevel.CRITICAL]
    assert critical_calls == []


def test_poll_once_stops_dispatch_after_callback_requests_shutdown(tmp_path):
    seen_events = []

    def on_event(event, state):
        seen_events.append((event.transaction_id, state["balances"]["USDT"]["available"]))
        monitor.stop()

    monitor = _build_monitor(tmp_path, on_event=on_event, config={"bootstrap_history_on_startup": False})
    monitor.api_client.get_balances.return_value = {
        "USDT": {"available": 500.0, "reserved": 0.0, "total": 500.0}
    }
    monitor.api_client.get_fiat_deposit_history.return_value = [
        {"txn_id": "dep-1", "status": "complete", "amount": "100", "time": "2024-01-01T00:00:00Z"},
        {"txn_id": "dep-2", "status": "complete", "amount": "200", "time": "2024-01-01T00:01:00Z"},
    ]
    monitor.api_client.get_fiat_withdraw_history.return_value = []
    monitor.api_client.get_crypto_deposit_history.return_value = {"items": []}
    monitor.api_client.get_crypto_withdraw_history.return_value = {"items": []}

    events = monitor.poll_once()

    assert [event.transaction_id for event in events] == ["dep-1", "dep-2"]
    assert seen_events == [("dep-1", 500.0)]


def test_stop_clears_thread_handle_even_when_thread_is_not_alive(tmp_path):
    monitor = _build_monitor(tmp_path)
    monitor.running = True
    monitor._thread = Mock()
    monitor._thread.is_alive.return_value = False

    monitor.stop()

    assert monitor.running is False
    assert monitor._thread is None


def test_poll_once_uses_balance_monitor_timeout_profile(tmp_path):
    monitor = _build_monitor(tmp_path, config={"api_timeout_seconds": 7.5, "bootstrap_history_on_startup": False})

    monitor.poll_once()

    monitor.api_client.get_balances.assert_called_once_with(force_refresh=True, allow_stale=False, timeout=7.5)
    monitor.api_client.get_fiat_deposit_history.assert_called_once_with(limit=50, timeout=7.5)
    monitor.api_client.get_fiat_withdraw_history.assert_called_once_with(limit=50, timeout=7.5)
    monitor.api_client.get_crypto_deposit_history.assert_called_once_with(limit=100, timeout=7.5)
    monitor.api_client.get_crypto_withdraw_history.assert_called_once_with(limit=100, timeout=7.5)


def test_start_stop_joins_monitor_thread_and_clears_handle(tmp_path):
    entered_poll = threading.Event()
    release_poll = threading.Event()
    monitor = _build_monitor(tmp_path)

    def blocking_poll_once():
        entered_poll.set()
        release_poll.wait(timeout=1.0)
        return []

    monitor.poll_once = blocking_poll_once
    monitor.poll_interval_seconds = 5

    monitor.start()

    assert entered_poll.wait(timeout=1.0)
    release_poll.set()
    monitor.stop()

    assert monitor.running is False
    assert monitor._thread is None