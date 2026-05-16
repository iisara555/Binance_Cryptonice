"""
Float-oriented financial arithmetic for risk sizing and fills.
Keeps Decimal internally; surfaced as float where the rest of the bot expects floats.
"""

from decimal import ROUND_DOWN, Decimal

from execution.decimal_money import to_decimal


def precise_add(a: float, b: float) -> float:
    """Precise addition using Decimal to avoid float accumulation errors."""
    return float(to_decimal(a) + to_decimal(b))


def precise_subtract(a: float, b: float) -> float:
    """Precise subtraction using Decimal."""
    return float(to_decimal(a) - to_decimal(b))


def precise_multiply(a: float, b: float) -> float:
    """Precise multiplication using Decimal."""
    return float(to_decimal(a) * to_decimal(b))


def precise_divide(a: float, b: float) -> float:
    """Precise division using Decimal with safety check."""
    if b == 0 or b != b:
        return 0.0
    return float(to_decimal(a) / to_decimal(b))


def precise_round(value: float, decimals: int = 8) -> float:
    """Precise rounding for financial calculations."""
    quantize_str = "0." + "0" * decimals
    return float(to_decimal(value).quantize(Decimal(quantize_str), rounding=ROUND_DOWN))
