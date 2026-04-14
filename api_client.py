"""
Bitkub API v3 Client.

Base URL: https://api.bitkub.com
Docs:     https://github.com/bitkub/bitkub-official-api-docs

Authentication (secure endpoints)
----------------------------------
All secure endpoints require three custom headers:
  X-BTK-APIKEY     – your API key
  X-BTK-TIMESTAMP  – current server time in milliseconds (from /api/v3/servertime)
  X-BTK-SIGN       – HMAC-SHA256 signature (hex) of:
                      {timestamp}{method}{path}{query}{json_body}

Signature string examples:
  GET  → "1699381086593GET/api/v3/market/my-open-orders?sym=BTC_THB"
  POST → "1699376552354POST/api/v3/market/place-bid{\"sym\":\"thb_btc\",\"amt\":1000,\"rat\":10,\"typ\":\"limit\"}"
"""

import hmac
import json
import time
import hashlib
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from config import BITKUB, TRADING

logger = logging.getLogger(__name__)

# ── Public IP Detection ──────────────────────────────────────────────────────
_IP_SERVICES = [
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me/ip",
]
_LAST_KNOWN_IP_FILE = Path(__file__).resolve().parent / ".last_known_ip"


def get_public_ip(timeout: float = 5) -> Optional[str]:
    """Fetch this machine's current public IP (best-effort)."""
    for url in _IP_SERVICES:
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                ip = resp.text.strip()
                if ip:
                    return ip
        except Exception:
            continue
    return None


def check_ip_change_on_startup() -> Optional[str]:
    """Compare current IP with last-known. Returns current IP or None on failure.

    Logs a WARNING if the IP changed — the user likely needs to update
    the Bitkub API allowlist.
    """
    current_ip = get_public_ip()
    if not current_ip:
        logger.warning("Could not determine public IP — Bitkub allowlist cannot be verified")
        return None

    previous_ip: Optional[str] = None
    try:
        if _LAST_KNOWN_IP_FILE.exists():
            previous_ip = _LAST_KNOWN_IP_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        pass

    if previous_ip and previous_ip != current_ip:
        logger.warning(
            "⚠️ Public IP changed: %s → %s — update Bitkub API allowlist!",
            previous_ip,
            current_ip,
        )
    else:
        logger.info("Public IP: %s", current_ip)

    try:
        _LAST_KNOWN_IP_FILE.write_text(current_ip, encoding="utf-8")
    except Exception:
        pass

    return current_ip

BITKUB_ERROR_MESSAGES = {
    1: "Invalid JSON payload",
    2: "Missing X-BTK-APIKEY",
    3: "Invalid API key",
    4: "API pending for activation",
    5: "IP not allowed",
    6: "Missing or invalid signature",
    7: "Missing timestamp",
    8: "Invalid timestamp",
    9: "Invalid user",
    10: "Invalid parameter",
    11: "Invalid symbol",
    12: "Invalid amount",
    15: "Amount too low",
    16: "Failed to get balance",
    17: "Wallet is empty",
    18: "Insufficient balance",
    21: "Invalid order for cancellation",
    24: "Invalid order for lookup",
    25: "KYC level 1 is required to proceed",
    52: "Invalid permission",
    90: "Server error",
}


def _bitkub_error_message(code: Any, fallback: Optional[str] = None) -> str:
    try:
        normalized = int(code)
    except (TypeError, ValueError):
        normalized = code

    mapped = BITKUB_ERROR_MESSAGES.get(normalized)
    if mapped:
        return mapped

    fallback_text = str(fallback or "").strip()
    return fallback_text or "Unknown error"

# ── Global Shutdown Flag ─────────────────────────────────────────────────────
# Set to True when a fatal, non-recoverable error occurs (e.g. invalid API
# credentials, Bitkub Error 5). The main trading loop checks this flag and
# exits gracefully instead of spam-retrying the exchange.
SHOULD_SHUTDOWN: bool = False
SHUTDOWN_REASON: str = ""


def _normalize_market_symbol(symbol: str) -> str:
    """Normalize internal symbols like THB_BTC to Bitkub's btc_thb format."""
    sym_raw = (symbol or "").upper()
    if '_' in sym_raw:
        parts = sym_raw.split('_')
        if len(parts) == 2:
            return f"{parts[1].lower()}_{parts[0].lower()}"
    return sym_raw.lower()


def _normalize_tradingview_symbol(symbol: str) -> str:
    """Normalize internal symbols like THB_BTC to TradingView's BTC_THB format."""
    sym_raw = (symbol or "").upper().strip()
    if '_' not in sym_raw:
        return sym_raw

    parts = sym_raw.split('_')
    if len(parts) != 2:
        return sym_raw

    quote_assets = {"THB", "USDT", "USD"}
    left, right = parts
    if left in quote_assets and right not in quote_assets:
        return f"{right}_{left}"
    return sym_raw


def _resolution_to_seconds(resolution: str) -> int:
    normalized = str(resolution or "60").strip().upper()
    mapping = {
        "1": 60,
        "1M": 60,
        "5": 300,
        "5M": 300,
        "15": 900,
        "15M": 900,
        "30": 1800,
        "30M": 1800,
        "60": 3600,
        "1H": 3600,
        "240": 14400,
        "4H": 14400,
        "1D": 86400,
        "D": 86400,
        "1W": 604800,
        "W": 604800,
    }
    return mapping.get(normalized, 3600)


def _normalize_tradingview_candles(payload: Any) -> List[List[float]]:
    """Convert TradingView history payload into list rows [t, o, h, l, c, v]."""
    if not payload or not isinstance(payload, dict):
        return []

    timestamps = payload.get("t") or []
    opens = payload.get("o") or []
    highs = payload.get("h") or []
    lows = payload.get("l") or []
    closes = payload.get("c") or []
    volumes = payload.get("v") or []

    return [
        [timestamp, open_price, high_price, low_price, close_price, volume]
        for timestamp, open_price, high_price, low_price, close_price, volume in zip(
            timestamps,
            opens,
            highs,
            lows,
            closes,
            volumes,
        )
    ]


# ── Circuit Breaker ──────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    Circuit breaker to stop trading after consecutive API errors.
    
    States:
      CLOSED   → Normal operation, requests pass through
      OPEN     → Circuit is tripped, requests are blocked
      HALF     → Testing if the API has recovered
    """
    
    CLOSED = "closed"
    OPEN   = "open"
    HALF   = "half"
    
    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
        half_max_calls: int = 2,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout  # seconds before trying again
        self.half_max_calls    = half_max_calls
        
        self._state         = self.CLOSED
        self._failure_count  = 0
        self._success_count  = 0
        self._last_failure   = 0.0
        self._half_calls     = 0
        self._lock           = threading.RLock()
    
    @property
    def state(self) -> str:
        with self._lock:
            return self._state
    
    def is_available(self) -> bool:
        """Return True if requests are allowed to pass."""
        with self._lock:
            now = time.time()
            
            if self._state == self.CLOSED:
                return True
            
            if self._state == self.OPEN:
                if now - self._last_failure >= self.recovery_timeout:
                    self._state    = self.HALF
                    self._half_calls = 0
                    logger.warning("CircuitBreaker: OPEN → HALF (testing recovery)")
                    return True
                return False
            
            # HALF: allow up to half_max_calls through
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
            self._last_failure  = time.time()
            
            if self._state == self.HALF:
                self._state = self.OPEN
                logger.warning(f"CircuitBreaker: HALF → OPEN (still failing)")
            elif self._failure_count >= self.failure_threshold:
                self._state = self.OPEN
                logger.warning(
                    f"CircuitBreaker: CLOSED → OPEN "
                    f"({self.failure_threshold} consecutive failures)"
                )
            
            if error_msg:
                logger.error(f"CircuitBreaker failure #{self._failure_count}: {error_msg}")
    
    def reset(self):
        with self._lock:
            self._state        = self.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._half_calls   = 0


# ── Clock Sync ────────────────────────────────────────────────────────────────

class ClockSync:
    """
    Tracks offset between local clock and Bitkub server clock.
    Raises a warning if the offset exceeds a threshold.
    """
    
    def __init__(self, max_offset: float = 30.0):
        self.max_offset    = max_offset  # seconds
        self._offset        = 0.0
        self._last_sync     = 0.0
        self._sync_interval = 300.0       # re-sync every 5 minutes
        self._locked       = threading.Lock()
        self._alerted       = False       # only alert once per session
    
    @property
    def offset(self) -> float:
        with self._locked:
            return self._offset
    
    def sync(self, base_url: str) -> float:
        """
        Fetch server time and compute offset.
        Returns the offset in seconds.
        """
        try:
            r = requests.get(f"{base_url}/api/v3/servertime", timeout=10)
            r.raise_for_status()
            server_ts_ms = int(r.json())
            server_ts    = server_ts_ms / 1000.0
            local_ts     = time.time()
            
            offset = server_ts - local_ts
            
            with self._locked:
                self._offset    = offset
                self._last_sync = local_ts
            
            if abs(offset) > self.max_offset and not self._alerted:
                logger.warning(
                    f"ClockSync: LARGE OFFSET detected "
                    f"({offset:+.1f}s) — requests may be rejected"
                )
                self._alerted = True
            elif abs(offset) <= self.max_offset:
                self._alerted = False
            
            logger.debug(f"ClockSync: offset={offset:+.3f}s")
            return offset
            
        except Exception as e:
            logger.error(f"ClockSync: sync failed: {e}")
            return 0.0
    
    def is_synced(self) -> bool:
        """Return True if offset is within tolerance."""
        return abs(self._offset) <= self.max_offset
    
    def should_resync(self) -> bool:
        """Return True if it's time to re-sync."""
        return (time.time() - self._last_sync) >= self._sync_interval


# ── Exceptions ───────────────────────────────────────────────────────────────

class BitkubAPIError(Exception):
    """Raised when the Bitkub API returns an error code != 0."""

    def __init__(self, code: Any, message: str, raw: Optional[Dict] = None):
        self.code = code
        self.message = message
        self.raw = raw or {}
        super().__init__(f"[{code}] {message}")


class CircuitBreakerOpen(BitkubAPIError):
    """Raised when the circuit breaker is open and blocking requests."""
    pass


# ── BitkubClient ─────────────────────────────────────────────────────────────

class BitkubClient:
    """
    Thin wrapper around Bitkub REST API v3.

    Methods are grouped:
      - market  (public, no auth)
      - account (private, requires auth)
    """

    BASE_URL = BITKUB.base_url
    TIMEOUT  = 30  # seconds

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        symbol: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.api_key    = api_key    or BITKUB.api_key
        self.api_secret = api_secret or BITKUB.api_secret
        self.symbol     = symbol     or BITKUB.default_symbol
        self.base_url   = base_url   or self.BASE_URL
        
        # Rate limiting
        self._last_request_time: float = 0.0
        self._min_request_interval: float = 0.2  # seconds between requests
        
        # Circuit breaker
        self._cb = CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=60.0,
        )
        
        # Clock sync
        self._clock = ClockSync(max_offset=30.0)
        
        # Track consecutive errors in the main loop
        self._loop_error_count  = 0
        self._loop_error_reset  = 3.0   # reset after 3s of no errors
        self._last_error_time   = 0.0
        
        # FIX HIGH-02: Instance-level balance cache with proper TTL
        # Avoids creating new client instances with separate circuit breakers
        self._balances_cache: Optional[Dict[str, Dict[str, float]]] = None
        self._balances_cache_time: float = 0.0
        self._balances_cache_ttl: float = 5.0  # 5 second TTL - shorter for freshness

        # ── H1: Thread-safety lock ──────────────────────────────────────────
        # Protects _last_request_time (rate limiter) and balance cache
        # reads/writes.  The lock is held only during state mutation — never
        # during blocking I/O — so it cannot cause deadlocks.
        self._state_lock = threading.Lock()

        # Startup-only suppression hook: lets callers absorb auth error 5
        # without emitting fatal shutdown signals when they intentionally
        # degrade into a safe public-only mode.
        self._fatal_auth_suppression_context: str = ""

    # ── Circuit Breaker helpers ─────────────────────────────────────────────

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
        """Temporarily downgrade auth error 5 from fatal shutdown to caller-managed warning."""
        previous = self._fatal_auth_suppression_context
        self._fatal_auth_suppression_context = str(context or "startup")
        try:
            yield self
        finally:
            self._fatal_auth_suppression_context = previous

    # ── Clock Sync ─────────────────────────────────────────────────────────

    def sync_clock(self) -> float:
        """Force-sync the local clock with the Bitkub server."""
        return self._clock.sync(self.base_url)

    def check_clock_sync(self) -> bool:
        """
        Ensure clock is synced before auth requests.
        Returns True if synced; logs warning and returns False if not.
        """
        # Re-sync if needed
        if self._clock.should_resync():
            self.sync_clock()
        
        if not self._clock.is_synced():
            logger.warning(
                f"Clock offset {self._clock.offset:+.1f}s exceeds "
                f"threshold {self._clock.max_offset}s — requests may fail"
            )
            return False
        return True

    # ── Internal helpers ────────────────────────────────────────────────────

    def _sign(self, timestamp: int, method: str, path: str,
              query: str, body: str) -> str:
        """
        Generate HMAC-SHA256 signature.
        String to sign: {timestamp}{method}{path}{query}{body}
        """
        payload = f"{timestamp}{method}{path}{query}{body}"
        return hmac.new(
            self.api_secret.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _get_server_time(self) -> int:
        """
        Get current server timestamp in milliseconds.
        Uses cached offset if recent; otherwise fetches fresh server time.
        """
        now = time.time()
        if (self._clock.offset != 0.0 
                and (now - self._clock._last_sync) < 60):
            return int((now + self._clock.offset) * 1000)
        
        self._clock.sync(self.base_url)
        return int((time.time() + self._clock.offset) * 1000)

    def _unwrap_response_payload(self, data: Any, endpoint: str) -> Any:
        """Normalize Bitkub V3 and V4 payloads or raise BitkubAPIError."""
        if not isinstance(data, dict):
            return data

        if "error" in data:
            err_code = data.get("error", 0)
            if err_code != 0:
                msg = _bitkub_error_message(err_code, data.get("message"))
                raise BitkubAPIError(err_code, msg, raw=data)
            return data.get("result", data)

        if "code" in data:
            err_code = str(data.get("code", "0") or "0")
            if err_code != "0":
                msg = _bitkub_error_message(err_code, data.get("message"))
                raise BitkubAPIError(err_code, msg, raw=data)
            return data.get("data", data)

        return data

    def _request_aux(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        query_params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Authenticated helper for non-trading endpoints.

        This bypasses circuit-breaker/fatal-shutdown side effects so auxiliary
        history lookups cannot pause trading on their own.
        """
        if not self.check_clock_sync():
            raise BitkubAPIError(
                -998,
                f"Clock offset {self._clock.offset:+.1f}s exceeds limit — refusing auxiliary authenticated request",
            )

        url = f"{self.base_url}{endpoint}"
        body = ""
        query = ""

        # ── H1: Thread-safe rate limiting (aux path) ──────────────────────
        with self._state_lock:
            now = time.time()
            elapsed = now - self._last_request_time
            sleep_for = max(0.0, self._min_request_interval - elapsed)
            self._last_request_time = now + sleep_for
        if sleep_for:
            time.sleep(sleep_for)

        if params:
            body = json.dumps(params, separators=(",", ":"))

        if query_params:
            qs = "&".join(f"{k}={v}" for k, v in query_params.items() if v is not None)
            query = f"?{qs}" if qs else ""
            if query:
                url = f"{url}{query}"

        ts = self._get_server_time()
        sign = self._sign(ts, method, endpoint, query, body)
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-BTK-APIKEY": self.api_key,
            "X-BTK-TIMESTAMP": str(ts),
            "X-BTK-SIGN": sign,
        }

        response = requests.request(
            method,
            url,
            headers=headers,
            data=body if body else None,
            timeout=self.TIMEOUT if timeout is None else timeout,
        )
        if response.status_code >= 400:
            logger.warning(
                "Auxiliary Bitkub HTTP %s from %s — Raw response: %s",
                response.status_code,
                endpoint,
                response.text[:500],
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise BitkubAPIError(-1, f"Non-JSON response ({response.status_code}): {response.text[:300]}") from exc

        return self._unwrap_response_payload(payload, endpoint)

    def _request(
        self,
        method: str,
        endpoint: str,
        authenticated: bool = False,
        params: Optional[Dict[str, Any]] = None,
        query_params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Core request wrapper with circuit breaker and clock sync.

        Args:
            method:        HTTP method (GET / POST)
            endpoint:      API path (e.g. /api/v3/market/ticker)
            authenticated: Include auth headers
            params:        POST body (dict, will be JSON-encoded)
            query_params:  URL query string params
        """
        # ── Circuit Breaker gate ────────────────────────────────────────────
        if not self._cb.is_available():
            raise CircuitBreakerOpen(
                -999,
                "Circuit breaker is OPEN — too many consecutive errors. "
                "Trading paused. Will retry after cooldown."
            )
        
        # ── Clock sync check for authenticated requests ─────────────────────
        if authenticated:
            if not self.check_clock_sync():
                raise BitkubAPIError(
                    -998,
                    f"Clock offset {self._clock.offset:+.1f}s exceeds "
                    f"limit — refusing authenticated request"
                )
        
        url  = f"{self.base_url}{endpoint}"

        # ── H1: Thread-safe rate limiting ──────────────────────────────────
        # Atomically claim a request slot and compute how long to sleep.
        # The sleep itself happens *outside* the lock so other threads are
        # not blocked while this thread waits for its slot.
        with self._state_lock:
            now = time.time()
            elapsed = now - self._last_request_time
            sleep_for = max(0.0, self._min_request_interval - elapsed)
            # Pre-advance the timestamp to reserve the slot for this thread
            self._last_request_time = now + sleep_for
        if sleep_for:
            time.sleep(sleep_for)

        body  = ""
        query = ""

        if params:
            body = json.dumps(params, separators=(",", ":"))

        if query_params:
            qs = "&".join(f"{k}={v}" for k, v in query_params.items())
            query = f"?{qs}"
            url   = f"{url}{query}"

        headers: Dict[str, str] = {"Accept": "application/json"}
        if authenticated:
            ts   = self._get_server_time()
            sign = self._sign(ts, method, endpoint, query, body)
            headers.update({
                "Content-Type":    "application/json",
                "X-BTK-APIKEY":    self.api_key,
                "X-BTK-TIMESTAMP": str(ts),
                "X-BTK-SIGN":      sign,
            })

        try:
            r = requests.request(
                method,
                url,
                headers=headers,
                data=body if body else None,
                timeout=self.TIMEOUT if timeout is None else timeout,
            )
            
            # Log raw body on non-200 BEFORE parsing, so we see the exact Bitkub error
            if r.status_code >= 400:
                is_suppressed_auth_probe = (
                    authenticated
                    and r.status_code == 401
                    and bool(self._fatal_auth_suppression_context)
                )
                log_method = logger.warning if is_suppressed_auth_probe else logger.error
                suffix = (
                    f" during {self._fatal_auth_suppression_context}"
                    if is_suppressed_auth_probe else ""
                )
                log_method(
                    f"HTTP {r.status_code} from {endpoint}{suffix} — "
                    f"Raw Bitkub response: {r.text[:500]}"
                )

            # Parse response
            try:
                data = r.json()
            except ValueError:
                raise BitkubAPIError(-1, f"Non-JSON response ({r.status_code}): {r.text[:300]}")

            # Success
            result = self._unwrap_response_payload(data, endpoint)
            self._cb.record_success()
            return result
            
        except BitkubAPIError as e:
            # ── Error Classification ───────────────────────────────────
            # Some Bitkub errors are NOT infrastructure failures and must
            # NOT trip the circuit breaker:
            #
            #   5  = Unauthorized (invalid API key) → FATAL, shutdown
            #  11  = Invalid order for history lookup → stale order, harmless
            #  15  = Invalid amount (e.g. 0 qty)     → bad request, not infra
            #  21  = Invalid order for cancellation  → already filled/gone
            #  24  = Order not found (order-info)    → stale order, harmless
            #
            # These "order-not-found" errors happen when reconciliation
            # tries to look up old orders that no longer exist on Bitkub.
            # Counting them toward the circuit breaker would cause a chain
            # reaction that blocks ALL trading for 60s on every restart.

            # Error 5 = IP not allowed — treated as fatal/private API unavailable
            if e.code == 5:
                if self._fatal_auth_suppression_context:
                    logger.warning(
                        "Bitkub auth error 5 on %s during %s — caller will downgrade to degraded mode; fatal shutdown side effects suppressed",
                        endpoint,
                        self._fatal_auth_suppression_context,
                    )
                    raise

                global SHOULD_SHUTDOWN, SHUTDOWN_REASON
                current_ip = get_public_ip() or "unknown"
                logger.critical(
                    "🚨 FATAL: Bitkub Auth Error 5 — IP not allowed. "
                    "Current IP: %s | Message: %s | Raw: %s",
                    current_ip, e.message, e.raw,
                )
                # Send immediate Telegram alert
                try:
                    import os as _os
                    _bot_token = _os.environ.get("TELEGRAM_BOT_TOKEN", "")
                    _chat_id = _os.environ.get("TELEGRAM_CHAT_ID", "")
                    if _bot_token and _chat_id:
                        from alerts import send_error_token
                        send_error_token(
                            _bot_token, _chat_id,
                            title="FATAL: Bitkub Auth Error 5",
                            details=(
                                f"Bitkub rejected the request — IP not allowed.\n"
                                f"🌐 Current IP: {current_ip}\n"
                                f"Add this IP to the Bitkub API allowlist:\n"
                                f"https://www.bitkub.com/publicapi\n"
                                f"Then restart the bot."
                            ),
                            status="SHUTDOWN",
                        )
                        logger.info("Telegram shutdown alert sent successfully")
                except Exception as tg_err:
                    logger.error(f"Failed to send Telegram shutdown alert: {tg_err}")

                # Record failure in circuit breaker (prevents further API spam)
                self._cb.record_failure(f"FATAL Auth Error 5 on {endpoint}")

                # Set global shutdown flag
                SHOULD_SHUTDOWN = True
                SHUTDOWN_REASON = (
                    f"🚨 FATAL: Bitkub Auth Error 5. "
                    f"IP not allowed (current IP: {current_ip}). "
                    f"Add this IP to the Bitkub API allowlist and restart."
                )

            # Errors 11, 15, 21, 24 = stale/missing order or bad request — NOT infra
            elif e.code in (11, 15, 21, 24):
                if e.code == 21 and "cancel-order" in str(endpoint):
                    logger.info(
                        f"Expected stale cancel {e.code} on {endpoint}: {e.message} "
                        f"(order already filled or gone)"
                    )
                else:
                    logger.warning(
                        f"⚠️ Stale order error {e.code} on {endpoint}: {e.message} "
                        f"(order no longer exists on Bitkub — circuit breaker NOT affected)"
                    )
            else:
                self._cb.record_failure(str(endpoint))
            raise
        except Exception as e:
            self._cb.record_failure(str(e))
            raise BitkubAPIError(-1, f"Request failed: {e}")

    # ── Market data (public) ────────────────────────────────────────────────

    def get_ticker(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """
        GET /api/v3/market/ticker?sym={symbol}

        Returns ticker info for the given symbol.
        Response: list of dicts (one per symbol) or a single dict.

        Note: Bitkub API v3 expects 'sym' in BASE_QUOTE format (e.g. 'btc_thb').
        Ticker is a PUBLIC endpoint - no authentication needed.
        """
        sym = _normalize_market_symbol(symbol or self.symbol)
        
        url = f"{self.base_url}/api/v3/market/ticker"
        params = {"sym": sym}
        
        try:
            r = requests.get(url, params=params, timeout=self.TIMEOUT)
            result = r.json()
        except Exception as e:
            raise BitkubAPIError(-1, f"Ticker request failed: {e}")
        
        if isinstance(result, list):
            result = [t for t in result if t.get("symbol", "").upper() == sym.upper()]
            if not result:
                raise BitkubAPIError(-1, f"Symbol '{sym}' not found in ticker response")
            return result[0]
        return result

    def get_tickers_batch(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Get multiple tickers in a single API call (more efficient than individual calls).

        Args:
            symbols: List of trading pairs in THB_BASE format (e.g., ['THB_BTC', 'THB_ETH'])

        Returns:
            Dict mapping pair name -> ticker dict
        """
        url = f"{self.base_url}/api/v3/market/ticker"
        try:
            r = requests.get(url, timeout=self.TIMEOUT)
            all_tickers = r.json()
        except Exception as e:
            raise BitkubAPIError(-1, f"Batch ticker request failed: {e}")

        if not isinstance(all_tickers, list):
            return {}

        results = {}
        for sym_raw in symbols:
            sym_upper = sym_raw.upper()
            parts = sym_raw.split('_')
            if len(parts) == 2:
                api_sym = f"{parts[1].upper()}_{parts[0].upper()}"  # BASE_QUOTE
            else:
                api_sym = sym_upper

            for ticker in all_tickers:
                if ticker.get('symbol', '') == api_sym:
                    results[sym_raw] = ticker
                    break

        return results

    def get_candle(self, symbol: str, timeframe: str = "1h", limit: int = 250) -> Dict[str, Any]:
        """
        GET /tradingview/history
        Returns TradingView-style candlestick data.

        Args:
            symbol: Trading pair (e.g. 'BTC_THB')
            timeframe: '1', '5', '15', '60', '240', '1D' or friendly values like '1h'
            limit: Number of candles (max 300)

        Returns:
            Dict with error=0 and result=[[t, o, h, l, c, v], ...]
        Cached: 60 second TTL.
        """
        # TradingView resolution: 1, 5, 15, 60, 240, 1D, etc.
        tf_map = {"1m": "1", "5m": "5", "15m": "15", "30m": "30",
                  "1h": "60", "4h": "240", "1d": "1D", "1w": "1W",
                  "1": "1", "5": "5", "15": "15", "30": "30",
                  "60": "60", "240": "240", "1440": "1D", "1D": "1D", "1W": "1W"}
        resolution = tf_map.get(str(timeframe), str(timeframe or "60"))
        sym = _normalize_tradingview_symbol(symbol)
        url = f"{self.base_url}/tradingview/history"
        window_seconds = max(limit, 1) * _resolution_to_seconds(resolution)
        to_time = int(time.time())
        from_time = max(to_time - window_seconds, 0)
        payload = self._get_candle_cached(sym, resolution, from_time, to_time, url)
        if isinstance(payload, dict) and "error" in payload and payload.get("error") not in (0, None):
            return payload

        candles = _normalize_tradingview_candles(payload if isinstance(payload, dict) else {})
        return {
            "error": 0,
            "result": candles,
            "status": payload.get("s") if isinstance(payload, dict) else None,
        }

    # TTL cache for candles - replaces lru_cache with proper time-based expiration
    _candle_cache: Dict[str, tuple] = {}  # key -> (timestamp, result)
    _candle_cache_ttl: float = 60.0  # 60 seconds TTL
    _candle_cache_max_size: int = 200  # Max entries before cleanup
    _candle_cache_lock = threading.Lock()

    @staticmethod
    def _prune_candle_cache(now: int) -> None:
        expired_keys = [
            key
            for key, (cached_at, _) in BitkubClient._candle_cache.items()
            if now - cached_at >= BitkubClient._candle_cache_ttl
        ]
        for key in expired_keys:
            BitkubClient._candle_cache.pop(key, None)

        overflow = len(BitkubClient._candle_cache) - BitkubClient._candle_cache_max_size
        if overflow > 0:
            oldest_keys = [
                key
                for key, _ in sorted(
                    BitkubClient._candle_cache.items(),
                    key=lambda item: item[1][0],
                )[:overflow]
            ]
            for key in oldest_keys:
                BitkubClient._candle_cache.pop(key, None)

    @staticmethod
    def _get_candle_cached(symbol: str, resolution: str, from_time: int, to_time: int, url: str) -> Dict[str, Any]:
        """Cached candle lookup - TTL 60 seconds via custom cache.
        
        FIX: Include from/to in cache key to prevent stale data.
        """
        now = int(time.time())
        
        # Create cache key from stable parameters INCLUDING from/to
        cache_key = f"{symbol}:{resolution}:{from_time}:{to_time}"
        
        # Check cache (thread-safe)
        with BitkubClient._candle_cache_lock:
            BitkubClient._prune_candle_cache(now)
            if cache_key in BitkubClient._candle_cache:
                cached_time, cached_result = BitkubClient._candle_cache[cache_key]
                if now - cached_time < BitkubClient._candle_cache_ttl:
                    return cached_result
        
        # Fetch fresh data
        try:
            r = requests.get(url, params={"symbol": symbol, "resolution": resolution,
                                           "from": from_time,
                                           "to": to_time},
                              timeout=30)
            result = r.json()
            # Cache the result (thread-safe) + prune stale/overflow entries
            with BitkubClient._candle_cache_lock:
                BitkubClient._candle_cache[cache_key] = (now, result)
                BitkubClient._prune_candle_cache(now)
            return result
        except Exception as e:
            return {"error": 1, "message": str(e)}

    def get_symbols(self) -> List[Dict[str, Any]]:
        """GET /api/v3/market/symbols — list all available trading pairs."""
        return self._request("GET", "/api/v3/market/symbols")

    def get_depth(
        self,
        symbol: Optional[str] = None,
        limit: int = 25,
    ) -> Dict[str, List]:
        """
        GET /api/v3/market/depth?sym={symbol}&lmt={limit}

        Returns order book with 'asks' and 'bids'.
        """
        sym = (symbol or self.symbol).lower()
        return self._request(
            "GET",
            "/api/v3/market/depth",
            query_params={"sym": sym, "lmt": limit},
        )

    def get_bids(
        self,
        symbol: Optional[str] = None,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """GET /api/v3/market/bids — open buy orders."""
        sym = (symbol or self.symbol).lower()
        data = self._request(
            "GET", "/api/v3/market/bids",
            query_params={"sym": sym, "lmt": limit},
        )
        return data.get("result", [])

    def get_asks(
        self,
        symbol: Optional[str] = None,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """GET /api/v3/market/asks — open sell orders."""
        sym = (symbol or self.symbol).lower()
        data = self._request(
            "GET", "/api/v3/market/asks",
            query_params={"sym": sym, "lmt": limit},
        )
        return data.get("result", [])

    def get_trades(
        self,
        symbol: Optional[str] = None,
        limit: int = 25,
    ) -> List[List[Any]]:
        """GET /api/v3/market/trades — recent trades."""
        sym = (symbol or self.symbol).lower()
        data = self._request(
            "GET", "/api/v3/market/trades",
            query_params={"sym": sym, "lmt": limit},
        )
        return data.get("result", [])

    # ── Account (authenticated) ─────────────────────────────────────────────

    def get_wallet(self) -> Dict[str, float]:
        """
        POST /api/v3/market/wallet
        Returns available balances (not reserved).

        Response: { "THB": 188379.27, "BTC": 8.90397323, ... }
        """
        return self._request("POST", "/api/v3/market/wallet", authenticated=True)

    def get_balances(
        self,
        *,
        force_refresh: bool = False,
        allow_stale: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Dict[str, float]]:
        """
        POST /api/v3/market/balances
        Returns available + reserved balances.

        Response:
          { "THB": { "available": ..., "reserved": ... }, ... }

        FIX HIGH-02: Instance-level cache with short TTL to avoid stale balances
        and prevent creating new client instances with separate circuit breakers.
        """
        # ── H1: Thread-safe cache read ─────────────────────────────────────
        # Check inside the lock so two threads cannot both see a stale cache
        # and both fire redundant API calls.
        with self._state_lock:
            if (not force_refresh
                    and self._balances_cache is not None
                    and time.time() - self._balances_cache_time < self._balances_cache_ttl):
                return self._balances_cache

        # Fetch *outside* the lock — blocking I/O must not hold _state_lock.
        try:
            fresh = self._request(
                "POST", "/api/v3/market/balances", authenticated=True, timeout=timeout
            )
            with self._state_lock:
                self._balances_cache = fresh
                self._balances_cache_time = time.time()
            return fresh
        except Exception:
            # On error, return stale cache if available (better than nothing)
            with self._state_lock:
                if allow_stale and self._balances_cache is not None:
                    logger.warning("[Balance] Using stale cache due to API error")
                    return self._balances_cache
            raise

    def get_balances_fresh(self) -> Dict[str, Dict[str, float]]:
        """Fetch balances without cache reuse or stale-cache fallback."""
        return self.get_balances(force_refresh=True, allow_stale=False)

    def get_balance(self) -> Dict[str, float]:
        """
        POST /api/v3/market/wallet
        Returns available balances (not reserved).
        Simplified version for trading bot compatibility.

        Returns:
            { "THB": 1000.0, "BTC": 0.5, ... }
        """
        return self._request("POST", "/api/v3/market/wallet", authenticated=True)

    def get_open_orders(
        self,
        symbol: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        GET /api/v3/market/my-open-orders?sym={symbol}
        List all open orders for the given symbol.
        """
        requested_symbol = str(symbol or self.symbol).upper()
        sym = _normalize_market_symbol(symbol or self.symbol)
        
        data = self._request(
            "GET",
            "/api/v3/market/my-open-orders",
            authenticated=True,
            query_params={"sym": sym},
        )
        rows = data if isinstance(data, list) else (data.get("result", []))
        normalized_rows = []
        for row in rows:
            if isinstance(row, dict):
                row_copy = dict(row)
                row_copy.setdefault("_checked_symbol", requested_symbol)
                normalized_rows.append(row_copy)
            else:
                normalized_rows.append(row)
        return normalized_rows

    def place_bid(
        self,
        symbol: str,
        amount: float,
        rate: float,
        order_type: str = "limit",
        client_id: Optional[str] = None,
        post_only: bool = False,
    ) -> Dict[str, Any]:
        """
        POST /api/v3/market/place-bid — place a BUY (bid) order.

        Args:
            symbol:     Trading pair, lowercase with underscore (e.g. "btc_thb")
            amount:     Amount to spend (THB side for bid)
            rate:       Price per unit; use 0 for market orders
            order_type: "limit" or "market"
            client_id:  Optional client reference string
            post_only:  If True, order only matches as maker

        Returns:
            { "id", "typ", "amt", "rat", "fee", "cre", "rec", "ts", "ci" }
        """
        # Amount and rate must have NO trailing zeros
        # Convert THB_BTC -> btc_thb format for Bitkub API
        sym_lower = symbol.lower()
        if '_' in sym_lower:
            parts = sym_lower.split('_')
            if len(parts) == 2:
                sym_formatted = f"{parts[1]}_{parts[0]}"  # BASE_QUOTE
            else:
                sym_formatted = sym_lower
        else:
            sym_formatted = sym_lower
        
        body: Dict[str, Any] = {
            "sym": sym_formatted,
            "amt": _no_trailing_zeros(amount),
            "rat": _no_trailing_zeros(rate),
            "typ": order_type,
        }
        if client_id:
            body["client_id"] = client_id
        if post_only:
            body["post_only"] = True

        return self._request(
            "POST",
            "/api/v3/market/place-bid",
            authenticated=True,
            params=body,
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
        """
        POST /api/v3/market/place-ask — place a SELL (ask) order.

        Args:
            symbol:     Trading pair, lowercase with underscore (e.g. "btc_thb")
            amount:     Quantity of base asset to sell (e.g. BTC amount)
            rate:       Price per unit; use 0 for market orders
            order_type: "limit" or "market"
            client_id:  Optional client reference string
            post_only:  If True, order only matches as maker

        Returns:
            { "id", "typ", "amt", "rat", "fee", "cre", "rec", "ts", "ci" }
        """
        # Convert THB_BTC -> btc_thb format for Bitkub API
        sym_lower = symbol.lower()
        if '_' in sym_lower:
            parts = sym_lower.split('_')
            if len(parts) == 2:
                sym_formatted = f"{parts[1]}_{parts[0]}"  # BASE_QUOTE
            else:
                sym_formatted = sym_lower
        else:
            sym_formatted = sym_lower
        
        body: Dict[str, Any] = {
            "sym": sym_formatted,
            "amt": _no_trailing_zeros(amount),
            "rat": _no_trailing_zeros(rate),
            "typ": order_type,
        }
        if client_id:
            body["client_id"] = client_id
        if post_only:
            body["post_only"] = True

        return self._request(
            "POST",
            "/api/v3/market/place-ask",
            authenticated=True,
            params=body,
        )

    def cancel_order(
        self,
        symbol: str,
        order_id: str,
        side: str,
    ) -> Dict[str, Any]:
        """
        POST /api/v3/market/cancel-order — cancel an open order.

        Args:
            symbol:   Trading pair (e.g. "THB_BTC" or "btc_thb")
            order_id: Order ID to cancel
            side:     "buy" or "sell"
        """
        return self._request(
            "POST",
            "/api/v3/market/cancel-order",
            authenticated=True,
            params={
                "sym": _normalize_market_symbol(symbol),
                "id":  str(order_id),
                "sd":  side.lower(),
            },
        )

    def get_order_info(
        self,
        symbol: str,
        order_id: str,
        side: str = "",
    ) -> Dict[str, Any]:
        """
        GET /api/v3/market/order-info?sym={symbol}&id={order_id}&sd={side}

        Bitkub v3 requires all three params: sym, id, AND sd (buy/sell).
        Missing 'sd' causes a 400 error and trips the circuit breaker.
        """
        sym = _normalize_market_symbol(symbol)

        qp: Dict[str, Any] = {"sym": sym, "id": str(order_id)}
        if side:
            qp["sd"] = side.lower()

        return self._request(
            "GET",
            "/api/v3/market/order-info",
            authenticated=True,
            query_params=qp,
        )

    def get_order_history(
        self,
        symbol: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """GET /api/v3/market/my-order-history?sym={symbol}&lmt={limit}"""
        sym = _normalize_market_symbol(symbol)
        data = self._request(
            "GET",
            "/api/v3/market/my-order-history",
            authenticated=True,
            query_params={"sym": sym, "lmt": limit},
        )
        return data if isinstance(data, list) else (data.get("result", []))

    def get_fiat_deposit_history(
        self,
        page: int = 1,
        limit: int = 50,
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """POST /api/v3/fiat/deposit-history — fiat deposit history."""
        data = self._request_aux(
            "POST",
            "/api/v3/fiat/deposit-history",
            query_params={"p": page, "lmt": limit},
            timeout=timeout,
        )
        return data if isinstance(data, list) else []

    def get_fiat_withdraw_history(
        self,
        page: int = 1,
        limit: int = 50,
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """POST /api/v3/fiat/withdraw-history — fiat withdrawal history."""
        data = self._request_aux(
            "POST",
            "/api/v3/fiat/withdraw-history",
            query_params={"p": page, "lmt": limit},
            timeout=timeout,
        )
        return data if isinstance(data, list) else []

    def get_crypto_deposit_history(
        self,
        *,
        page: int = 1,
        limit: int = 100,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """GET /api/v4/crypto/deposits — crypto deposit history."""
        query_params: Dict[str, Any] = {"page": page, "limit": limit}
        if symbol:
            query_params["symbol"] = str(symbol).upper()
        if status:
            query_params["status"] = status
        data = self._request_aux(
            "GET",
            "/api/v4/crypto/deposits",
            query_params=query_params,
            timeout=timeout,
        )
        return data if isinstance(data, dict) else {"items": []}

    def get_crypto_withdraw_history(
        self,
        *,
        page: int = 1,
        limit: int = 100,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """GET /api/v4/crypto/withdraws — crypto withdrawal history."""
        query_params: Dict[str, Any] = {"page": page, "limit": limit}
        if symbol:
            query_params["symbol"] = str(symbol).upper()
        if status:
            query_params["status"] = status
        data = self._request_aux(
            "GET",
            "/api/v4/crypto/withdraws",
            query_params=query_params,
            timeout=timeout,
        )
        return data if isinstance(data, dict) else {"items": []}

    # ── Convenience helpers ─────────────────────────────────────────────────

    def is_authenticated(self) -> bool:
        """Check whether API credentials are configured."""
        return bool(self.api_key and self.api_secret
                    and self.api_key != "your_bitkub_api_key_here")

    def fmt_balance(self, asset: str = "THB") -> str:
        """Return a formatted balance string for an asset."""
        balances = self.get_balances()
        info = balances.get(asset.upper(), {"available": 0.0, "reserved": 0.0})
        av   = info.get("available", 0.0)
        rv   = info.get("reserved",  0.0)
        return f"{asset}: available={av:.8f}  reserved={rv:.8f}"

    def fmt_ticker(self, symbol: Optional[str] = None) -> str:
        """Return a human-readable ticker string."""
        tickers = self.get_ticker(symbol)
        if isinstance(tickers, list) and tickers:
            t = tickers[0]
        else:
            t = tickers
        sym = t.get("symbol", symbol or self.symbol)
        return (
            f"{sym} | last={t['last']} | "
            f"bid={t['highest_bid']} / ask={t['lowest_ask']} | "
            f"24h: {t['percent_change']}% (H:{t['high_24_hr']} L:{t['low_24_hr']})"
        )


# ── Utilities ────────────────────────────────────────────────────────────────

def _no_trailing_zeros(value: float) -> float:
    """
    Convert float to a number with no trailing zeros.
    Bitkub rejects trailing zeros (e.g. 1000.00 is invalid, 1000 is ok).
    """
    s = f"{value:.10f}".rstrip("0").rstrip(".")
    return float(s)


# ── Module-level singleton (lazy) ─────────────────────────────────────────────

_client: Optional[BitkubClient] = None


def get_client() -> BitkubClient:
    """Return a shared BitkubClient instance."""
    global _client
    if _client is None:
        _client = BitkubClient()
    return _client
