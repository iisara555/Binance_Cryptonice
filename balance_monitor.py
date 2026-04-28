"""Exchange balance monitoring for deposits, withdrawals, and threshold alerts."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from alerts import AlertLevel, AlertSystem

logger = logging.getLogger(__name__)

_RETRY_BACKOFF_SECONDS = (2, 4, 8)
_API_ERROR_COIN = "ALL"


class _MonitorStopping(RuntimeError):
    """Internal sentinel used to stop the monitor without emitting noisy errors."""


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_asset_threshold_map(raw: str) -> Dict[str, float]:
    thresholds: Dict[str, float] = {}
    for chunk in str(raw or "").replace(";", ",").split(","):
        item = chunk.strip()
        if not item:
            continue
        separator = "=" if "=" in item else ":" if ":" in item else ""
        if not separator:
            continue
        key, value = item.split(separator, 1)
        symbol = key.strip().upper()
        amount = _safe_float(value.strip().rstrip("%"), 0.0)
        if symbol and amount > 0:
            thresholds[symbol] = amount
    return thresholds


def _format_amount(value: float, symbol: str) -> str:
    asset = str(symbol or "").upper()
    if asset in {"THB", "USDT"}:
        return f"{value:,.2f}"
    return f"{value:,.8f}".rstrip("0").rstrip(".")


def _parse_event_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    return datetime.now()


@dataclass(frozen=True)
class BalanceEvent:
    event_type: str
    coin: str
    amount: float
    balance: float
    occurred_at: datetime
    transaction_id: str = ""
    status: str = "complete"
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["occurred_at"] = self.occurred_at.isoformat()
        return payload


class BalanceMonitor:
    """Poll exchange balance and history endpoints to detect wallet activity."""

    def __init__(
        self,
        api_client,
        config: Optional[Dict[str, Any]] = None,
        *,
        alert_system: Optional[AlertSystem] = None,
        on_event: Optional[Callable[[BalanceEvent, Dict[str, Any]], None]] = None,
    ):
        self.api_client = api_client
        self.config = dict(config or {})
        self.alert_system = alert_system or AlertSystem()
        self.on_event = on_event

        self.enabled = bool(self.config.get("enabled", True))
        self.poll_interval_seconds = max(5, int(self.config.get("poll_interval_seconds", 30) or 30))
        self.api_timeout_seconds = max(1.0, float(self.config.get("api_timeout_seconds", 10.0) or 10.0))
        self.persist_path = Path(self.config.get("persist_path") or "balance_monitor_state.json")
        self.quote_asset = str(self.config.get("quote_asset") or os.environ.get("QUOTE_ASSET") or "USDT").upper()
        self.quote_min_threshold = _safe_float(
            os.environ.get(
                "QUOTE_MIN_THRESHOLD",
                self.config.get("quote_min_threshold", 0.0),
            ),
            0.0,
        )
        self.global_coin_min_threshold = _safe_float(
            os.environ.get("COIN_MIN_THRESHOLD", self.config.get("coin_min_threshold", 0.0)),
            0.0,
        )
        self.coin_min_thresholds = dict(self.config.get("coin_min_thresholds") or {})
        self.coin_min_thresholds.update(_parse_asset_threshold_map(os.environ.get("COIN_MIN_THRESHOLDS", "")))
        self._bootstrap_history_on_startup = bool(self.config.get("bootstrap_history_on_startup", True))
        self._clean_startup_event_tape = bool(self.config.get("clean_startup_event_tape", True))

        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._state_lock = threading.RLock()
        self._state: Dict[str, Any] = {
            "updated_at": None,
            "balances": {},
            "api_health": {},
            "last_events": [],
        }
        self._endpoint_health: Dict[str, Dict[str, Any]] = {}
        self._seen_fiat_deposits: set[str] = set()
        self._seen_fiat_withdrawals: set[str] = set()
        self._seen_crypto_deposits: set[str] = set()
        self._crypto_withdraw_status: Dict[str, str] = {}
        self._low_balance_alerts: set[str] = set()
        self._history_bootstrapped = False
        self._load_state()

    def start(self) -> None:
        if not self.enabled or self.running:
            return
        self._stop_event.clear()
        self.running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="BalanceMonitorThread")
        self._thread.start()
        logger.info("BalanceMonitor started | interval=%ss", self.poll_interval_seconds)

    def stop(self) -> None:
        self.running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                logger.warning("BalanceMonitor thread did not stop within 10 seconds")
        self._thread = None
        logger.info("BalanceMonitor stopped")

    def get_state(self) -> Dict[str, Any]:
        with self._state_lock:
            return copy.deepcopy(self._state)

    def poll_once(self) -> List[BalanceEvent]:
        self._raise_if_stopping()
        balances = self._call_with_retries(
            lambda: self.api_client.get_balances(
                force_refresh=True,
                allow_stale=False,
                timeout=self.api_timeout_seconds,
            ),
            "market.balances",
        )
        self._raise_if_stopping()
        fiat_deposits = self._call_with_retries(
            lambda: self.api_client.get_fiat_deposit_history(limit=50, timeout=self.api_timeout_seconds),
            "fiat.deposit_history",
        )
        self._raise_if_stopping()
        fiat_withdrawals = self._call_with_retries(
            lambda: self.api_client.get_fiat_withdraw_history(limit=50, timeout=self.api_timeout_seconds),
            "fiat.withdraw_history",
        )
        self._raise_if_stopping()
        crypto_deposits = self._call_with_retries(
            lambda: self.api_client.get_crypto_deposit_history(limit=100, timeout=self.api_timeout_seconds),
            "crypto.deposit_history",
        )
        self._raise_if_stopping()
        crypto_withdrawals = self._call_with_retries(
            lambda: self.api_client.get_crypto_withdraw_history(limit=100, timeout=self.api_timeout_seconds),
            "crypto.withdraw_history",
        )
        self._raise_if_stopping()

        normalized_balances = self._normalize_balances(balances)

        if not self._history_bootstrapped:
            self._history_bootstrapped = True

            if self._bootstrap_history_on_startup:
                self._seed_seen_history(
                    fiat_deposits or [],
                    fiat_withdrawals or [],
                    crypto_deposits or {},
                    crypto_withdrawals or {},
                )

                with self._state_lock:
                    self._state["updated_at"] = datetime.now().isoformat()
                    self._state["balances"] = normalized_balances
                    self._state["api_health"] = copy.deepcopy(self._endpoint_health)
                    if self._clean_startup_event_tape:
                        self._state["last_events"] = []

                self._check_thresholds(normalized_balances)
                self._save_state()
                logger.info("BalanceMonitor history bootstrap complete; existing history marked as seen")
                return []

        events: List[BalanceEvent] = []
        events.extend(self._detect_fiat_events(fiat_deposits or [], fiat_withdrawals or [], normalized_balances))
        events.extend(self._detect_crypto_events(crypto_deposits or {}, crypto_withdrawals or {}, normalized_balances))

        with self._state_lock:
            self._state["updated_at"] = datetime.now().isoformat()
            self._state["balances"] = normalized_balances
            self._state["api_health"] = copy.deepcopy(self._endpoint_health)
            if events:
                history = [event.to_dict() for event in events]
                self._state["last_events"] = (history + self._state.get("last_events", []))[:25]
        self._check_thresholds(normalized_balances)
        self._save_state()

        for event in events:
            if self._stop_event.is_set():
                break
            self._log_event(event)
            if self._stop_event.is_set():
                break
            self._notify_event(event)
            if self._stop_event.is_set():
                break
            if self.on_event:
                try:
                    self.on_event(event, self.get_state())
                except Exception as exc:
                    logger.error("Balance event callback failed: %s", exc, exc_info=True)
                if self._stop_event.is_set():
                    break

        return events

    def _monitor_loop(self) -> None:
        try:
            while self.running and not self._stop_event.is_set():
                started_at = time.time()
                try:
                    self.poll_once()
                except _MonitorStopping:
                    break
                except Exception as exc:
                    logger.error("Balance monitor poll failed: %s", exc, exc_info=True)
                elapsed = time.time() - started_at
                sleep_for = max(1.0, self.poll_interval_seconds - elapsed)
                if self._stop_event.wait(sleep_for):
                    break
        finally:
            self.running = False

    def _call_with_retries(self, func: Callable[[], Any], label: str) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(len(_RETRY_BACKOFF_SECONDS) + 1):
            self._raise_if_stopping()
            try:
                result = func()
                previous = self._endpoint_health.get(label, {}).get("healthy")
                self._endpoint_health[label] = {
                    "healthy": True,
                    "last_error": "",
                    "restored_at": (
                        datetime.now().isoformat()
                        if previous is False
                        else self._endpoint_health.get(label, {}).get("restored_at")
                    ),
                }
                if previous is False:
                    logger.info("Balance monitor connection restored for %s", label)
                return result
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Balance monitor API call failed for %s (attempt %s/%s): %s",
                    label,
                    attempt + 1,
                    len(_RETRY_BACKOFF_SECONDS) + 1,
                    exc,
                )
                if attempt < len(_RETRY_BACKOFF_SECONDS):
                    if self._stop_event.wait(_RETRY_BACKOFF_SECONDS[attempt]):
                        raise _MonitorStopping()

        self._raise_if_stopping()
        self._endpoint_health[label] = {
            "healthy": False,
            "last_error": str(last_error or "unknown error"),
            "failed_at": datetime.now().isoformat(),
        }
        self._notify_api_error(label)
        raise RuntimeError(f"{label} failed after retries: {last_error}")

    def _raise_if_stopping(self) -> None:
        if self._stop_event.is_set():
            raise _MonitorStopping()

    def _normalize_balances(self, balances: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        normalized: Dict[str, Dict[str, float]] = {}
        for symbol, payload in (balances or {}).items():
            asset = str(symbol or "").upper()
            if not asset:
                continue
            if isinstance(payload, dict):
                available = _safe_float(payload.get("available"), 0.0)
                reserved = _safe_float(payload.get("reserved"), 0.0)
            else:
                available = _safe_float(payload, 0.0)
                reserved = 0.0
            normalized[asset] = {
                "available": available,
                "reserved": reserved,
                "total": available + reserved,
            }
        return normalized

    def _seed_seen_history(
        self,
        fiat_deposits: List[Dict[str, Any]],
        fiat_withdrawals: List[Dict[str, Any]],
        crypto_deposits: Dict[str, Any],
        crypto_withdrawals: Dict[str, Any],
    ) -> None:
        for item in fiat_deposits:
            txn_id = str(item.get("txn_id") or item.get("txn") or "")
            if txn_id and str(item.get("status") or "").lower() == "complete":
                self._seen_fiat_deposits.add(txn_id)

        for item in fiat_withdrawals:
            txn_id = str(item.get("txn_id") or item.get("txn") or "")
            if txn_id and str(item.get("status") or "").lower() == "complete":
                self._seen_fiat_withdrawals.add(txn_id)

        for item in crypto_deposits.get("items", []) or []:
            txn_id = str(item.get("hash") or item.get("txn_id") or "")
            if txn_id and str(item.get("status") or "").lower() == "complete":
                self._seen_crypto_deposits.add(txn_id)

        for item in crypto_withdrawals.get("items", []) or []:
            txn_id = str(item.get("txn_id") or item.get("hash") or "")
            if txn_id:
                self._crypto_withdraw_status[txn_id] = str(item.get("status") or "").lower()

    def _detect_fiat_events(
        self,
        fiat_deposits: List[Dict[str, Any]],
        fiat_withdrawals: List[Dict[str, Any]],
        balances: Dict[str, Dict[str, float]],
    ) -> List[BalanceEvent]:
        events: List[BalanceEvent] = []
        quote_balance = balances.get(self.quote_asset, {}).get("available", 0.0)

        for item in fiat_deposits:
            txn_id = str(item.get("txn_id") or item.get("txn") or "")
            if not txn_id or txn_id in self._seen_fiat_deposits:
                continue
            if str(item.get("status") or "").lower() != "complete":
                continue
            self._seen_fiat_deposits.add(txn_id)
            events.append(
                BalanceEvent(
                    event_type="DEPOSIT",
                    coin=self.quote_asset,
                    amount=_safe_float(item.get("amount"), 0.0),
                    balance=quote_balance,
                    occurred_at=_parse_event_time(item.get("time")),
                    transaction_id=txn_id,
                    status="complete",
                    source="fiat",
                )
            )

        for item in fiat_withdrawals:
            txn_id = str(item.get("txn_id") or item.get("txn") or "")
            if not txn_id or txn_id in self._seen_fiat_withdrawals:
                continue
            if str(item.get("status") or "").lower() != "complete":
                continue
            self._seen_fiat_withdrawals.add(txn_id)
            events.append(
                BalanceEvent(
                    event_type="WITHDRAWAL",
                    coin=self.quote_asset,
                    amount=_safe_float(item.get("amount"), 0.0),
                    balance=quote_balance,
                    occurred_at=_parse_event_time(item.get("time")),
                    transaction_id=txn_id,
                    status="complete",
                    source="fiat",
                )
            )

        return events

    def _detect_crypto_events(
        self,
        crypto_deposits: Dict[str, Any],
        crypto_withdrawals: Dict[str, Any],
        balances: Dict[str, Dict[str, float]],
    ) -> List[BalanceEvent]:
        events: List[BalanceEvent] = []

        for item in crypto_deposits.get("items", []) or []:
            status = str(item.get("status") or "").lower()
            if status != "complete":
                continue
            txn_id = str(item.get("hash") or item.get("txn_id") or "")
            if not txn_id or txn_id in self._seen_crypto_deposits:
                continue
            symbol = str(item.get("symbol") or "").upper()
            self._seen_crypto_deposits.add(txn_id)
            events.append(
                BalanceEvent(
                    event_type="DEPOSIT",
                    coin=symbol,
                    amount=_safe_float(item.get("amount"), 0.0),
                    balance=balances.get(symbol, {}).get("available", 0.0),
                    occurred_at=_parse_event_time(item.get("completed_at") or item.get("created_at")),
                    transaction_id=txn_id,
                    status=f"confirmed ({int(item.get('confirmations') or 0)} conf)",
                    source="crypto",
                )
            )

        active_statuses = {"pending", "processing", "reported"}
        for item in crypto_withdrawals.get("items", []) or []:
            txn_id = str(item.get("txn_id") or item.get("hash") or "")
            if not txn_id:
                continue
            symbol = str(item.get("symbol") or "").upper()
            status = str(item.get("status") or "").lower()
            previous_status = self._crypto_withdraw_status.get(txn_id)
            self._crypto_withdraw_status[txn_id] = status

            if status in active_statuses and previous_status is None:
                events.append(
                    BalanceEvent(
                        event_type="WITHDRAWAL_INITIATED",
                        coin=symbol,
                        amount=_safe_float(item.get("amount"), 0.0),
                        balance=balances.get(symbol, {}).get("available", 0.0),
                        occurred_at=_parse_event_time(item.get("created_at")),
                        transaction_id=txn_id,
                        status=status,
                        source="crypto",
                    )
                )
            elif status == "complete" and previous_status != "complete":
                events.append(
                    BalanceEvent(
                        event_type="WITHDRAWAL_COMPLETED" if previous_status else "WITHDRAWAL",
                        coin=symbol,
                        amount=_safe_float(item.get("amount"), 0.0),
                        balance=balances.get(symbol, {}).get("available", 0.0),
                        occurred_at=_parse_event_time(item.get("completed_at") or item.get("created_at")),
                        transaction_id=txn_id,
                        status="complete",
                        source="crypto",
                    )
                )

        return events

    def _check_thresholds(self, balances: Dict[str, Dict[str, float]]) -> None:
        quote_available = balances.get(self.quote_asset, {}).get("available", 0.0)
        self._handle_threshold_alert(self.quote_asset, quote_available, self.quote_min_threshold)

        tracked_coins = set(self.coin_min_thresholds)
        tracked_coins.update(
            asset for asset, payload in balances.items() if asset != self.quote_asset and payload.get("total", 0.0) > 0
        )
        for coin in tracked_coins:
            minimum = self.coin_min_thresholds.get(coin, self.global_coin_min_threshold)
            if minimum <= 0:
                continue
            current = balances.get(coin, {}).get("available", 0.0)
            self._handle_threshold_alert(coin, current, minimum)

    def _handle_threshold_alert(self, coin: str, balance: float, minimum: float) -> None:
        if minimum <= 0:
            return
        key = str(coin or "").upper()
        if balance < minimum:
            if key in self._low_balance_alerts:
                return
            self._low_balance_alerts.add(key)
            message = self._format_critical_alert(
                alert_type="LOW BALANCE",
                coin=key,
                balance=balance,
                minimum=minimum,
            )
            self.alert_system.send(AlertLevel.CRITICAL, message)
            logger.warning("Low balance alert triggered for %s | balance=%.8f minimum=%.8f", key, balance, minimum)
            return

        self._low_balance_alerts.discard(key)

    def _notify_api_error(self, label: str) -> None:
        message = self._format_critical_alert(
            alert_type="API ERROR",
            coin=_API_ERROR_COIN,
            balance=None,
            minimum=None,
        )
        self.alert_system.send(AlertLevel.CRITICAL, message)
        logger.error("Balance monitor API failure on %s", label)

    def _log_event(self, event: BalanceEvent) -> None:
        logger.info(
            "[BalanceMonitor] %s | coin=%s | amount=%s | balance=%s | tx=%s | time=%s",
            event.event_type,
            event.coin,
            _format_amount(event.amount, event.coin),
            _format_amount(event.balance, event.coin),
            event.transaction_id or "-",
            event.occurred_at.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def _notify_event(self, event: BalanceEvent) -> None:
        self.alert_system.send(AlertLevel.TRADE, self._format_event_alert(event))

    def _format_event_alert(self, event: BalanceEvent) -> str:
        event_labels = {
            "DEPOSIT": "ฝากเงินเข้า",
            "WITHDRAWAL": "ถอนเงินออก",
            "WITHDRAWAL_INITIATED": "ถอนเงิน (รอดำเนินการ)",
            "WITHDRAWAL_COMPLETED": "ถอนเงินสำเร็จ",
        }
        label = event_labels.get(event.event_type, event.event_type.replace("_", " "))
        emoji = "🟢" if "DEPOSIT" in event.event_type else "🔴"
        return (
            f"{emoji} <b>{label}</b>  {event.coin}\n"
            f"{'─' * 20}\n"
            f"จำนวน  <code>{_format_amount(event.amount, event.coin)}</code> {event.coin}\n"
            f"ยอดคงเหลือ  <code>{_format_amount(event.balance, event.coin)}</code> {event.coin}\n"
            f"🕐 {event.occurred_at.strftime('%H:%M:%S')}"
        )

    def _format_critical_alert(
        self,
        *,
        alert_type: str,
        coin: str,
        balance: Optional[float],
        minimum: Optional[float],
    ) -> str:
        balance_text = "N/A" if balance is None else f"<code>{_format_amount(balance, coin)}</code> {coin}"
        minimum_text = "N/A" if minimum is None else f"<code>{_format_amount(minimum, coin)}</code> {coin}"
        if alert_type == "LOW BALANCE":
            return (
                f"⚠️ <b>ยอดเงินต่ำ</b>  {coin}\n"
                f"{'─' * 20}\n"
                f"คงเหลือ  {balance_text}\n"
                f"ขั้นต่ำ  {minimum_text}\n"
                f"🕐 {datetime.now().strftime('%H:%M:%S')}"
            )
        return (
            f"🚨 <b>{alert_type}</b>  {coin}\n"
            f"{'─' * 20}\n"
            f"ยอด  {balance_text}\n"
            f"ขั้นต่ำ  {minimum_text}\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )

    def _load_state(self) -> None:
        if not self.persist_path.exists():
            return
        try:
            payload = json.loads(self.persist_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load balance monitor state: %s", exc)
            return

        self._seen_fiat_deposits = set(payload.get("seen_fiat_deposits", []))
        self._seen_fiat_withdrawals = set(payload.get("seen_fiat_withdrawals", []))
        self._seen_crypto_deposits = set(payload.get("seen_crypto_deposits", []))
        self._crypto_withdraw_status = dict(payload.get("crypto_withdraw_status", {}))
        self._low_balance_alerts = set(payload.get("low_balance_alerts", []))
        with self._state_lock:
            self._state.update(payload.get("state", {}))
            raw_last_events = list(self._state.get("last_events") or [])
            normalized_events: List[Dict[str, Any]] = []
            for row in raw_last_events:
                if not isinstance(row, dict):
                    continue
                event_coin = str(row.get("coin") or "").upper()
                if event_coin and event_coin not in {self.quote_asset, "THB", "USDT"}:
                    # Keep non-cash asset events as-is.
                    normalized_events.append(dict(row))
                    continue
                # Keep only recent cash events to avoid noisy stale startup tape.
                occurred_at = _parse_event_time(row.get("occurred_at"))
                if (datetime.now() - occurred_at).total_seconds() > 24 * 3600:
                    continue
                normalized_events.append(dict(row))
            self._state["last_events"] = normalized_events[:25]

    def _save_state(self) -> None:
        payload = {
            "seen_fiat_deposits": sorted(self._seen_fiat_deposits),
            "seen_fiat_withdrawals": sorted(self._seen_fiat_withdrawals),
            "seen_crypto_deposits": sorted(self._seen_crypto_deposits),
            "crypto_withdraw_status": dict(self._crypto_withdraw_status),
            "low_balance_alerts": sorted(self._low_balance_alerts),
            "state": self.get_state(),
        }
        try:
            self.persist_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to persist balance monitor state: %s", exc)
