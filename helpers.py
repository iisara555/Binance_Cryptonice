"""
Shared Helper Utilities — Phase 3 code quality cleanup.

Provides common functions used across multiple modules to reduce
duplication and improve consistency:
- Price fetching (WebSocket cache → REST fallback)
- Balance lookups (THB / base asset with safe defaults)
- Price formatting / logging helpers
- Typed aliases for common return types
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

BITKUB_TIMEZONE = timezone(timedelta(hours=7), name="ICT")


def now_bitkub() -> datetime:
    """Return the current time in Bitkub's Thailand timezone."""
    return datetime.now(timezone.utc).astimezone(BITKUB_TIMEZONE)


def parse_as_bitkub_time(value: Any) -> Optional[datetime]:
    """Normalize a datetime-like value to Bitkub's Thailand timezone.

    Naive runtime timestamps are treated as UTC so VPS-rendered CLI times stay
    aligned with Bitkub time.
    """
    if not value:
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BITKUB_TIMEZONE)


def format_bitkub_time(value: Any, fmt: str = "%H:%M:%S") -> str:
    """Format a datetime-like value in Bitkub's Thailand timezone."""
    if not value:
        return "-"
    try:
        dt = parse_as_bitkub_time(value)
    except (TypeError, ValueError):
        return str(value)
    if dt is None:
        return "-"
    return dt.strftime(fmt)


# ── Price Fetching ──────────────────────────────────────────────────────────────

def get_current_price(
    symbol: str,
    api_client: Any = None,
    ws_client: Any = None,
) -> Tuple[Optional[float], str]:
    """
    Get the current market price for a symbol using fastest available source.

    Priority: WebSocket cache → REST API ticker → None

    Args:
        symbol: Trading pair symbol (e.g. 'THB_BTC' or 'BTC_THB')
        api_client: BitkubClient instance (None to skip REST fallback)
        ws_client: BitkubWebSocket instance (None to skip WS lookup)

    Returns:
        (price, source) tuple where source is one of:
        - 'ws' — live WebSocket data
        - 'rest' — REST API fallback
        - 'none' — unavailable
    """
    # Try WebSocket cache first
    if ws_client is not None:
        try:
            # Direct import of module-level function — avoids circular deps
            from bitkub_websocket import get_latest_ticker
            tick = get_latest_ticker(symbol)
            if tick and getattr(tick, "last", 0) > 0:
                return float(tick.last), "ws"
        except Exception:
            pass  # Fall through to REST

    # REST API fallback
    if api_client is not None:
        try:
            ticker = api_client.get_ticker(symbol)
            if isinstance(ticker, list) and ticker:
                return float(ticker[0].get("last", 0)), "rest"
            elif isinstance(ticker, dict):
                return float(ticker.get("last", ticker.get("close", 0))), "rest"
        except Exception as e:
            logger.debug(f"REST ticker failed for {symbol}: {e}")

    return None, "none"


# ── Balance Helpers ────────────────────────────────────────────────────────────

def get_balance(
    api_client: Any,
    asset: str,
    default: float = 0.0,
) -> float:
    """
    Safely get available balance for an asset from Bitkub API.

    Handles both nested dict (v3 API) and flat dict formats.
    Caches the result per call (no internal cache) — API is always hit.

    Args:
        api_client: BitkubClient instance
        asset: Asset symbol (e.g. 'THB', 'BTC', 'XAUT')
        default: Value to return if balance cannot be fetched

    Returns:
        Available balance as float, or `default` on error
    """
    try:
        balances = api_client.get_balances()
        return _extract_balance(balances, asset.upper(), default)
    except Exception as e:
        logger.debug(f"Balance fetch failed for {asset}: {e}")
        return default


def _extract_balance(
    balances: Any,
    asset: str,
    default: float = 0.0,
) -> float:
    """Internal: extract available amount from balances response."""
    if isinstance(balances, dict):
        info = balances.get(asset, {})
        if isinstance(info, dict):
            return float(info.get("available", default))
        return default
    return default


def get_thb_balance(api_client: Any, default: float = 0.0) -> float:
    """Get THB balance — convenience alias."""
    return get_balance(api_client, "THB", default)


def get_crypto_balance(
    api_client: Any,
    symbol: str,
    default: float = 0.0,
) -> float:
    """
    Get crypto asset balance from a trading pair symbol.

    Examples:
        'THB_BTC' → BTC balance
        'BTC_THB' → BTC balance (auto-extracts base asset)
    """
    # Extract base asset: 'THB_BTC' → 'BTC', 'BTC_THB' → 'BTC'
    parts = symbol.upper().split("_")
    base = parts[1] if len(parts) == 2 else symbol.upper()

    # THB pairs: exclude quote currency from result
    if base in ("THB",):
        # For 'THB_BTC', base asset is at index 1
        if len(parts) == 2 and parts[0] == "THB":
            base = parts[1]

    return get_balance(api_client, base, default)


# ── Symbol Formatting ─────────────────────────────────────────────────────────

def normalize_symbol(symbol: str) -> str:
    """
    Normalize a trading pair symbol to standard format.

    Ensures THB pairs have consistent casing:
        btc_thb  → BTC_THB
        THB_btc  → THB_BTC
    """
    parts = symbol.upper().split("_")
    return "_".join(parts)


def extract_base_asset(symbol: str) -> str:
    """
    Extract the base (non-THB) asset from a trading pair.

    Examples:
        'THB_BTC' → 'BTC'
        'BTC_THB' → 'BTC'
        'THB_XAUT' → 'XAUT'
    """
    parts = symbol.upper().split("_")
    if len(parts) == 2:
        return parts[0] if parts[1] == "THB" else parts[1]
    return symbol.upper()


def symbol_for_api(symbol: str) -> str:
    """
    Format symbol for Bitkub API calls.

    Some API endpoints expect 'THB_BTC' while others accept
    lowercase. This ensures consistent uppercase format.
    """
    return normalize_symbol(symbol)


# ── Logging Helpers ────────────────────────────────────────────────────────────

def format_price(price: float, symbol: str = "THB") -> str:
    """Format a price value with appropriate commas/decimals."""
    base = extract_base_asset(symbol) if symbol else ""
    if base in ("BTC", "ETH", "XAUT", "SOL"):
        return f"{price:,.2f}"
    return f"{price:,.0f}"


def format_thb(value: float) -> str:
    """Format THB amount with 2 decimal places."""
    return f"{value:,.2f}"


def format_crypto(qty: float, asset: str) -> str:
    """Format crypto quantity with appropriate precision."""
    decimals = {
        "BTC": 8,
        "ETH": 8,
        "XAUT": 8,
        "BNB": 8,
        "SOL": 8,
        "XRP": 0,
        "ADA": 0,
        "DOGE": 0,
    }.get(asset.upper(), 4)
    return f"{qty:.{decimals}f}"


# ── Trade Value Calculations ──────────────────────────────────────────────────

def calc_order_value(price: float, quantity: float, side: str) -> float:
    """
    Calculate order value in THB.

    Args:
        price: Order price per unit
        quantity: Amount to trade
        side: 'buy' or 'sell'

    For BUY on Bitkub: quantity is already THB amount.
    For SELL: value = price × quantity (in THB).
    """
    if side.lower() == "buy":
        return quantity  # quantity is THB spent
    return price * quantity


def calc_net_pnl(
    entry_cost: float,
    exit_price: float,
    quantity: float,
    side: str,
    fee_pct: float = 0.0025,
) -> Dict[str, float]:
    """
    Calculate net P/L after Bitkub fees (0.25% per side).

    Returns dict with:
        entry_fee, exit_fee, total_fees, gross_exit, net_exit, net_pnl, net_pnl_pct
    """
    entry_fee = entry_cost * fee_pct
    gross_exit = exit_price * quantity
    exit_fee = gross_exit * fee_pct
    total_fees = entry_fee + exit_fee
    net_exit = gross_exit - exit_fee

    if side.lower() == "buy":
        net_pnl = net_exit - entry_cost
    else:
        net_pnl = entry_cost - (net_exit + exit_fee)

    return {
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "total_fees": total_fees,
        "gross_exit": gross_exit,
        "net_exit": net_exit,
        "net_pnl": net_pnl,
        "net_pnl_pct": (net_pnl / entry_cost * 100) if entry_cost > 0 else 0.0,
    }


# ── Ticker Parsing ─────────────────────────────────────────────────────────────

def parse_ticker_last(ticker: Any) -> Optional[float]:
    """
    Extract the last/current price from a Bitkub ticker response.

    Handles multiple response formats:
    - List with dict element: [{'last': ..., ...}, ...]
    - Dict with 'last' key: {'last': ..., ...}
    - Dict with 'close' key: {'close': ..., ...} (fallback)

    Returns:
        Price as float, or None if unavailable
    """
    if isinstance(ticker, list) and ticker:
        val = ticker[0].get("last")
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                return None
    elif isinstance(ticker, dict):
        for key in ("last", "close"):
            val = ticker.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return None
    return None
