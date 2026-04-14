"""
Trading Module Package
====================
Refactored trading components split from monolithic trading_bot.py

Modules:
- orchestrator: Main bot loop and coordination (BotMode, SignalSource, TradeDecision)
"""

# Core imports - always available
from trading.orchestrator import BotMode, SignalSource, TradeDecision

__all__ = [
    'BotMode',
    'SignalSource',
    'TradeDecision',
]

# Optional imports - may fail if dependencies not installed
try:
    from trading.position_manager import PositionManager
except ImportError:
    PositionManager = None  # type: ignore[assignment]
else:
    __all__.append('PositionManager')
