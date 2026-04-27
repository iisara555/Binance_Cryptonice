from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from risk_management import calculate_atr


logger = logging.getLogger(__name__)

# Mark-to-quote totals for live CLI: reuse up to this many seconds when balances
# are unchanged (avoids N sequential REST tickers every portfolio-cache expiry).
_LIGHTWEIGHT_MTM_CACHE_TTL_SEC = 45.0
_PORTFOLIO_PERF_WARN_MS = 500.0


def _balances_signature(balances: Dict[str, Any], quote: str) -> str:
    """Stable key from spot holdings; invalidates when any non-zero balance changes."""
    quote_u = str(quote or "").upper()
    parts: list[str] = []
    for asset in sorted(balances.keys(), key=lambda x: str(x).upper()):
        asset_u = str(asset or "").upper()
        payload = balances.get(asset) or {}
        if isinstance(payload, dict):
            av = float(payload.get("available", 0.0) or 0.0)
            rv = float(payload.get("reserved", 0.0) or 0.0)
            tot = payload.get("total")
            if tot is not None:
                t = float(tot or 0.0)
            else:
                t = av + rv
        else:
            t = float(payload or 0.0)
        if t <= 0.0:
            continue
        parts.append(f"{asset_u}:{t:.8f}")
    parts.append(f"Q={quote_u}")
    return "|".join(parts)


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
        # Set to {"ws_hits": int, "rest_ticker": int} while profiling mark-to-quote work.
        self._pricing_stats: Optional[Dict[str, int]] = None

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

    def quote_asset(self) -> str:
        config = getattr(self.bot, "config", {}) or {}
        hybrid_cfg = (config.get("data", {}) or {}).get("hybrid_dynamic_coin_config", {}) or {}
        return str(hybrid_cfg.get("quote_asset") or "USDT").upper()

    def get_portfolio_mark_price(self, symbol: str) -> float:
        pair = str(symbol or "").upper()
        if not pair:
            return 0.0

        ws_client = getattr(self.bot, "_ws_client", None)
        if ws_client and ws_client.is_connected() and self.websocket_available and self.latest_ticker_getter:
            try:
                tick = self.latest_ticker_getter(pair)
                if tick and getattr(tick, "last", None):
                    if self._pricing_stats is not None:
                        self._pricing_stats["ws_hits"] = int(self._pricing_stats.get("ws_hits", 0)) + 1
                    return float(tick.last)
            except Exception as exc:
                logger.debug("[PortfolioRuntime] WS mark price lookup failed for %s: %s", pair, exc)

        try:
            ticker = self.bot.api_client.get_ticker(pair)
            if isinstance(ticker, dict):
                if self._pricing_stats is not None:
                    self._pricing_stats["rest_ticker"] = int(self._pricing_stats.get("rest_ticker", 0)) + 1
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

            quote = self.quote_asset()
            if asset_symbol == quote:
                total_value += asset_total
                continue

            current_price = self.get_portfolio_mark_price(f"{asset_symbol}{quote}")
            if current_price > 0:
                total_value += asset_total * current_price

        return total_value

    def get_portfolio_state(self, allow_refresh: bool = True) -> Dict[str, Any]:
        t_entry = time.perf_counter()
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
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[PORTFOLIO PERF] path=cache_hit allow_refresh=%s total_ms=%.2f",
                    allow_refresh,
                    (time.perf_counter() - t_entry) * 1000.0,
                )
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
            t_bm0 = time.perf_counter()
            balance_monitor = getattr(self.bot, "_balance_monitor", None)
            balance_state = balance_monitor.get_state() if balance_monitor else {}
            balance_monitor_ms = (time.perf_counter() - t_bm0) * 1000.0

            balances = balance_state.get("balances") or {}
            quote = self.quote_asset()
            quote_payload = balances.get(quote) or {}
            quote_balance = float(quote_payload.get("total", quote_payload.get("available", 0.0)) or 0.0)
            non_quote_assets = sum(
                1
                for a, p in balances.items()
                if str(a or "").upper() != quote
                and self.extract_total_balance({"balances": {str(a).upper(): p}}, str(a)) > 0
            )
            sig = _balances_signature(balances, quote)
            t_est0 = time.perf_counter()
            mtm_cache = getattr(self.bot, "_lightweight_mtm_cache", None)
            used_mtm_cache = False
            pricing_stats: Dict[str, int] = {"ws_hits": 0, "rest_ticker": 0}
            if (
                isinstance(mtm_cache, dict)
                and mtm_cache.get("sig") == sig
                and (now - float(mtm_cache.get("ts", 0.0) or 0.0)) < _LIGHTWEIGHT_MTM_CACHE_TTL_SEC
            ):
                total_balance = float(mtm_cache.get("total", 0.0) or 0.0)
                used_mtm_cache = True
            else:
                self._pricing_stats = pricing_stats
                try:
                    total_balance = self.estimate_total_portfolio_balance(balances) or quote_balance
                finally:
                    self._pricing_stats = None
                setattr(
                    self.bot,
                    "_lightweight_mtm_cache",
                    {"sig": sig, "total": float(total_balance or 0.0), "ts": now},
                )
            estimate_ms = (time.perf_counter() - t_est0) * 1000.0

            t_pos0 = time.perf_counter()
            positions = self.bot.executor.get_open_orders()
            open_orders_ms = (time.perf_counter() - t_pos0) * 1000.0

            stale_balance_result = {
                "balance": quote_balance,
                "total_balance": total_balance,
                "positions": positions,
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

            ws_connected = bool(
                getattr(self.bot, "_ws_client", None)
                and getattr(self.bot._ws_client, "is_connected", lambda: False)()
            )
            total_ms = (time.perf_counter() - t_entry) * 1000.0
            rest_n = 0 if used_mtm_cache else int(pricing_stats.get("rest_ticker", 0))
            ws_n = 0 if used_mtm_cache else int(pricing_stats.get("ws_hits", 0))
            if total_ms >= _PORTFOLIO_PERF_WARN_MS or logger.isEnabledFor(logging.DEBUG):
                log_fn = logger.warning if total_ms >= _PORTFOLIO_PERF_WARN_MS else logger.debug
                log_fn(
                    "[PORTFOLIO PERF] path=lightweight allow_refresh=False total_ms=%.1f "
                    "balance_monitor_ms=%.1f estimate_ms=%.1f get_open_orders_ms=%.1f "
                    "non_quote_assets=%d rest_tickers=%d ws_ticker_hits=%d ws_connected=%s mtm_cache_hit=%s",
                    total_ms,
                    balance_monitor_ms,
                    estimate_ms,
                    open_orders_ms,
                    non_quote_assets,
                    rest_n,
                    ws_n,
                    ws_connected,
                    used_mtm_cache,
                )

            if balance_state and not is_stale:
                return _store_portfolio_cache(stale_balance_result)

        try:
            t_rest0 = time.perf_counter()
            response = self.bot.api_client.get_balances()
            get_balances_ms = (time.perf_counter() - t_rest0) * 1000.0

            if isinstance(response, dict):
                quote = self.quote_asset()
                quote_info = response.get(quote, {})
                quote_balance = float(quote_info.get("available", 0) or 0.0)
                total_balance = self.estimate_total_portfolio_balance(response) or quote_balance
            elif isinstance(response, list):
                resp2 = self.bot.api_client.get_balance()
                if isinstance(resp2, dict) and resp2.get("error") == 0:
                    result_data = resp2.get("result", {})
                    quote_balance = float(result_data.get(self.quote_asset(), 0) or 0.0) if isinstance(result_data, dict) else 0.0
                else:
                    quote_balance = 0.0
                total_balance = quote_balance
            else:
                quote_balance = 0.0
                total_balance = 0.0

            result = {
                "balance": quote_balance,
                "total_balance": total_balance,
                "positions": self.bot.executor.get_open_orders(),
                "timestamp": datetime.now(),
            }
            setattr(self.bot, "_lightweight_mtm_cache", None)
            full_ms = (time.perf_counter() - t_entry) * 1000.0
            if full_ms >= _PORTFOLIO_PERF_WARN_MS or logger.isEnabledFor(logging.DEBUG):
                log_fn = logger.warning if full_ms >= _PORTFOLIO_PERF_WARN_MS else logger.debug
                log_fn(
                    "[PORTFOLIO PERF] path=rest_balances allow_refresh=%s total_ms=%.1f get_balances_ms=%.1f",
                    allow_refresh,
                    full_ms,
                    get_balances_ms,
                )
            return _store_portfolio_cache(result)
        except Exception as exc:
            logger.error("Error getting portfolio state from exchange: %s", exc, exc_info=True)
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

                    columns = pd.Index(["timestamp", "open", "high", "low", "close", "volume"])
                    df = pd.DataFrame(data, columns=columns)
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

        columns = pd.Index(["timestamp", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows, columns=columns)
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
