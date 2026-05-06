"""Unified alert system with shared Telegram transport and rate limiting."""

from __future__ import annotations

import html
import json
import logging
import os
import time
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool) -> bool:
    """Parse a boolean environment flag with a sensible default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class AlertLevel:
    TRADE = "trade"
    CRITICAL = "critical"
    SUMMARY = "summary"
    INFO = "info"
    DEBUG = "debug"


class RateLimiter:
    """Prevents Telegram alert spam by alert level."""

    def __init__(self):
        self._lock = Lock()
        self._last_sent: Dict[str, datetime] = {}
        self._count_minute: Dict[str, List[datetime]] = {}
        self._limits = {
            AlertLevel.TRADE: (10, 6),
            AlertLevel.CRITICAL: (30, 2),
            AlertLevel.SUMMARY: (60, 1),
            AlertLevel.INFO: (5, 10),
            AlertLevel.DEBUG: (5, 10),
        }

    def can_send(self, level: str) -> bool:
        if level not in self._limits:
            return True

        min_interval, max_per_minute = self._limits[level]
        now = datetime.now()

        with self._lock:
            last_sent = self._last_sent.get(level)
            if last_sent is not None:
                time_since_last = (now - last_sent).total_seconds()
                if time_since_last < min_interval:
                    logger.debug("Rate limited %s: %.1fs < %ss", level, time_since_last, min_interval)
                    return False

            history = self._count_minute.setdefault(level, [])
            cutoff = now - timedelta(minutes=1)
            self._count_minute[level] = [timestamp for timestamp in history if timestamp > cutoff]

            if len(self._count_minute[level]) >= max_per_minute:
                logger.debug(
                    "Rate limited %s: %s >= %s per minute",
                    level,
                    len(self._count_minute[level]),
                    max_per_minute,
                )
                return False

            self._last_sent[level] = now
            self._count_minute[level].append(now)

        return True

    def get_status(self) -> Dict[str, Any]:
        now = datetime.now()
        status: Dict[str, Any] = {}

        with self._lock:
            for level in self._limits:
                last_sent = self._last_sent.get(level)
                status[level] = {
                    "last_sent_seconds_ago": (now - last_sent).total_seconds() if last_sent else None,
                    "count_last_minute": len(self._count_minute.get(level, [])),
                }

        return status


class TelegramSender:
    """Shared Telegram transport for alerts and Telegram bot commands."""

    API_URL = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.runtime_enabled = _env_flag("TELEGRAM_ENABLED", True)
        self.bot_token: Optional[str] = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = str(chat_id or os.environ.get("TELEGRAM_CHAT_ID", "") or "")
        self.api_enabled = self.runtime_enabled and bool(self.bot_token)
        self.enabled = self.api_enabled and bool(self.chat_id)
        self.auth_failed = False
        self.auth_failure_reason = ""

        if not self.runtime_enabled:
            logger.info("Telegram explicitly disabled via TELEGRAM_ENABLED=false")
        elif not self.bot_token:
            logger.warning("Telegram bot token not configured - Telegram transport disabled")
        elif not self.chat_id:
            logger.warning("Telegram chat id not configured - sendMessage/editMessage disabled")

    def _mark_auth_failed(self, reason: str):
        message = str(reason or "Telegram authentication failed")
        if self.auth_failed and self.auth_failure_reason == message:
            return

        self.auth_failed = True
        self.auth_failure_reason = message
        self.api_enabled = False
        self.enabled = False
        logger.error("Telegram transport disabled after authentication failure: %s", message)

    def _request(
        self,
        method: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        timeout: int,
        max_retries: int,
        require_chat: bool = False,
    ) -> Dict[str, Any]:
        if not self.api_enabled:
            raise RuntimeError("Telegram API is disabled or bot token is missing")
        if require_chat and not self.chat_id:
            raise RuntimeError("Telegram chat id is missing")

        url = self.API_URL.format(token=self.bot_token, method=method)
        last_error: Optional[Exception] = None

        for attempt in range(max_retries):
            try:
                response = requests.post(url, json=payload or {}, timeout=timeout)
                if response.status_code in {401, 403}:
                    detail = response.text[:200] if getattr(response, "text", None) else response.reason
                    self._mark_auth_failed(detail or f"HTTP {response.status_code}")
                # Handle Telegram flood control (HTTP 429 or error_code 429)
                if response.status_code == 429:
                    retry_after = 5
                    try:
                        body = response.json()
                        retry_after = body.get("parameters", {}).get("retry_after", retry_after)
                    except Exception as exc:
                        logger.debug("Failed to parse Telegram 429 response body: %s", exc)
                    logger.warning(
                        "Telegram rate-limited (429). Waiting %s seconds before retry.",
                        retry_after,
                    )
                    if attempt < max_retries - 1:
                        time.sleep(retry_after)
                        continue
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and data.get("ok", True) is False:
                    description = data.get("description") or data
                    description_text = str(description)
                    error_code = data.get("error_code")
                    if "unauthorized" in description_text.lower() or "forbidden" in description_text.lower():
                        self._mark_auth_failed(description_text)
                    # Flood control at API level (error_code 429 in JSON body)
                    if error_code == 429:
                        retry_after = 5
                        try:
                            retry_after = data.get("parameters", {}).get("retry_after", retry_after)
                        except Exception as exc:
                            logger.debug("Failed to parse Telegram flood-control retry_after: %s", exc)
                        logger.warning(
                            "Telegram flood control (error_code 429): %s. Waiting %s seconds.",
                            description_text,
                            retry_after,
                        )
                        if attempt < max_retries - 1:
                            time.sleep(retry_after)
                            continue
                    raise requests.HTTPError(str(description), response=response)
                return data
            except Exception as exc:
                last_error = exc
                logger.error(
                    "Telegram %s failed (attempt %s/%s): %s",
                    method,
                    attempt + 1,
                    max_retries,
                    exc,
                )
                if attempt < max_retries - 1:
                    time.sleep(min(2**attempt, 30))

        assert last_error is not None
        raise last_error

    def send_message(
        self,
        message: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
        disable_notification: bool = False,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        # --- NEW: SPEC_09 (Freqtrade-style) ---
        # ``disable_notification=True`` delivers the message without sound,
        # implementing the "silent" verbosity level.
        if disable_notification:
            payload["disable_notification"] = True
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        return self._request(
            "sendMessage",
            payload,
            timeout=10,
            max_retries=3,
            require_chat=True,
        )

    def edit_message(
        self,
        message_id: int,
        text: str,
        *,
        parse_mode: str = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        return self._request(
            "editMessageText",
            payload,
            timeout=15,
            max_retries=2,
            require_chat=True,
        )

    def get_updates(self, offset: int = 0, timeout: int = 30) -> Dict[str, Any]:
        return self._request(
            "getUpdates",
            {"offset": offset, "timeout": timeout},
            timeout=timeout + 5,
            max_retries=1,
            require_chat=False,
        )

    def answer_callback(self, callback_query_id: str, text: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return self._request(
            "answerCallbackQuery",
            payload,
            timeout=10,
            max_retries=2,
            require_chat=False,
        )

    def delete_webhook(self, drop_pending_updates: bool = True) -> Dict[str, Any]:
        return self._request(
            "deleteWebhook",
            {"drop_pending_updates": drop_pending_updates},
            timeout=5,
            max_retries=1,
            require_chat=False,
        )

    def send(
        self,
        message: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: str = "HTML",
    ) -> bool:
        if not self.enabled:
            return False
        try:
            self.send_message(message, reply_markup=reply_markup, parse_mode=parse_mode)
            return True
        except Exception:
            return False


class AlertSystem:
    """Unified alert system with rate limiting and fault tolerance."""

    TELEGRAM_LEVELS = {AlertLevel.TRADE, AlertLevel.CRITICAL, AlertLevel.SUMMARY}

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.telegram = TelegramSender(bot_token, chat_id)
        self.rate_limiter = RateLimiter()
        self._lock = Lock()
        # --- NEW: SPEC_09 (Freqtrade-style) ---
        # ``config`` is the parsed bot_config.yaml (or any dict containing the
        # ``notifications.notification_settings`` map). It controls per-event
        # verbosity ("on" | "silent" | "off"). When omitted, every event
        # defaults to "on" (matches legacy behaviour).
        self.config: Dict[str, Any] = config or {}

    def send(
        self,
        level: str,
        message: str,
        *,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: str = "HTML",
    ) -> bool:
        with self._lock:
            # Strip non-BMP characters (emoji) from the log line so VPS terminals
            # that lack full Unicode support don't crash or show garbled output.
            _log_msg = "".join(c if ord(c) <= 0xFFFF else "?" for c in message)[:300]
            logger.info("[%s] %s", level.upper(), _log_msg)

            if level not in self.TELEGRAM_LEVELS:
                return True

            if level != AlertLevel.CRITICAL and not self.rate_limiter.can_send(level):
                logger.debug("Rate limited: %s - %s...", level, message[:50])
                return False

            success = self.telegram.send(message, reply_markup=reply_markup, parse_mode=parse_mode)
            if not success and not self.telegram.auth_failed:
                logger.error("Failed to send %s alert to Telegram", level)
            return success

    def can_send(self, level: str) -> bool:
        return self.rate_limiter.can_send(level)

    def get_status(self) -> Dict[str, Any]:
        return {
            "telegram_enabled": self.telegram.enabled,
            "telegram_api_enabled": self.telegram.api_enabled,
            "rate_limits": self.rate_limiter.get_status(),
        }

    def __call__(self, message: str) -> bool:
        return self.send(AlertLevel.TRADE, message)

    def create_trade_sender(self) -> Callable[[str], bool]:
        def legacy_alert_sender(message: str) -> bool:
            return self.send(AlertLevel.TRADE, message)

        return legacy_alert_sender




_alert_system: Optional[AlertSystem] = None


def get_alert_system() -> AlertSystem:
    global _alert_system
    if _alert_system is None:
        _alert_system = AlertSystem()
    return _alert_system


def send_alert(level: str, message: str) -> bool:
    return get_alert_system().send(level, message)


def create_telegram_alert_function(bot_token: Optional[str] = None, chat_id: Optional[str] = None):
    return AlertSystem(bot_token, chat_id).create_trade_sender()


def _ts() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def escape_html(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def _safe_text(value: Any) -> str:
    return escape_html(value)


def format_fatal_auth_alert(details: str, *, title: str = "FATAL: API Auth Error") -> str:
    return format_error_alert(title=title, details=details, status="SHUTDOWN")


def format_trade_alert(
    symbol: str,
    side: str,
    price: float,
    amount: float,
    value_quote: float,
    pnl_amt: Optional[float] = None,
    pnl_pct: Optional[float] = None,
    status: str = "filled",
    extra: Optional[str] = None,
    quote_asset: str = "USDT",
) -> str:
    """Format a trade alert message.

    Args:
        quote_asset: Quote currency (default "USDT" for Binance TH).
    """
    pair = _safe_text(symbol)
    coin = _safe_text(str(symbol or "").replace("THB_", "").replace("USDT_", ""))
    side_val = _safe_text(str(side or "").upper() or "N/A")
    status_val = _safe_text(str(status or "filled").upper())

    if status in ("rejected", "error"):
        icon = "❗"
    elif pnl_amt is not None and pnl_amt < 0:
        icon = "🔻"
    else:
        icon = "✅"

    lines = [
        f"{icon} <b>Trade Update</b>",
        f"{'-' * 22}",
        f"Pair: <code>{pair}</code>",
        f"Side: <b>{side_val}</b>  |  Status: <code>{status_val}</code>",
        f"Entry/Fill: <code>{price:,.2f}</code> {quote_asset}",
        f"Amount: <code>{amount:.6f}</code> {coin}",
        f"Notional: <code>{value_quote:,.2f}</code> {quote_asset}",
    ]

    if pnl_amt is not None and pnl_pct is not None:
        pnl_sign = "+" if pnl_amt >= 0 else ""
        lines.append(f"PnL: <code>{pnl_sign}{pnl_amt:,.2f}</code> {quote_asset} (<code>{pnl_sign}{pnl_pct:.2f}%</code>)")

    if extra:
        lines.append(f"Note: {_safe_text(extra)}")

    lines.append(f"Time: <code>{_ts()}</code>")
    return "\n".join(lines)


def format_error_alert(title: str, details: str, status: str = "error") -> str:
    status_val = _safe_text(str(status or "error").upper())
    lines = [
        "❗ <b>Critical Alert</b>",
        f"{'-' * 22}",
        f"Title: <b>{_safe_text(title)}</b>",
    ]
    if details:
        lines.append(f"Detail: {_safe_text(details)}")
    lines.append(f"Status: <code>{status_val}</code>")
    lines.append(f"Time: <code>{_ts()}</code>")
    return "\n".join(lines)


def format_status_alert(
    balance_quote: float,
    portfolio_value: float,
    pnl_amt: float,
    pnl_pct: float,
    uptime: Optional[str] = None,
    pairs_status: Optional[List[str]] = None,
    quote_asset: str = "USDT",
) -> str:
    """Format a status alert message.

    Args:
        quote_asset: Quote currency (default "USDT" for Binance TH).
    """
    uptime_val = _safe_text(uptime) if uptime else "-"
    pnl_emoji = "✅" if pnl_amt >= 0 else "🔻"
    pnl_sign = "+" if pnl_amt >= 0 else ""

    lines = [
        "📊 <b>Portfolio Summary</b>",
        f"{'-' * 22}",
        f"Total Value: <code>{portfolio_value:,.2f}</code> {quote_asset}",
        f"{pnl_emoji} PnL: <code>{pnl_sign}{pnl_amt:,.2f}</code> {quote_asset} (<code>{pnl_sign}{pnl_pct:.2f}%</code>)",
        f"Cash: <code>{balance_quote:,.2f}</code> {quote_asset}",
        f"Uptime: <code>{uptime_val}</code>",
    ]

    if pairs_status:
        lines.append(f"Active Assets: <code>{len(pairs_status)}</code>")

    lines.append(f"Time: <code>{_ts()}</code>")
    return "\n".join(lines)


def send_error_token(bot_token: str, chat_id: str, title: str, details: str, status: str = "error") -> bool:
    msg = format_error_alert(title, details, status)
    sender = TelegramSender(bot_token, chat_id)
    return sender.send(msg)


def send_status_token(
    bot_token: str,
    chat_id: str,
    balance_thb: float,
    portfolio_value: float,
    pnl_amt: float,
    pnl_pct: float,
    uptime: Optional[str] = None,
    pairs_status: Optional[List[str]] = None,
) -> bool:
    msg = format_status_alert(balance_thb, portfolio_value, pnl_amt, pnl_pct, uptime, pairs_status)
    sender = TelegramSender(bot_token, chat_id)
    return sender.send(msg)

