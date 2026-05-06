"""
Telegram Bot Handler
====================
Long-polling Telegram bot that handles commands from the user.

Commands:
- /start          : Welcome message + bot status
- /status         : Show current trading status, open orders, balances
- /pairs          : Show configured trading pairs
- /kill           : EMERGENCY STOP - requires inline button confirmation
- /pnl            : P&L summary in quote currency

Usage (integrated into main.py):
  from telegram_bot import TelegramBotHandler
  tg_handler = TelegramBotHandler(app, bot_token, chat_id)
  tg_handler.start()   # starts polling thread
  tg_handler.stop()    # stops polling thread
"""

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from alerts import TelegramSender, escape_html

logger = logging.getLogger(__name__)


def _flatten_exchange_open_orders(payload: Any) -> List[Dict[str, Any]]:
    """Normalize ``get_open_orders`` return value to a list of order rows (handles legacy wrappers)."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, dict):
        inner = payload.get("result")
        if isinstance(inner, list):
            return list(inner)
        return []
    return []


def _extract_cancel_fields(order: Dict[str, Any]) -> Tuple[str, str, str]:
    """Extract order id, symbol pair, and side (lower) for ``cancel_order``."""
    oid = str(order.get("id", "") or order.get("orderId", "") or "")
    pair = str(order.get("symbol", "") or "").strip()
    if not pair:
        raw = order.get("_raw")
        if isinstance(raw, dict):
            pair = str(raw.get("symbol", "") or "").strip()
    raw_side = order.get("side", "sell") or "sell"
    if isinstance(raw_side, str) and raw_side.upper() in {"BUY", "SELL"}:
        side_lower = raw_side.lower()
    else:
        side_lower = "sell"
    return oid, pair, side_lower


def _resolve_sqlite_db_path(app_ref: Any, raw_db_path: str) -> str:
    """Resolve relative DB paths against config directory (same rule as app startup), not cwd."""
    path = Path(raw_db_path or "crypto_bot.db")
    if path.is_absolute():
        return str(path)
    config_path = getattr(app_ref, "_config_path", None)
    if config_path is not None:
        return str(Path(config_path).resolve().parent / path)
    return str(Path(__file__).resolve().parent / path)


def _emergency_kill_collect_open_orders(api: Any, pairs_to_check: List[str]) -> List[Dict[str, Any]]:
    """Fetch all open orders (preferred); on failure fall back to per-pair enumeration."""
    try:
        got = api.get_open_orders(None)
    except Exception as exc:
        logger.error("Emergency kill: get_open_orders(all) failed: %s", exc)
        got = None
    if got is not None:
        return _flatten_exchange_open_orders(got)
    aggregated: List[Dict[str, Any]] = []
    for pair in pairs_to_check:
        try:
            aggregated.extend(_flatten_exchange_open_orders(api.get_open_orders(pair)))
        except Exception as exc:
            logger.error("Emergency kill: get_open_orders(%s) failed: %s", pair, exc)
    return aggregated


def _http_status_code(error: Exception) -> Optional[int]:
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None)


def kill_confirm_keyboard() -> Dict[str, Any]:
    """Inline keyboard for /kill confirmation."""
    return {
        "inline_keyboard": [
            [
                {"text": "โ ๏ธ CONFIRM KILL", "callback_data": "kill_confirm"},
                {"text": "โ CANCEL", "callback_data": "kill_cancel"},
            ]
        ]
    }


def no_keyboard() -> Dict[str, Any]:
    """Empty reply markup to remove keyboard."""
    return {"inline_keyboard": []}


def _normalize_ticker_symbol(symbol: str) -> str:
    """Normalize exchange ticker rows to a known runtime symbol."""
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    if raw.endswith("USDT"):
        return raw
    return ""


def _extract_base_asset(symbol: str, quote_asset: str = "USDT") -> str:
    raw = str(symbol or "").strip().upper()
    quote = str(quote_asset or "USDT").strip().upper()
    if not raw:
        return ""
    if quote and raw.endswith(quote):
        return raw[: -len(quote)]
    return raw


def _build_pair(base_asset: str, quote_asset: str = "USDT") -> str:
    base = str(base_asset or "").strip().upper()
    quote = str(quote_asset or "USDT").strip().upper()
    if not base:
        return ""
    return f"{base}{quote}"


class TelegramBotHandler:
    """
    Long-polling Telegram bot handler.

    Args:
        app_ref:          Reference to TradingBotApp
        bot_token:        Telegram bot token
        chat_id:           Authorized Telegram chat ID (str)
        pairs:             List of trading pairs
        trading_disabled:  Shared threading.Event that signals trading is disabled
    """

    def __init__(
        self,
        app_ref,
        bot_token: str,
        chat_id: str,
        pairs: Optional[List[str]] = None,
        trading_disabled: Optional[threading.Event] = None,
    ):
        self.app_ref = app_ref
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.pairs = list(pairs) if pairs is not None else []
        self.trading_disabled = trading_disabled or threading.Event()
        shared_alert_system = getattr(app_ref, "alert_system", None)
        shared_telegram = getattr(shared_alert_system, "telegram", None)
        self.telegram = (
            shared_telegram if isinstance(shared_telegram, TelegramSender) else TelegramSender(bot_token, self.chat_id)
        )

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._offset = 0
        self._lock = threading.Lock()

        self._pending_kill_msg_id: Optional[int] = None
        self._base_assets = ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOT", "LINK", "DOGE", "POL"]
        self._start_time = time.time()

    # โ”€โ”€ Public API โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

    def start(self):
        """Start the polling thread."""
        if self._running:
            return
        if not self.telegram.api_enabled:
            logger.info("Telegram bot handler not started: Telegram API transport is unavailable")
            return
        try:
            self.telegram.delete_webhook(drop_pending_updates=True)
            logger.info("Cleared Telegram webhook")
        except requests.exceptions.HTTPError as e:
            if _http_status_code(e) in {401, 403} or getattr(self.telegram, "auth_failed", False):
                logger.error(
                    "Telegram bot handler disabled due to authentication failure. "
                    "Fix Telegram credentials or set notifications.telegram_command_polling_enabled=false."
                )
                return
            logger.warning("Could not delete webhook: %s", e)
        except Exception as e:
            logger.warning("Could not delete webhook: %s", e)

        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="TelegramPolling")
        self._thread.start()
        logger.info("Telegram bot handler started")

    def stop(self):
        """Stop the polling thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Telegram bot handler stopped")

    # โ”€โ”€ Polling loop โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

    def _poll_loop(self):
        """Background thread: long-poll Telegram for updates."""
        while self._running:
            try:
                updates = self.telegram.get_updates(offset=self._offset, timeout=30)
                if updates.get("ok") and updates.get("result"):
                    for update in updates["result"]:
                        self._offset = max(self._offset, update["update_id"]) + 1
                        self._dispatch(update)
            except requests.exceptions.HTTPError as e:
                status_code = _http_status_code(e)
                if status_code == 409:
                    logger.error(
                        "Telegram Error 409 (Conflict): Another Telegram client already owns getUpdates for this token. "
                        "Disabling command polling for this process to avoid repeated conflicts. "
                        "Stop the other polling/webhook consumer, rotate the token, or set "
                        "notifications.telegram_command_polling_enabled=false if polling is not needed here."
                    )
                    self._running = False
                    break
                elif status_code in {401, 403} or getattr(self.telegram, "auth_failed", False):
                    logger.error(
                        "Telegram polling disabled due to authentication failure. "
                        "Fix Telegram credentials or disable command polling in bot_config."
                    )
                    self._running = False
                    break
                else:
                    logger.error("Telegram poll HTTP error: %s", e)
                    time.sleep(5)
            except requests.exceptions.Timeout:
                continue
            except Exception as e:
                if getattr(self.telegram, "auth_failed", False):
                    logger.error(
                        "Telegram polling disabled after transport authentication failure: %s",
                        getattr(self.telegram, "auth_failure_reason", e),
                    )
                    self._running = False
                    break
                logger.error("Telegram poll error: %s", e)
                time.sleep(5)

    def _dispatch(self, update: Dict[str, Any]):
        """Route an incoming update to the appropriate handler."""
        try:
            if "callback_query" in update:
                self._handle_callback(update["callback_query"])
            elif "message" in update:
                self._handle_message(update["message"])
        except Exception as e:
            logger.error("Error dispatching update: %s", e)

    # โ”€โ”€ Message handler โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

    def _handle_message(self, msg: Dict[str, Any]):
        """Handle incoming text messages."""
        chat = msg.get("chat", {})
        if str(chat.get("id", "")) != self.chat_id:
            return

        text = msg.get("text", "").strip()

        if text == "/start":
            self._cmd_start()
        elif text == "/status":
            self._cmd_status()
        elif text == "/pairs":
            self._cmd_pairs()
        elif text == "/kill":
            self._cmd_kill()
        elif text == "/resume":
            self._cmd_resume()
        elif text == "/pnl":
            try:
                self._cmd_pnl()
            except Exception as e:
                self._send("โ /pnl error: %s", e)
        else:
            self._send(
                "๐ค– <b>เธเธณเธชเธฑเนเธเธ—เธตเนเนเธเนเนเธ”เน</b>\n\n"
                "/status  เธชเธ–เธฒเธเธฐเธเธญเธฃเนเธ•\n"
                "/pnl  เธเธณเนเธฃเธเธฒเธ”เธ—เธธเธ\n"
                "/kill  เธซเธขเธธเธ”เธเธธเธเน€เธเธดเธ\n"
                "/resume  เธเธฅเธฑเธเธกเธฒเน€เธ—เธฃเธ”"
            )

    # โ”€โ”€ Callback handler โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

    def _handle_callback(self, cq: Dict[str, Any]):
        """Handle inline button presses."""
        msg = cq.get("message", {})
        chat_id = str((msg.get("chat", {}) or {}).get("id", cq.get("from", {}).get("id", "")))
        if chat_id != self.chat_id:
            return

        data = cq.get("data", "")
        message_id = msg.get("message_id")
        cq_id = cq.get("id")

        if data == "kill_confirm":
            if cq_id is not None:
                try:
                    self.telegram.answer_callback(str(cq_id))
                except Exception as e:
                    logger.warning("Failed to answer callback kill_confirm: %s", e)
            threading.Thread(target=self._execute_kill, args=(msg,), daemon=True, name="emergency-kill").start()
        elif data == "kill_cancel":
            if cq_id is not None:
                try:
                    self.telegram.answer_callback(str(cq_id))
                except Exception as e:
                    logger.warning("Failed to answer callback kill_cancel: %s", e)
            if message_id:
                self.telegram.edit_message(
                    message_id,
                    "โ <b>Kill Cancelled</b>\n\nเธเธฒเธฃเน€เธ—เธฃเธ”เธขเธฑเธเธเธเธ—เธณเธเธฒเธเธ•เนเธญ",
                    reply_markup=no_keyboard(),
                )
        elif data == "resume_confirm":
            if cq_id is not None:
                try:
                    self.telegram.answer_callback(str(cq_id))
                except Exception as e:
                    logger.warning("Failed to answer callback resume_confirm: %s", e)
            self._execute_resume(msg)

    # โ”€โ”€ Command handlers โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

    def _cmd_start(self):
        disabled = self.trading_disabled.is_set()
        bot_ref = getattr(self.app_ref, "bot", None)
        bot_status = bot_ref.get_status() if bot_ref and hasattr(bot_ref, "get_status") else {}
        degraded = bool((bot_status.get("auth_degraded") or {}).get("active", False))
        degraded_reason = str((bot_status.get("auth_degraded") or {}).get("reason") or "")
        status_emoji = "๐ก DEGRADED" if degraded else ("๐”ด DISABLED" if disabled else "๐ข ACTIVE")
        mode = bot_status.get("mode") or getattr(self.app_ref, "config", {}).get("mode", "unknown")
        pairs = bot_status.get("trading_pairs") or self.pairs

        text = (
            f"๐ค– <b>Binance Thailand Trading Bot</b>\n"
            f"{'โ”€' * 20}\n"
            f"เธชเธ–เธฒเธเธฐ  {status_emoji}\n"
            f"เนเธซเธกเธ”  {mode}\n"
            f"เธเธนเนเน€เธ—เธฃเธ”  {', '.join(pairs)}\n"
            f"\n"
            f"<b>เธเธณเธชเธฑเนเธ</b>\n"
            f"/status  เธชเธ–เธฒเธเธฐเธเธญเธฃเนเธ•\n"
            f"/pnl  เธเธณเนเธฃเธเธฒเธ”เธ—เธธเธ\n"
            f"/pairs  เธเธนเนเน€เธ—เธฃเธ”\n"
            f"/kill  เธซเธขเธธเธ”เธเธธเธเน€เธเธดเธ\n"
            f"/resume  เธเธฅเธฑเธเธกเธฒเน€เธ—เธฃเธ”"
        )
        if degraded:
            text += (
                f"\n\nโ ๏ธ <b>Degraded Mode</b>\n"
                f"{degraded_reason or 'Exchange private API unavailable'}\n"
                f"เธเธฒเธฃเน€เธ—เธฃเธ”เธ–เธนเธเธเธดเธ”เนเธเนเธเธฒเธเธเธเธเธงเนเธฒเธเธฐเนเธเนเนเธ credentials"
            )
        self._send(text)

    @staticmethod
    def _safe_balance_amount(payload: Any) -> float:
        if isinstance(payload, dict):
            return float(payload.get("available", 0.0) or 0.0)
        return float(payload or 0.0)

    def _get_status_balances(self) -> Dict[str, Dict[str, float]]:
        """Prefer monitor snapshot to avoid duplicate private API calls on /status."""
        state_balances = {}
        if hasattr(self.app_ref, "get_balance_state"):
            state = self.app_ref.get_balance_state() or {}
            state_balances = state.get("balances") or {}

        normalized = {
            str(sym).upper(): {"available": self._safe_balance_amount(payload)}
            for sym, payload in state_balances.items()
        }
        if normalized:
            return normalized

        api = self.app_ref.api_client
        return api.get_balances()

    def _get_quote_asset(self) -> str:
        config = getattr(self.app_ref, "config", {}) or {}
        hybrid_cfg = (config.get("data", {}) or {}).get("hybrid_dynamic_coin_config", {}) or {}
        quote = str(hybrid_cfg.get("quote_asset") or "").strip().upper()
        if quote:
            return quote
        configured_pairs = list(self.pairs or [])
        bot_ref = getattr(self.app_ref, "bot", None)
        if bot_ref and hasattr(bot_ref, "get_status"):
            try:
                status_pairs = bot_ref.get_status().get("trading_pairs") or []
                configured_pairs.extend(status_pairs)
            except Exception:
                pass
        for pair in configured_pairs:
            text = str(pair or "").upper()
            if text.endswith("USDT"):
                return "USDT"
        return "USDT"

    def _get_cached_price(self, symbol: str) -> Optional[float]:
        """Fast price lookup that avoids network calls in Telegram /status."""
        quote_asset = self._get_quote_asset()
        pair = _build_pair(symbol, quote_asset)

        cache = getattr(self.app_ref, "_cli_price_cache", {}) or {}
        cached = cache.get(pair)
        if isinstance(cached, tuple) and cached:
            try:
                return float(cached[0]) if cached[0] is not None else None
            except Exception:
                return None
        return None

    def _cmd_status(self):
        """Clean /status command with portfolio overview."""
        disabled = self.trading_disabled.is_set()
        bot_ref = getattr(self.app_ref, "bot", None)
        bot_status = bot_ref.get_status() if bot_ref and hasattr(bot_ref, "get_status") else {}
        degraded_info = bot_status.get("auth_degraded") or {}
        auth_degraded = bool(degraded_info.get("active", False))
        auth_reason = escape_html(degraded_info.get("reason") or "")
        uptime_secs = time.time() - self._start_time
        hours, rem = divmod(int(uptime_secs), 3600)
        mins, secs = divmod(rem, 60)
        uptime_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m {secs}s"
        status_text = "๐ก DEGRADED" if auth_degraded else ("๐”ด DISABLED" if disabled else "๐ข ACTIVE")

        text = f"๐“ <b>เธชเธ–เธฒเธเธฐเธฃเธฐเธเธ</b>\n" f"{'โ”€' * 20}\n" f"เธชเธ–เธฒเธเธฐ  {status_text}\n" f"Uptime  {uptime_str}\n"

        if auth_degraded:
            text += (
                f"\nโ ๏ธ <b>Degraded Mode</b>\n"
                f"{auth_reason or 'Exchange private API unavailable'}\n"
                f"เธเธฒเธฃเน€เธ—เธฃเธ”เธ–เธนเธเธเธดเธ”เนเธเนเธเธฒเธ\n"
            )
        else:
            try:
                balances = self._get_status_balances()
                quote_asset = self._get_quote_asset()

                quote_balance = self._safe_balance_amount(balances.get(quote_asset, {}))
                total_value = quote_balance

                holdings = []
                for sym, bal in balances.items():
                    sym = str(sym).upper()
                    avail = self._safe_balance_amount(bal)
                    if sym != quote_asset and avail > 0.0001:
                        price = self._get_cached_price(sym)
                        if price is not None and price > 0:
                            val = avail * price
                            total_value += val
                            holdings.append(f"  {sym}  <code>{avail:.6f}</code>  ~{val:,.2f} {quote_asset}")
                        else:
                            holdings.append(f"  {sym}  <code>{avail:.6f}</code>  ~N/A")

                initial = float(self.app_ref.config.get("portfolio", {}).get("initial_balance", 500.0))
                pnl = total_value - initial
                pnl_pct = (pnl / initial) * 100 if initial > 0 else 0
                pnl_emoji = "๐ข" if pnl >= 0 else "๐”ด"

                text += f"\n<b>เธเธญเธฃเนเธ•</b>\n" f"เน€เธเธดเธเธชเธ”  <code>{quote_balance:,.2f}</code> {quote_asset}\n"
                if holdings:
                    text += "\n".join(holdings) + "\n"
                text += (
                    f"เธฃเธงเธก  <code>{total_value:,.2f}</code> {quote_asset}\n"
                    f"{pnl_emoji} PnL  <code>{pnl:+,.2f}</code> {quote_asset} ({pnl_pct:+.2f}%)\n"
                )
            except Exception as e:
                logger.warning("Balance error: %s", e)
                text += "\nเธ”เธถเธเธขเธญเธ”เน€เธเธดเธเนเธกเนเนเธ”เน\n"

        self._send(text)

    def _cmd_pairs(self):
        text = "๐“ <b>เธเธนเนเน€เธ—เธฃเธ”</b>\n" + "\n".join(f"  โ€ข {p}" for p in self.pairs)
        self._send(text)

    def _cmd_kill(self):
        """Send kill confirmation message with inline buttons."""
        self._send(
            "โ ๏ธ <b>เธซเธขเธธเธ”เธเธธเธเน€เธเธดเธ</b>\n"
            f"{'โ”€' * 20}\n"
            "เธเธ” CONFIRM KILL เน€เธเธทเนเธญ:\n"
            "  1. เธขเธเน€เธฅเธดเธเธเธณเธชเธฑเนเธเธเธทเนเธญเธเธฒเธขเธ—เธฑเนเธเธซเธกเธ”\n"
            "  2. เธเธฒเธขเน€เธซเธฃเธตเธขเธเธ—เธฑเนเธเธซเธกเธ” (เธฃเธฒเธเธฒเธ•เธฅเธฒเธ”)\n"
            "  3. เธเธดเธ”เธเธฒเธฃเน€เธ—เธฃเธ”\n\n"
            "เนเธกเนเธชเธฒเธกเธฒเธฃเธ–เธขเนเธญเธเธเธฅเธฑเธเนเธ”เน",
            reply_markup=kill_confirm_keyboard(),
        )

    def _cmd_resume(self):
        """Send resume confirmation message with inline buttons."""
        if not self.trading_disabled.is_set():
            self._send("โ… Bot เธเธณเธฅเธฑเธเน€เธ—เธฃเธ”เธญเธขเธนเนเนเธฅเนเธง")
            return
        self._send(
            "โ–ถ๏ธ <b>เธเธฅเธฑเธเธกเธฒเน€เธ—เธฃเธ”</b>\n\nเธเธ”เธเธธเนเธกเธ”เนเธฒเธเธฅเนเธฒเธเน€เธเธทเนเธญเธขเธทเธเธขเธฑเธ",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "โ–ถ๏ธ CONFIRM RESUME", "callback_data": "resume_confirm"},
                    ]
                ]
            },
        )

    def _cmd_pnl(self):
        """Show detailed P&L summary from closed trades in quote currency."""
        quote_asset = self._get_quote_asset()
        raw_db = (self.app_ref.config.get("database") or {}).get("db_path", "crypto_bot.db")
        db_path = _resolve_sqlite_db_path(self.app_ref, raw_db)
        if not os.path.exists(db_path):
            self._send("โ เนเธกเนเธเธเธเธฒเธเธเนเธญเธกเธนเธฅ")
            return

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        # Total P&L
        cur.execute("""
            SELECT COUNT(*), COALESCE(SUM(net_pnl), 0),
                   COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(total_fees), 0)
            FROM closed_trades
        """)
        total_trades, total_pnl, wins, total_fees = cur.fetchone()
        total_pnl = total_pnl or 0
        total_fees = total_fees or 0
        losses = total_trades - wins
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

        # Today's P&L
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        cur.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(net_pnl), 0),
                   COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END), 0)
            FROM closed_trades
            WHERE closed_at >= ?
        """,
            (today_start.strftime("%Y-%m-%d %H:%M:%S.%f"),),
        )
        today_trades, today_pnl, _ = cur.fetchone()
        today_pnl = today_pnl or 0

        # Recent trades (last 10)
        cur.execute("""
            SELECT symbol, side, net_pnl, net_pnl_pct, trigger, closed_at
            FROM closed_trades ORDER BY id DESC LIMIT 10
        """)
        recent = cur.fetchall()
        conn.close()

        pnl_emoji = "๐ข" if total_pnl >= 0 else "๐”ด"
        lines = [
            f"{pnl_emoji} <b>เธชเธฃเธธเธ P&amp;L</b>",
            f"{'โ”€' * 20}",
            "",
            f"<b>เธ•เธฅเธญเธ”เธเธฒเธฅ</b>",
            f"  เธเธณเธเธงเธ  {total_trades} เน€เธ—เธตเนเธขเธง",
            f"  เธเธฅเธฅเธฑเธเธเน  <code>{total_pnl:+,.2f}</code> {quote_asset}",
            f"  เธเนเธฒเธเธฃเธฃเธกเน€เธเธตเธขเธก  <code>{total_fees:,.2f}</code> {quote_asset}",
            f"  เธชเธ–เธดเธ•เธด  {wins}W / {losses}L ({win_rate:.1f}%)",
            "",
            f"<b>เธงเธฑเธเธเธตเน</b>",
            f"  เธเธณเธเธงเธ  {today_trades} เน€เธ—เธตเนเธขเธง",
            f"  เธเธฅเธฅเธฑเธเธเน  <code>{today_pnl:+,.2f}</code> {quote_asset}",
        ]
        if recent:
            lines.append("")
            lines.append("<b>10 เธฃเธฒเธขเธเธฒเธฃเธฅเนเธฒเธชเธธเธ”</b>")
            lines.append("โ”€" * 20)
            for sym, side, pnl, pnl_pct, trigger, _closed_at in recent:
                coin = _extract_base_asset(sym, quote_asset) if sym else sym
                pnl_e = "๐ข" if (pnl or 0) >= 0 else "๐”ด"
                trigger_str = f" [{trigger}]" if trigger else ""
                pct = float(pnl_pct or 0.0)
                lines.append(f"{pnl_e} {coin}  <code>{pnl:+,.2f}</code> {quote_asset}  ({pct:+.1f}%){trigger_str}")

        lines.extend(["", "โ”€" * 20])
        lines.append(f"๐• {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
        self._send("\n".join(lines))

    # โ”€โ”€ Kill execution โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

    def _execute_kill(self, msg: Dict[str, Any]):
        """Execute the full emergency kill sequence."""
        logger.warning("=== EMERGENCY KILL EXECUTED ===")
        self.trading_disabled.set()
        results = {"cancelled": [], "sold": [], "errors": []}
        pairs_to_check = self.pairs

        try:
            api = self.app_ref.api_client
            executor = self.app_ref.executor

            # Reset circuit breaker so cancel/sell calls are never silently blocked
            api.reset_circuit()
            logger.info("Emergency kill: circuit breaker reset to CLOSED")

            # 1. Cancel ALL open orders โ€” prefer single global fetch so symbols outside self.pairs are included
            for order in _emergency_kill_collect_open_orders(api, pairs_to_check):
                oid, pair, side = _extract_cancel_fields(order if isinstance(order, dict) else {})
                if not oid or not pair:
                    continue
                try:
                    r = api.cancel_order(pair, oid, side)
                    if r.get("error", 0) == 0:
                        results["cancelled"].append(f"{pair} #{oid}")
                        logger.info("Cancelled order %s on %s", oid, pair)
                    else:
                        results["errors"].append(f"cancel {pair} #{oid}: {r.get('message', r)}")
                except Exception as exc:
                    logger.error("Emergency kill: cancel order failed: %s", exc)

            # Clear bot's tracked open orders
            try:
                for o in executor.get_open_orders():
                    oid = str(o.get("order_id", ""))
                    if oid:
                        executor.cancel_order(oid)
            except Exception as e:
                logger.error("Error clearing tracked orders: %s", e)

            # 2. Sell ALL holdings
            # Fetch balances ONCE before the loop โ€” avoids Nร—API calls (one per pair)
            try:
                balances = api.get_balance()
            except Exception as _bal_err:
                logger.error("Emergency kill: failed to fetch balances: %s", _bal_err)
                balances = {}
            for pair in pairs_to_check:
                try:
                    base = _extract_base_asset(pair, self._get_quote_asset())
                    if not base:
                        continue
                    amt = float(balances.get(base, 0))
                    if amt > 0.00001:
                        r = api.place_ask(symbol=pair, amount=amt, rate=0, order_type="market")
                        if r.get("error", 0) == 0:
                            results["sold"].append(f"{base} x{amt:.6f} ({pair})")
                        else:
                            results["errors"].append(f"sell {base}: {r.get('message', r)}")
                except Exception as e:
                    logger.error("Error selling pair %s: %s", pair, e)
        except Exception as e:
            logger.error("Emergency kill failed: %s", e)
            results["errors"].append(str(e))

        cancelled_txt = "\n".join(f"  โ… {c}" for c in results["cancelled"]) or "  (เนเธกเนเธกเธต)"
        sold_txt = "\n".join(f"  โ… {s}" for s in results["sold"]) or "  (เนเธกเนเธกเธต)"
        errors_txt = "\n".join(f"  โ {e}" for e in results["errors"]) or "  (เนเธกเนเธกเธต)"

        summary = (
            f"๐จ <b>เธซเธขเธธเธ”เธเธธเธเน€เธเธดเธเธชเธณเน€เธฃเนเธ</b>\n"
            f"{'โ”€' * 20}\n"
            f"<b>เธขเธเน€เธฅเธดเธเธเธณเธชเธฑเนเธ:</b>\n{cancelled_txt}\n\n"
            f"<b>เธเธฒเธขเน€เธซเธฃเธตเธขเธ:</b>\n{sold_txt}\n\n"
            f"<b>เธเนเธญเธเธดเธ”เธเธฅเธฒเธ”:</b>\n{errors_txt}\n\n"
            f"๐”ด <b>เธเธฒเธฃเน€เธ—เธฃเธ”เธ–เธนเธเธเธดเธ”</b>\n"
            f"เนเธเน /resume เน€เธเธทเนเธญเธเธฅเธฑเธเธกเธฒเน€เธ—เธฃเธ”"
        )
        msg_id = msg.get("message_id")
        if msg_id:
            try:
                self.telegram.edit_message(
                    msg_id,
                    summary,
                    reply_markup=no_keyboard(),
                )
            except Exception as e:
                logger.error("Could not edit kill message: %s", e)
                self._send(summary)
        else:
            self._send(summary)
        try:
            if hasattr(self.app_ref, "alert_sender") and self.app_ref.alert_sender:
                self.app_ref.alert_sender(summary)
        except Exception as exc:
            logger.warning("Emergency kill alert callback failed: %s", exc)

    def _execute_resume(self, msg: Dict[str, Any]):
        """Re-enable trading."""
        self.trading_disabled.clear()
        logger.info("Trading resumed via Telegram /resume command")
        msg_id = msg.get("message_id")
        summary = "โ… <b>เธเธฅเธฑเธเธกเธฒเน€เธ—เธฃเธ”</b>\n\nBot เธเธฅเธฑเธเธกเธฒเธ—เธณเธเธฒเธเธเธเธ•เธดเนเธฅเนเธง"
        if msg_id:
            try:
                self.telegram.edit_message(msg_id, summary, reply_markup=no_keyboard())
            except Exception:
                self._send(summary)
        else:
            self._send(summary)

    # โ”€โ”€ Low-level send โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€โ”€

    def _send(self, text: str, *format_args: Any, reply_markup: Optional[Dict] = None):
        """Send an HTML message to the configured chat ID."""
        rendered = text % format_args if format_args else text
        try:
            self.telegram.send_message(
                rendered,
                reply_markup=reply_markup,
            )
        except Exception as e:
            logger.error("Failed to send Telegram message: %s", e)
