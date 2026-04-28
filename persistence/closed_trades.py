"""Closed-trade reads — thin wrapper for callers migrating off direct ``Database`` methods."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from database import Database
from models import ClosedTrade


class ClosedTradesRepository:
    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    def list_recent(
        self,
        symbol: Optional[str] = None,
        start_time: Optional[datetime] = None,
        limit: int = 50,
    ) -> List[ClosedTrade]:
        return self._db.get_closed_trades(symbol=symbol, start_time=start_time, limit=limit)
