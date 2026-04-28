"""
Decimal coercion for order sizing and fills (OMS boundary).
Extracted from trade_executor for clearer domain separation.
"""

from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

# Binance Thailand fee model: 0.2% round trip = 0.1% per side.
BINANCE_TH_ROUND_TRIP_FEE = 0.002
BINANCE_TH_FEE_PCT = BINANCE_TH_ROUND_TRIP_FEE / 2

# Backward-compatible name used across runtime PnL paths.
BITKUB_FEE_PCT = BINANCE_TH_FEE_PCT


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            return Decimal("0")
        return value
    if value is None:
        return Decimal("0")
    if isinstance(value, float) and (value != value or value == float("inf") or value == float("-inf")):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def quantize_decimal(value: Any, decimals: int) -> Decimal:
    digits = max(0, int(decimals or 0))
    quantizer = Decimal("1") if digits == 0 else Decimal("1").scaleb(-digits)
    return to_decimal(value).quantize(quantizer, rounding=ROUND_DOWN)
