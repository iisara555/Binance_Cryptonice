"""
Trading Bot Orchestrator
========================
Main orchestrator that coordinates the trading bot execution flow.
Refactored from monolithic trading_bot.py into focused module.

Responsibilities:
- Main loop execution
- Component coordination
- Bot lifecycle management
- Mode handling (full_auto, semi_auto, dry_run)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from backtesting_validation import BacktestingValidator, ValidationStatus

if TYPE_CHECKING:
    from api_client import BinanceThClient
    from bitkub_websocket import BitkubWebSocket
    from monitoring import MonitoringService
    from risk_management import RiskManager
    from signal_generator import SignalGenerator
    from trade_executor import TradeExecutor

logger = logging.getLogger(__name__)


class BotMode(Enum):
    """Bot operation mode"""

    FULL_AUTO = "full_auto"  # Auto execute trades
    SEMI_AUTO = "semi_auto"  # Alert only, manual confirmation needed
    DRY_RUN = "dry_run"  # Simulate only, no orders


class SignalSource(Enum):
    """Supported signal sources."""

    STRATEGY = "strategy"


@dataclass
class TradeDecision:
    """A trade decision ready for execution or review"""

    plan: Any
    signal: Any
    risk_check: Any
    decision_time: datetime = None
    status: str = "pending"  # pending, approved, rejected, executed, failed
    signal_source: SignalSource = SignalSource.STRATEGY
