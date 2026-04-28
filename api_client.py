"""
Binance Thailand REST API Client (api.binance.th).

Base URL: https://api.binance.th
Docs:     https://www.binance.com/en/binance-api  (Binance.th mirrors the
          Spot API surface used here: /api/v1/*.)

Authentication (signed endpoints)
---------------------------------
Signed (TRADE / USER_DATA) requests need:
  * Header  X-MBX-APIKEY  – your API key
  * Query parameter  timestamp   – current time in milliseconds
  * Query parameter  recvWindow  – 5000 ms (default)
  * Query parameter  signature   – HMAC-SHA256(secret, urlencode(params))

The signature is computed over the *full* querystring (including timestamp
and recvWindow) and appended last. Binance accepts signed POST/DELETE bodies
on the query string — we follow that convention here.

# --- NEW: SPEC_01 --- Binance Thailand REST client implementation.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import threading
import time
from contextlib import contextmanager
from decimal import ROUND_DOWN, Decimal
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from config import BINANCE, TRADING
from symbol_registry import get_symbol_map

logger = logging.getLogger(__name__)


# ── Public IP Detection (diagnostic only) ────────────────────────────────────
_IP_SERVICES = [
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me/ip",
]


def get_public_ip(timeout: float = 5) -> Optional[str]:
    """Best-effort public IP lookup — used only for diagnostics now that
    Binance.th does not require IP allowlisting on a per-key basis."""
    for url in _IP_SERVICES:
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                ip = resp.text.strip()
                if ip:
                    return ip
        except Exception as exc:
            logger.debug("Public IP lookup failed via %s: %s", url, exc)
            continue
    return None


# --- NEW: SPEC_01 --- Binance error code map. These are the error codes the
# bot actually classifies on; full table in the Binance API docs.
BINANCE_ERROR_MESSAGES: Dict[int, str] = {
    -1000: "Unknown error",
    -1001: "Internal error / disconnect",
    -1003: "Too many requests",
    -1013: "Filter failure (LOT_SIZE / PRICE_FILTER / MIN_NOTIONAL)",
    -1021: "Timestamp outside recvWindow",
    -1022: "Invalid signature",
    -1100: "Illegal characters in a parameter",
    -1102: "Mandatory parameter missing",
    -1121: "Invalid symbol",
    -2010: "New order rejected",
    -2011: "Cancel rejected (order already gone)",
    -2013: "Order does not exist",
    -2014: "API-key format invalid",
    -2015: "Invalid API-key, IP, or permissions",
    -2026: "Order is in a bad status",
}


def _binance_error_message(code: Any, fallback: Optional[str] = None) -> str:
    try:
        normalized = int(code)
    except (TypeError, ValueError):
        normalized = code
    mapped = BINANCE_ERROR_MESSAGES.get(normalized) if isinstance(normalized, int) else None
    if mapped:
        return mapped
    fallback_text = str(fallback or "").strip()
    return fallback_text or "Unknown error"


# --- NEW: SPEC_01 --- Internal "1m"/"15m" already matches Binance interval
# format; this map only normalises the legacy TradingView-style numeric
# resolutions that some older callers still pass in.
INTERVAL_MAP: Dict[str, str] = {
    "1": "1m",
    "1m": "1m",
    "5": "5m",
    "5m": "5m",
    "15": "15m",
    "15m": "15m",
    "30": "30m",
    "30m": "30m",
    "60": "1h",
    "1h": "1h",
    "240": "4h",
    "4h": "4h",
    "1D": "1d",
    "1d": "1d",
    "1440": "1d",
    "1W": "1w",
    "1w": "1w",
}


# Legacy fatal-auth flags retained for backward compatibility with startup
# helpers that still poll them.
SHOULD_SHUTDOWN: bool = False
SHUTDOWN_REASON: str = ""


# ── Circuit Breaker ──────────────────────────────────────────────────────────
# Logic identical to the previous exchange implementation — kept verbatim per
# SPEC_01.  Only doc strings reference the new exchange.


class CircuitBreaker:
    """
    Circuit breaker to stop trading after consecutive API errors.

    States:
      CLOSED   → Normal operation, requests pass through
      OPEN     → Circuit is tripped, requests are blocked
      HALF     → Testing if the API has recovered
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF = "half"

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
        half_max_calls: int = 2,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_max_calls = half_max_calls

        self._state = self.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure = 0.0
        self._half_calls = 0
        self._lock = threading.RLock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def is_available(self) -> bool:
        with self._lock:
            now = time.time()

            if self._state == self.CLOSED:
                return True

            if self._state == self.OPEN:
                if now - self._last_failure >= self.recovery_timeout:
                    self._state = self.HALF
                    self._half_calls = 0
                    logger.warning("CircuitBreaker: OPEN → HALF (testing recovery)")
                    return True
                return False

            if self._half_calls < self.half_max_calls:
                self._half_calls += 1
                return True
            return False

    def record_success(self):
        with self._lock:
            self._failure_count = 0
            self._success_count += 1
            if self._state == self.HALF:
                self._state = self.CLOSED
                self._half_calls = 0
                logger.info("CircuitBreaker: HALF → CLOSED (recovered)")

    def record_failure(self, error_msg: str = ""):
        with self._lock:
            self._failure_count += 1
            self._last_failure = time.time()

            if self._state == self.HALF:
                self._state = self.OPEN
                logger.warning("CircuitBreaker: HALF → OPEN (still failing)")
            elif self._failure_count >= self.failure_threshold:
                self._state = self.OPEN
                logger.warning(f"CircuitBreaker: CLOSED → OPEN " f"({self.failure_threshold} consecutive failures)")

            if error_msg:
                logger.error(f"CircuitBreaker failure #{self._failure_count}: {error_msg}")

    def reset(self):
        with self._lock:
            self._state = self.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._half_calls = 0


# ── Clock Sync ────────────────────────────────────────────────────────────────


class ClockSync:
    """Tracks offset between local clock and Binance.th server clock."""

    def __init__(self, max_offset: float = 30.0):
        self.max_offset = max_offset
        self._offset = 0.0
        self._last_sync = 0.0
        self._sync_interval = 300.0
        self._locked = threading.Lock()
        self._alerted = False

    @property
    def offset(self) -> float:
        with self._locked:
            return self._offset

    def sync(self, base_url: str) -> float:
        """
        Fetch server time and compute offset.  Returns offset in seconds.

        # --- NEW: SPEC_01 --- endpoint moved from /api/v3/servertime →
        # /api/v1/time and the response shape changed from a bare integer to
        # {"serverTime": <ms>}.
        """
        try:
            r = requests.get(f"{base_url}/api/v1/time", timeout=10)
            r.raise_for_status()
            payload = r.json()
            if isinstance(payload, dict):
                server_ts_ms = int(payload.get("serverTime", 0))
            else:
                server_ts_ms = int(payload)
            server_ts = server_ts_ms / 1000.0
            local_ts = time.time()

            offset = server_ts - local_ts
            with self._locked:
                self._offset = offset
                self._last_sync = local_ts

            if abs(offset) > self.max_offset and not self._alerted:
                logger.warning(f"ClockSync: LARGE OFFSET detected " f"({offset:+.1f}s) — requests may be rejected")
                self._alerted = True
            elif abs(offset) <= self.max_offset:
                self._alerted = False

            logger.debug(f"ClockSync: offset={offset:+.3f}s")
            return offset
        except Exception as e:
            logger.error(f"ClockSync: sync failed: {e}")
            return 0.0

    def is_synced(self) -> bool:
        return abs(self._offset) <= self.max_offset

    def should_resync(self) -> bool:
        return (time.time() - self._last_sync) >= self._sync_interval


# ── Exceptions ───────────────────────────────────────────────────────────────


class BinanceAPIError(Exception):
    """Raised when the Binance.th API returns an error code."""

    def __init__(self, code: Any, message: str, raw: Optional[Dict] = None):
        self.code = code
        self.message = message
        self.raw = raw or {}
        super().__init__(f"[{code}] {message}")


class CircuitBreakerOpen(BinanceAPIError):
    """Raised when the circuit breaker is open and blocking requests."""

    pass


class BinanceAuthException(BinanceAPIError):
    """Raised when Binance.th rejects private API auth and the bot must stop."""

    pass


# ── Helpers ──────────────────────────────────────────────────────────────────


def _round_step(value: float, step: float) -> float:
    """Round ``value`` DOWN to the nearest multiple of ``step``."""
    if step is None or step <= 0 or value <= 0:
        return float(value)
    d_val = Decimal(str(value))
    d_step = Decimal(str(step))
    rounded = (d_val / d_step).quantize(Decimal("1"), rounding=ROUND_DOWN) * d_step
    return float(rounded)


def _format_decimal(value: float, precision: int = 8) -> str:
    """Render a float without scientific notation, trimmed of trailing zeros."""
    s = f"{value:.{precision}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _decimal_places_from_step(step: float) -> int:
    """Infer a safe string precision from LOT_SIZE stepSize or PRICE_FILTER tickSize."""
    if step is None or step <= 0:
        return 8
    d = Decimal(str(step))
    if d.is_zero():
        return 8
    exp = d.as_tuple().exponent
    if exp >= 0:
        return 0
    return min(16, -exp)


def _quantize_down(value: float, decimals: int) -> float:
    if decimals < 0:
        decimals = 0
    q = Decimal("1") if decimals == 0 else Decimal("1").scaleb(-decimals)
    return float(Decimal(str(float(value))).quantize(q, rounding=ROUND_DOWN))


def _no_trailing_zeros(value: float) -> float:
    """Compatibility helper for legacy tests/callers; prefer ``_format_decimal``."""
    return float(_format_decimal(float(value), precision=8))


# ── BinanceThClient ──────────────────────────────────────────────────────────


class BinanceThClient:
    """
    Thin wrapper around the Binance Thailand REST API.

    Public API surface mirrors earlier client semantics so the
    rest of the codebase can keep calling ``get_ticker`` / ``get_candle`` /
    ``place_bid`` / ``place_ask`` / ``get_balances`` / ``cancel_order``
    without modification.  Internal symbol/timeframe normalisation is done
    by ``_to_binance_symbol`` / ``_to_binance_interval``.
    """

    # --- NEW: SPEC_01 --- Hardcoded base URL for Binance.th. We do NOT use
    # BINANCE.base_url here. Override via BINANCE_BASE_URL env var if needed.
    BASE_URL: str = "https://api.binance.th"
    TIMEOUT: float = 30.0

    # Number of consecutive signed-call auth errors (-2014/-2015/-1022) tolerated
    # before they are escalated to BinanceAuthException (which the trading loop
    # treats as a fatal/graceful-shutdown signal). A small N tolerates Binance.th
    # IP-whitelist propagation: right after the user adds an IP, a fraction of
    # signed requests can still hit gateway nodes whose cache hasn't refreshed
    # and return -2015 even though the key/IP/permissions are correct. Any
    # successful signed response resets the counter to 0.
    AUTH_FATAL_CONSECUTIVE_THRESHOLD: int = 5

    # When a signed call gets HTTP 401/403 with Binance body code -2015 (intermittent
    # gateway / IP whitelist propagation), retry the same logical request this many
    # times with a short backoff before surfacing BinanceAPIError to callers.
    SIGNED_TRANSIENT_2015_HTTP_RETRIES: int = 3

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        symbol: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.api_key = api_key or BINANCE.api_key
        self.api_secret = api_secret or BINANCE.api_secret

        default_symbol = (symbol or getattr(BINANCE, "default_symbol", "BTCUSDT")) or "BTCUSDT"
        if default_symbol.lower() in {"btc_thb", "thb_btc"}:
            default_symbol = "BTCUSDT"
        self.symbol = default_symbol

        # Resolve base URL — prefer explicit arg, then BINANCE config, then class default.
        configured = (base_url or getattr(BINANCE, "base_url", "") or "").strip()
        if configured.startswith("https://api.binance"):
            self.base_url = configured
        else:
            self.base_url = self.BASE_URL

        # Rate limiting
        self._last_request_time: float = 0.0
        self._min_request_interval: float = 0.2

        # Circuit breaker
        self._cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)

        # Clock sync
        self._clock = ClockSync(max_offset=30.0)

        # Loop error tracking (kept for parity with prior client)
        self._loop_error_count = 0
        self._loop_error_reset = 3.0
        self._last_error_time = 0.0

        # Balance cache
        self._balances_cache: Optional[Dict[str, Dict[str, float]]] = None
        self._balances_cache_time: float = 0.0
        self._balances_cache_ttl: float = 5.0

        # Thread-safety lock — guards _last_request_time and balance cache state.
        self._state_lock = threading.Lock()

        # Suppress fatal-auth shutdown side effects during opt-in startup probes.
        self._fatal_auth_suppression_context: str = ""

        # Tolerate transient signed-call auth errors (-2014/-2015/-1022) that
        # Binance.th gateways occasionally return while a freshly added IP
        # whitelist propagates. We only raise BinanceAuthException after this
        # many *consecutive* auth failures with no intervening signed success.
        self._consecutive_auth_failures: int = 0
        self.auth_fatal_threshold: int = int(self.AUTH_FATAL_CONSECUTIVE_THRESHOLD)
        self.signed_transient_2015_http_retries: int = int(self.SIGNED_TRANSIENT_2015_HTTP_RETRIES)

        # --- NEW: SPEC_01 --- exchangeInfo cache (per Binance symbol).
        self._exchange_info_cache: Dict[str, Dict[str, Any]] = {}
        self._exchange_info_lock = threading.Lock()

        # --- NEW: SPEC_01 --- Stub-history WARN tracking (one-shot per kind).
        self._history_stub_warned: Dict[str, bool] = {}

    # ── Circuit Breaker / clock helpers ────────────────────────────────────

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._cb

    def is_circuit_open(self) -> bool:
        return self._cb.state == CircuitBreaker.OPEN

    def reset_circuit(self):
        self._cb.reset()
        logger.info("CircuitBreaker: manually reset to CLOSED")

    @contextmanager
    def suppress_fatal_auth_handling(self, context: str = "startup"):
        """Temporarily downgrade auth errors from fatal shutdown to caller-managed warnings."""
        previous = self._fatal_auth_suppression_context
        self._fatal_auth_suppression_context = str(context or "startup")
        try:
            yield self
        finally:
            self._fatal_auth_suppression_context = previous

    def sync_clock(self) -> float:
        return self._clock.sync(self.base_url)

    def check_clock_sync(self) -> bool:
        if self._clock.should_resync():
            self.sync_clock()
        if not self._clock.is_synced():
            logger.warning(
                f"Clock offset {self._clock.offset:+.1f}s exceeds "
                f"threshold {self._clock.max_offset}s — requests may fail"
            )
            return False
        return True

    # ── Symbol / interval / signing ────────────────────────────────────────

    def _to_binance_symbol(self, symbol: str) -> str:
        """# --- NEW: SPEC_01 --- Map THB_* / btc_thb / BTC_THB → Binance form."""
        if not symbol:
            return self.symbol
        sym = str(symbol).strip()
        upper = sym.upper()
        symbol_map = get_symbol_map()
        if upper in symbol_map:
            return symbol_map[upper]

        # Already a Binance-style symbol
        if upper.endswith("USDT") and "_" not in upper:
            return upper

        # btc_thb / BTC_THB → BTCUSDT (legacy lower / base_quote shapes)
        if "_" in upper:
            parts = upper.split("_")
            if len(parts) == 2:
                left, right = parts
                base = right if left in {"THB", "USDT", "USD"} else left
                key = f"THB_{base}"
                if key in symbol_map:
                    return symbol_map[key]
                return f"{base}USDT"
        return upper

    @staticmethod
    def _to_binance_interval(timeframe: str) -> str:
        """# --- NEW: SPEC_01 --- Normalise timeframe strings to Binance form."""
        if not timeframe:
            return "15m"
        return INTERVAL_MAP.get(str(timeframe), str(timeframe))

    def _sign(self, params: Dict[str, Any]) -> str:
        """# --- NEW: SPEC_01 --- HMAC-SHA256 of urlencode(params)."""
        query_string = urlencode(params, doseq=True)
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _server_now_ms(self) -> int:
        """Current millisecond timestamp adjusted by the cached clock offset."""
        return int((time.time() + self._clock.offset) * 1000)

    # ── Filter cache (LOT_SIZE / PRICE_FILTER) ─────────────────────────────

    def _get_symbol_filters(self, binance_symbol: str) -> Dict[str, Any]:
        """# --- NEW: SPEC_01 --- Cached lookup of LOT_SIZE / PRICE_FILTER /
        MIN_NOTIONAL filters per symbol.  Falls back to 6-decimal precision
        if exchangeInfo is unavailable."""
        with self._exchange_info_lock:
            cached = self._exchange_info_cache.get(binance_symbol)
            if cached is not None:
                return cached

        filters: Dict[str, Any] = {
            "stepSize": 0.000001,
            "tickSize": 0.000001,
            "minQty": 0.0,
            "minNotional": 0.0,
            "baseAssetPrecision": 8,
            "quoteAssetPrecision": 8,
            "_fallback": True,
        }
        try:
            r = requests.get(
                f"{self.base_url}/api/v1/exchangeInfo",
                params={"symbol": binance_symbol},
                timeout=self.TIMEOUT,
            )
            data = r.json() if r.status_code == 200 else {}
            symbols = data.get("symbols") if isinstance(data, dict) else None
            entry = symbols[0] if symbols else None
            if entry:
                try:
                    filters["baseAssetPrecision"] = int(entry.get("baseAssetPrecision") or 8)
                except (TypeError, ValueError):
                    filters["baseAssetPrecision"] = 8
                try:
                    qp = entry.get("quoteAssetPrecision")
                    if qp is None:
                        qp = entry.get("quotePrecision")
                    filters["quoteAssetPrecision"] = int(qp or 8)
                except (TypeError, ValueError):
                    filters["quoteAssetPrecision"] = 8
            if entry and isinstance(entry.get("filters"), list):
                for f in entry["filters"]:
                    ftype = f.get("filterType")
                    if ftype == "LOT_SIZE":
                        filters["stepSize"] = float(f.get("stepSize") or filters["stepSize"])
                        filters["minQty"] = float(f.get("minQty") or 0.0)
                    elif ftype == "PRICE_FILTER":
                        filters["tickSize"] = float(f.get("tickSize") or filters["tickSize"])
                    elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                        filters["minNotional"] = float(f.get("minNotional") or f.get("notional") or 0.0)
                filters["_fallback"] = False
        except Exception as exc:
            logger.warning(
                "exchangeInfo lookup failed for %s: %s — using fallback precision",
                binance_symbol,
                exc,
            )

        with self._exchange_info_lock:
            self._exchange_info_cache[binance_symbol] = filters
        return filters

    def _round_quantity(self, binance_symbol: str, qty: float) -> float:
        f = self._get_symbol_filters(binance_symbol)
        if f.get("_fallback"):
            raise BinanceAPIError(
                -1013, f"Exchange filters unavailable for {binance_symbol}; refusing to submit quantity"
            )
        step = float(f.get("stepSize") or 0.0)
        if step <= 0:
            raise BinanceAPIError(-1013, f"Missing LOT_SIZE stepSize for {binance_symbol}")
        rounded = _round_step(qty, step)
        if rounded <= 0 and qty > 0:
            raise BinanceAPIError(
                -1013,
                f"Quantity {qty} rounds to 0 under LOT_SIZE stepSize {step} for {binance_symbol}",
            )
        min_qty = float(f.get("minQty") or 0.0)
        if min_qty > 0 and 0 < rounded < min_qty:
            raise BinanceAPIError(
                -1013,
                f"Quantity {rounded} below minQty {min_qty} for {binance_symbol}",
            )
        return rounded

    def _round_price(self, binance_symbol: str, price: float) -> float:
        f = self._get_symbol_filters(binance_symbol)
        if f.get("_fallback"):
            raise BinanceAPIError(-1013, f"Exchange filters unavailable for {binance_symbol}; refusing to submit price")
        tick = float(f.get("tickSize") or 0.0)
        if tick <= 0:
            raise BinanceAPIError(-1013, f"Missing PRICE_FILTER tickSize for {binance_symbol}")
        rounded = _round_step(price, tick)
        if rounded <= 0 and price > 0:
            raise BinanceAPIError(
                -1013,
                f"Price {price} rounds to 0 under PRICE_FILTER tickSize {tick} for {binance_symbol}",
            )
        return rounded

    def _format_qty_string(self, binance_symbol: str, qty: float) -> str:
        """String quantity for API params using LOT_SIZE-derived precision."""
        f = self._get_symbol_filters(binance_symbol)
        step = float(f.get("stepSize") or 0.0)
        if step > 0:
            prec = _decimal_places_from_step(step)
        else:
            try:
                prec = int(f.get("baseAssetPrecision") or 8)
            except (TypeError, ValueError):
                prec = 8
        return _format_decimal(float(qty), precision=min(16, max(0, prec)))

    def _format_price_string(self, binance_symbol: str, price: float) -> str:
        """String price for API params using PRICE_FILTER tick precision."""
        f = self._get_symbol_filters(binance_symbol)
        tick = float(f.get("tickSize") or 0.0)
        prec = _decimal_places_from_step(tick) if tick > 0 else 8
        return _format_decimal(float(price), precision=min(16, max(0, prec)))

    def _format_quote_order_qty_string(self, binance_symbol: str, quote_amount: float) -> str:
        """MARKET BUY quoteOrderQty using quote asset precision from exchangeInfo."""
        f = self._get_symbol_filters(binance_symbol)
        try:
            qp = int(f.get("quoteAssetPrecision") or 8)
        except (TypeError, ValueError):
            qp = 8
        qp = min(16, max(0, qp))
        rounded = _quantize_down(float(quote_amount), qp)
        return _format_decimal(rounded, precision=qp)

    def validate_symbol_exchange_info(self, symbols: List[str]) -> List[str]:
        """Warm exchangeInfo filters for each unique Binance symbol; return human-readable warnings."""
        warnings: List[str] = []
        seen: set[str] = set()
        for sym in symbols or []:
            raw = str(sym or "").strip()
            if not raw:
                continue
            bs = self._to_binance_symbol(raw)
            if not bs or bs in seen:
                continue
            seen.add(bs)
            f = self._get_symbol_filters(bs)
            if f.get("_fallback"):
                warnings.append(f"{raw} ({bs}): exchangeInfo filters unavailable")
        return warnings

    # ── Core request ───────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        signed: bool = False,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Shared request wrapper with circuit-breaker, clock sync, and rate limiting.

        Per Binance convention all parameters (including for POST/DELETE) are
        sent on the query string — that is also what gets signed.
        """
        if not self._cb.is_available():
            raise CircuitBreakerOpen(
                -999,
                "Circuit breaker is OPEN — too many consecutive errors. " "Trading paused. Will retry after cooldown.",
            )

        if signed and not self.check_clock_sync():
            raise BinanceAPIError(
                -998,
                f"Clock offset {self._clock.offset:+.1f}s exceeds limit — " "refusing authenticated request",
            )

        # Thread-safe rate limiter — claim a slot inside the lock; sleep outside.
        with self._state_lock:
            now = time.time()
            elapsed = now - self._last_request_time
            sleep_for = max(0.0, self._min_request_interval - elapsed)
            self._last_request_time = now + sleep_for
        if sleep_for:
            time.sleep(sleep_for)

        url = f"{self.base_url}{path}"
        base_params: Dict[str, Any] = {k: v for k, v in (params or {}).items() if v is not None}
        max_http = 1
        if signed:
            max_http = max(
                1,
                int(getattr(self, "signed_transient_2015_http_retries", self.SIGNED_TRANSIENT_2015_HTTP_RETRIES)),
            )

        try:
            data: Any = None
            for http_attempt in range(max_http):
                request_params = dict(base_params)
                headers: Dict[str, str] = {"Accept": "application/json"}
                if signed:
                    request_params["timestamp"] = self._server_now_ms()
                    request_params.setdefault("recvWindow", 5000)
                    request_params["signature"] = self._sign(request_params)
                    headers["X-MBX-APIKEY"] = self.api_key
                elif self.api_key and method.upper() != "GET":
                    headers["X-MBX-APIKEY"] = self.api_key

                r = requests.request(
                    method.upper(),
                    url,
                    params=request_params,
                    headers=headers,
                    timeout=self.TIMEOUT if timeout is None else timeout,
                )

                try:
                    data = r.json()
                except ValueError:
                    raise BinanceAPIError(
                        -1,
                        f"Non-JSON response ({r.status_code}): {r.text[:300]}",
                    )

                if isinstance(data, dict) and "code" in data and "msg" in data:
                    code_val = data.get("code")
                    if code_val is None:
                        code_int = 0
                    else:
                        try:
                            code_int = int(code_val)
                        except (TypeError, ValueError):
                            code_int = code_val
                    if isinstance(code_int, int) and code_int < 0:
                        if signed and r.status_code in (401, 403) and code_int == -2015 and http_attempt < max_http - 1:
                            logger.warning(
                                "HTTP %s + code -2015 on %s — retrying signed request (%s/%s) after gateway blip",
                                r.status_code,
                                path,
                                http_attempt + 1,
                                max_http,
                            )
                            time.sleep(0.35 * (1.45**http_attempt))
                            continue

                        if r.status_code >= 400:
                            is_suppressed_auth = (
                                signed and r.status_code in (401, 403) and bool(self._fatal_auth_suppression_context)
                            )
                            threshold = max(
                                1,
                                int(getattr(self, "auth_fatal_threshold", self.AUTH_FATAL_CONSECUTIVE_THRESHOLD)),
                            )
                            is_transient_soft = (
                                signed
                                and r.status_code in (401, 403)
                                and code_int == -2015
                                and (self._consecutive_auth_failures + 1) < threshold
                            )
                            log_method = logger.warning if (is_suppressed_auth or is_transient_soft) else logger.error
                            suffix = f" during {self._fatal_auth_suppression_context}" if is_suppressed_auth else ""
                            log_method(f"HTTP {r.status_code} from {path}{suffix} — " f"Raw response: {r.text[:500]}")
                        msg = _binance_error_message(code_int, data.get("msg"))
                        raise BinanceAPIError(code_int, msg, raw=data)

                if r.status_code >= 400:
                    logger.error(f"HTTP {r.status_code} from {path} — " f"Raw response: {r.text[:500]}")
                    raise BinanceAPIError(
                        -1,
                        f"HTTP {r.status_code} from {path}: {str(data)[:300]}",
                    )

                break

            if signed and self._consecutive_auth_failures:
                logger.info(
                    "Signed call recovered on %s after %d transient auth error(s) — " "resetting auth-failure counter.",
                    path,
                    self._consecutive_auth_failures,
                )
            self._consecutive_auth_failures = 0
            self._cb.record_success()
            return data

        except BinanceAPIError as e:
            is_auth_error = e.code in (-2014, -2015) or (signed and isinstance(e.code, int) and e.code in {-1022})
            if is_auth_error:
                if self._fatal_auth_suppression_context:
                    logger.warning(
                        "Binance auth error %s on %s during %s — caller will downgrade to "
                        "degraded mode; fatal shutdown side effects suppressed",
                        e.code,
                        path,
                        self._fatal_auth_suppression_context,
                    )
                    raise

                self._consecutive_auth_failures += 1
                threshold = max(
                    1,
                    int(getattr(self, "auth_fatal_threshold", self.AUTH_FATAL_CONSECUTIVE_THRESHOLD)),
                )

                if self._consecutive_auth_failures < threshold:
                    logger.warning(
                        "Transient Binance auth error %s on %s "
                        "(consecutive %d/%d, likely IP-whitelist propagation). "
                        "Caller may retry; circuit breaker NOT tripped.",
                        e.code,
                        path,
                        self._consecutive_auth_failures,
                        threshold,
                    )
                    raise

                current_ip = get_public_ip() or "unknown"
                logger.critical(
                    "🚨 FATAL: Binance auth error %s on %s after %d consecutive failures. "
                    "Current IP: %s | Message: %s | Raw: %s",
                    e.code,
                    path,
                    self._consecutive_auth_failures,
                    current_ip,
                    e.message,
                    e.raw,
                )
                self._cb.record_failure(f"FATAL Auth Error {e.code} on {path}")
                raise BinanceAuthException(
                    e.code,
                    f"🚨 FATAL: Binance.th auth error {e.code} ({e.message}) after "
                    f"{self._consecutive_auth_failures} consecutive failures. "
                    f"Verify API key/secret and permissions, then restart.",
                    raw=e.raw,
                )

            # Bad/stale order family — do NOT trip the global circuit breaker.
            if isinstance(e.code, int) and e.code in (-1013, -2010, -2011, -2013, -2026):
                if e.code == -2011 and "/api/v1/order" in path and method.upper() == "DELETE":
                    logger.info(
                        f"Expected stale cancel {e.code} on {path}: {e.message} " f"(order already filled or gone)"
                    )
                else:
                    logger.warning(
                        f"⚠️ Order error {e.code} on {path}: {e.message} "
                        f"(treated as bad request — circuit breaker NOT affected)"
                    )
                raise

            self._cb.record_failure(str(path))
            raise
        except Exception as e:
            self._cb.record_failure(str(e))
            raise BinanceAPIError(-1, f"Request failed: {e}")

    # ── Market data (public) ────────────────────────────────────────────────

    def get_ticker(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/v1/ticker/24hr?symbol=BTCUSDT — returns normalized ticker dict."""
        original = symbol or self.symbol
        binance_symbol = self._to_binance_symbol(original)
        try:
            r = requests.get(
                f"{self.base_url}/api/v1/ticker/24hr",
                params={"symbol": binance_symbol},
                timeout=self.TIMEOUT,
            )
            data = r.json()
        except Exception as e:
            raise BinanceAPIError(-1, f"Ticker request failed: {e}")

        if isinstance(data, dict) and "code" in data and isinstance(data.get("code"), int) and data["code"] < 0:
            raise BinanceAPIError(
                data["code"],
                _binance_error_message(data["code"], data.get("msg")),
                raw=data,
            )

        return _normalize_ticker(data, requested_symbol=original)

    def get_tickers_batch(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """Batch-fetch tickers via /api/v1/ticker/24hr (no symbol → returns all)."""
        try:
            r = requests.get(f"{self.base_url}/api/v1/ticker/24hr", timeout=self.TIMEOUT)
            payload = r.json()
        except Exception as e:
            raise BinanceAPIError(-1, f"Batch ticker request failed: {e}")

        if not isinstance(payload, list):
            return {}

        index: Dict[str, Dict[str, Any]] = {}
        for entry in payload:
            if isinstance(entry, dict) and entry.get("symbol"):
                index[str(entry["symbol"]).upper()] = entry

        results: Dict[str, Dict[str, Any]] = {}
        for sym in symbols:
            binance_symbol = self._to_binance_symbol(sym)
            entry = index.get(binance_symbol)
            if entry is not None:
                results[sym] = _normalize_ticker(entry, requested_symbol=sym)
        return results

    def get_candle(
        self,
        symbol: str,
        timeframe: str = "15m",
        limit: int = 250,
    ) -> Dict[str, Any]:
        """
        GET /api/v1/klines — Binance.th klines.

        # --- NEW: SPEC_01 --- Binance returns timestamps in milliseconds; we
        # convert to seconds so callers (which were written against the old
        # legacy caller format) keep working without changes.
        """
        binance_symbol = self._to_binance_symbol(symbol)
        interval = self._to_binance_interval(timeframe)
        cache_key = f"{binance_symbol}:{interval}:{int(limit)}"

        cached = _candle_cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            r = requests.get(
                f"{self.base_url}/api/v1/klines",
                params={
                    "symbol": binance_symbol,
                    "interval": interval,
                    "limit": int(limit),
                },
                timeout=self.TIMEOUT,
            )
            rows = r.json()
        except Exception as exc:
            return {"error": 1, "message": f"klines request failed: {exc}", "result": []}

        if isinstance(rows, dict) and "code" in rows and isinstance(rows.get("code"), int) and rows["code"] < 0:
            return {
                "error": rows["code"],
                "message": _binance_error_message(rows["code"], rows.get("msg")),
                "result": [],
            }

        if not isinstance(rows, list):
            return {"error": 1, "message": "Unexpected klines payload", "result": []}

        candles: List[List[float]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                candles.append(
                    [
                        int(int(row[0]) / 1000),
                        float(row[1]),
                        float(row[2]),
                        float(row[3]),
                        float(row[4]),
                        float(row[5]),
                    ]
                )
            except (TypeError, ValueError):
                continue

        result = {"error": 0, "result": candles, "status": "ok"}
        _candle_cache_put(cache_key, result)
        return result

    def get_symbols(self) -> List[Dict[str, Any]]:
        """GET /api/v1/exchangeInfo — list trading pairs."""
        try:
            r = requests.get(f"{self.base_url}/api/v1/exchangeInfo", timeout=self.TIMEOUT)
            data = r.json()
        except Exception as exc:
            raise BinanceAPIError(-1, f"exchangeInfo request failed: {exc}")
        if isinstance(data, dict):
            return list(data.get("symbols") or [])
        return []

    def get_depth(
        self,
        symbol: Optional[str] = None,
        limit: int = 25,
    ) -> Dict[str, List]:
        """GET /api/v1/depth — order book for the given symbol."""
        binance_symbol = self._to_binance_symbol(symbol or self.symbol)
        data = self._request(
            "GET",
            "/api/v1/depth",
            params={"symbol": binance_symbol, "limit": int(limit)},
        )
        if not isinstance(data, dict):
            return {"asks": [], "bids": []}
        return {
            "asks": [[float(p), float(q)] for p, q, *_ in data.get("asks", [])],
            "bids": [[float(p), float(q)] for p, q, *_ in data.get("bids", [])],
        }

    def get_bids(
        self,
        symbol: Optional[str] = None,
        limit: int = 25,
    ) -> List[List[float]]:
        return self.get_depth(symbol=symbol, limit=limit).get("bids", [])

    def get_asks(
        self,
        symbol: Optional[str] = None,
        limit: int = 25,
    ) -> List[List[float]]:
        return self.get_depth(symbol=symbol, limit=limit).get("asks", [])

    def get_trades(
        self,
        symbol: Optional[str] = None,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """GET /api/v1/trades — recent trades."""
        binance_symbol = self._to_binance_symbol(symbol or self.symbol)
        data = self._request(
            "GET",
            "/api/v1/trades",
            params={"symbol": binance_symbol, "limit": int(limit)},
        )
        if not isinstance(data, list):
            return []
        return data

    # ── Account (signed) ────────────────────────────────────────────────────

    def _account_snapshot(self, *, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Internal: GET /api/v1/account."""
        return self._request("GET", "/api/v1/account", signed=True, timeout=timeout)

    def get_wallet(self) -> Dict[str, float]:
        """Return a flat dict of available balances."""
        balances = self.get_balances()
        return {asset: float(info.get("available", 0.0)) for asset, info in balances.items()}

    def get_balance(self) -> Dict[str, float]:
        """Alias of get_wallet — kept for backward compatibility."""
        return self.get_wallet()

    def get_balances(
        self,
        *,
        force_refresh: bool = False,
        allow_stale: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Dict[str, float]]:
        """GET /api/v1/account → {"BTC": {"available": x, "reserved": y}, ...}."""
        with self._state_lock:
            if (
                not force_refresh
                and self._balances_cache is not None
                and time.time() - self._balances_cache_time < self._balances_cache_ttl
            ):
                return self._balances_cache

        try:
            data = self._account_snapshot(timeout=timeout)
            result: Dict[str, Dict[str, float]] = {}
            for entry in (data.get("balances") if isinstance(data, dict) else None) or []:
                asset = str(entry.get("asset") or "").upper()
                if not asset:
                    continue
                try:
                    available = float(entry.get("free", 0.0) or 0.0)
                    reserved = float(entry.get("locked", 0.0) or 0.0)
                except (TypeError, ValueError):
                    available, reserved = 0.0, 0.0
                if available <= 0 and reserved <= 0:
                    continue
                result[asset] = {"available": available, "reserved": reserved}

            with self._state_lock:
                self._balances_cache = result
                self._balances_cache_time = time.time()
            return result
        except Exception as exc:
            with self._state_lock:
                if allow_stale and self._balances_cache is not None:
                    logger.warning("[Balance] Using stale cache due to API error: %s", exc)
                    return self._balances_cache
            if isinstance(exc, BinanceAPIError) and exc.code == -2015:
                logger.warning(
                    "[Balance] Failed to fetch balances (transient -2015), no stale cache: %s",
                    exc,
                )
            else:
                logger.error("[Balance] Failed to fetch balances and no stale cache is available", exc_info=True)
            raise

    def get_balances_fresh(self) -> Dict[str, Dict[str, float]]:
        """Fetch balances bypassing the cache and disabling stale fallback."""
        return self.get_balances(force_refresh=True, allow_stale=False)

    def get_open_orders(
        self,
        symbol: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """GET /api/v1/openOrders — list open orders for ``symbol`` (or all)."""
        params: Dict[str, Any] = {}
        requested_symbol = ""
        if symbol:
            requested_symbol = str(symbol).upper()
            params["symbol"] = self._to_binance_symbol(symbol)

        data = self._request("GET", "/api/v1/openOrders", signed=True, params=params)
        rows = data if isinstance(data, list) else []

        normalized: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            entry = _normalize_order_response(row, fallback_symbol=requested_symbol or symbol or "")
            if requested_symbol:
                entry.setdefault("_checked_symbol", requested_symbol)
            normalized.append(entry)
        return normalized

    # ── Trading (signed) ───────────────────────────────────────────────────

    def _build_order_params(
        self,
        *,
        binance_symbol: str,
        side: str,
        order_type: str,
        amount: float,
        rate: float,
        post_only: bool,
        client_id: Optional[str],
        is_buy: bool,
    ) -> Dict[str, Any]:
        """Translate legacy (amount, rate) inputs into Binance order parameters."""
        side = side.upper()
        ot = (order_type or "limit").lower()

        params: Dict[str, Any] = {"symbol": binance_symbol, "side": side}

        if ot == "market":
            if is_buy:
                # BUY semantic: amount = quote-currency to spend at market.
                f = self._get_symbol_filters(binance_symbol)
                try:
                    qp = int(f.get("quoteAssetPrecision") or 8)
                except (TypeError, ValueError):
                    qp = 8
                qp = min(16, max(0, qp))
                quote_qty = _quantize_down(float(amount), qp)
                if quote_qty <= 0:
                    raise BinanceAPIError(-1100, "MARKET BUY requires amount > 0")
                min_notional = float(f.get("minNotional") or 0.0)
                if min_notional > 0 and quote_qty < min_notional:
                    raise BinanceAPIError(
                        -1013,
                        f"MARKET BUY notional {quote_qty} below minNotional {min_notional}",
                    )
                params["type"] = "MARKET"
                params["quoteOrderQty"] = self._format_quote_order_qty_string(binance_symbol, quote_qty)
            else:
                qty = self._round_quantity(binance_symbol, float(amount))
                if qty <= 0:
                    raise BinanceAPIError(-1100, "MARKET SELL requires amount > 0")
                params["type"] = "MARKET"
                params["quantity"] = self._format_qty_string(binance_symbol, qty)
        else:
            if rate is None or float(rate) <= 0:
                raise BinanceAPIError(-1100, "LIMIT order requires a positive rate")
            price = self._round_price(binance_symbol, float(rate))
            qty_raw = (float(amount) / price) if is_buy else float(amount)
            qty = self._round_quantity(binance_symbol, qty_raw)
            if qty <= 0:
                raise BinanceAPIError(
                    -1013,
                    f"LIMIT {side} quantity {qty_raw} rounds to 0 under LOT_SIZE filter",
                )
            f = self._get_symbol_filters(binance_symbol)
            min_notional = float(f.get("minNotional") or 0.0)
            notional = qty * price
            if min_notional > 0 and notional < min_notional:
                raise BinanceAPIError(
                    -1013,
                    f"LIMIT {side} notional {notional} below minNotional {min_notional}",
                )
            params["type"] = "LIMIT_MAKER" if post_only else "LIMIT"
            params["quantity"] = self._format_qty_string(binance_symbol, qty)
            params["price"] = self._format_price_string(binance_symbol, price)
            if not post_only:
                params["timeInForce"] = "GTC"

        if client_id:
            cid = str(client_id)[:36]
            params["newClientOrderId"] = cid
        return params

    def place_bid(
        self,
        symbol: str,
        amount: float,
        rate: float,
        order_type: str = "limit",
        client_id: Optional[str] = None,
        post_only: bool = False,
    ) -> Dict[str, Any]:
        """POST /api/v1/order side=BUY — place a buy order."""
        binance_symbol = self._to_binance_symbol(symbol)
        params = self._build_order_params(
            binance_symbol=binance_symbol,
            side="BUY",
            order_type=order_type,
            amount=amount,
            rate=rate,
            post_only=post_only,
            client_id=client_id,
            is_buy=True,
        )
        data = self._request("POST", "/api/v1/order", signed=True, params=params)
        return _normalize_order_response(
            data if isinstance(data, dict) else {},
            fallback_symbol=symbol,
        )

    def place_ask(
        self,
        symbol: str,
        amount: float,
        rate: float,
        order_type: str = "limit",
        client_id: Optional[str] = None,
        post_only: bool = False,
    ) -> Dict[str, Any]:
        """POST /api/v1/order side=SELL — place a sell order."""
        binance_symbol = self._to_binance_symbol(symbol)
        params = self._build_order_params(
            binance_symbol=binance_symbol,
            side="SELL",
            order_type=order_type,
            amount=amount,
            rate=rate,
            post_only=post_only,
            client_id=client_id,
            is_buy=False,
        )
        data = self._request("POST", "/api/v1/order", signed=True, params=params)
        return _normalize_order_response(
            data if isinstance(data, dict) else {},
            fallback_symbol=symbol,
        )

    def cancel_order(
        self,
        symbol: str,
        order_id: str,
        side: str,
    ) -> Dict[str, Any]:
        """DELETE /api/v1/order — cancel an open order."""
        binance_symbol = self._to_binance_symbol(symbol)
        data = self._request(
            "DELETE",
            "/api/v1/order",
            signed=True,
            params={"symbol": binance_symbol, "orderId": str(order_id)},
        )
        return _normalize_order_response(
            data if isinstance(data, dict) else {},
            fallback_symbol=symbol,
        )

    def get_order_info(
        self,
        symbol: str,
        order_id: str,
        side: str = "",
    ) -> Dict[str, Any]:
        """GET /api/v1/order — fetch a single order's status."""
        binance_symbol = self._to_binance_symbol(symbol)
        data = self._request(
            "GET",
            "/api/v1/order",
            signed=True,
            params={"symbol": binance_symbol, "orderId": str(order_id)},
        )
        return _normalize_order_response(
            data if isinstance(data, dict) else {},
            fallback_symbol=symbol,
        )

    def get_order_history(
        self,
        symbol: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """GET /api/v1/allOrders — historical orders for ``symbol``."""
        binance_symbol = self._to_binance_symbol(symbol)
        data = self._request(
            "GET",
            "/api/v1/allOrders",
            signed=True,
            params={"symbol": binance_symbol, "limit": int(limit)},
        )
        rows = data if isinstance(data, list) else []
        return [_normalize_order_response(row, fallback_symbol=symbol) for row in rows if isinstance(row, dict)]

    # ── Wallet / fiat history (stubbed in SPEC_01) ─────────────────────────
    # Binance.th does not expose direct equivalents of the legacy
    # /api/v3/fiat/* and /api/v4/crypto/* history feeds.  monitoring.py still
    # calls these helpers; per the SPEC_01 decision we return empty payloads
    # and log a one-shot WARN per kind so the balance monitor keeps running.

    def _stub_history(self, kind: str, *, as_dict: bool = False) -> Any:
        if not self._history_stub_warned.get(kind):
            logger.warning(
                "[history] %s history unavailable on Binance.th — returning empty result "
                "(SPEC_01 stub; revisit in a dedicated history SPEC).",
                kind,
            )
            self._history_stub_warned[kind] = True
        return {"items": []} if as_dict else []

    def get_fiat_deposit_history(self, *args, **kwargs) -> List[Dict[str, Any]]:
        return self._stub_history("fiat_deposit")

    def get_fiat_withdraw_history(self, *args, **kwargs) -> List[Dict[str, Any]]:
        return self._stub_history("fiat_withdraw")

    def get_crypto_deposit_history(self, *args, **kwargs) -> Dict[str, Any]:
        return self._stub_history("crypto_deposit", as_dict=True)

    def get_crypto_withdraw_history(self, *args, **kwargs) -> Dict[str, Any]:
        return self._stub_history("crypto_withdraw", as_dict=True)

    # ── Convenience helpers ─────────────────────────────────────────────────

    def is_authenticated(self) -> bool:
        """Check whether plausible API credentials are configured."""
        if not self.api_key or not self.api_secret:
            return False
        placeholder_markers = ("your_binance", "placeholder", "changeme")
        key_lower = self.api_key.lower()
        return not any(m in key_lower for m in placeholder_markers)

    def fmt_balance(self, asset: str = "USDT") -> str:
        balances = self.get_balances()
        info = balances.get(asset.upper(), {"available": 0.0, "reserved": 0.0})
        av = info.get("available", 0.0)
        rv = info.get("reserved", 0.0)
        return f"{asset}: available={av:.8f}  reserved={rv:.8f}"

    def fmt_ticker(self, symbol: Optional[str] = None) -> str:
        t = self.get_ticker(symbol)
        sym = t.get("symbol", symbol or self.symbol)
        return (
            f"{sym} | last={t['last']} | "
            f"bid={t['highest_bid']} / ask={t['lowest_ask']} | "
            f"24h: {t['percent_change']}% (H:{t['high_24_hr']} L:{t['low_24_hr']})"
        )


# ── Module-level helpers ─────────────────────────────────────────────────────


def _normalize_ticker(data: Dict[str, Any], *, requested_symbol: str) -> Dict[str, Any]:
    """Normalize a Binance 24hr ticker payload into the expected internal shape."""
    if not isinstance(data, dict):
        return {}

    def _f(key: str, default: float = 0.0) -> float:
        try:
            return float(data.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    return {
        "last": _f("lastPrice"),
        "high": _f("highPrice"),
        "low": _f("lowPrice"),
        "highest_bid": _f("bidPrice"),
        "lowest_ask": _f("askPrice"),
        "volume": _f("volume"),
        "quote_volume": _f("quoteVolume"),
        "percent_change": _f("priceChangePercent"),
        "high_24_hr": _f("highPrice"),
        "low_24_hr": _f("lowPrice"),
        "symbol": requested_symbol or data.get("symbol", ""),
        "_raw": data,
    }


_BINANCE_ORDER_STATUS_MAP = {
    "NEW": "unfilled",
    "PARTIALLY_FILLED": "partial",
    "FILLED": "filled",
    "CANCELED": "cancelled",
    "PENDING_CANCEL": "cancelling",
    "REJECTED": "rejected",
    "EXPIRED": "expired",
}


def _normalize_order_response(data: Dict[str, Any], *, fallback_symbol: str) -> Dict[str, Any]:
    """
    Normalize a Binance order JSON into the internal order dict that
    trade_executor.py and friends still parse: id / amt / rat / rec / fee /
    cre / ts / typ / ci / status / side.

    The original Binance payload is preserved under ``_raw``.
    """

    def _f(key: str, default: float = 0.0) -> float:
        try:
            return float(data.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    raw_symbol = str(data.get("symbol") or fallback_symbol or "").upper()
    side = str(data.get("side") or "").upper()
    order_type = str(data.get("type") or "").lower()
    status = str(data.get("status") or "").upper()

    orig_qty = _f("origQty")
    executed_qty = _f("executedQty")
    cum_quote_qty = _f("cummulativeQuoteQty")
    price = _f("price")
    transact_ts_ms = data.get("transactTime") or data.get("updateTime") or data.get("time") or 0
    try:
        transact_ts = int(int(transact_ts_ms) / 1000)
    except (TypeError, ValueError):
        transact_ts = int(time.time())

    if side == "BUY":
        amt = cum_quote_qty if cum_quote_qty > 0 else (orig_qty * price if price > 0 else orig_qty)
        rec = executed_qty
    else:
        amt = orig_qty
        rec = cum_quote_qty if cum_quote_qty > 0 else executed_qty

    fee = 0.0
    fills = data.get("fills") if isinstance(data, dict) else None
    if isinstance(fills, list):
        for f in fills:
            try:
                fee += float(f.get("commission") or 0.0)
            except (TypeError, ValueError):
                continue

    avg_price = 0.0
    if executed_qty > 0 and cum_quote_qty > 0:
        avg_price = cum_quote_qty / executed_qty

    return {
        "id": str(data.get("orderId") or data.get("id") or ""),
        "ci": str(data.get("clientOrderId") or ""),
        "typ": order_type or ("limit" if price > 0 else "market"),
        "side": side.lower() or "",
        "amt": amt,
        "rat": price if price > 0 else avg_price,
        "rec": rec,
        "fee": fee,
        "cre": transact_ts,
        "ts": transact_ts,
        "status": _BINANCE_ORDER_STATUS_MAP.get(status, status.lower()),
        "avg_price": avg_price,
        "symbol": raw_symbol or fallback_symbol,
        "_raw": data,
    }


# ── Candle cache (TTL 60s, max 200 entries) ─────────────────────────────────

_candle_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_candle_cache_lock = threading.Lock()
_CANDLE_CACHE_TTL = 60.0
_CANDLE_CACHE_MAX = 200


def _candle_cache_get(key: str) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _candle_cache_lock:
        entry = _candle_cache.get(key)
        if entry is None:
            return None
        cached_at, payload = entry
        if now - cached_at < _CANDLE_CACHE_TTL:
            return payload
        _candle_cache.pop(key, None)
    return None


def _candle_cache_put(key: str, payload: Dict[str, Any]) -> None:
    now = time.time()
    with _candle_cache_lock:
        _candle_cache[key] = (now, payload)
        if len(_candle_cache) > _CANDLE_CACHE_MAX:
            expired = [k for k, (cached_at, _) in _candle_cache.items() if now - cached_at >= _CANDLE_CACHE_TTL]
            for k in expired:
                _candle_cache.pop(k, None)
            overflow = len(_candle_cache) - _CANDLE_CACHE_MAX
            if overflow > 0:
                oldest = sorted(_candle_cache.items(), key=lambda item: item[1][0])[:overflow]
                for k, _ in oldest:
                    _candle_cache.pop(k, None)


# ── Module-level singleton (lazy) ─────────────────────────────────────────────

_client: Optional[BinanceThClient] = None


def get_client() -> BinanceThClient:
    """Return a shared BinanceThClient instance."""
    global _client
    if _client is None:
        _client = BinanceThClient()
    return _client


def check_ip_change_on_startup() -> Optional[str]:
    """Startup public IP diagnostic helper.

    Binance.th does not lock API keys to source IPs by default, so we just
    log the current public IP for operator visibility and return it.
    """
    current_ip = get_public_ip()
    if current_ip:
        logger.info("Public IP: %s (Binance.th does not enforce IP allowlist by default)", current_ip)
    else:
        logger.debug("Public IP lookup unavailable — skipping startup IP log")
    return current_ip


# Legacy compatibility aliases while active tests/modules finish the Binance TH
# migration. New code should import the Binance* names above.
BitkubClient = BinanceThClient
BitkubAPIError = BinanceAPIError
FatalAuthException = BinanceAuthException
BITKUB = BINANCE
