"""
Shared Helper Utilities — Phase 3 code quality cleanup.

Provides common functions used across multiple modules to reduce
duplication and improve consistency:
- Price fetching (WebSocket cache → REST fallback)
- Balance lookups (quote / base asset with safe defaults)
- Price formatting / logging helpers
- Typed aliases for common return types
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

THAILAND_TIMEZONE = timezone(timedelta(hours=7), name="ICT")


def now_exchange_time() -> datetime:
    """Return the current time in the exchange-local Thailand timezone."""
    return datetime.now(timezone.utc).astimezone(THAILAND_TIMEZONE)


def parse_as_exchange_time(value: Any) -> Optional[datetime]:
    """Normalize a datetime-like value to the exchange-local Thailand timezone.

    Naive runtime timestamps are treated as UTC so VPS-rendered CLI times stay
    aligned with Binance Thailand operating time.
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
    return dt.astimezone(THAILAND_TIMEZONE)


def format_exchange_time(value: Any, fmt: str = "%H:%M:%S") -> str:
    """Format a datetime-like value in the exchange-local Thailand timezone."""
    if not value:
        return "-"
    try:
        dt = parse_as_exchange_time(value)
    except (TypeError, ValueError):
        return str(value)
    if dt is None:
        return "-"
    return dt.strftime(fmt)



# ── Price Fetching ──────────────────────────────────────────────────────────────

_WS_TICK_MAX_AGE_SECONDS = 30.0  # fallback to REST if WS tick is older than this


def get_current_price(
    symbol: str,
    api_client: Any = None,
    ws_client: Any = None,
) -> Tuple[Optional[float], str]:
    """
    Get the current market price for a symbol using fastest available source.

    Priority: WebSocket cache (fresh) → REST API ticker → stale WebSocket cache → None

    Args:
        symbol: Trading pair symbol (e.g. 'BTCUSDT', 'THB_BTC', or 'BTC_THB')
        api_client: BinanceThClient-compatible instance (None to skip REST fallback)
        ws_client: WebSocket client instance (None to skip WS lookup)

    Returns:
        (price, source) tuple where source is one of:
        - 'ws' — live WebSocket data (fresh)
        - 'ws_stale' — WebSocket data older than _WS_TICK_MAX_AGE_SECONDS (used only when REST unavailable)
        - 'rest' — REST API fallback
        - 'none' — unavailable
    """
    import time as _time

    # Try WebSocket cache first — only accept fresh ticks
    stale_ws_price: Optional[float] = None
    if ws_client is not None:
        try:
            ticker_getter = getattr(ws_client, "get_latest_ticker", None)
            tick = ticker_getter(symbol) if callable(ticker_getter) else None
            if tick is None:
                # Module-level lookup — use Binance native WS adapter.
                try:
                    from binance_websocket import get_latest_ticker as _bn_get  # type: ignore

                    tick = _bn_get(symbol)
                except Exception:
                    tick = None
            if tick and getattr(tick, "last", 0) > 0:
                tick_age = _time.time() - getattr(tick, "timestamp", 0.0)
                if tick_age <= _WS_TICK_MAX_AGE_SECONDS:
                    return float(tick.last), "ws"
                # Keep stale value as last-resort fallback only when REST is off
                stale_ws_price = float(tick.last)
        except Exception as exc:
            logger.debug("WS ticker lookup failed for %s: %s", symbol, exc)

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

    # Last resort: return stale WS price (better than None during reconnect window)
    if stale_ws_price is not None:
        return stale_ws_price, "ws_stale"

    return None, "none"


# ── Balance Helpers ────────────────────────────────────────────────────────────


def get_balance(
    api_client: Any,
    asset: str,
    default: float = 0.0,
) -> float:
    """
    Safely get available balance for an asset from the active exchange API.

    Handles both nested dict (v3 API) and flat dict formats.
    Caches the result per call (no internal cache) — API is always hit.

    Args:
        api_client: BinanceThClient-compatible instance
        asset: Asset symbol (e.g. 'USDT', 'THB', 'BTC', 'XAUT')
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



def get_quote_balance(api_client: Any, quote_asset: str = "USDT", default: float = 0.0) -> float:
    """Get quote-asset balance, defaulting to Binance-style USDT."""
    return get_balance(api_client, quote_asset, default)


def get_crypto_balance(
    api_client: Any,
    symbol: str,
    default: float = 0.0,
) -> float:
    """
    Get crypto asset balance from a trading pair symbol.

    Examples:
        'BTCUSDT' → BTC balance
        'THB_BTC' → BTC balance
        'BTC_THB' → BTC balance (auto-extracts base asset)
    """
    normalized = str(symbol or "").strip().upper()
    for quote in ("USDT", "THB"):
        if normalized.endswith(quote) and "_" not in normalized and len(normalized) > len(quote):
            return get_balance(api_client, normalized[: -len(quote)], default)

    # Extract base asset: 'THB_BTC' → 'BTC', 'BTC_THB' → 'BTC'
    parts = normalized.split("_")
    base = parts[1] if len(parts) == 2 else normalized

    # THB pairs: exclude quote currency from result
    if base in ("THB", "USDT"):
        # For 'THB_BTC', base asset is at index 1
        if len(parts) == 2 and parts[0] in {"THB", "USDT"}:
            base = parts[1]

    return get_balance(api_client, base, default)


# ── Symbol Formatting ─────────────────────────────────────────────────────────


def normalize_symbol(symbol: str) -> str:
    """
    Normalize a trading pair symbol to standard format.

    Ensures pair strings have consistent casing:
        btc_thb  → BTC_THB
        THB_btc  → THB_BTC
    """
    parts = symbol.upper().split("_")
    return "_".join(parts)


def extract_base_asset(symbol: str) -> str:
    """
    Extract the base (non-quote) asset from a trading pair.

    Examples:
        'BTCUSDT' → 'BTC'
        'THB_BTC' → 'BTC'
        'BTC_THB' → 'BTC'
        'THB_XAUT' → 'XAUT'
    """
    normalized = str(symbol or "").upper()
    for quote in ("USDT", "THB"):
        if normalized.endswith(quote) and "_" not in normalized and len(normalized) > len(quote):
            return normalized[: -len(quote)]
    parts = normalized.split("_")
    if len(parts) == 2:
        return parts[0] if parts[1] in {"THB", "USDT"} else parts[1]
    return normalized


def normalize_side_value(side: Any, default: str = "") -> str:
    """Normalize side-like values to lowercase strings (e.g. 'buy'/'sell')."""
    normalized = str(getattr(side, "value", side) or "").strip().lower()
    return normalized or str(default or "").strip().lower()


def symbol_for_api(symbol: str) -> str:
    """
    Format symbol for exchange API calls.

    The Binance client accepts both Binance symbols and legacy internal pairs,
    then maps them to Binance Thailand symbols.
    """
    return normalize_symbol(symbol)


def calc_net_pnl(
    entry_cost: float,
    exit_price: float,
    quantity: float,
    side: str,
    fee_pct: float = 0.001,
) -> Dict[str, float]:
    """
    Calculate net P/L after exchange fees (default: Binance TH 0.1% per side).

    Returns dict with:
        entry_fee, exit_fee, total_fees, gross_exit, net_exit, net_pnl, net_pnl_pct
    """
    entry_fee = entry_cost * fee_pct
    gross_exit = exit_price * quantity
    exit_fee = gross_exit * fee_pct
    total_fees = entry_fee + exit_fee
    net_exit = gross_exit - exit_fee
    normalized_side = str(getattr(side, "value", side) or "").lower()

    if normalized_side == "buy":
        net_pnl = net_exit - entry_cost - entry_fee
    else:
        net_pnl = (entry_cost - gross_exit) - entry_fee

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
    Extract the last/current price from an exchange ticker response.

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
