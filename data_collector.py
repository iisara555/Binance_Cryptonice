"""
Data Collector for Crypto Trading Bot.

# --- NEW: SPEC_02 --- Binance Thailand exchange collector.
The public class is ``BinanceThCollector``.

Exchange notes
--------------
* Base URL:        ``https://api.binance.th``
* Kline endpoint:  ``GET /api/v1/klines``
* Ticker endpoint: ``GET /api/v1/ticker/24hr``
* Symbol format:   ``BTCUSDT`` (NOT ``THB_BTC``)
* Timestamps in kline rows are *milliseconds* — divide by 1000 before
  ``datetime.fromtimestamp``.
* Rate limit:      Binance.th uses request-weight tracked in the
  ``X-MBX-USED-WEIGHT-1M`` response header (cap = 1200 / minute).
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import threading
from typing import Any, Dict, List, Optional

import requests

from database import get_database

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=6)
atexit.register(_executor.shutdown, wait=False)


# --- NEW: SPEC_02 --- Default pair set for Binance Thailand (USDT-quoted).
_DEFAULT_BINANCE_TH_PAIRS: List[str] = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "DOGEUSDT"]

# --- NEW: SPEC_02 --- Valid Binance kline intervals — used for soft validation.
_BINANCE_INTERVALS = frozenset(
    {
        "1m", "3m", "5m", "15m", "30m",
        "1h", "2h", "4h", "6h", "8h", "12h",
        "1d", "3d", "1w", "1M",
    }
)

# --- NEW: SPEC_02 --- Map legacy minute-int values onto Binance interval
# strings. Lets older callers keep passing ``interval=15`` while new callers
# pass ``interval="15m"`` directly.
_LEGACY_MINUTES_TO_INTERVAL: Dict[int, str] = {
    1: "1m",
    3: "3m",
    5: "5m",
    15: "15m",
    30: "30m",
    60: "1h",
    120: "2h",
    240: "4h",
    360: "6h",
    480: "8h",
    720: "12h",
    1440: "1d",
}


def _coerce_interval(interval: Any, default: str = "15m") -> str:
    """Normalize legacy minute-int intervals to Binance kline strings."""
    if isinstance(interval, str):
        candidate = interval.strip()
        if candidate in _BINANCE_INTERVALS:
            return candidate
        try:
            mapped = _LEGACY_MINUTES_TO_INTERVAL.get(int(candidate))
        except (TypeError, ValueError):
            mapped = None
        return mapped or default

    try:
        return _LEGACY_MINUTES_TO_INTERVAL.get(int(interval), default)
    except (TypeError, ValueError):
        return default


class BinanceThCollector:
    """Collects OHLCV data from the Binance Thailand REST API.

    # --- NEW: SPEC_02 --- Binance collector. Symbol
    # format is now ``BTCUSDT`` (not ``THB_BTC``) and kline timestamps are
    # delivered in milliseconds — they are converted to seconds before being
    # stored as timezone-aware UTC datetimes.
    """

    BASE_URL = "https://api.binance.th"

    INTERVALS = {
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "2h": 7200,
        "4h": 14400,
        "6h": 21600,
        "8h": 28800,
        "12h": 43200,
        "1d": 86400,
        "3d": 259200,
        "1w": 604800,
    }

    def __init__(
        self,
        pairs: Optional[List[str]] = None,
        interval: int = 60,
        multi_timeframe_config: Optional[Dict[str, Any]] = None,
    ):
        """Initialize collector.

        Args:
            pairs: List of trading pairs (e.g., ``["BTCUSDT", "ETHUSDT"]``).
                Falls back to ``_DEFAULT_BINANCE_TH_PAIRS`` when ``None`` is
                passed. An *explicit* empty list is preserved unchanged so the
                runtime auto-detection layer can disable collection cleanly.
            interval: Collection interval in seconds for the ticker loop.
            multi_timeframe_config: Optional multi-timeframe collection
                settings.
        """
        # --- NEW: SPEC_02 --- Default pair list now matches Binance Thailand;
        # an explicitly passed empty list is preserved untouched.
        if pairs is None:
            self.pairs: List[str] = list(_DEFAULT_BINANCE_TH_PAIRS)
        else:
            self.pairs = list(pairs)
        self.interval = interval
        self.db = get_database()
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._pairs_lock = threading.Lock()

        mtf_config = dict(multi_timeframe_config or {})
        self.multi_timeframe_enabled = bool(mtf_config.get("enabled", False))
        self.multi_timeframes = [
            str(timeframe).strip()
            for timeframe in (mtf_config.get("timeframes") or ["1m", "5m", "15m", "1h"])
            if str(timeframe).strip()
        ]
        if not self.multi_timeframes:
            self.multi_timeframes = ["1m", "5m", "15m", "1h"]

        refresh_interval = mtf_config.get("collection_interval_seconds")
        if refresh_interval is None:
            refresh_interval = mtf_config.get("refresh_interval_seconds")
        if refresh_interval is None:
            refresh_interval = interval
        try:
            self.multi_timeframe_interval = max(int(refresh_interval), max(int(interval), 1))
        except (TypeError, ValueError):
            self.multi_timeframe_interval = max(int(interval), 1)

        self._last_multi_timeframe_run: Optional[datetime] = None
        self._last_multi_timeframe_results: Dict[str, Dict[str, int]] = {}
        self._next_multi_timeframe_collect_at = 0.0

        logger.info("BinanceThCollector initialized with pairs: %s", self.pairs)

    def set_pairs(self, pairs: List[str]) -> None:
        """Update the collector pair set safely at runtime."""
        normalized = [str(pair).upper() for pair in (pairs or []) if str(pair).strip()]
        previous_pairs = self.get_pairs()
        with self._pairs_lock:
            self.pairs = normalized
        logger.info("Collector pairs updated: %s", normalized)
        added_pairs = [pair for pair in normalized if pair not in previous_pairs]
        if self.running and self.multi_timeframe_enabled and added_pairs:
            self._warm_pairs_backfill(added_pairs)

    def get_pairs(self) -> List[str]:
        """Return a snapshot of the current collector pairs."""
        with self._pairs_lock:
            return list(self.pairs)

    @staticmethod
    def _format_collection_timestamp(value: Optional[datetime]) -> str:
        """Format collector timestamps consistently for human-readable logs."""
        if value is None:
            return "unknown"

        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()

        if not isinstance(value, datetime):
            return str(value)

        normalized = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")

    # --- NEW: SPEC_02 --- Binance.th rate-limit bookkeeping. Reads the
    # X-MBX-USED-WEIGHT-1M response header and sleeps when usage approaches
    # the 1200 request-weight cap so we stay clear of HTTP 429 throttling.
    def _check_rate_limit(self, response: requests.Response) -> None:
        """Throttle further requests if Binance.th request weight is high."""
        try:
            used = int(response.headers.get("X-MBX-USED-WEIGHT-1M", 0))
        except (TypeError, ValueError):
            return
        if used > 1000:
            logger.warning("[RateLimit] Weight %d/1200 — sleeping 10s", used)
            time.sleep(10)
        elif used > 800:
            logger.debug("[RateLimit] Weight %d/1200 — approaching limit", used)

    def get_ohlc(
        self,
        symbol: str,
        interval: Any = "15m",
        limit: int = 500,
    ) -> List[List[Any]]:
        """Fetch klines from Binance Thailand.

        ``GET /api/v1/klines``

        Args:
            symbol: Binance symbol such as ``"BTCUSDT"`` (NOT ``"THB_BTC"``).
            interval: Binance kline interval string (``"1m"``, ``"5m"``,
                ``"15m"``, ``"1h"``, ``"4h"``, ``"1d"``). Legacy minute-int
                values are accepted and mapped automatically.
            limit: Maximum number of klines to fetch (Binance hard limit
                is 1000).

        Returns:
            List of kline rows ``[open_time_ms, o, h, l, c, v, ...]``.
            On failure, an empty list is returned (never ``None``) so callers
            can iterate safely without extra guards.
        """
        normalized_interval = _coerce_interval(interval, default="15m")
        url = f"{self.BASE_URL}/api/v1/klines"
        params = {
            "symbol": str(symbol).upper(),
            "interval": normalized_interval,
            "limit": int(limit),
        }
        try:
            response = requests.get(url, params=params, timeout=10)
            self._check_rate_limit(response)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                return data
            logger.warning(
                "[%s] Unexpected klines payload type: %s",
                symbol,
                type(data).__name__,
            )
            return []
        except requests.exceptions.RequestException as exc:
            logger.error("[%s] klines error: %s", symbol, exc)
            return []
        except ValueError as exc:
            logger.error("[%s] klines decode error: %s", symbol, exc)
            return []

    def _collect_ohlc_result(
        self,
        symbol: str,
        interval: Any = "15m",
        timeframe: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Collect klines and return a detailed outcome for richer logging.

        # --- NEW: SPEC_02 --- Parses Binance kline rows where ``row[0]`` is
        # the open time in *milliseconds* — divided by 1000 before being
        # converted to a UTC ``datetime`` so the rest of the bot (which works
        # in seconds) stays consistent.
        """
        normalized_interval = _coerce_interval(interval, default="15m")
        tf = timeframe or normalized_interval

        outcome: Dict[str, Any] = {
            "pair": symbol,
            "timeframe": tf,
            "stored": 0,
            "status": "no_data",
            "latest_stored": None,
            "latest_fetched": None,
        }

        rows = self.get_ohlc(symbol, interval=normalized_interval)
        if not rows:
            return outcome

        latest_stored = self.db.get_latest_price(symbol, timeframe=tf)
        latest_timestamp = latest_stored.timestamp if latest_stored else None
        outcome["latest_stored"] = latest_timestamp
        if latest_timestamp is not None and latest_timestamp.tzinfo is None:
            latest_timestamp = latest_timestamp.replace(tzinfo=timezone.utc)

        candles_to_insert: List[Dict[str, Any]] = []
        latest_fetched: Optional[datetime] = None
        for row in rows:
            try:
                if len(row) < 6:
                    continue
                ts_ms = int(row[0])
                ts_seconds = ts_ms // 1000
                candle_timestamp = datetime.fromtimestamp(ts_seconds, tz=timezone.utc)
                if latest_fetched is None or candle_timestamp > latest_fetched:
                    latest_fetched = candle_timestamp
                if latest_timestamp is not None and candle_timestamp <= latest_timestamp:
                    continue
                candles_to_insert.append(
                    {
                        "pair": symbol,
                        "timestamp": candle_timestamp,
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                        "timeframe": tf,
                    }
                )
            except (TypeError, ValueError, IndexError) as exc:
                logger.warning("Invalid candle row for %s %s: %s", symbol, tf, exc)
                continue

        outcome["latest_fetched"] = latest_fetched

        if not candles_to_insert:
            if (
                latest_timestamp is not None
                and latest_fetched is not None
                and latest_fetched <= latest_timestamp
            ):
                outcome["status"] = "up_to_date"
            return outcome

        try:
            stored = self.db.insert_prices_batch(candles_to_insert)
        except Exception as exc:
            logger.error("Batch insert failed for %s %s: %s", symbol, tf, exc)
            stored = 0
            for price_data in candles_to_insert:
                try:
                    if self.db.insert_price(**price_data):
                        stored += 1
                except Exception:
                    continue

        outcome["stored"] = int(stored or 0)
        outcome["status"] = "stored" if outcome["stored"] > 0 else "up_to_date"
        return outcome

    def _log_ohlc_collection_result(self, result: Dict[str, Any]) -> None:
        """Emit a collector log line that explains whether zero inserts are healthy."""
        pair = result.get("pair", "unknown")
        timeframe = result.get("timeframe", "unknown")
        stored = int(result.get("stored", 0) or 0)
        status = result.get("status", "stored")

        if status == "stored":
            if stored > 0:
                logger.info("[%s] %s: stored %d new candle(s)", pair, timeframe, stored)
            else:
                logger.debug("[%s] %s: stored 0 new candle(s)", pair, timeframe)
            return

        if status == "up_to_date":
            latest_stored = self._format_collection_timestamp(result.get("latest_stored"))
            latest_fetched = self._format_collection_timestamp(result.get("latest_fetched"))
            logger.info(
                "[%s] %s: no new closed candles, already up to date "
                "(stored=%s, fetched=%s)",
                pair,
                timeframe,
                latest_stored,
                latest_fetched,
            )
            return

        logger.debug("[%s] %s: no candle data returned", pair, timeframe)

    def get_current_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get the 24-hour rolling ticker for a Binance symbol.

        # --- NEW: SPEC_02 --- ``GET /api/v1/ticker/24hr?symbol=BTCUSDT``.
        """
        url = f"{self.BASE_URL}/api/v1/ticker/24hr"
        try:
            response = requests.get(
                url,
                params={"symbol": str(symbol).upper()},
                timeout=10,
            )
            self._check_rate_limit(response)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                logger.error(
                    "[%s] ticker payload not a dict: %s",
                    symbol,
                    type(data).__name__,
                )
                return None
            return {
                "pair": symbol,
                "last": float(data.get("lastPrice", 0.0) or 0.0),
                "high": float(data.get("highPrice", 0.0) or 0.0),
                "low": float(data.get("lowPrice", 0.0) or 0.0),
                "volume": float(data.get("volume", 0.0) or 0.0),
                "quoteVolume": float(data.get("quoteVolume", 0.0) or 0.0),
                "timestamp": int(time.time()),
            }
        except requests.exceptions.RequestException as exc:
            logger.error("[%s] ticker error: %s", symbol, exc)
            return None
        except (ValueError, KeyError) as exc:
            logger.error("[%s] ticker decode error: %s", symbol, exc)
            return None

    def collect_price(self, symbol: str) -> bool:
        """Collect current ticker snapshot and store it as a price row."""
        ticker = self.get_current_ticker(symbol)
        if not ticker:
            return False

        price_data = {
            "pair": symbol,
            "timestamp": datetime.now(timezone.utc),
            "open": ticker["last"],
            "high": ticker["high"],
            "low": ticker["low"],
            "close": ticker["last"],
            "volume": ticker.get("quoteVolume", 0) or ticker.get("volume", 0),
        }
        try:
            self.db.insert_price(**price_data)
            logger.debug("Stored price for %s: %s", symbol, ticker["last"])
            return True
        except Exception as exc:
            logger.error("Failed to store price for %s: %s", symbol, exc)
            return False

    def collect_ohlc(
        self,
        symbol: str,
        interval: Any = "15m",
        timeframe: Optional[str] = None,
    ) -> int:
        """Collect OHLC data and store in database. Returns rows inserted."""
        return int(self._collect_ohlc_result(symbol, interval, timeframe).get("stored", 0) or 0)

    def collect_multi_timeframe(
        self,
        symbol: str,
        timeframes: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        """Collect OHLC data for multiple timeframes IN PARALLEL.

        # --- NEW: SPEC_02 --- Binance interval strings are used directly as
        # the timeframe label, so no minute mapping is needed any more.
        """
        if timeframes is None:
            timeframes = ["1m", "5m", "15m", "1h"]

        results: Dict[str, int] = {}
        futures = {}
        for tf in timeframes:
            future = _executor.submit(self._collect_ohlc_result, symbol, tf, tf)
            futures[future] = tf

        for future in as_completed(futures):
            tf = futures[future]
            try:
                detail = future.result()
                if isinstance(detail, dict):
                    results[tf] = int(detail.get("stored", 0) or 0)
                    self._log_ohlc_collection_result(detail)
                else:
                    count = int(detail or 0)
                    results[tf] = count
                    if count > 0:
                        logger.info("[%s] %s: stored %d new candle(s)", symbol, tf, count)
                    else:
                        logger.debug("[%s] %s: stored 0 new candle(s)", symbol, tf)
            except Exception as exc:
                logger.error("[%s] %s collection failed: %s", symbol, tf, exc)
                results[tf] = 0

        return results

    def _collect_multi_timeframe_for_pairs(
        self,
        pairs: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, int]]:
        """Collect multi-timeframe candles across pairs without nesting the shared executor."""
        target_pairs = [str(pair).upper() for pair in (pairs or self.get_pairs()) if str(pair).strip()]
        if not target_pairs:
            return {}

        if len(target_pairs) == 1:
            pair = target_pairs[0]
            try:
                return {pair: self.collect_multi_timeframe(pair, self.multi_timeframes)}
            except Exception as exc:
                logger.error("collect_multi_timeframe(%s) failed: %s", pair, exc)
                return {pair: {tf: 0 for tf in self.multi_timeframes}}

        results: Dict[str, Dict[str, int]] = {}
        max_workers = min(len(target_pairs), 4)
        with ThreadPoolExecutor(max_workers=max_workers) as pair_executor:
            futures = {
                pair: pair_executor.submit(self.collect_multi_timeframe, pair, self.multi_timeframes)
                for pair in target_pairs
            }
            for pair, future in futures.items():
                try:
                    results[pair] = future.result()
                except Exception as exc:
                    logger.error("collect_multi_timeframe(%s) failed: %s", pair, exc)
                    results[pair] = {tf: 0 for tf in self.multi_timeframes}

        return results

    def collect_all_multi_timeframe(self) -> Dict[str, Dict[str, int]]:
        """Collect multi-timeframe OHLC data for all configured pairs in parallel."""
        return self._collect_multi_timeframe_for_pairs(self.get_pairs())

    # --- NEW: SPEC_02 --- Renamed from ``_warm_pairs_multi_timeframe``. Primes
    # missing multi-timeframe candles immediately for startup or newly added
    # pairs. The legacy name is kept as an alias below for backward compat.
    def _warm_pairs_backfill(
        self,
        pairs: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, int]]:
        """Prime missing multi-timeframe candles for startup or newly added pairs."""
        if not self.multi_timeframe_enabled:
            return {}

        target_pairs = [str(pair).upper() for pair in (pairs or self.get_pairs()) if str(pair).strip()]
        if not target_pairs:
            return {}

        results = self._collect_multi_timeframe_for_pairs(target_pairs)

        merged_results = dict(getattr(self, "_last_multi_timeframe_results", {}) or {})
        merged_results.update(results)
        self._last_multi_timeframe_results = merged_results
        self._last_multi_timeframe_run = datetime.now(timezone.utc)
        self._next_multi_timeframe_collect_at = time.time() + self.multi_timeframe_interval
        return results

    # --- NEW: SPEC_02 --- Legacy alias so existing callers/tests that still
    # reference ``_warm_pairs_multi_timeframe`` continue to work. New code
    # should call ``_warm_pairs_backfill`` directly.
    _warm_pairs_multi_timeframe = _warm_pairs_backfill

    # --- NEW: SPEC_02 --- Public backfill helpers for explicit pre-startup
    # historical hydration.
    async def backfill(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
    ) -> int:
        """Asynchronously backfill historical klines for a single pair.

        The HTTP/DB work itself is synchronous, but the wrapper is exposed as
        ``async`` so an ``asyncio``-based runtime can ``await`` it without
        blocking its event loop. Returns the number of candles inserted.
        """
        del limit
        result = await asyncio.to_thread(
            self._collect_ohlc_result,
            symbol,
            interval,
            interval,
        )
        stored = int(result.get("stored", 0) or 0)
        logger.info("[Backfill] %s/%s: %d candles stored", symbol, interval, stored)
        return stored

    def backfill_all_sync(
        self,
        timeframes: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, int]]:
        """Synchronous backfill across every configured pair × timeframe.

        Intended to be called once at startup, before the main trading loop,
        so indicators have enough history to compute values on the very first
        iteration.
        """
        if timeframes is None:
            timeframes = list(self.multi_timeframes)

        previous_timeframes = list(self.multi_timeframes)
        try:
            if timeframes:
                self.multi_timeframes = list(timeframes)
            pairs = self.get_pairs()
            logger.info(
                "[Backfill] Starting for %d pair(s) × %d timeframe(s) | pairs=%s | tfs=%s",
                len(pairs),
                len(timeframes),
                pairs,
                timeframes,
            )
            results = self._collect_multi_timeframe_for_pairs(pairs)
        finally:
            self.multi_timeframes = previous_timeframes

        merged_results = dict(getattr(self, "_last_multi_timeframe_results", {}) or {})
        merged_results.update(results)
        self._last_multi_timeframe_results = merged_results
        self._last_multi_timeframe_run = datetime.now(timezone.utc)
        self._next_multi_timeframe_collect_at = time.time() + self.multi_timeframe_interval

        total = sum(
            count
            for pair_results in results.values()
            for count in pair_results.values()
        )
        logger.info("[Backfill] ✅ Complete — %d candles stored across all pairs", total)
        return results

    def collect_all(self) -> Dict[str, bool]:
        """Collect prices for all configured pairs IN PARALLEL."""
        results: Dict[str, bool] = {}
        pairs = self.get_pairs()
        futures = {pair: _executor.submit(self.collect_price, pair) for pair in pairs}
        for pair, future in futures.items():
            try:
                results[pair] = future.result()
            except Exception as exc:
                logger.error("collect_price(%s) failed: %s", pair, exc)
                results[pair] = False
        return results

    def collect_all_ohlc(self, interval: Any = "15m") -> Dict[str, int]:
        """Collect OHLC data for all configured pairs IN PARALLEL."""
        results: Dict[str, int] = {}
        pairs = self.get_pairs()
        futures = {pair: _executor.submit(self.collect_ohlc, pair, interval) for pair in pairs}
        for pair, future in futures.items():
            try:
                results[pair] = future.result()
            except Exception as exc:
                logger.error("collect_ohlc(%s) failed: %s", pair, exc)
                results[pair] = 0
        return results

    def _collector_loop(self) -> None:
        """Main collection loop (runs in background thread)."""
        logger.info(
            "Collector started - interval: %ss, pairs: %s",
            self.interval,
            self.get_pairs(),
        )
        consecutive_errors = 0

        while self.running:
            try:
                results = self.collect_all()
                current_pairs = self.get_pairs()
                success_count = sum(1 for v in results.values() if v)
                logger.debug("Collected %d/%d pairs", success_count, len(current_pairs))

                if (
                    self.multi_timeframe_enabled
                    and time.time() >= self._next_multi_timeframe_collect_at
                ):
                    mtf_results = self.collect_all_multi_timeframe()
                    self._last_multi_timeframe_results = mtf_results
                    self._last_multi_timeframe_run = datetime.now(timezone.utc)
                    self._next_multi_timeframe_collect_at = time.time() + self.multi_timeframe_interval
                    logger.debug(
                        "Collected multi-timeframe candles for %s pair(s): %s",
                        len(mtf_results),
                        self.multi_timeframes,
                    )
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                logger.error("Collection error (%dx consecutive): %s", consecutive_errors, exc)
                if consecutive_errors >= 5:
                    logger.critical(
                        "Data collection failed %d times in a row — price data may be STALE",
                        consecutive_errors,
                    )

            for _ in range(self.interval):
                if not self.running:
                    break
                time.sleep(1)

        logger.info("Collector stopped")

    def start(self, blocking: bool = False) -> None:
        """Start collecting data.

        Args:
            blocking: If ``True``, run in current thread. Otherwise run in
                a background daemon thread.
        """
        self.running = True
        if self.multi_timeframe_enabled:
            self._warm_pairs_backfill()

        if blocking:
            self._collector_loop()
        else:
            self._thread = threading.Thread(target=self._collector_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop the collector."""
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Collector shutdown complete")

    def get_status(self) -> Dict[str, Any]:
        """Expose collector runtime state for observability."""
        return {
            "running": self.running,
            "pairs": self.get_pairs(),
            "interval_seconds": self.interval,
            "multi_timeframe": {
                "enabled": self.multi_timeframe_enabled,
                "timeframes": list(self.multi_timeframes),
                "interval_seconds": self.multi_timeframe_interval,
                "last_run": self._last_multi_timeframe_run.isoformat() if self._last_multi_timeframe_run else None,
                "last_results": self._last_multi_timeframe_results,
            },
        }


class DataAggregator:
    """Aggregates collected data for daily summaries."""

    def __init__(self):
        self.db = get_database()

    def get_daily_summary(self, pair: str, days: int = 30) -> List[Dict]:
        """Get daily OHLCV summaries."""
        prices = self.db.get_price_df(pair, days=days)

        if not prices:
            return []

        daily_data: Dict[str, Dict[str, Any]] = {}
        for p in prices:
            date_key = p["timestamp"].date().isoformat()

            if date_key not in daily_data:
                daily_data[date_key] = {
                    "date": date_key,
                    "open": p["open"],
                    "high": p["high"],
                    "low": p["low"],
                    "close": p["close"],
                    "volume": p["volume"],
                    "count": 1,
                }
            else:
                daily_data[date_key]["high"] = max(daily_data[date_key]["high"], p["high"])
                daily_data[date_key]["low"] = min(daily_data[date_key]["low"], p["low"])
                daily_data[date_key]["close"] = p["close"]
                daily_data[date_key]["volume"] += p["volume"]
                daily_data[date_key]["count"] += 1

        return sorted(daily_data.values(), key=lambda x: x["date"])

    def get_pair_stats(self, pair: str, days: int = 30) -> Dict:
        """Get statistical summary for a pair."""
        prices = self.db.get_price_df(pair, days=days)

        if not prices:
            return {}

        closes = [p["close"] for p in prices]

        return {
            "pair": pair,
            "period_days": days,
            "data_points": len(closes),
            "current_price": closes[-1] if closes else 0,
            "highest_price": max(closes) if closes else 0,
            "lowest_price": min(closes) if closes else 0,
            "avg_price": sum(closes) / len(closes) if closes else 0,
            "volatility": self._calculate_volatility(closes),
            "price_change_pct": (
                (closes[-1] - closes[0]) / closes[0] * 100
                if closes and closes[0] > 0
                else 0
            ),
        }

    def _calculate_volatility(self, prices: List[float]) -> float:
        """Calculate price volatility (standard deviation of returns)."""
        if len(prices) < 2:
            return 0.0

        returns: List[float] = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0:
                ret = (prices[i] - prices[i - 1]) / prices[i - 1]
                returns.append(ret)

        if not returns:
            return 0.0

        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return variance ** 0.5


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Crypto Data Collector (Binance Thailand)")
    parser.add_argument(
        "--pairs",
        nargs="+",
        default=list(_DEFAULT_BINANCE_TH_PAIRS),
        help="Trading pairs to collect (e.g. BTCUSDT ETHUSDT)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Collection interval in seconds",
    )
    parser.add_argument("--once", action="store_true", help="Collect once and exit")
    parser.add_argument(
        "--ohlc",
        action="store_true",
        help="Collect OHLC data instead of ticker",
    )
    parser.add_argument(
        "--ohlc-interval",
        default="15m",
        help='Kline interval to use with --ohlc (e.g. "1m", "15m", "1h")',
    )

    args = parser.parse_args()

    collector = BinanceThCollector(pairs=args.pairs, interval=args.interval)

    if args.once:
        if args.ohlc:
            results = collector.collect_all_ohlc(interval=args.ohlc_interval)
            for pair, count in results.items():
                print(f"{pair}: {count} candles stored")
        else:
            results = collector.collect_all()
            for pair, success in results.items():
                status = "OK" if success else "FAILED"
                print(f"{pair}: {status}")
    else:
        print(f"Starting collector for {args.pairs}...")
        print("Press Ctrl+C to stop")
        try:
            collector.start(blocking=True)
        except KeyboardInterrupt:
            collector.stop()
