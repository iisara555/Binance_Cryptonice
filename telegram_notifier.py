# DEPRECATED: This module has been consolidated into alerts.py
# All functionality has been moved to the unified alerts.py module
# Please update your imports to use 'from alerts import ...' instead

import warnings
warnings.warn(
    "telegram_notifier.py is deprecated. Use 'from alerts import ...' instead.",
    DeprecationWarning,
    stacklevel=2
)

"""
Telegram Notifier — Compact HTML Templates
=============================================
All bot notifications go through here with fault tolerance.

Alert Levels:
  TRADE    → Always to Telegram (order executed)
  CRITICAL → Always to Telegram (API errors, circuit breaker, kill)
  SUMMARY  → Always to Telegram (status, PnL)
  INFO     → Log ONLY (heartbeats, analyzing)
  DEBUG    → Log ONLY

Parse Mode: HTML (not Markdown) — cleaner on mobile
"""

import os
import time
import json
import logging
import requests
from typing import Dict, Any, Optional, List
from datetime import datetime

# Import AlertLevel from unified alerts module
from alerts import AlertLevel

logger = logging.getLogger(__name__)


# ─── Alert Router (Spam Filter) ───────────────────────────────────────────

class AlertRouter:
    """
    Routes messages: sends TELEGRAM for TRADE/CRITICAL/SUMMARY,
    logs everything else to local .log file only.

    Usage:
        router = AlertRouter(notifier, min_level=AlertLevel.SUMMARY)
        router.alert(AlertLevel.TRADE, format_trade_alert(...))
        router.alert(AlertLevel.INFO, "Bot is running...")  # Log only
    """

    TELEGRAM_LEVELS = {AlertLevel.TRADE, AlertLevel.CRITICAL, AlertLevel.SUMMARY}

    def __init__(self, notifier=None, min_level: str = AlertLevel.SUMMARY):
        self.notifier = notifier
        self.min_level = min_level

    def alert(self, level: str, message: str) -> bool:
        """
        Route alert — send to Telegram or log only.

        Args:
            level: AlertLevel.* constant
            message: Formatted text (HTML)
        Returns:
            True if sent to Telegram, False otherwise
        """
        # INFO/DEBUG → log only
        if level not in self.TELEGRAM_LEVELS:
            logger.info(f"[{level.upper()}] {message[:200]}")
            return False

        # TRADE/CRITICAL/SUMMARY → Telegram
        if self.notifier is None:
            logger.warning(f"[{level.upper()}] No notifier configured")
            return False

        sent = self.notifier.send(message)
        if sent:
            logger.info(f"[{level.upper()}] Sent to Telegram")
        else:
            logger.warning(f"[{level.upper()}] Failed to send to Telegram")

        return sent


# ─── Compact HTML Templates ───────────────────────────────────────────────

def _ts() -> str:
    """Timestamp: DD/MM/YYYY HH:MM:SS"""
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def format_trade_alert(
    symbol: str,
    side: str,
    price: float,
    amount: float,
    value_thb: float,
    pnl_amt: Optional[float] = None,
    pnl_pct: Optional[float] = None,
    status: str = "filled",
    extra: Optional[str] = None,
) -> str:
    is_buy = side.upper() == "BUY"
    coin = symbol.replace("THB_", "")
    action = "เข้าซื้อ" if is_buy else "ออกขาย"

    if status in ("rejected", "error"):
        emoji = "❌"
    elif pnl_amt is not None and pnl_amt >= 0:
        emoji = "🟢"
    elif pnl_amt is not None:
        emoji = "🔴"
    else:
        emoji = "🟢" if is_buy else "🔴"

    lines = [
        f"{emoji} <b>{action}</b>  {coin}  ({status})",
        f"{'─' * 20}",
        f"ราคา  <code>{price:,.2f}</code> THB",
        f"จำนวน  <code>{amount:.6f}</code> {coin} ≈ <code>{value_thb:,.2f}</code> THB",
    ]

    if pnl_amt is not None and pnl_pct is not None:
        pnl_sign = "+" if pnl_amt >= 0 else ""
        lines.append(f"ผลลัพธ์  <code>{pnl_sign}{pnl_amt:,.2f}</code> THB ({pnl_sign}{pnl_pct:.2f}%)")

    if extra:
        lines.append(f"หมายเหตุ: {extra}")

    lines.append(f"🕐 {_ts()}")
    return "\n".join(lines)


def format_error_alert(
    title: str,
    details: str,
    status: str = "error",
) -> str:
    lines = [
        f"🚨 <b>{title}</b>",
        f"{'─' * 20}",
    ]
    if details:
        lines.append(details)
    lines.append(f"Status: <code>{status}</code>")
    lines.append(f"🕐 {_ts()}")
    return "\n".join(lines)


def format_status_alert(
    balance_thb: float,
    portfolio_value: float,
    pnl_amt: float,
    pnl_pct: float,
    uptime: Optional[str] = None,
    pairs_status: Optional[List[str]] = None,
) -> str:
    _ = uptime
    pnl_emoji = "🟢" if pnl_amt >= 0 else "🔴"
    pnl_sign = "+" if pnl_amt >= 0 else ""

    lines = [
        f"📊 <b>สรุปพอร์ตรายวัน</b>",
        f"{'─' * 20}",
        f"ยอดรวม  <code>{portfolio_value:,.2f}</code> THB",
        f"{pnl_emoji} กำไร/ขาดทุน  <code>{pnl_sign}{pnl_amt:,.2f}</code> THB ({pnl_sign}{pnl_pct:.2f}%)",
        f"เงินสด  <code>{balance_thb:,.2f}</code> THB",
    ]

    if pairs_status:
        lines.append(f"ถือครอง  {len(pairs_status)} เหรียญ")

    lines.append(f"🕐 {_ts()}")
    return "\n".join(lines)



# ─── Telegram API Helpers ──────────────────────────────────────────────────

def telegram_send_message(bot_token: str, chat_id: str, text: str,
                         reply_markup: Optional[Dict] = None, parse_mode: str = "HTML") -> bool:
    """Send message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Telegram send failed: {resp.status_code} - {resp.text[:200]}")
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def telegram_edit_message(bot_token: str, chat_id: str, message_id: int,
                         text: str, reply_markup: Optional[Dict] = None) -> bool:
    """Edit an existing message."""
    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Telegram edit failed: {e}")
        return False


def telegram_answer_callback(bot_token: str, callback_query_id: str,
                             text: Optional[str] = None) -> bool:
    """Answer a callback query."""
    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Telegram callback answer failed: {e}")
        return False


# ─── Main Notifier Class ───────────────────────────────────────────────────

class TelegramNotifier:
    """
    Centralized Telegram notification handler.
    All bot notifications go through here with fault tolerance.

    Usage:
        notifier = TelegramNotifier(token, chat_id, pairs=["THB_BTC"])
        router = AlertRouter(notifier)
        router.alert(AlertLevel.TRADE, format_trade_alert(...))
    """

    def __init__(self, bot_token: str, chat_id: str, pairs: Optional[List[str]] = None):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.pairs = pairs or ["THB_BTC", "THB_DOGE"]
        self.enabled = bool(bot_token and chat_id)

    def send(self, text: str, reply_markup: Optional[Dict] = None) -> bool:
        """Send a message to Telegram."""
        if not self.enabled:
            logger.warning(f"[Notifier] Disabled — would send: {text[:100]}")
            return False
        try:
            return telegram_send_message(self.bot_token, self.chat_id, text, reply_markup)
        except Exception as e:
            logger.error(f"[Notifier] send() failed: {e}")
            return False


# ─── Inline Keyboards ──────────────────────────────────────────────────────

def kill_confirm_keyboard() -> Dict[str, Any]:
    """Inline keyboard for kill confirmation."""
    return {
        "inline_keyboard": [[
            {"text": "⚠️ CONFIRM KILL", "callback_data": "kill_confirm"},
            {"text": "❌ CANCEL", "callback_data": "kill_cancel"},
        ]]
    }

def inline_keyboard() -> Dict[str, Any]:
    """Empty reply markup — removes keyboard."""
    return {"inline_keyboard": []}


# ─── Convenience Functions (for direct use by trading_bot.py) ─────────────

def send_trade_token(bot_token: str, chat_id: str,
                     symbol: str, side: str, price: float,
                     amount: float, value_thb: float,
                     pnl_amt: Optional[float] = None, pnl_pct: Optional[float] = None,
                     status: str = "filled", extra: Optional[str] = None) -> bool:
    """Quick-send a trade alert."""
    msg = format_trade_alert(symbol, side, price, amount, value_thb,
                             pnl_amt, pnl_pct, status, extra)
    return telegram_send_message(bot_token, chat_id, msg)

def send_error_token(bot_token: str, chat_id: str,
                     title: str, details: str,
                     status: str = "error") -> bool:
    """Quick-send an error alert."""
    msg = format_error_alert(title, details, status)
    return telegram_send_message(bot_token, chat_id, msg)

def send_status_token(bot_token: str, chat_id: str,
                      balance_thb: float, portfolio_value: float,
                      pnl_amt: float, pnl_pct: float,
                      uptime: Optional[str] = None, pairs_status: Optional[List[str]] = None) -> bool:
    """Quick-send a status report (for /status command)."""
    msg = format_status_alert(balance_thb, portfolio_value, pnl_amt, pnl_pct,
                              uptime, pairs_status)
    return telegram_send_message(bot_token, chat_id, msg)