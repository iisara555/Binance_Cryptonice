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

import time
import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum

from backtesting_validation import BacktestingValidator, ValidationStatus

if TYPE_CHECKING:
    from api_client import BinanceThClient
    from signal_generator import SignalGenerator
    from risk_management import RiskManager
    from trade_executor import TradeExecutor
    from monitoring import MonitoringService
    from bitkub_websocket import BitkubWebSocket

logger = logging.getLogger(__name__)

class BotMode(Enum):
    """Bot operation mode"""
    FULL_AUTO = "full_auto"   # Auto execute trades
    SEMI_AUTO = "semi_auto"   # Alert only, manual confirmation needed
    DRY_RUN = "dry_run"       # Simulate only, no orders

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
    
