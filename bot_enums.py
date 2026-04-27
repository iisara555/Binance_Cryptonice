"""
Small project-local enums for order types and related spot-bot concepts.

Keeps trading code free of optional Freqtrade packages.
"""

from __future__ import annotations

from enum import Enum


class SpotOrderType(str, Enum):
    """Normalized spot order type strings used with Binance.th REST."""

    LIMIT = "limit"
    MARKET = "market"
