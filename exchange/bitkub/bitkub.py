"""
Bitkub Exchange - Custom Exchange Implementation for Freqtrade
Uses Bitkub REST API and WebSocket directly (no CCXT)
"""

import logging
from copy import deepcopy
from typing import Any

import pandas as pd
from freqtrade.exchange import Exchange
from freqtrade.exchange.bitkub.bitkub_api import BitkubAPI
from freqtrade.exchange.bitkub.bitkub_ws import BitkubWS
from freqtrade.exchange.exchange_types import CcxtOrder, FtHas, Ticker
from freqtrade.exchange.exchange_utils_timeframe import timeframe_to_seconds
from freqtrade.util import dt_ts

logger = logging.getLogger(__name__)


class Bitkub(Exchange):
    """
    Bitkub Exchange class for Freqtrade.

    This is a custom implementation that uses Bitkub API directly
    instead of CCXT, providing better stability and direct compatibility
    with Bitkub's trading system.
    """

    _ft_has: FtHas = {
        # Trading capabilities
        "stoploss_on_exchange": False,  # Bitkub doesn't support stoploss on exchange
        "stop_price_param": "stopLossPrice",
        "stop_price_prop": "stopLossPrice",
        "stoploss_order_types": {},
        "stoploss_blocks_assets": True,
        "stoploss_query_requires_stop_flag": False,
        "order_time_in_force": ["GTC"],  # Good Till Cancel
        "exchange_has_overrides": {"createMarketOrder": False},  # Bitkub only supports limit orders
        # OHLCV settings
        "ohlcv_params": {},
        "ohlcv_has_history": True,
        "ohlcv_partial_candle": True,
        "ohlcv_require_since": False,
        "download_data_parallel_quick": True,
        "ohlcv_volume_currency": "quote",  # THB for most pairs
        # API settings
        "always_require_api_keys": True,  # Requires API keys for trading
        # Trade history
        "trades_has_history": True,  # Limited to recent trades
        "trades_limit": 100,
        # Order book
        "l2_coinvert_bid_ask": True,
        # Market data
        "tickers_have_quoteVolume": True,
        "tickers_have_percentage": True,
        "tickers_have_bid_ask": True,
        "tickers_have_price": True,
        # Candle limits
        "ohlcv_candle_limit": 1000,
        # Supported features
        "ws_enabled": True,  # WebSocket supported
        "ccxt_futures_name": None,  # No futures support
    }

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

    def __init__(
        self,
        config: dict[str, Any],
        *,
        exchange_config: dict[str, Any] | None = None,
        validate: bool = True,
        load_leverage_tiers: bool = False,
    ) -> None:
        """
        Initialize Bitkub Exchange
        """
        self._api: BitkubAPI | None = None
        self._ws: BitkubWS | None = None
        self._markets: dict[str, Any] = {}

        super().__init__(config, exchange_config=exchange_config, validate=validate)

    def _init_api(self) -> None:
        """Initialize REST API client"""
        if self._api is None:
            api_key = self._config["exchange"].get("key", "")
            api_secret = self._config["exchange"].get("secret", "")
            self._api = BitkubAPI(api_key, api_secret)
            logger.info("Bitkub API initialized")

    def _init_ws(self) -> None:
        """Initialize WebSocket client"""
        if self._ws is None and self._ft_has.get("ws_enabled"):
            self._ws = BitkubWS()
            self._ws.start()
            logger.info("Bitkub WebSocket initialized")

    def _load_markets(self, reload: bool = False) -> dict[str, Any]:
        """Load trading markets from Bitkub"""
        if not self._markets or reload:
            self._init_api()
            try:
                response = self._api.get_symbols()
                if response.get("error", 0) == 0:
                    symbols_data = response.get("result", [])

                    # Convert to CCXT-like market format
                    self._markets = {}
                    for symbol_info in symbols_data:
                        symbol = symbol_info.get("symbol", "")
                        base_currency = symbol_info.get("base", "")
                        quote_currency = symbol_info.get("quote", "")

                        self._markets[symbol] = {
                            "symbol": symbol,
                            "base": base_currency,
                            "quote": quote_currency,
                            "active": symbol_info.get("status", "") == "active",
                            "precision": {
                                "amount": symbol_info.get("lotSize", 8),
                                "price": symbol_info.get("tickSize", 8),
                            },
                            "limits": {
                                "amount": {
                                    "min": symbol_info.get("minOrderSize", 0),
                                    "max": symbol_info.get("maxOrderSize", 0),
                                },
                                "price": {
                                    "min": symbol_info.get("minPrice", 0),
                                    "max": symbol_info.get("maxPrice", 0),
                                },
                            },
                            "info": symbol_info,
                        }

                    logger.info(f"Loaded {len(self._markets)} markets from Bitkub")
                else:
                    logger.error(f"Failed to load markets: {response}")

            except Exception as e:
                logger.error(f"Error loading markets: {e}")

        return self._markets

    @property
    def markets(self) -> dict[str, Any]:
        """Get cached markets"""
        if not self._markets:
            self._load_markets()
        return self._markets

    @property
    def markets_df(self) -> pd.DataFrame:
        """Get markets as DataFrame"""
        return pd.DataFrame.from_dict(self.markets, orient="index")

    def symbol(self, pair: str) -> str:
        """Get exchange symbol format for a pair"""
        return pair.replace("/", "_")

    def pair_with_symbol(self, symbol: str) -> str:
        """Get Freqtrade pair format from exchange symbol"""
        return symbol.replace("_", "/")

    # ==================== OHLCV METHODS ====================

    def fetch_ohlcv(
        self,
        pair: str,
        timeframe: str = "1h",
        since: int | None = None,
        limit: int = 1000,
        candle_type: str = "spot",
    ) -> list:
        """
        Fetch OHLCV (candlestick) data from Bitkub
        """
        self._init_api()
        symbol = self.symbol(pair)
        interval = self.TIMEFRAME_MAP.get(timeframe, "60")

        try:
            # Use since in seconds for Bitkub API
            since_sec = since // 1000 if since else None

            response = self._api.get_ohlcv(symbol=symbol, interval=interval, start=since_sec, end=None)

            if response.get("error", 0) == 0:
                ohlcv_data = response.get("result", [])

                # Convert to ccxt-like format
                result = []
                for candle in ohlcv_data:
                    result.append(
                        [
                            candle.get("t", 0) * 1000,  # Timestamp in ms
                            candle.get("o", 0),  # Open
                            candle.get("h", 0),  # High
                            candle.get("l", 0),  # Low
                            candle.get("c", 0),  # Close
                            candle.get("v", 0),  # Volume
                        ]
                    )

                # Sort by timestamp
                result.sort(key=lambda x: x[0])

                # Limit results
                if limit and len(result) > limit:
                    result = result[-limit:]

                return result
            else:
                logger.error(f"Failed to fetch OHLCV: {response}")
                return []

        except Exception as e:
            logger.error(f"Error fetching OHLCV for {pair}: {e}")
            return []

    # ==================== TICKER METHODS ====================

    def fetch_ticker(self, pair: str, candle_type: str = "spot") -> Ticker:
        """
        Fetch ticker for a trading pair
        """
        self._init_api()
        symbol = self.symbol(pair)

        try:
            response = self._api.get_ticker(symbol)

            if response.get("error", 0) == 0:
                ticker_data = response.get("result", {})

                return {
                    "symbol": symbol,
                    "bid": ticker_data.get("highestBid", 0),
                    "ask": ticker_data.get("lowestAsk", 0),
                    "last": ticker_data.get("last", 0),
                    "high": ticker_data.get("high24hr", 0),
                    "low": ticker_data.get("low24hr", 0),
                    "volume": ticker_data.get("volume", 0),
                    "quoteVolume": ticker_data.get("quoteVolume", 0),
                    "percentage": ticker_data.get("percentChange", 0),
                    "info": ticker_data,
                }
            else:
                logger.error(f"Failed to fetch ticker: {response}")
                return {}

        except Exception as e:
            logger.error(f"Error fetching ticker for {pair}: {e}")
            return {}

    # ==================== ORDER BOOK METHODS ====================

    def fetch_order_book(self, pair: str, limit: int = 100, candle_type: str = "spot") -> dict[str, Any]:
        """
        Fetch order book for a trading pair
        """
        self._init_api()
        symbol = self.symbol(pair)

        try:
            response = self._api.get_orderbook(symbol, limit)

            if response.get("error", 0) == 0:
                orderbook_data = response.get("result", {})

                return {
                    "bids": [
                        [float(b.get("price", 0)), float(b.get("volume", 0))] for b in orderbook_data.get("bids", [])
                    ],
                    "asks": [
                        [float(a.get("price", 0)), float(a.get("volume", 0))] for a in orderbook_data.get("asks", [])
                    ],
                    "symbol": symbol,
                    "timestamp": dt_ts(),
                }
            else:
                logger.error(f"Failed to fetch order book: {response}")
                return {"bids": [], "asks": [], "symbol": symbol}

        except Exception as e:
            logger.error(f"Error fetching order book for {pair}: {e}")
            return {"bids": [], "asks": [], "symbol": symbol}

    # ==================== BALANCE METHODS ====================

    def fetch_balance(self, **kwargs) -> dict[str, Any]:
        """
        Fetch account balance
        """
        self._init_api()

        try:
            response = self._api.get_balances()

            if response.get("error", 0) == 0:
                balances_data = response.get("result", {})

                # Convert to CCXT-like format
                balance = {
                    "free": {},
                    "used": {},
                    "total": {},
                    "info": balances_data,
                }

                for currency, balance_info in balances_data.items():
                    if isinstance(balance_info, dict):
                        balance["free"][currency] = float(balance_info.get("available", 0))
                        balance["used"][currency] = float(balance_info.get("hold", 0))
                        balance["total"][currency] = float(
                            balance_info.get("available", 0) + balance_info.get("hold", 0)
                        )

                return balance
            else:
                logger.error(f"Failed to fetch balance: {response}")
                return {"free": {}, "used": {}, "total": {}}

        except Exception as e:
            logger.error(f"Error fetching balance: {e}")
            return {"free": {}, "used": {}, "total": {}}

    # ==================== ORDER METHODS ====================

    def create_order(self, pair: str, side: str, ordertype: str, amount: float, price: float, **kwargs) -> CcxtOrder:
        """
        Place an order (buy or sell)
        """
        self._init_api()
        symbol = self.symbol(pair)

        try:
            if side.lower() == "buy":
                response = self._api.place_bid(symbol, amount, price)
            else:
                response = self._api.place_ask(symbol, amount, price)

            if response.get("error", 0) == 0:
                order_data = response.get("result", {})

                return {
                    "id": str(order_data.get("id", "")),
                    "symbol": symbol,
                    "type": ordertype.lower(),
                    "side": side.lower(),
                    "amount": amount,
                    "price": price,
                    "status": "open",
                    "filled": 0,
                    "remaining": amount,
                    "info": order_data,
                }
            else:
                error_msg = response.get("error", {}).get("message", "Unknown error")
                logger.error(f"Failed to create order: {error_msg}")
                raise Exception(f"Order creation failed: {error_msg}")

        except Exception as e:
            logger.error(f"Error creating order for {pair}: {e}")
            raise

    def cancel_order(self, order_id: str | int, pair: str, side: str | None = None, **kwargs) -> dict[str, Any]:
        """
        Cancel an order
        """
        self._init_api()
        symbol = self.symbol(pair)

        try:
            # If side is not provided, get order info first
            if side is None:
                order_info = self.fetch_order(order_id, pair)
                side = order_info.get("side", "BUY").upper()

            response = self._api.cancel_order(int(order_id), symbol, side.upper())

            if response.get("error", 0) == 0:
                return {"id": order_id, "symbol": symbol, "status": "canceled"}
            else:
                error_msg = response.get("error", {}).get("message", "Unknown error")
                logger.error(f"Failed to cancel order: {error_msg}")
                raise Exception(f"Order cancellation failed: {error_msg}")

        except Exception as e:
            logger.error(f"Error canceling order {order_id}: {e}")
            raise

    def fetch_order(self, order_id: str | int, pair: str = "", **kwargs) -> CcxtOrder:
        """
        Fetch information about an order
        """
        self._init_api()
        symbol = self.symbol(pair) if pair else ""

        try:
            response = self._api.get_order_info(int(order_id), symbol)

            if response.get("error", 0) == 0:
                order_data = response.get("result", {})

                # Determine status
                status = "open"
                if order_data.get("status") == "filled":
                    status = "closed"
                elif order_data.get("status") == "cancelled":
                    status = "canceled"

                return {
                    "id": str(order_data.get("id", "")),
                    "symbol": order_data.get("symbol", symbol),
                    "type": order_data.get("type", "limit").lower(),
                    "side": order_data.get("side", "").lower(),
                    "amount": float(order_data.get("amount", 0)),
                    "price": float(order_data.get("rate", 0)),
                    "status": status,
                    "filled": float(order_data.get("filled", 0)),
                    "remaining": float(order_data.get("amount", 0)) - float(order_data.get("filled", 0)),
                    "info": order_data,
                }
            else:
                logger.error(f"Failed to fetch order: {response}")
                return {}

        except Exception as e:
            logger.error(f"Error fetching order {order_id}: {e}")
            return {}

    def fetch_open_orders(self, pair: str | None = None, limit: int | None = None, **kwargs) -> list[CcxtOrder]:
        """
        Fetch all open orders
        """
        self._init_api()

        try:
            if pair:
                symbol = self.symbol(pair)
                response = self._api.get_open_orders(symbol)
            else:
                # If no pair specified, get all symbols' open orders
                response = {"error": 0, "result": []}
                for market_symbol in self.markets.keys():
                    try:
                        pair_response = self._api.get_open_orders(market_symbol)
                        if pair_response.get("error", 0) == 0:
                            orders = pair_response.get("result", [])
                            response["result"].extend(orders)
                    except Exception:
                        continue

            if response.get("error", 0) == 0:
                orders_data = response.get("result", [])

                orders = []
                for order_data in orders_data:
                    orders.append(
                        {
                            "id": str(order_data.get("id", "")),
                            "symbol": order_data.get("symbol", ""),
                            "type": order_data.get("type", "limit").lower(),
                            "side": order_data.get("side", "").lower(),
                            "amount": float(order_data.get("amount", 0)),
                            "price": float(order_data.get("rate", 0)),
                            "status": "open",
                            "filled": float(order_data.get("filled", 0)),
                            "remaining": float(order_data.get("amount", 0)) - float(order_data.get("filled", 0)),
                            "info": order_data,
                        }
                    )

                return orders
            else:
                logger.error(f"Failed to fetch open orders: {response}")
                return []

        except Exception as e:
            logger.error(f"Error fetching open orders: {e}")
            return []

    # ==================== WEBSOCKET METHODS ====================

    def ohlcv_ws(self, pair: str, timeframe: str) -> list:
        """Get OHLCV data from WebSocket cache"""
        if self._ws:
            symbol = self.symbol(pair)
            return self._ws.get_ohlcv(symbol, timeframe)
        return []

    def ticker_ws(self, pair: str) -> dict:
        """Get ticker data from WebSocket cache"""
        if self._ws:
            symbol = self.symbol(pair)
            return self._ws.get_ticker(symbol)
        return {}

    # ==================== UTILITY METHODS ====================

    def close(self) -> None:
        """Clean up resources"""
        if self._api:
            self._api.close()
        if self._ws:
            self._ws.stop()
        logger.info("Bitkub exchange closed")

    def validate_required_credentials(self) -> None:
        """Validate that required credentials are set"""
        if not self._config["exchange"].get("key"):
            raise ValueError("Bitkub API key is required")
        if not self._config["exchange"].get("secret"):
            raise ValueError("Bitkub API secret is required")

    def get_balance(self, currency: str) -> float:
        """Get balance for a specific currency"""
        balance = self.fetch_balance()
        return balance.get("total", {}).get(currency, 0.0)
