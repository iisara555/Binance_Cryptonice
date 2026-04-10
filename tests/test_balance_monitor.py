from unittest.mock import Mock

import pytest

from alerts import AlertLevel
from balance_monitor import BalanceMonitor, _MonitorStopping


def _build_monitor(tmp_path, alert_system=None, config=None):
    api_client = Mock()
    api_client.get_balances.return_value = {}
    api_client.get_fiat_deposit_history.return_value = []
    api_client.get_fiat_withdraw_history.return_value = []
    api_client.get_crypto_deposit_history.return_value = {"items": []}
    api_client.get_crypto_withdraw_history.return_value = {"items": []}
    monitor_config = {"persist_path": str(tmp_path / "balance-monitor-state.json")}
    monitor_config.update(config or {})
    return BalanceMonitor(api_client, monitor_config, alert_system=alert_system or Mock())


def test_detect_fiat_deposit_and_withdrawal_events(tmp_path):
    alert_system = Mock()
    monitor = _build_monitor(tmp_path, alert_system=alert_system)

    balances = {"THB": {"available": 12_500.0, "reserved": 0.0, "total": 12_500.0}}
    deposits = [{"txn_id": "dep-1", "status": "complete", "amount": "5000", "time": "2024-01-01T10:00:00Z"}]
    withdrawals = [{"txn_id": "wd-1", "status": "complete", "amount": "1500", "time": "2024-01-01T11:00:00Z"}]

    events = monitor._detect_fiat_events(deposits, withdrawals, balances)

    assert [event.event_type for event in events] == ["DEPOSIT", "WITHDRAWAL"]
    assert [event.coin for event in events] == ["THB", "THB"]
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


def test_threshold_alerts_only_repeat_after_recovery(tmp_path):
    alert_system = Mock()
    monitor = _build_monitor(
        tmp_path,
        alert_system=alert_system,
        config={
            "thb_min_threshold": 500.0,
            "coin_min_thresholds": {"BTC": 0.01},
        },
    )

    low_balances = {
        "THB": {"available": 100.0, "reserved": 0.0, "total": 100.0},
        "BTC": {"available": 0.001, "reserved": 0.0, "total": 0.001},
    }
    healthy_balances = {
        "THB": {"available": 1_000.0, "reserved": 0.0, "total": 1_000.0},
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