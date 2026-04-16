from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from risk_management import calculate_atr


logger = logging.getLogger(__name__)


class PortfolioRuntimeHelper:
    def __init__(
        self,
        bot: Any,
        *,
        websocket_available: bool,
        latest_ticker_getter: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.bot = bot
        self.websocket_available = bool(websocket_available)
        self.latest_ticker_getter = latest_ticker_getter if callable(latest_ticker_getter) else None

    @staticmethod
    def extract_total_balance(balance_state: Optional[Dict[str, Any]], asset: str) -> float:
        balances = (balance_state or {}).get("balances") or {}
        payload = balances.get(str(asset or "").upper()) or {}
        if isinstance(payload, dict):
            available = float(payload.get("available", 0.0) or 0.0)
            reserved = float(payload.get("reserved", 0.0) or 0.0)
            total = payload.get("total")
            if total is not None:
                return float(total or 0.0)
            return available + reserved
        return float(payload or 0.0)

    @staticmethod
    def get_risk_portfolio_value(portfolio_state: Optional[Dict[str, Any]]) -> float:
        state = portfolio_state or {}
        total_balance = float(state.get("total_balance", 0.0) or 0.0)
        if total_balance > 0:
            return total_balance
        return float(state.get("balance", 0.0) or 0.0)

    def get_portfolio_mark_price(self, symbol: str) -> float:
        pair = str(symbol or "").upper()
        if not pair:
            return 0.0

        ws_client = getattr(self.bot, "_ws_client", None)
        if ws_client and ws_client.is_connected() and self.websocket_available and self.latest_ticker_getter:
            try:
                tick = self.latest_ticker_getter(pair)
                if tick and getattr(tick, "last", None):
                    return float(tick.last)
            except Exception as exc:
                logger.debug("[PortfolioRuntime] WS mark price lookup failed for %s: %s", pair, exc)

        try:
            ticker = self.bot.api_client.get_ticker(pair)
            if isinstance(ticker, dict):
                return float(ticker.get("last", ticker.get("close", 0.0)) or 0.0)
        except Exception as exc:
            logger.debug("[PortfolioRuntime] REST mark price lookup failed for %s: %s", pair, exc)
            return 0.0

        return 0.0

    def estimate_total_portfolio_balance(self, balances: Optional[Dict[str, Any]]) -> float:
        balances_dict = balances if isinstance(balances, dict) else {}
        if not balances_dict:
            return 0.0

        total_value = 0.0
        for asset, payload in balances_dict.items():
            asset_symbol = str(asset or "").upper()
            if not asset_symbol:
                continue

            asset_total = self.extract_total_balance({"balances": {asset_symbol: payload}}, asset_symbol)
            if asset_total <= 0:
                continue

            if asset_symbol == "THB":
                total_value += asset_total
                continue

            current_price = self.get_portfolio_mark_price(f"THB_{asset_symbol}")
            if current_price > 0:
                total_value += asset_total * current_price

        return total_value

    def get_portfolio_state(self, allow_refresh: bool = True) -> Dict[str, Any]:
        now = time.time()
        _cache_lock = getattr(self.bot, "_portfolio_cache_lock", None)
        if _cache_lock:
            with _cache_lock:
                portfolio_cache = dict(getattr(self.bot, "_portfolio_cache", {"data": None, "timestamp": 0.0}))
        else:
            portfolio_cache = getattr(self.bot, "_portfolio_cache", {"data": None, "timestamp": 0.0})
        cache_ttl = float(((getattr(self.bot, "_cache_ttl", {}) or {}).get("portfolio", 10) or 10))

        def _store_portfolio_cache(result: Dict[str, Any]) -> Dict[str, Any]:
            payload = {"data": result, "timestamp": now}
            if _cache_lock:
                with _cache_lock:
                    self.bot._portfolio_cache = payload
            else:
                self.bot._portfolio_cache = payload
            return result

        if portfolio_cache["data"] is not None and (now - float(portfolio_cache.get("timestamp", 0.0) or 0.0)) < cache_ttl:
            return portfolio_cache["data"]

        if getattr(self.bot, "_auth_degraded", False):
            result = {
                "balance": 0.0,
                "positions": self.bot.executor.get_open_orders(),
                "timestamp": datetime.now(),
            }
            return _store_portfolio_cache(result)

        stale_balance_result: Optional[Dict[str, Any]] = None
        if not allow_refresh:
            balance_monitor = getattr(self.bot, "_balance_monitor", None)
            balance_state = balance_monitor.get_state() if balance_monitor else {}
            balances = balance_state.get("balances") or {}
            thb_payload = balances.get("THB") or {}
            thb_balance = float(thb_payload.get("total", thb_payload.get("available", 0.0)) or 0.0)
            total_balance = self.estimate_total_portfolio_balance(balances) or thb_balance
            stale_balance_result = {
                "balance": thb_balance,
                "total_balance": total_balance,
                "positions": self.bot.executor.get_open_orders(),
                "timestamp": datetime.now(),
            }

            updated_at_raw = balance_state.get("updated_at")
            updated_at = None
            if updated_at_raw:
                try:
                    updated_at = datetime.fromisoformat(str(updated_at_raw))
                except ValueError:
                    updated_at = None

            raw_poll_interval_seconds = getattr(balance_monitor, "poll_interval_seconds", 30.0)
            try:
                poll_interval_seconds = float(raw_poll_interval_seconds or 30.0)
            except (TypeError, ValueError):
                poll_interval_seconds = 30.0
            stale_after_seconds = max(poll_interval_seconds * 2.0, 60.0)
            is_stale = False
            if updated_at is not None:
                is_stale = (datetime.now() - updated_at).total_seconds() > stale_after_seconds

            if balance_state and not is_stale:
                return _store_portfolio_cache(stale_balance_result)

        try:
            response = self.bot.api_client.get_balances()

            if isinstance(response, dict):
                thb_info = response.get("THB", {})
                thb_balance = float(thb_info.get("available", 0) or 0.0)
                total_balance = self.estimate_total_portfolio_balance(response) or thb_balance
            elif isinstance(response, list):
                resp2 = self.bot.api_client.get_balance()
                if isinstance(resp2, dict) and resp2.get("error") == 0:
                    result_data = resp2.get("result", {})
                    thb_balance = float(result_data.get("THB", 0) or 0.0) if isinstance(result_data, dict) else 0.0
                else:
                    thb_balance = 0.0
                total_balance = thb_balance
            else:
                thb_balance = 0.0
                total_balance = 0.0

            result = {
                "balance": thb_balance,
                "total_balance": total_balance,
                "positions": self.bot.executor.get_open_orders(),
                "timestamp": datetime.now(),
            }
            return _store_portfolio_cache(result)
        except Exception as exc:
            logger.error("Error getting portfolio state from Bitkub: %s", exc, exc_info=True)
            if stale_balance_result is not None:
                return _store_portfolio_cache(stale_balance_result)
            return {
                "balance": 0.0,
                "total_balance": 0.0,
                "positions": self.bot.executor.get_open_orders(),
                "timestamp": datetime.now(),
            }

    def get_market_data_for_symbol(self, symbol: str):
        now = time.time()
        cache_key = f"market_data_{symbol}_{self.bot.timeframe}"

        if (
            cache_key in self.bot._symbol_market_cache
            and (now - self.bot._symbol_market_cache[cache_key]["timestamp"]) < self.bot._cache_ttl["market_data"]
        ):
            return self.bot._symbol_market_cache[cache_key]["data"]

        rows = None
        try:
            conn = self.bot.db.get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT timestamp, open, high, low, close, volume
                    FROM prices
                    WHERE pair = ?
                      AND COALESCE(timeframe, '1h') = ?
                    ORDER BY timestamp DESC
                    LIMIT 250
                """,
                    (symbol, self.bot.timeframe),
                )
                rows = cursor.fetchall()
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(f"Error fetching market data for {symbol}: {exc}")

        if not rows:
            try:
                response = self.bot.api_client.get_candle(symbol, timeframe=self.bot.timeframe)
                if response.get("error") == 0:
                    data = response.get("result", [])
                    import pandas as pd

                    df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    df = df.sort_values("timestamp").reset_index(drop=True)
                    df.attrs["_data_source"] = "api"
                    self.bot._symbol_market_cache[cache_key] = {"data": df, "timestamp": now}
                    return df

                logger.warning(
                    "API candle request for %s returned error=%s: %s",
                    symbol,
                    response.get("error"),
                    response.get("message", ""),
                )
            except Exception as exc:
                logger.warning("API fallback failed for %s: %s", symbol, exc)
            return None

        import pandas as pd

        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        df.attrs["_data_source"] = "db"
        self.bot._symbol_market_cache[cache_key] = {"data": df, "timestamp": now}
        return df

    def get_latest_atr(self, symbol: Optional[str] = None, period: int = 14) -> Optional[float]:
        symbol = symbol or self.bot.trading_pair or ""
        if not symbol:
            return None

        now = time.time()
        cache_key = f"atr_{symbol}_{period}"
        if (
            self.bot._atr_cache.get(cache_key) is not None
            and (now - self.bot._atr_cache[cache_key]["timestamp"]) < self.bot._cache_ttl["atr"]
        ):
            return self.bot._atr_cache[cache_key]["value"]

        try:
            data = self.bot._get_market_data_for_symbol(symbol)
            if data is None or len(data) < period + 1:
                return None

            if not all(key in data.columns for key in ["high", "low", "close"]):
                return None

            highs = data["high"].tolist()
            lows = data["low"].tolist()
            closes = data["close"].tolist()

            atr_values = calculate_atr(highs, lows, closes, period=period)
            atr = atr_values[-1]
            if atr <= 0:
                return None

            result = float(atr)
            self.bot._atr_cache[cache_key] = {"value": result, "timestamp": now}
            return result
        except Exception as exc:
            logger.debug(f"Could not calculate ATR for {symbol}: {exc}")
            return None
