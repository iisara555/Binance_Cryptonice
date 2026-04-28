"""
Bitkub REST API Client
Documentation: https://github.com/bitkub/bitkub-official-api-docs
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)


class BitkubAPI:
    """
    Bitkub REST API Client for Spot Trading
    """

    BASE_URL = "https://api.bitkub.com"

    def __init__(self, api_key: str = "", api_secret: str = "") -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def _sign(self, payload: str) -> str:
        """Generate HMAC-SHA256 signature"""
        return hmac.new(self.api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    def _prepare_headers(self, payload: dict) -> dict:
        """Prepare headers with signature for authenticated requests"""
        payload_str = json.dumps(payload)
        signature = self._sign(payload_str)

        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-BTK-APIKEY": self.api_key,
            "X-BTK-SIGNATURE": signature,
        }

    # ==================== PUBLIC ENDPOINTS ====================

    def get_server_time(self) -> dict[str, Any]:
        """Get server time"""
        response = self._session.get(f"{self.BASE_URL}/api/v3/time")
        response.raise_for_status()
        return response.json()

    def get_symbols(self) -> dict[str, Any]:
        """Get available trading pairs"""
        response = self._session.get(f"{self.BASE_URL}/api/v3/market/symbols")
        response.raise_for_status()
        return response.json()

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        """
        Get ticker information for a symbol
        :param symbol: Trading pair symbol (e.g., 'THB_BTC')
        """
        params = {"sym": symbol}
        response = self._session.get(f"{self.BASE_URL}/api/v3/market/ticker", params=params)
        response.raise_for_status()
        return response.json()

    def get_orderbook(self, symbol: str, limit: int = 100) -> dict[str, Any]:
        """
        Get order book (bids and asks)
        :param symbol: Trading pair symbol (e.g., 'THB_BTC')
        :param limit: Order book depth (1-100)
        """
        params = {"sym": symbol, "lmt": limit}
        response = self._session.get(f"{self.BASE_URL}/api/v3/market/books", params=params)
        response.raise_for_status()
        return response.json()

    def get_trades(self, symbol: str, limit: int = 100) -> dict[str, Any]:
        """
        Get recent trades
        :param symbol: Trading pair symbol (e.g., 'THB_BTC')
        :param limit: Number of recent trades
        """
        params = {"sym": symbol, "lmt": limit}
        response = self._session.get(f"{self.BASE_URL}/api/v3/market/trades", params=params)
        response.raise_for_status()
        return response.json()

    def get_ohlcv(
        self, symbol: str, interval: str = "1H", start: int | None = None, end: int | None = None
    ) -> dict[str, Any]:
        """
        Get OHLCV (candlestick) data
        :param symbol: Trading pair symbol (e.g., 'THB_BTC')
        :param interval: Time interval (1m, 5m, 15m, 30m, 1H, 1D, 1W, 1M)
        :param start: Start timestamp in seconds
        :param end: End timestamp in seconds
        """
        params: dict[str, Any] = {"sym": symbol, "int": interval}
        if start:
            params["start"] = start
        if end:
            params["end"] = end

        response = self._session.get(f"{self.BASE_URL}/api/v3/market/ohlcv", params=params)
        response.raise_for_status()
        return response.json()

    # ==================== AUTHENTICATED ENDPOINTS ====================

    def _post_authenticated(self, endpoint: str, payload: dict) -> dict[str, Any]:
        """Make authenticated POST request"""
        headers = self._prepare_headers(payload)

        # Add timestamp to payload
        server_time = self.get_server_time()
        payload["ts"] = server_time.get("timestamp", int(time.time() * 1000))

        response = self._session.post(f"{self.BASE_URL}{endpoint}", headers=headers, data=json.dumps(payload))
        response.raise_for_status()
        return response.json()

    def get_balances(self) -> dict[str, Any]:
        """
        Get account balances
        Requires API key with 'read' permission
        """
        payload: dict[str, Any] = {}
        return self._post_authenticated("/api/v3/market/balances", payload)

    def place_bid(self, symbol: str, amount: float, price: float) -> dict[str, Any]:
        """
        Place a buy order (bid)
        :param symbol: Trading pair symbol (e.g., 'THB_BTC')
        :param amount: Amount to buy
        :param price: Price per unit
        """
        payload = {"sym": symbol, "amt": amount, "rat": price, "typ": "limit"}  # Only limit orders supported
        return self._post_authenticated("/api/v3/market/place-bid", payload)

    def place_ask(self, symbol: str, amount: float, price: float) -> dict[str, Any]:
        """
        Place a sell order (ask)
        :param symbol: Trading pair symbol (e.g., 'THB_BTC')
        :param amount: Amount to sell
        :param price: Price per unit
        """
        payload = {"sym": symbol, "amt": amount, "rat": price, "typ": "limit"}  # Only limit orders supported
        return self._post_authenticated("/api/v3/market/place-ask", payload)

    def cancel_order(self, order_id: int, symbol: str, side: str) -> dict[str, Any]:
        """
        Cancel an order
        :param order_id: Order ID to cancel
        :param symbol: Trading pair symbol
        :param side: Order side ('BUY' or 'SELL')
        """
        payload = {"sym": symbol, "id": order_id, "sd": side}
        return self._post_authenticated("/api/v3/market/cancel-order", payload)

    def get_open_orders(self, symbol: str) -> dict[str, Any]:
        """
        Get open orders for a symbol
        :param symbol: Trading pair symbol (e.g., 'THB_BTC')
        """
        payload = {"sym": symbol}
        return self._post_authenticated("/api/v3/market/my-open-orders", payload)

    def get_order_history(self, symbol: str) -> dict[str, Any]:
        """
        Get order history for a symbol
        :param symbol: Trading pair symbol (e.g., 'THB_BTC')
        """
        payload = {"sym": symbol}
        return self._post_authenticated("/api/v3/market/order-history", payload)

    def get_order_info(self, order_id: int, symbol: str) -> dict[str, Any]:
        """
        Get order information
        :param order_id: Order ID
        :param symbol: Trading pair symbol
        """
        payload = {"sym": symbol, "id": order_id}
        return self._post_authenticated("/api/v3/market/order-info", payload)

    # ==================== UTILITY METHODS ====================

    def close(self) -> None:
        """Close the session"""
        self._session.close()

    def test_connection(self) -> bool:
        """Test API connection"""
        try:
            result = self.get_server_time()
            return result.get("error", 0) == 0
        except Exception as e:
            logger.error(f"Failed to connect to Bitkub API: {e}")
            return False
