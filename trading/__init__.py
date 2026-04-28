"""
Trading Module Package
====================
Refactored trading components split from monolithic trading_bot.py

Modules:
- orchestrator: BotMode, SignalSource, TradeDecision
- coercion: Shared DB/API float coercion (``coerce_trade_float``)
- bot_runtime/: slices delegated from ``TradingBotOrchestrator`` (loop, WS, deps, pause, …)
- execution_runtime, signal_runtime, portfolio_runtime, position_monitor, …: runtime helpers
"""

# Core imports - always available
from trading.coercion import coerce_trade_float
from trading.orchestrator import BotMode, SignalSource, TradeDecision

__all__ = [
    "BotMode",
    "SignalSource",
    "TradeDecision",
    "coerce_trade_float",
]

# Optional imports - may fail if dependencies not installed
try:
    from trading.position_manager import PositionManager
except ImportError:
    PositionManager = None  # type: ignore[assignment]
else:
    __all__.append("PositionManager")
