"""Unified alert system with shared Telegram transport and rate limiting."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from threading import Lock, Thread
from typing import Any, Awaitable, Callable, Dict, List, Optional

import requests

# --- NEW: SPEC_09 (Freqtrade-style) ---
# aiohttp is used by ``TelegramCommandHandler`` for non-blocking long-poll
# command dispatch. It is an optional dependency: notifications keep working
# (via the sync ``TelegramSender`` + ``asyncio.to_thread``) even when aiohttp
# is unavailable, but two-way commands require it.
try:
    import aiohttp
except ImportError:  # pragma: no cover - aiohttp is optional but recommended
    aiohttp = None  # type: ignore[assignment]

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
                    time.sleep(2**attempt)

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
            logger.info("[%s] %s", level.upper(), message)

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

    # --- NEW: SPEC_09 (Freqtrade-style) ---
    # The methods below add Freqtrade-inspired notifications with per-event
    # verbosity ("on" | "silent" | "off") routed via
    # ``config["notifications"]["notification_settings"]``. They are
    # ``async def`` to match the cursorrules ("async/await throughout") and
    # internally use ``asyncio.to_thread`` so they reuse the existing sync
    # ``TelegramSender`` retry / auth-failure / flood-control machinery
    # without introducing a separate aiohttp HTTP path for one-shot sends.

    def update_config(self, config: Optional[Dict[str, Any]]) -> None:
        """Refresh the cached config dict used for verbosity routing."""
        self.config = config or {}

    def _notification_level(self, event: str) -> str:
        """Return the verbosity level for ``event`` ("on" | "silent" | "off").

        Defaults to "on" when the event is not configured, so new events do
        not silently disappear after upgrades.
        """
        notif_section = self.config.get("notifications") or {}
        settings = notif_section.get("notification_settings") or {}
        raw = settings.get(event, "on")
        return str(raw).strip().lower() if raw is not None else "on"

    async def _send_event(
        self,
        message: str,
        event: str = "status",
        *,
        parse_mode: str = "Markdown",
    ) -> bool:
        """Send ``message`` to Telegram according to the event's verbosity.

        - ``on``     → send normally (with notification sound)
        - ``silent`` → send with ``disable_notification=True`` (no sound)
        - ``off``    → log only, do not send

        Always mirrors the message to the Python logger so SRE/devs can audit
        notification activity even when Telegram is muted or disabled.
        """
        level = self._notification_level(event)
        # Always log — useful when Telegram is disabled, off, or rate-limited.
        logger.info("[NOTIFY:%s:%s] %s", event, level, message[:300])

        if level == "off":
            return False
        if not self.telegram.enabled:
            return False

        disable_notification = level == "silent"
        try:
            await asyncio.to_thread(
                self.telegram.send_message,
                message,
                None,
                parse_mode,
                True,
                disable_notification,
            )
            return True
        except Exception as exc:
            if not self.telegram.auth_failed:
                logger.error("Failed to send %s notification: %s", event, exc)
            return False

    @staticmethod
    def _safe_pct(numerator: float, denominator: float) -> float:
        """Percentage helper that returns 0.0 instead of raising on div-by-zero."""
        try:
            if not denominator:
                return 0.0
            return (numerator / denominator) * 100.0
        except (TypeError, ZeroDivisionError):
            return 0.0

    @staticmethod
    def _short_order_id(order_id: Any) -> str:
        text = str(order_id or "")
        if not text:
            return "-"
        return text if len(text) <= 8 else f"{text[:8]}..."

    async def send_entry_fill(
        self,
        symbol: str,
        side: str,
        filled_price: float,
        planned_price: float,
        quantity: float,
        amount_quote: float,
        stop_loss: float,
        take_profit: float,
        mode: str,
        order_id: str,
        quote_asset: str = "USDT",
    ) -> bool:
        """Notify when a BUY (or entry) order has filled on the exchange.

        Reports actual slippage vs the planned signal price so traders can
        track execution quality across market regimes.
        
        Args:
            quote_asset: Quote currency (default "USDT", can be "THB" for Binance TH)
        """
        slippage = self._safe_pct(filled_price - planned_price, planned_price)
        sl_pct = self._safe_pct(abs(filled_price - stop_loss), filled_price)
        tp_pct = self._safe_pct(abs(take_profit - filled_price), filled_price)

        msg = (
            f"✅ *ORDER FILLED* — {symbol}\n"
            f"Side: `{str(side or 'BUY').upper()}` | Mode: `{mode}`\n"
            f"Filled: `${filled_price:,.4f}` × `{quantity:.6f}`\n"
            f"Amount: `${amount_quote:,.2f} {quote_asset}`\n"
            f"Slippage: `{slippage:+.3f}%`\n"
            f"SL: `${stop_loss:,.4f}` (-{sl_pct:.2f}%)\n"
            f"TP: `${take_profit:,.4f}` (+{tp_pct:.2f}%)\n"
            f"Order: `{self._short_order_id(order_id)}`"
        )
        return await self._send_event(msg, event="entry_fill")

    async def send_exit_fill(
        self,
        symbol: str,
        exit_reason: str,
        filled_price: float,
        entry_price: float,
        quantity: float,
        pnl_quote: float,
        pnl_pct: float,
        hold_minutes: float,
        order_id: str,
        quote_asset: str = "USDT",
    ) -> bool:
        """Notify when a SELL (or exit) order has filled on the exchange.

        Maps ``exit_reason`` to a glyph (TP / SL / TRAILING_SL / MINIMAL_ROI /
        SIGNAL / MANUAL) and computes a human-readable hold duration.
        
        Args:
            quote_asset: Quote currency (default "USDT", can be "THB" for Binance TH)
        """
        emoji = "🟢" if (pnl_quote or 0) >= 0 else "🔴"
        reason_emoji = {
            "TP": "🎯",
            "SL": "🛑",
            "TRAILING_SL": "📉",
            "MINIMAL_ROI": "⏱️",
            "SIGNAL": "📊",
            "MANUAL": "👤",
        }.get(str(exit_reason or "").upper(), "❌")

        if hold_minutes is None:
            hold_str = "-"
        elif hold_minutes < 60:
            hold_str = f"{hold_minutes:.0f}m"
        else:
            hold_str = f"{hold_minutes / 60:.1f}h"

        msg = (
            f"{emoji} *TRADE CLOSED* — {symbol}\n"
            f"Reason: {reason_emoji} `{exit_reason}`\n"
            f"Entry: `${entry_price:,.4f}` → Exit: `${filled_price:,.4f}`\n"
            f"PnL: `{pnl_pct:+.2f}%` (`{pnl_quote:+.2f} {quote_asset}`)\n"
            f"Hold: `{hold_str}` | Qty: `{quantity:.6f}`\n"
            f"Order: `{self._short_order_id(order_id)}`"
        )
        return await self._send_event(msg, event="exit_fill")

    async def send_trailing_stop_moved(
        self,
        symbol: str,
        old_stop: float,
        new_stop: float,
        current_price: float,
        unrealized_pct: float,
    ) -> bool:
        """Notify when the trailing stop ratchets to a new level.

        Routes through the ``trailing_stop_loss_moved`` event so users can
        silence the (potentially noisy) per-tick updates while keeping the
        coarser ``trailing_stop_loss`` activation alert audible.
        """
        distance_pct = self._safe_pct(abs(current_price - new_stop), current_price)
        msg = (
            f"📈 *TRAILING STOP MOVED* — {symbol}\n"
            f"Price: `${current_price:,.4f}` (+{unrealized_pct:.2f}%)\n"
            f"Stop: `${old_stop:,.4f}` → `${new_stop:,.4f}`\n"
            f"Distance: `{distance_pct:.2f}%`"
        )
        return await self._send_event(msg, event="trailing_stop_loss_moved")

    async def send_protection_trigger(
        self,
        symbol: str,
        failed_checks: List[str],
        signal_confidence: float,
    ) -> bool:
        """Notify when the PreTradeGate (or any guard) blocks a trade.

        ``failed_checks`` should be human-readable check names so the user can
        immediately see which protection fired (e.g. "daily_loss_limit",
        "max_open_positions", "low_volume").
        """
        if failed_checks:
            checks_str = "\n".join(f"  ❌ {check}" for check in failed_checks)
        else:
            checks_str = "  ❌ (no detail provided)"
        msg = (
            f"🛡️ *TRADE BLOCKED* — {symbol}\n" f"Confidence: `{signal_confidence:.2f}`\n" f"Failed gates:\n{checks_str}"
        )
        return await self._send_event(msg, event="protection_trigger")

    async def send_heartbeat(
        self,
        uptime_hours: float,
        open_positions: int,
        portfolio_value: float,
        daily_pnl_pct: float,
        mode: str,
    ) -> bool:
        """Send the periodic "still alive" heartbeat.

        Defaults to ``off`` in the config — operators must opt-in by setting
        ``notification_settings.heartbeat`` to ``on`` or ``silent``.
        """
        pnl_emoji = "🟢" if (daily_pnl_pct or 0) >= 0 else "🔴"
        now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")
        msg = (
            f"💓 *Bot Alive* — {now_utc}\n"
            f"Uptime: `{uptime_hours:.1f}h` | Mode: `{mode}`\n"
            f"Positions: `{open_positions}` open\n"
            f"Portfolio: `${portfolio_value:,.2f}`\n"
            f"{pnl_emoji} Today: `{daily_pnl_pct:+.2f}%`"
        )
        return await self._send_event(msg, event="heartbeat")


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


def format_fatal_auth_alert(details: str, *, title: str = "FATAL: Bitkub Auth Error") -> str:
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
    quote_asset: str = "THB",
) -> str:
    """Format a trade alert message.
    
    Args:
        quote_asset: Quote currency (default "THB" for Binance TH, can be "USDT" for other exchanges)
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
    quote_asset: str = "THB",
) -> str:
    """Format a status alert message.
    
    Args:
        quote_asset: Quote currency (default "THB" for Binance TH, can be "USDT" for other exchanges)
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


# --- NEW: SPEC_09 (Freqtrade-style) ---
# Two-way Telegram command handler. Polls ``getUpdates`` on a short interval
# and dispatches authorized commands to the trading bot. Inspired by
# Freqtrade's command set, trimmed to the surface a crypto sniper needs.
# Static command definitions (help text with placeholder for dynamic currency)
_COMMAND_HELP: Dict[str, str] = {
    "/status": "แสดง open positions ทั้งหมด",
    "/balance": "ดู {quote} balance",
    "/profit": "แสดง P&L วันนี้ + สัปดาห์นี้",
    "/stop": "หยุด bot ฉุกเฉิน (close ทุก position)",
    "/pause": "หยุดเปิด position ใหม่ (manage เก่าต่อ)",
    "/resume": "กลับมาเทรดปกติ",
    "/mode": "ดู / เปลี่ยน trading mode",
    "/count": "จำนวน trades วันนี้ / สัปดาห์",
    "/logs": "แสดง log 10 บรรทัดล่าสุด",
    "/help": "แสดง commands ทั้งหมด",
}


def get_commands(quote_asset: str = "USDT") -> Dict[str, str]:
    """Return commands dict with dynamic currency placeholder filled."""
    return {cmd: desc.format(quote=quote_asset) for cmd, desc in _COMMAND_HELP.items()}


# Backward compatible: default to USDT for existing callers
COMMANDS: Dict[str, str] = get_commands("USDT")


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it is a coroutine, otherwise return it as-is.

    Lets command handlers call bot methods that may be either ``def`` or
    ``async def`` without forcing the integration layer to commit to one.
    """
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value  # type: ignore[arg-type]
    return value


class TelegramCommandHandler:
    """Polls Telegram ``getUpdates`` and dispatches commands to the trading bot.

    Security
    --------
    Only messages from the configured ``chat_id`` are processed. All other
    chats are silently ignored — never echoed, never replied to.

    Lifecycle
    ---------
    The handler is async-native (uses ``aiohttp`` for non-blocking long-poll).
    Use :meth:`start_polling` from inside an asyncio event loop, or
    :meth:`start_in_thread` to run it on its own background event loop when
    the caller is fully synchronous (e.g. legacy ``trading_bot.py``).
    """

    BASE_URL = "https://api.telegram.org/bot{token}"
    POLL_INTERVAL_SECONDS = 5
    LONG_POLL_TIMEOUT_SECONDS = 3
    HTTP_TIMEOUT_SECONDS = 15

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        trading_bot: Any,
        *,
        enabled: bool = True,
    ) -> None:
        self.token = str(bot_token or "")
        self.chat_id = str(chat_id or "")
        self.bot = trading_bot
        self._offset = 0
        self._running = False
        self._stop_event: Optional[asyncio.Event] = None
        self._thread: Optional[Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # ``enabled`` lets callers wire the handler regardless of config and
        # then decide at runtime whether to actually poll. Polling is also
        # gated on having a token + chat_id + aiohttp installed.
        self.enabled = bool(enabled and self.token and self.chat_id and aiohttp is not None)
        if enabled and aiohttp is None:
            logger.warning(
                "TelegramCommandHandler disabled: aiohttp is not installed. "
                "Run `pip install aiohttp` to enable two-way commands."
            )

    @property
    def running(self) -> bool:
        return self._running

    async def start_polling(self) -> None:
        """Block until :meth:`stop` is called, polling Telegram for commands."""
        if not self.enabled:
            logger.info("[TelegramCmd] Polling skipped — handler disabled")
            return
        if self._running:
            logger.warning("[TelegramCmd] start_polling called while already running")
            return

        self._running = True
        self._stop_event = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        logger.info("[TelegramCmd] Command polling started (chat_id=%s)", self.chat_id)

        # Drop any pending webhook so getUpdates can be used.
        await self._delete_webhook_safely()

        try:
            while self._running:
                try:
                    await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover - defensive
                    logger.error("[TelegramCmd] Poll error: %s", exc)
                # Sleep between polls but break out promptly on stop().
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.POLL_INTERVAL_SECONDS,
                    )
                    break
                except asyncio.TimeoutError:
                    continue
        finally:
            self._running = False
            self._stop_event = None
            self._loop = None
            logger.info("[TelegramCmd] Command polling stopped")

    def stop(self) -> None:
        """Signal the polling loop to exit on the next iteration."""
        self._running = False
        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and stop_event is not None and not stop_event.is_set():
            loop.call_soon_threadsafe(stop_event.set)

    def start_in_thread(self) -> Optional[Thread]:
        """Run :meth:`start_polling` on a dedicated background event loop.

        This adapter exists for sync callers (e.g. the current
        ``trading_bot.py`` main loop) until trading_bot is migrated to
        asyncio. Returns the spawned thread, or ``None`` when the handler is
        disabled.
        """
        if not self.enabled:
            logger.info("[TelegramCmd] start_in_thread skipped — handler disabled")
            return None
        if self._thread is not None and self._thread.is_alive():
            return self._thread

        def _runner() -> None:
            try:
                asyncio.run(self.start_polling())
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("[TelegramCmd] Background loop crashed: %s", exc)

        thread = Thread(target=_runner, name="TelegramCmdHandler", daemon=True)
        thread.start()
        self._thread = thread
        return thread

    # ── HTTP plumbing ──────────────────────────────────────────────────

    def _api_url(self, method: str) -> str:
        return f"{self.BASE_URL.format(token=self.token)}/{method}"

    async def _delete_webhook_safely(self) -> None:
        if aiohttp is None:
            return
        try:
            timeout = aiohttp.ClientTimeout(total=self.HTTP_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self._api_url("deleteWebhook"),
                    json={"drop_pending_updates": False},
                ) as resp:
                    await resp.read()
        except Exception as exc:
            logger.debug("[TelegramCmd] deleteWebhook ignored: %s", exc)

    async def _poll_once(self) -> None:
        if aiohttp is None:
            return
        params = {"offset": self._offset, "timeout": self.LONG_POLL_TIMEOUT_SECONDS}
        timeout = aiohttp.ClientTimeout(total=self.LONG_POLL_TIMEOUT_SECONDS + self.HTTP_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self._api_url("getUpdates"), params=params) as resp:
                if resp.status >= 400:
                    body = (await resp.text())[:200]
                    logger.warning("[TelegramCmd] getUpdates HTTP %s: %s", resp.status, body)
                    return
                data = await resp.json()

        if not data.get("ok", True):
            logger.warning("[TelegramCmd] getUpdates not ok: %s", data.get("description"))
            return

        for update in data.get("result", []) or []:
            try:
                self._offset = int(update["update_id"]) + 1
            except (KeyError, TypeError, ValueError):
                continue

            msg = update.get("message") or update.get("edited_message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()

            # Security gate: only respond to the configured chat_id.
            if not chat_id or chat_id != self.chat_id:
                logger.info(
                    "[TelegramCmd] Ignored message from unauthorized chat_id=%s",
                    chat_id or "<empty>",
                )
                continue

            if not text:
                continue

            try:
                await self._handle_command(text)
            except Exception as exc:
                logger.error("[TelegramCmd] handler error for %r: %s", text, exc)
                await self._reply(f"❌ Error: `{exc}`")

    async def _reply(self, message: str) -> None:
        """Send ``message`` back to the authorized chat.

        Uses aiohttp directly so we can reply even when the bot's primary
        ``AlertSystem`` is rate-limited.
        """
        if aiohttp is None or not self.token or not self.chat_id:
            return
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=self.HTTP_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self._api_url("sendMessage"), json=payload) as resp:
                    if resp.status >= 400:
                        body = (await resp.text())[:200]
                        logger.warning(
                            "[TelegramCmd] sendMessage HTTP %s: %s",
                            resp.status,
                            body,
                        )
        except Exception as exc:
            logger.error("[TelegramCmd] Reply failed: %s", exc)

    # ── Command dispatch ───────────────────────────────────────────────

    async def _handle_command(self, text: str) -> None:
        # Lower-case the command verb only — preserve argument casing for
        # things like /mode that need exact-match strategy names.
        parts = text.split()
        if not parts:
            return
        cmd = parts[0].lower()

        handlers: Dict[str, Callable[[str], Awaitable[None]]] = {
            "/status": self._cmd_status,
            "/balance": self._cmd_balance,
            "/profit": self._cmd_profit,
            "/stop": self._cmd_stop,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
            "/mode": self._cmd_mode,
            "/count": self._cmd_count,
            "/logs": self._cmd_logs,
            "/help": self._cmd_help,
            "/start": self._cmd_help,
        }
        handler = handlers.get(cmd)
        if handler is None:
            return
        await handler(text)

    async def _bot_call(self, attr: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke ``self.bot.<attr>(*args)`` whether sync or async, with safety."""
        method = getattr(self.bot, attr, None)
        if method is None:
            raise AttributeError(f"trading_bot has no method '{attr}'")
        if not callable(method):
            return method
        result = method(*args, **kwargs)
        return await _maybe_await(result)

    async def _cmd_status(self, _text: str) -> None:
        try:
            positions = await self._bot_call("get_open_positions") or []
        except AttributeError:
            await self._reply("⚠️ `get_open_positions` ยังไม่ได้ implement บน bot")
            return
        if not positions:
            await self._reply("📭 ไม่มี open positions")
            return

        lines = ["📊 *Open Positions*\n"]
        for p in positions:
            pnl_pct = float(p.get("pnl_pct", 0) or 0)
            emoji = "🟢" if pnl_pct >= 0 else "🔴"
            lines.append(
                f"{emoji} *{p.get('symbol', '?')}*\n"
                f"  Entry: `${float(p.get('entry_price', 0) or 0):,.4f}`"
                f" | Now: `${float(p.get('current_price', 0) or 0):,.4f}`\n"
                f"  PnL: `{pnl_pct:+.2f}%`"
                f" (`{float(p.get('pnl_usdt', 0) or 0):+.2f} USDT`)\n"
                f"  SL: `${float(p.get('stop_loss', 0) or 0):,.4f}`"
                f" | Age: `{float(p.get('hold_minutes', 0) or 0):.0f}m`"
            )
        await self._reply("\n".join(lines))

    async def _cmd_balance(self, _text: str) -> None:
        try:
            balance = await self._bot_call("get_balance") or {}
            portfolio = await self._bot_call("get_portfolio_value") or 0.0
        except AttributeError:
            await self._reply("⚠️ `get_balance`/`get_portfolio_value` ยังไม่ได้ implement")
            return

        usdt = balance.get("USDT") or {}
        free = float(usdt.get("available", 0) or 0)
        reserved = float(usdt.get("reserved", 0) or 0)
        await self._reply(
            f"💰 *Balance*\n"
            f"USDT: `${free:,.2f}` (free)\n"
            f"USDT: `${reserved:,.2f}` (in orders)\n"
            f"Portfolio: `${float(portfolio):,.2f}`"
        )

    async def _cmd_profit(self, _text: str) -> None:
        try:
            stats = await self._bot_call("get_daily_stats") or {}
        except AttributeError:
            await self._reply("⚠️ `get_daily_stats` ยังไม่ได้ implement บน bot")
            return
        await self._reply(
            f"📈 *Profit Summary*\n"
            f"Today: `{float(stats.get('day_pnl_pct', 0) or 0):+.2f}%`"
            f" (`{float(stats.get('day_pnl_usdt', 0) or 0):+.2f} USDT`)\n"
            f"Week:  `{float(stats.get('week_pnl_pct', 0) or 0):+.2f}%`"
            f" (`{float(stats.get('week_pnl_usdt', 0) or 0):+.2f} USDT`)\n"
            f"Trades today: `{int(stats.get('trades_today', 0) or 0)}`\n"
            f"Win rate: `{float(stats.get('win_rate', 0) or 0):.1f}%`"
        )

    async def _cmd_stop(self, _text: str) -> None:
        await self._reply("🛑 *Emergency Stop* — closing all positions...")
        try:
            await self._bot_call("emergency_stop")
        except AttributeError:
            await self._reply("⚠️ `emergency_stop` ยังไม่ได้ implement บน bot")
            return
        except Exception as exc:
            await self._reply(f"❌ emergency_stop failed: `{exc}`")
            return
        await self._reply("✅ Bot stopped. All positions closed.")

    async def _cmd_pause(self, _text: str) -> None:
        try:
            await self._bot_call("set_paused", True)
        except AttributeError:
            # Soft-fall to a direct attribute set so legacy bots still work.
            try:
                setattr(self.bot, "_paused", True)
            except Exception as exc:
                await self._reply(f"❌ Pause failed: `{exc}`")
                return
        await self._reply(
            "⏸️ *Bot Paused*\n" "จะไม่เปิด position ใหม่\n" "ของเก่าจะยัง manage ต่อ\n" "พิมพ์ /resume เพื่อกลับมาเทรด"
        )

    async def _cmd_resume(self, _text: str) -> None:
        try:
            await self._bot_call("set_paused", False)
        except AttributeError:
            try:
                setattr(self.bot, "_paused", False)
            except Exception as exc:
                await self._reply(f"❌ Resume failed: `{exc}`")
                return
        await self._reply("▶️ *Bot Resumed* — กลับมาเทรดปกติแล้ว")

    async def _cmd_mode(self, text: str) -> None:
        parts = text.split()
        if len(parts) == 1:
            try:
                current = await self._bot_call("get_current_mode")
            except AttributeError:
                current = "?"
            await self._reply(
                f"⚙️ *Trading Mode*: `{current}`\n"
                "เปลี่ยนได้: `/mode scalping` | `/mode trend_only` | `/mode standard`"
            )
            return

        new_mode = parts[1].lower()
        valid = ["scalping", "trend_only", "standard"]
        if new_mode not in valid:
            await self._reply(f"❌ Mode ไม่ถูกต้อง. Valid: `{', '.join(valid)}`")
            return
        try:
            await self._bot_call("set_mode", new_mode)
        except AttributeError:
            await self._reply("⚠️ `set_mode` ยังไม่ได้ implement บน bot")
            return
        except Exception as exc:
            await self._reply(f"❌ set_mode failed: `{exc}`")
            return
        await self._reply(f"✅ Mode เปลี่ยนเป็น `{new_mode}`")

    async def _cmd_count(self, _text: str) -> None:
        try:
            stats = await self._bot_call("get_trade_counts") or {}
        except AttributeError:
            await self._reply("⚠️ `get_trade_counts` ยังไม่ได้ implement บน bot")
            return
        await self._reply(
            f"🔢 *Trade Counts*\n"
            f"Today: `{int(stats.get('today', 0) or 0)}` trades\n"
            f"This week: `{int(stats.get('week', 0) or 0)}` trades\n"
            f"Open positions: `{int(stats.get('open', 0) or 0)}`"
        )

    async def _cmd_logs(self, _text: str) -> None:
        try:
            logs = await self._bot_call("get_last_logs", 10) or []
        except AttributeError:
            await self._reply("⚠️ `get_last_logs` ยังไม่ได้ implement บน bot")
            return
        if not logs:
            await self._reply("📋 (no recent logs)")
            return
        log_text = "\n".join(f"`{line}`" for line in list(logs)[-10:])
        await self._reply(f"📋 *Last 10 Logs*\n{log_text}")

    async def _cmd_help(self, _text: str) -> None:
        # Try to get quote_asset from bot config, default to USDT
        quote_asset = "USDT"
        try:
            config = getattr(self.bot, "config", {}) or {}
            data_cfg = config.get("data", {}) or {}
            hybrid_cfg = data_cfg.get("hybrid_dynamic_coin_config", {}) or {}
            quote_asset = str(hybrid_cfg.get("quote_asset") or "USDT").upper()
        except Exception:
            pass
        
        lines = ["🤖 *Available Commands*\n"]
        for cmd, desc in get_commands(quote_asset).items():
            lines.append(f"`{cmd}` — {desc}")
        await self._reply("\n".join(lines))
