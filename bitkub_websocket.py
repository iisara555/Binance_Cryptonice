"""
Bitkub WebSocket API Client (Improved)
======================================
Handles real-time tick-by-tick market data streams.
Features:
- Automatic exponential backoff reconnection
- Ping/pong heartbeat for connection health
- Connection state machine for reliable state tracking
- Thread-safe operations
- Global price cache for fast access
"""

import json
import time
import logging
import threading
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Callable, Optional, Dict, Any

try:
    import websocket
except ImportError:
    logging.error("websocket-client not installed. Please run: pip install websocket-client")
    websocket = None

logger = logging.getLogger(__name__)


class ConnectionState(Enum):
    """WebSocket connection state machine."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


@dataclass
class PriceTick:
    """Represents a single price tick from the WebSocket."""
    symbol: str
    last: float
    bid: float
    ask: float
    percent_change_24h: float
    timestamp: float


# Thread-safe internal cache for fastest possible access by the trade executor
_price_cache: Dict[str, PriceTick] = {}
_cache_lock = threading.Lock()


def get_latest_ticker(symbol: str) -> Optional[PriceTick]:
    """Return the most recent price tick from the websocket cache (thread-safe)."""
    with _cache_lock:
        return _price_cache.get(symbol.upper())


class BitkubWebSocket:
    """
    Improved Bitkub WebSocket client with robust reconnection logic.
    
    Features:
    - Exponential backoff with jitter for reconnection
    - Ping/pong heartbeat to detect dead connections
    - Connection state machine for reliable status tracking
    - Circuit breaker to prevent excessive reconnection attempts
    - Thread-safe operations
    - Subscription renewal on reconnect
    """
    
    # Reconnection parameters
    INITIAL_RECONNECT_DELAY = 1.0       # seconds
    MAX_RECONNECT_DELAY = 60.0          # maximum delay (60 seconds)
    BACKOFF_MULTIPLIER = 2.0            # exponential backoff multiplier
    MAX_RECONNECT_ATTEMPTS = 10          # max retries before giving up
    HEARTBEAT_INTERVAL = 30.0            # ping every 30 seconds
    CONNECTION_TIMEOUT = 10.0            # connection establishment timeout
    
    def __init__(self, symbols: List[str], on_tick: Callable[[PriceTick], None]):
        """
        Initialize WebSocket client.
        
        Args:
            symbols: List of trading pair symbols (e.g., ['BTC_THB', 'ETH_THB'])
            on_tick: Callback function called on each price tick
        """
        self.symbols = [s.upper() for s in symbols]  # Normalize to uppercase
        self.on_tick = on_tick
        
        # Connection state
        self._state = ConnectionState.DISCONNECTED
        self._state_lock = threading.RLock()
        
        # WebSocket instance
        self.ws: Optional[Any] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._wakeup_event = threading.Event()
        
        # Reconnection logic
        self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
        self._reconnect_attempts = 0
        self._last_connection_time: float = 0.0
        self._last_pong_time: float = time.time()
        self._last_activity_time: float = 0.0
        
        # Circuit breaker
        self._consecutive_failures = 0
        self._circuit_open_time: float = 0.0
        self._circuit_breaker_timeout = 300.0  # 5 minutes before retry after circuit opens
        
        # Statistics
        self._stats = {
            'total_messages': 0,
            'reconnections': 0,
            'last_error': None,
            'uptime_start': None,
        }

    @property
    def state(self) -> ConnectionState:
        """Get current connection state (thread-safe)."""
        with self._state_lock:
            return self._state
    
    def _set_state(self, new_state: ConnectionState):
        """Set connection state (thread-safe)."""
        with self._state_lock:
            old_state = self._state
            self._state = new_state
            
            if old_state != new_state:
                logger.info(f"[WS] State transition: {old_state.value} -> {new_state.value}")
                
                # Track uptime
                if new_state == ConnectionState.CONNECTED:
                    self._stats['uptime_start'] = time.time()
                elif new_state == ConnectionState.DISCONNECTED and self._stats['uptime_start']:
                    uptime = time.time() - self._stats['uptime_start']
                    logger.info(f"[WS] Connection uptime: {uptime:.1f}s")

    def _get_stream_url(self) -> str:
        """Build Bitkub WebSocket URL with stream subscriptions."""
        streams = []
        for s in self.symbols:
            s_lower = s.lower()
            # Convert internal THB_BTC to Bitkub's WS format (btc_thb)
            if '_' in s_lower:
                parts = s_lower.split('_')
                if len(parts) == 2 and parts[0] == 'thb':
                    s_lower = f"{parts[1]}_{parts[0]}"
            streams.append(f"market.ticker.{s_lower}")
        stream_path = ",".join(streams)
        return f"wss://api.bitkub.com/websocket-api/{stream_path}"

    def start(self):
        """Start the WebSocket connection in a background thread."""
        if not websocket:
            logger.error("[WS] Cannot start: websocket-client package missing.")
            self._set_state(ConnectionState.FAILED)
            return

        if self._running:
            logger.warning("[WS] Already running")
            return

        self._running = True
        self._wakeup_event.clear()
        self._set_state(ConnectionState.CONNECTING)
        
        # Main connection loop thread
        self._thread = threading.Thread(target=self._run_forever, daemon=True, name="WS-Connector")
        self._thread.start()
        
        logger.info(f"[WS] Started - subscribing to: {self.symbols}")

    def _run_forever(self):
        """Main loop: connect, monitor, and reconnect as needed."""
        while self._running:
            # ── Circuit Breaker Check ────────────────────────────────────────
            if not self._enforce_circuit_breaker():
                if not self._running:
                    break
                continue
            
            # ── Connect ──────────────────────────────────────────────────────
            self._set_state(ConnectionState.CONNECTING)
            url = self._get_stream_url()
            
            logger.info(f"[WS] Connecting to: {url}")
            
            try:
                self.ws = websocket.WebSocketApp(
                    url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                    on_pong=self._on_pong,
                )
                
                # Run with ping/pong enabled
                self.ws.run_forever(
                    ping_interval=self.HEARTBEAT_INTERVAL,
                    ping_timeout=5.0,
                    ping_payload="ping",
                )
                
            except Exception as e:
                logger.error(f"[WS] Connection error: {e}")
                self._stats['last_error'] = str(e)
            
            # ── Handle Disconnection ──────────────────────────────────────────
            if not self._running:
                break
                
            if not self._handle_disconnection():
                break

    def _enforce_circuit_breaker(self) -> bool:
        """Return True when connection attempts are allowed to proceed."""
        if self._consecutive_failures < self.MAX_RECONNECT_ATTEMPTS:
            return True

        now = time.time()
        if self._circuit_open_time <= 0:
            self._circuit_open_time = now
            logger.warning(
                "[WS] Circuit breaker OPEN after %d failures; pausing reconnects",
                self._consecutive_failures,
            )

        elapsed = now - self._circuit_open_time
        remaining = self._circuit_breaker_timeout - elapsed
        if remaining > 0:
            logger.warning("[WS] Circuit breaker active; retry in %.1fs", remaining)
            self._wakeup_event.wait(min(remaining, self._circuit_breaker_timeout))
            return False

        # Reset breaker after cooldown and allow a fresh reconnect cycle.
        logger.info("[WS] Circuit breaker cooldown finished; reconnecting")
        self._consecutive_failures = 0
        self._circuit_open_time = 0.0
        self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
        return True

    def _handle_disconnection(self):
        """Handle reconnection after unexpected disconnection."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.MAX_RECONNECT_ATTEMPTS and self._circuit_open_time <= 0:
            self._circuit_open_time = time.time()
        self._set_state(ConnectionState.RECONNECTING)
        
        # Stop heartbeat thread if running
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=1.0)
        
        # Calculate delay with jitter (random 0.5x to 1.5x)
        import random
        jitter = random.uniform(0.5, 1.5)
        delay = min(self._reconnect_delay * jitter, self.MAX_RECONNECT_DELAY)
        
        logger.warning(
            f"[WS] Disconnected. Reconnecting in {delay:.1f}s "
            f"(attempt {self._reconnect_attempts + 1}/{self.MAX_RECONNECT_ATTEMPTS})"
        )
        
        if self._wakeup_event.wait(delay):
            return False
        
        # Exponential backoff
        self._reconnect_delay = min(
            self._reconnect_delay * self.BACKOFF_MULTIPLIER,
            self.MAX_RECONNECT_DELAY
        )
        self._reconnect_attempts += 1
        self._stats['reconnections'] += 1
        return True

    def _on_open(self, ws):
        """Called when WebSocket connection is established."""
        logger.info("[WS] Connected successfully")
        self._set_state(ConnectionState.CONNECTED)
        now = time.time()
        self._last_connection_time = now
        self._last_pong_time = now
        self._last_activity_time = now
        
        # Reset reconnection state
        self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
        self._reconnect_attempts = 0
        self._consecutive_failures = 0
        
        # Start heartbeat monitor thread
        self._start_heartbeat_monitor()

    def _on_pong(self, ws, payload):
        """Called when pong is received (connection is alive)."""
        now = time.time()
        self._last_pong_time = now
        self._last_activity_time = now
        logger.debug(f"[WS] Pong received - connection healthy")

    def _seconds_since_last_activity(self, now: Optional[float] = None) -> float:
        """Return age of the newest heartbeat or inbound message activity."""
        current_time = time.time() if now is None else now
        last_activity = max(self._last_pong_time, self._last_activity_time)
        if last_activity <= 0:
            return float('inf')
        return current_time - last_activity

    def _start_heartbeat_monitor(self):
        """Start background thread to monitor connection health."""
        def heartbeat_monitor():
            while self._running and self.state == ConnectionState.CONNECTED:
                time.sleep(5.0)  # Check every 5 seconds
                
                if self.state != ConnectionState.CONNECTED:
                    break
                
                # Treat inbound messages as activity too because some Bitkub streams
                # stay healthy while skipping explicit pong frames.
                time_since_activity = self._seconds_since_last_activity()
                if time_since_activity > self.HEARTBEAT_INTERVAL * 2:
                    logger.warning(
                        f"[WS] No pong or inbound message received for {time_since_activity:.1f}s - "
                        f"connection may be dead, reconnecting..."
                    )
                    if self.ws:
                        try:
                            self.ws.close()
                        except:
                            pass
                    break
        
        self._heartbeat_thread = threading.Thread(
            target=heartbeat_monitor,
            daemon=True,
            name="WS-Heartbeat"
        )
        self._heartbeat_thread.start()

    def _on_error(self, ws, error):
        """Called on WebSocket error."""
        logger.error(f"[WS] Error: {error}")
        self._stats['last_error'] = str(error)
        
        # Check for specific error types
        error_str = str(error).lower()
        if 'timeout' in error_str:
            logger.warning("[WS] Connection timeout - will reconnect")
        elif 'refused' in error_str or 'connection' in error_str:
            logger.warning("[WS] Connection refused - network issue")

    def _on_close(self, ws, close_status_code, close_msg):
        """Called when WebSocket connection is closed."""
        logger.info(f"[WS] Closed (status={close_status_code}, msg={close_msg})")
        
        if self._running:
            self._set_state(ConnectionState.RECONNECTING)

    def _on_message(self, ws, message):
        """Parse and handle incoming WebSocket messages."""
        try:
            self._last_activity_time = time.time()
            data = json.loads(message)
            self._stats['total_messages'] += 1
            
            # ── Handle error responses ──────────────────────────────────────
            if isinstance(data, dict) and "error" in data and data.get("error") != 0:
                error_msg = data.get("message", "Unknown error")
                error_code = data.get("error", "unknown")
                # Log as DEBUG since these are often harmless (rate limits, subscriptions, heartbeats)
                logger.debug(f"[WS] Server response with code {error_code}: {error_msg}")
                return
            
            # ── Handle ticker data ──────────────────────────────────────────
            stream = data.get("stream", "")
            
            if "market.ticker" in stream or "last" in data:
                # Extract symbol from stream path
                if stream:
                    parts = stream.split(".")
                    raw_sym = parts[-1].upper() if len(parts) > 1 else self.symbols[0]
                else:
                    raw_sym = data.get("symbol", self.symbols[0]).upper()
                
                # Denormalize BTC_THB back to internal THB_BTC format
                symbol_str = raw_sym
                if '_' in raw_sym:
                    p = raw_sym.split('_')
                    if len(p) == 2 and p[1] == 'THB':
                        symbol_str = f"THB_{p[0]}"
                
                # Create price tick
                tick = PriceTick(
                    symbol=symbol_str,
                    last=float(data.get("last", 0)),
                    bid=float(data.get("highestBid", 0)),
                    ask=float(data.get("lowestAsk", 0)),
                    percent_change_24h=float(data.get("percentChange", 0)),
                    timestamp=time.time()
                )
                
                # Update global cache (thread-safe)
                with _cache_lock:
                    _price_cache[symbol_str] = tick
                
                # Notify callback
                if self.on_tick:
                    try:
                        self.on_tick(tick)
                    except Exception as e:
                        logger.error(f"[WS] Error in tick callback: {e}")
            
            # ── Handle subscription confirmation ─────────────────────────────
            elif "subscribe" in stream.lower() or data.get("type") == "subscribe":
                logger.debug(f"[WS] Subscription confirmed: {data}")
            
            # ── Heartbeat/pong response ─────────────────────────────────────
            elif data.get("type") == "pong":
                self._last_pong_time = time.time()
            elif "pong" in data:
                self._last_pong_time = time.time()
            
        except json.JSONDecodeError:
            # Ignore non-JSON messages (pings, etc.)
            logger.debug(f"[WS] Non-JSON message ignored: {message[:100]}")
        except Exception as e:
            logger.error(f"[WS] Error parsing message: {e}")
            self._stats['last_error'] = str(e)

    def is_connected(self) -> bool:
        """
        Return True if WebSocket is currently connected and healthy.
        This is the proper way to check connection status.
        """
        with self._state_lock:
            return self._state == ConnectionState.CONNECTED

    def get_stats(self) -> Dict:
        """Get WebSocket statistics."""
        with self._state_lock:
            uptime = 0.0
            if self._stats['uptime_start']:
                uptime = time.time() - self._stats['uptime_start']
            
            return {
                'state': self._state.value,
                'total_messages': self._stats['total_messages'],
                'reconnections': self._stats['reconnections'],
                'consecutive_failures': self._consecutive_failures,
                'uptime_seconds': uptime,
                'last_error': self._stats['last_error'],
                'last_pong_ago': time.time() - self._last_pong_time,
                'last_activity_ago': self._seconds_since_last_activity(),
            }

    def stop(self):
        """Gracefully stop the WebSocket connection."""
        logger.info("[WS] Stopping...")
        self._running = False
        self._wakeup_event.set()
        self._set_state(ConnectionState.DISCONNECTED)
        
        # Close WebSocket
        if self.ws:
            try:
                self.ws.close()
            except Exception as e:
                logger.debug(f"[WS] Error closing: {e}")
        
        # Wait for threads to finish
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2.0)
        
        logger.info("[WS] Stopped")
        
        # Clear global cache
        with _cache_lock:
            _price_cache.clear()


# ── Singleton Interface ────────────────────────────────────────────────────────

_global_ws: Optional[BitkubWebSocket] = None
_global_ws_lock = threading.Lock()


def get_websocket(symbols: List[str], on_tick: Callable[[PriceTick], None]) -> BitkubWebSocket:
    """
    Get or create the global WebSocket instance.
    Thread-safe singleton pattern.
    """
    global _global_ws
    
    with _global_ws_lock:
        if _global_ws is None:
            _global_ws = BitkubWebSocket(symbols, on_tick)
            _global_ws.start()
        elif symbols != _global_ws.symbols:
            # If symbols changed, create new connection
            logger.info(f"[WS] Symbols changed, reconnecting with new symbols: {symbols}")
            _global_ws.stop()
            _global_ws = BitkubWebSocket(symbols, on_tick)
            _global_ws.start()
    
    return _global_ws


def stop_websocket():
    """Stop the global WebSocket instance."""
    global _global_ws
    
    with _global_ws_lock:
        if _global_ws:
            _global_ws.stop()
            _global_ws = None


def get_websocket_stats() -> Optional[Dict]:
    """Get statistics from the global WebSocket instance."""
    if _global_ws:
        return _global_ws.get_stats()
    return None
