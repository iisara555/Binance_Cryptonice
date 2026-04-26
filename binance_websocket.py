"""
Binance Thailand WebSocket Client
=================================
Native market stream adapter used by runtime price paths.

API contract intentionally mirrors the legacy `bitkub_websocket.py` module:
- PriceTick dataclass
- get_latest_ticker(symbol)
- get_websocket(symbols, on_tick)
- stop_websocket()
- get_websocket_stats()
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

try:
    import websocket  # type: ignore[import-untyped]
except ImportError:
    websocket = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning(
        "[BINANCE-WS] Package `websocket-client` is not installed for this interpreter (%s). "
        'Install with: "%s" -m pip install websocket-client',
        sys.executable,
        sys.executable,
    )

# True only when the PyPI `websocket-client` package is importable (module name `websocket`).
WEBSOCKET_RUNTIME_OK: bool = bool(websocket)

logger = logging.getLogger(__name__)


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


@dataclass
class PriceTick:
    symbol: str
    last: float
    bid: float
    ask: float
    percent_change_24h: float
    timestamp: float


_price_cache: Dict[str, PriceTick] = {}
_cache_lock = threading.Lock()


def get_latest_ticker(symbol: str) -> Optional[PriceTick]:
    with _cache_lock:
        return _price_cache.get(str(symbol or "").upper())


class BinanceWebSocket:
    INITIAL_RECONNECT_DELAY = 1.0
    MAX_RECONNECT_DELAY = 60.0
    BACKOFF_MULTIPLIER = 2.0
    HEARTBEAT_INTERVAL = 20.0

    def __init__(self, symbols: List[str], on_tick: Callable[[PriceTick], None]) -> None:
        self.symbols = [str(s or "").upper() for s in symbols if str(s or "").strip()]
        self.on_tick = on_tick
        self.endpoint = "wss://stream.binance.th:9443/stream"

        self._state = ConnectionState.DISCONNECTED
        self._state_lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.ws: Optional[Any] = None
        self._wakeup_event = threading.Event()
        self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
        self._last_activity_time = 0.0
        self._stats = {
            "total_messages": 0,
            "reconnections": 0,
            "last_error": None,
            "uptime_start": None,
        }

    @property
    def state(self) -> ConnectionState:
        with self._state_lock:
            return self._state

    def _set_state(self, state: ConnectionState) -> None:
        with self._state_lock:
            previous = self._state
            self._state = state
            if previous != state:
                logger.info("[BINANCE-WS] State %s -> %s", previous.value, state.value)
                if state == ConnectionState.CONNECTED:
                    self._stats["uptime_start"] = time.time()

    def _stream_url(self) -> str:
        streams = [f"{symbol.lower()}@ticker" for symbol in self.symbols]
        return f"{self.endpoint}?streams={'/'.join(streams)}"

    def start(self) -> None:
        if not websocket:
            self._set_state(ConnectionState.FAILED)
            logger.error(
                "[BINANCE-WS] websocket-client not installed — cannot start Binance stream. "
                'Use the same Python as the bot: "%s" -m pip install websocket-client',
                sys.executable,
            )
            return
        if self._running:
            return
        if not self.symbols:
            self._set_state(ConnectionState.FAILED)
            logger.warning("[BINANCE-WS] No symbols configured")
            return

        self._running = True
        self._wakeup_event.clear()
        self._set_state(ConnectionState.CONNECTING)
        self._thread = threading.Thread(target=self._run_forever, daemon=True, name="BinanceWS")
        self._thread.start()

    def _run_forever(self) -> None:
        while self._running:
            self._set_state(ConnectionState.CONNECTING)
            try:
                self.ws = websocket.WebSocketApp(  # type: ignore[union-attr]
                    self._stream_url(),
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(  # type: ignore[union-attr]
                    ping_interval=self.HEARTBEAT_INTERVAL,
                    ping_timeout=10.0,
                    ping_payload="ping",
                )
            except Exception as exc:
                self._stats["last_error"] = str(exc)
                logger.error("[BINANCE-WS] run_forever error: %s", exc)

            if not self._running:
                break
            self._set_state(ConnectionState.RECONNECTING)
            delay = min(self._reconnect_delay, self.MAX_RECONNECT_DELAY)
            self._stats["reconnections"] += 1
            logger.warning("[BINANCE-WS] Reconnecting in %.1fs", delay)
            if self._wakeup_event.wait(delay):
                break
            self._reconnect_delay = min(self._reconnect_delay * self.BACKOFF_MULTIPLIER, self.MAX_RECONNECT_DELAY)

    def _on_open(self, _ws: Any) -> None:
        self._set_state(ConnectionState.CONNECTED)
        self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
        self._last_activity_time = time.time()
        logger.info("[BINANCE-WS] Connected symbols=%s", self.symbols)

    def _extract_payload(self, message: str) -> Optional[Dict[str, Any]]:
        data = json.loads(message)
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return data["data"]
        if isinstance(data, dict):
            return data
        return None

    def _on_message(self, _ws: Any, message: str) -> None:
        try:
            payload = self._extract_payload(message)
            if not payload:
                return
            symbol = str(payload.get("s") or "").upper()
            if not symbol:
                return

            last = float(payload.get("c") or payload.get("lastPrice") or 0.0)
            bid = float(payload.get("b") or payload.get("bidPrice") or 0.0)
            ask = float(payload.get("a") or payload.get("askPrice") or 0.0)
            pct = float(payload.get("P") or payload.get("priceChangePercent") or 0.0)
            if last <= 0:
                return

            tick = PriceTick(
                symbol=symbol,
                last=last,
                bid=bid,
                ask=ask,
                percent_change_24h=pct,
                timestamp=time.time(),
            )

            with _cache_lock:
                _price_cache[symbol] = tick

            self._last_activity_time = tick.timestamp
            self._stats["total_messages"] += 1
            if self.on_tick:
                self.on_tick(tick)
        except Exception as exc:
            self._stats["last_error"] = str(exc)
            logger.debug("[BINANCE-WS] message parse failed: %s", exc)

    def _on_error(self, _ws: Any, error: Any) -> None:
        self._stats["last_error"] = str(error)
        logger.warning("[BINANCE-WS] Error: %s", error)

    def _on_close(self, _ws: Any, code: Any, reason: Any) -> None:
        logger.info("[BINANCE-WS] Closed code=%s reason=%s", code, reason)
        if self._running:
            self._set_state(ConnectionState.RECONNECTING)
        else:
            self._set_state(ConnectionState.DISCONNECTED)

    def is_connected(self) -> bool:
        return self.state == ConnectionState.CONNECTED

    def get_latest_ticker(self, symbol: str) -> Optional[PriceTick]:
        return get_latest_ticker(symbol)

    def get_stats(self) -> Dict[str, Any]:
        uptime = 0.0
        started = self._stats.get("uptime_start")
        if started:
            uptime = max(0.0, time.time() - float(started))
        return {
            "state": self.state.value,
            "endpoint": self.endpoint,
            "symbols": list(self.symbols),
            "total_messages": int(self._stats.get("total_messages") or 0),
            "reconnections": int(self._stats.get("reconnections") or 0),
            "last_error": self._stats.get("last_error"),
            "uptime_seconds": uptime,
            "last_activity_ago": max(0.0, time.time() - float(self._last_activity_time or 0.0))
            if self._last_activity_time
            else None,
        }

    def stop(self) -> None:
        self._running = False
        self._wakeup_event.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._set_state(ConnectionState.DISCONNECTED)


_global_ws: Optional[BinanceWebSocket] = None
_global_ws_lock = threading.Lock()


def get_websocket(symbols: List[str], on_tick: Callable[[PriceTick], None]) -> BinanceWebSocket:
    global _global_ws
    normalized = [str(s or "").upper() for s in symbols if str(s or "").strip()]
    with _global_ws_lock:
        if _global_ws is None:
            _global_ws = BinanceWebSocket(normalized, on_tick)
            _global_ws.start()
        elif sorted(_global_ws.symbols) != sorted(normalized):
            _global_ws.stop()
            _global_ws = BinanceWebSocket(normalized, on_tick)
            _global_ws.start()
    return _global_ws


def stop_websocket() -> None:
    global _global_ws
    with _global_ws_lock:
        if _global_ws:
            _global_ws.stop()
            _global_ws = None


def get_websocket_stats() -> Optional[Dict[str, Any]]:
    if _global_ws:
        return _global_ws.get_stats()
    return None
