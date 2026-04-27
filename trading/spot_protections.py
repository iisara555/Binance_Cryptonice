"""
Lightweight spot protections (consecutive loss cooldown per pair) — complements :class:`risk_management.RiskManager`.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class PairLossStreakGuard:
    """
    After ``max_consecutive_losses`` realized losses in a row for a symbol, block new entries
    until ``cooldown_minutes`` elapse (wall clock from the blocking loss).
    """

    def __init__(self, *, max_consecutive_losses: int = 3, cooldown_minutes: float = 60.0) -> None:
        self._max = max(1, int(max_consecutive_losses or 1))
        self._cooldown_s = max(60.0, float(cooldown_minutes or 0) * 60.0)
        self._lock = threading.Lock()
        self._streak: Dict[str, int] = {}
        self._blocked_until: Dict[str, float] = {}

    def record_closed_pnl(self, symbol: str, net_pnl: float) -> None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        with self._lock:
            now = time.monotonic()
            if sym in self._blocked_until and now >= self._blocked_until[sym]:
                del self._blocked_until[sym]
                self._streak[sym] = 0
            if net_pnl >= 0:
                self._streak[sym] = 0
                return
            self._streak[sym] = self._streak.get(sym, 0) + 1
            if self._streak[sym] >= self._max:
                self._blocked_until[sym] = now + self._cooldown_s
                self._streak[sym] = 0

    def is_blocked(self, symbol: str) -> bool:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return False
        with self._lock:
            until = self._blocked_until.get(sym)
            if until is None:
                return False
            now = time.monotonic()
            if now >= until:
                del self._blocked_until[sym]
                self._streak[sym] = 0
                return False
            return True

    def block_reason(self, symbol: str) -> str:
        sym = str(symbol or "").strip().upper()
        with self._lock:
            until = self._blocked_until.get(sym)
            if until is None:
                return ""
            remain = max(0.0, until - time.monotonic())
            return f"pair loss cooldown ({remain / 60.0:.1f} min remaining)"


def build_pair_loss_streak_guard(config: Optional[Dict[str, Any]] = None) -> Optional[PairLossStreakGuard]:
    cfg = dict(config or {})
    prot = dict((cfg.get("risk") or {}).get("protections") or {})
    max_streak = int(prot.get("max_consecutive_losses_per_pair", 0) or 0)
    if max_streak <= 0:
        return None
    cooldown = float(prot.get("pair_loss_cooldown_minutes", 60.0) or 60.0)
    return PairLossStreakGuard(max_consecutive_losses=max_streak, cooldown_minutes=cooldown)
