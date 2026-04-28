"""
Single read/write surface for trade lifecycle state (CLI + OMS).
Delegates to ``TradeStateManager`` — prefer this over ad-hoc ``get_state`` from many modules.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from state_management import TradeStateManager

logger = logging.getLogger(__name__)


class TradeStateFacade:
    __slots__ = ("_m",)

    def __init__(self, manager: TradeStateManager) -> None:
        self._m = manager

    @property
    def inner(self) -> TradeStateManager:
        """Escape hatch for full API (managed lifecycle, tests). Prefer facade methods for new code."""
        return self._m

    def enrich_sl_tp_from_snapshot(
        self,
        symbol: str,
        stop_loss: Any,
        take_profit: Any,
    ) -> Tuple[Any, Any]:
        """Fill missing SL/TP from persisted state machine snapshot (e.g. CLI positions panel)."""
        pos_sl, pos_tp = stop_loss, take_profit
        if not symbol:
            return pos_sl, pos_tp
        need_sl = not pos_sl or float(pos_sl or 0) == 0
        need_tp = not pos_tp or float(pos_tp or 0) == 0
        if not need_sl and not need_tp:
            return pos_sl, pos_tp
        try:
            snapshot = self._m.get_state(symbol)
            if need_sl:
                pos_sl = snapshot.stop_loss if snapshot.stop_loss else pos_sl
            if need_tp:
                pos_tp = snapshot.take_profit if snapshot.take_profit else pos_tp
        except Exception as exc:
            logger.debug("[StateFacade] enrich_sl_tp_from_snapshot failed for %s: %s", symbol, exc)
        return pos_sl, pos_tp
