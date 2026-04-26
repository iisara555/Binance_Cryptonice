"""
Minimal ROI Table - Freqtrade-Inspired Time-Based Profit Exit
=============================================================

Concept
-------
แทนที่จะรอ TP fixed % เสมอ — ถ้าถือครบ N นาทีแล้วได้กำไรถึง threshold → ออกได้เลย

ข้อดี:
- ลด holding time โดยไม่เสียกำไร
- ป้องกัน position ที่ "กำไรแล้วแต่รอ TP ไม่ถึง" กลับมาขาดทุน
- เพิ่ม win rate โดยรวม

Usage
-----
>>> roi = MinimalROI({"0": 0.03, "15": 0.015, "30": 0.008, "60": 0.004})
>>> # Hold 20m, profit 1.2% net → threshold at 15m row is 1.5%, not enough
>>> roi.should_exit(current_profit_pct=0.012, hold_minutes=20)
(False, '')
>>> # Hold 35m, profit 1.2% net → threshold at 30m row is 0.8%, exit!
>>> hit, reason = roi.should_exit(current_profit_pct=0.012, hold_minutes=35)
>>> hit
True

Priority in position monitoring loop:
    SL > MinimalROI > regular TP / time-stop

Fee Handling
------------
Binance.th charges 0.1% per side = 0.2% round-trip. The caller is expected to
pass NET profit (after fee deduction) into ``should_exit``. Use the
``compute_net_profit_pct`` helper for consistent fee handling across the bot.

# --- NEW: SPEC_06 Minimal ROI ---
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

BINANCE_TH_ROUND_TRIP_FEE: float = 0.002


class MinimalROI:
    """Freqtrade-inspired time-based profit threshold checker.

    The ROI table maps ``hold_minutes -> minimum_net_profit_pct``. As a
    position is held longer, the required profit to trigger an exit drops,
    so the bot bails out of stagnant winners before they round-trip back
    to break-even.

    The constructor accepts the raw mapping straight from ``bot_config.yaml``:
    keys are minute strings/ints, values are net-profit fractions (0.015 = 1.5%).

    Raises:
        ValueError: If no valid rows can be parsed from ``roi_config``.
    """

    __slots__ = ("_table",)

    def __init__(self, roi_config: Mapping) -> None:
        parsed: List[Tuple[int, float]] = []
        for raw_key, raw_val in roi_config.items():
            try:
                minutes = int(raw_key)
                threshold = float(raw_val)
            except (TypeError, ValueError):
                logger.warning(
                    "MinimalROI: ignoring invalid row %r=%r (need int minutes, float threshold)",
                    raw_key,
                    raw_val,
                )
                continue
            if minutes < 0:
                logger.warning("MinimalROI: ignoring negative minutes row %d", minutes)
                continue
            parsed.append((minutes, threshold))

        if not parsed:
            raise ValueError(
                "MinimalROI requires at least one valid {minutes: threshold} row"
            )

        parsed.sort(key=lambda row: row[0], reverse=True)
        self._table: Tuple[Tuple[int, float], ...] = tuple(parsed)

    def should_exit(
        self,
        current_profit_pct: float,
        hold_minutes: float,
    ) -> Tuple[bool, str]:
        """Decide whether the position should exit on its current ROI row.

        Args:
            current_profit_pct: Current NET profit fraction (after round-trip
                fees). 0.015 = 1.5%. Negative values mean a loss and never trigger.
            hold_minutes: Minutes the position has been held (float allowed).

        Returns:
            ``(True, reason)`` when the active threshold has been met,
            ``(False, "")`` otherwise.
        """
        for minutes, threshold in self._table:
            if hold_minutes >= minutes:
                if current_profit_pct >= threshold:
                    reason = (
                        f"MinimalROI: profit {current_profit_pct * 100:.2f}% "
                        f">= {threshold * 100:.2f}% threshold at {minutes}m mark"
                    )
                    return True, reason
                return False, ""

        return False, ""

    def get_current_threshold(self, hold_minutes: float) -> float:
        """Return the active net-profit threshold (fraction) for a hold time.

        ``float('inf')`` is returned when ``hold_minutes`` is below the
        smallest configured row, meaning no threshold applies yet.
        """
        for minutes, threshold in self._table:
            if hold_minutes >= minutes:
                return threshold
        return float("inf")

    @property
    def rows(self) -> Tuple[Tuple[int, float], ...]:
        """ROI table rows sorted by minutes (descending). Read-only."""
        return self._table

    def __repr__(self) -> str:
        rows_repr = ", ".join(f"{m}m={t * 100:.2f}%" for m, t in reversed(self._table))
        return f"MinimalROI({rows_repr})"


def compute_net_profit_pct(
    entry_price: float,
    current_price: float,
    *,
    side: str = "BUY",
    fee_round_trip: float = BINANCE_TH_ROUND_TRIP_FEE,
) -> float:
    """Net profit fraction after deducting round-trip fees.

    Centralises the (gross - fee) calculation so every caller (trading_bot,
    weekly_review, backtests) reports the same number.

    Args:
        entry_price: Position entry price.
        current_price: Latest market price.
        side: ``"BUY"`` for long, ``"SELL"`` for short. Defaults to BUY.
        fee_round_trip: Round-trip fee fraction. Defaults to Binance.th 0.2%.

    Returns:
        Net profit fraction. 0.015 means +1.5% after fees; negative on a loss.
        Returns 0.0 if ``entry_price`` is non-positive (defensive guard).
    """
    if entry_price <= 0:
        return 0.0
    gross = (current_price - entry_price) / entry_price
    if side.upper() == "SELL":
        gross = -gross
    return gross - fee_round_trip


def build_roi_tables(roi_config: Mapping) -> Dict[str, MinimalROI]:
    """Build ``{mode_name: MinimalROI}`` from a raw ``minimal_roi`` YAML block.

    Top-level scalar keys (``enabled: true``) and any malformed sub-tables are
    skipped with a warning, so a typo in one mode never disables the others.

    Example:
        >>> cfg = {
        ...     "enabled": True,
        ...     "scalping": {"0": 0.03, "30": 0.008},
        ...     "trend_only": {"0": 0.08, "120": 0.02},
        ... }
        >>> sorted(build_roi_tables(cfg).keys())
        ['scalping', 'trend_only']
    """
    tables: Dict[str, MinimalROI] = {}
    for mode_name, table in roi_config.items():
        if not isinstance(table, Mapping):
            continue
        try:
            tables[str(mode_name)] = MinimalROI(table)
        except ValueError as exc:
            logger.warning(
                "MinimalROI: skipping mode %r — invalid table (%s)", mode_name, exc
            )
    return tables


__all__ = [
    "BINANCE_TH_ROUND_TRIP_FEE",
    "MinimalROI",
    "build_roi_tables",
    "compute_net_profit_pct",
]
