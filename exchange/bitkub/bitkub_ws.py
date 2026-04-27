"""
Bitkub WebSocket API Client
Documentation: https://github.com/bitkub/bitkub-official-api-docs
"""

import asyncio
import json
import logging
import threading
import time
from collections import defaultdict
from copy import deepcopy
from typing import Any

import websockets

from freqtrade.constants import PairWithTimeframe
from freqtrade.util import dt_ts

logger = logging.getLogger(__name__)


class BitkubWS:
    """
    Bitkub WebSocket API Client for real-time data
    
    WebSocket URL: wss://socket.bitkub.com
    """

    WS_URL = "wss://socket.bitkub.com"

    # Timeframe mapping from Freqtrade to Bitkub
    TIMEFRAME_MAP = {
        "1m": "1",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "4h": "240",
        "1d": "1D",
        "1w": "1W",
    }

    def __init__(self) -> None:
        self._websocket = None
        self._loop = None
        self._thread = None
        self._running = False
        self._lock = threading.Lock()

        # Data caches
        self._tickers: dict[str, dict] = defaultdict(dict)
        self._orderbooks: dict[str, dict] = defaultdict(dict)
        self._trades: dict[str, list] = defaultdict(list)
        self._ohlcvs: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))

        # Subscriptions
        self._subscribed_tickers: set[str] = set()
        self._subscribed_orderbooks: set[str] = set()
        self._subscribed_trades: set[str] = set()
        self._subscribed_ohlcvs: dict[str, set[str]] = defaultdict(set)  # symbol -> timeframes

        # Last update times
        self.klines_last_refresh: dict[PairWithTimeframe, float] = {}
        self.klines_last_request: dict[PairWithTimeframe, float] = {}

    def start(self) -> None:
        """Start the WebSocket connection in a background thread"""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(name="bitkub_ws", target=self._run_forever, daemon=True)
        self._thread.start()
        logger.info("Bitkub WebSocket thread started")

    def _run_forever(self) -> None:
        """Run WebSocket loop forever"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        while self._running:
            try:
                self._loop.run_until_complete(self._connect_and_listen())
            except asyncio.CancelledError:
                logger.info("WebSocket cancelled")
                break
            except Exception as e:
                logger.error(f"WebSocket error: {e}, reconnecting in 5 seconds...")
                time.sleep(5)

        if not self._loop.is_closed():
            self._loop.close()

    async def _connect_and_listen(self) -> None:
        """Connect to WebSocket and listen for messages"""
        async with websockets.connect(self.WS_URL, ping_interval=30) as websocket:
            self._websocket = websocket
            logger.info("Connected to Bitkub WebSocket")

            # Resubscribe to all previously subscribed channels
            await self._resubscribe()

            while self._running:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=30)
                    await self._handle_message(message)
                except asyncio.TimeoutError:
                    # Send ping to keep connection alive
                    await websocket.ping()
                except websockets.exceptions.ConnectionClosed:
                    logger.warning("WebSocket connection closed")
                    raise

    async def _resubscribe(self) -> None:
        """Resubscribe to all previously subscribed channels"""
        if self._subscribed_tickers:
            await self._subscribe_ticker(list(self._subscribed_tickers))
        if self._subscribed_orderbooks:
            await self._subscribe_orderbook(list(self._subscribed_orderbooks))
        if self._subscribed_trades:
            await self._subscribe_trade(list(self._subscribed_trades))
        for symbol, timeframes in self._subscribed_ohlcvs.items():
            for tf in timeframes:
                await self._subscribe_ohlcv(symbol, tf)

    async def _subscribe_ticker(self, symbols: list[str]) -> None:
        """Subscribe to ticker channel"""
        if not self._websocket:
            return
        
        message = {
            "type": "subscribe",
            "channel": "ticker",
            "symbols": symbols
        }
        await self._websocket.send(json.dumps(message))
        logger.debug(f"Subscribed to ticker: {symbols}")

    async def _subscribe_orderbook(self, symbols: list[str]) -> None:
        """Subscribe to orderbook channel"""
        if not self._websocket:
            return
        
        message = {
            "type": "subscribe",
            "channel": "market.books",
            "symbols": symbols
        }
        await self._websocket.send(json.dumps(message))
        logger.debug(f"Subscribed to orderbook: {symbols}")

    async def _subscribe_trade(self, symbols: list[str]) -> None:
        """Subscribe to trade channel"""
        if not self._websocket:
            return
        
        message = {
            "type": "subscribe",
            "channel": "trade",
            "symbols": symbols
        }
        await self._websocket.send(json.dumps(message))
        logger.debug(f"Subscribed to trade: {symbols}")

    async def _subscribe_ohlcv(self, symbol: str, timeframe: str) -> None:
        """Subscribe to OHLCV channel"""
        if not self._websocket:
            return
        
        bitkub_interval = self.TIMEFRAME_MAP.get(timeframe, "1")
        channel = f"market.ohlcv.{bitkub_interval}"
        
        message = {
            "type": "subscribe",
            "channel": channel,
            "symbols": [symbol]
        }
        await self._websocket.send(json.dumps(message))
        logger.debug(f"Subscribed to OHLCV {timeframe}: {symbol}")

    async def _handle_message(self, message: str) -> None:
        """Handle incoming WebSocket message"""
        try:
            data = json.loads(message)
            msg_type = data.get("type")
            channel = data.get("channel", "")

            if msg_type == "subscribe":
                logger.debug(f"Subscription confirmed: {channel}")
                return

            # Handle ticker data
            if channel == "ticker" and "data" in data:
                for ticker_data in data["data"]:
                    symbol = ticker_data.get("s", "")
                    self._tickers[symbol] = ticker_data

            # Handle orderbook data
            elif channel == "market.books" and "data" in data:
                symbol = data.get("sym", "")
                self._orderbooks[symbol] = data["data"]

            # Handle trade data
            elif channel == "trade" and "data" in data:
                symbol = data.get("sym", "")
                for trade in data["data"]:
                    self._trades[symbol].append(trade)
                    # Keep only last 1000 trades
                    if len(self._trades[symbol]) > 1000:
                        self._trades[symbol] = self._trades[symbol][-1000:]

            # Handle OHLCV data
            elif channel.startswith("market.ohlcv."):
                interval = channel.replace("market.ohlcv.", "")
                if "data" in data:
                    for ohlcv_data in data["data"]:
                        symbol = ohlcv_data.get("s", "")
                        # Convert to ccxt-like format [timestamp, open, high, low, close, volume]
                        candle = [
                            ohlcv_data.get("t", 0) * 1000,  # Convert to milliseconds
                            ohlcv_data.get("o", 0),
                            ohlcv_data.get("h", 0),
                            ohlcv_data.get("l", 0),
                            ohlcv_data.get("c", 0),
                            ohlcv_data.get("v", 0),
                        ]
                        # Reverse to get chronological order
                        if len(self._ohlcvs[symbol][interval]) > 0:
                            if self._ohlcvs[symbol][interval][-1][0] == candle[0]:
                                # Update existing candle
                                self._ohlcvs[symbol][interval][-1] = candle
                            else:
                                self._ohlcvs[symbol][interval].append(candle)
                        else:
                            self._ohlcvs[symbol][interval].append(candle)

                        # Update last refresh time
                        pair_timeframe = (symbol, self._reverse_timeframe_map(interval), "spot")
                        self.klines_last_refresh[pair_timeframe] = dt_ts()

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse WebSocket message: {e}")

    def _reverse_timeframe_map(self, interval: str) -> str:
        """Reverse the timeframe mapping"""
        reverse_map = {v: k for k, v in self.TIMEFRAME_MAP.items()}
        return reverse_map.get(interval, interval)

    # ==================== PUBLIC METHODS ====================

    def subscribe(self, symbol: str, timeframe: str) -> None:
        """Subscribe to ticker and OHLCV for a symbol"""
        with self._lock:
            # Subscribe to ticker
            if symbol not in self._subscribed_tickers:
                self._subscribed_tickers.add(symbol)
                asyncio.run_coroutine_threadsafe(
                    self._subscribe_ticker([symbol]), self._loop
                )

            # Subscribe to OHLCV
            if symbol not in self._subscribed_ohlcvs.get(timeframe, set()):
                self._subscribed_ohlcvs[symbol].add(timeframe)
                asyncio.run_coroutine_threadsafe(
                    self._subscribe_ohlcv(symbol, timeframe), self._loop
                )

            # Track request time
            pair_timeframe = (symbol, timeframe, "spot")
            self.klines_last_request[pair_timeframe] = dt_ts()

    def get_ohlcv(self, symbol: str, timeframe: str) -> list:
        """Get cached OHLCV data for a symbol and timeframe"""
        interval = self.TIMEFRAME_MAP.get(timeframe, "1")
        
        with self._lock:
            return deepcopy(self._ohlcvs.get(symbol, {}).get(interval, []))

    def get_ticker(self, symbol: str) -> dict:
        """Get cached ticker data for a symbol"""
        with self._lock:
            return deepcopy(self._tickers.get(symbol, {}))

    def get_orderbook(self, symbol: str) -> dict:
        """Get cached orderbook data for a symbol"""
        with self._lock:
            return deepcopy(self._orderbooks.get(symbol, {}))

    def get_trades(self, symbol: str) -> list:
        """Get cached trades for a symbol"""
        with self._lock:
            return deepcopy(self._trades.get(symbol, []))

    def stop(self) -> None:
        """Stop the WebSocket connection"""
        self._running = False
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._websocket.close() if self._websocket else asyncio.sleep(0),
                self._loop
            )
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Bitkub WebSocket stopped")

    def cleanup_expired(self) -> None:
        """Remove expired subscriptions"""
        current_time = dt_ts()
        timeout_ms = 60 * 1000  # 1 minute timeout

        with self._lock:
            for pair_timeframe in list(self.klines_last_request.keys()):
                last_request = self.klines_last_request.get(pair_timeframe, 0)
                if last_request > 0 and (current_time - last_request) > timeout_ms:
                    logger.info(f"Removing {pair_timeframe} from WebSocket subscription")
                    self.klines_last_request.pop(pair_timeframe, None)
