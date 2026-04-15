"""
Monitoring Service Module
=========================
Health checks, heartbeats, and reconciliation for the trading bot.

This module provides the MonitoringService class that monitors:
- Bot health and uptime
- API connection status
- Order reconciliation
- Position monitoring
- Alert system health
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Dict, Any, List

if TYPE_CHECKING:
    from trading_bot import TradingBotOrchestrator
    from api_client import BitkubClient
    from trade_executor import TradeExecutor

logger = logging.getLogger(__name__)


class MonitoringService:
    """
    Monitoring service for the trading bot.
    Provides health checks, heartbeats, and order reconciliation.
    """
    
    def __init__(
        self,
        bot_ref: "TradingBotOrchestrator",
        api_client: "BitkubClient",
        executor: "TradeExecutor",
        config: Dict[str, Any],
        alert_sender=None,
        start_time: datetime = None,
    ):
        """
        Initialize the monitoring service.
        
        Args:
            bot_ref: Reference to the TradingBotOrchestrator
            api_client: Bitkub API client
            executor: TradeExecutor instance
            config: Bot configuration dict
            alert_sender: Function to send alerts
            start_time: Bot start time for uptime calculation
        """
        self.bot_ref = bot_ref
        self.api_client = api_client
        self.executor = executor
        self.config = config
        self.alert_sender = alert_sender
        self.start_time = start_time or datetime.now()
        
        # Monitoring configuration
        monitoring_config = config.get("monitoring", {})
        self.enabled = monitoring_config.get("enabled", True)
        self.interval_seconds = monitoring_config.get("interval_seconds", 60)
        self.reconciliation_enabled = monitoring_config.get("reconciliation", {}).get("enabled", True)
        
        # State
        self.running = False
        self._monitor_thread = None
        self._loop_count = 0
        
        # Reconciliation state
        self._reconciler = ReconciliationState()
        
        logger.info(
            f"MonitoringService initialized | "
            f"Enabled: {self.enabled} | "
            f"Reconciliation: {self.reconciliation_enabled}"
        )
    
    def start(self):
        """Start the monitoring service in a background thread."""
        if self.running:
            logger.warning("MonitoringService is already running")
            return
        
        self.running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="MonitoringThread"
        )
        self._monitor_thread.start()
        logger.info("MonitoringService started")
    
    def stop(self):
        """Stop the monitoring service gracefully."""
        logger.info("Stopping MonitoringService...")
        self.running = False
        
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=10)
        
        logger.info("MonitoringService stopped")
    
    def _monitor_loop(self):
        """Main monitoring loop."""
        while self.running:
            try:
                self._loop_count += 1
                self._run_health_check()
                
                if self.reconciliation_enabled:
                    self._run_reconciliation()
                    
            except Exception as e:
                logger.error(f"Monitoring loop error: {e}", exc_info=True)
            
            time.sleep(self.interval_seconds)
    
    def _run_health_check(self):
        """Run health check on bot components."""
        try:
            # Check API connectivity
            api_ok = self._check_api_health()
            
            # Check executor state
            executor_state = self._check_executor_health()
            
            # Log health status
            if self._loop_count % 10 == 0:  # Log every 10th iteration
                logger.debug(
                    f"Health check | API: {'OK' if api_ok else 'FAIL'} | "
                    f"Executor: {executor_state}"
                )
                
        except Exception as e:
            logger.error(f"Health check failed: {e}")
    
    def _check_api_health(self) -> bool:
        """Check if API is responsive."""
        try:
            # Simple API ping check
            ticker = self.api_client.get_ticker("THB_BTC")
            return ticker is not None
        except Exception as exc:
            logger.warning("API health check failed: %s", exc)
            return False
    
    def _check_executor_health(self) -> str:
        """Check executor state."""
        try:
            open_orders = self.executor.get_open_orders()
            return f"{len(open_orders)} open orders"
        except Exception as e:
            return f"Error: {e}"
    
    def _run_reconciliation(self):
        """Run order reconciliation to detect discrepancies."""
        try:
            self._reconciler.check_positions(self.executor, self.api_client)
        except Exception as e:
            logger.error(f"Reconciliation failed: {e}")
    
    def get_status(self) -> Dict[str, Any]:
        """Get monitoring status."""
        uptime = (datetime.now() - self.start_time).total_seconds()
        auth_degraded = {
            "active": bool(getattr(self.bot_ref, "_auth_degraded", False)),
            "reason": str(getattr(self.bot_ref, "_auth_degraded_reason", "") or ""),
        }
        
        # Get circuit breaker status
        cb_state = "unknown"
        cb_failure_count = 0
        try:
            if hasattr(self.api_client, 'circuit_breaker'):
                cb_state = self.api_client.circuit_breaker.state
                cb_failure_count = self.api_client.circuit_breaker._failure_count
        except Exception as exc:
            logger.warning("Failed to read circuit breaker status: %s", exc)
        
        return {
            "running": self.running,
            "enabled": self.enabled,
            "uptime_seconds": uptime,
            "loop_count": self._loop_count,
            "circuit_breaker": {
                "state": cb_state,
                "failure_count": cb_failure_count,
                "is_open": cb_state == "open"
            },
            "auth_degraded": auth_degraded,
            "reconciliation": {
                "enabled": self.reconciliation_enabled,
                "paused": self._reconciler.is_paused()[0],
                "issues": self._reconciler.get_issues()
            }
        }


class ReconciliationState:
    """
    State manager for order reconciliation.
    Tracks discrepancies between bot state and exchange state.
    """
    
    def __init__(self):
        self._paused = False
        self._pause_reason = ""
        self._issues: List[str] = []
    
    def is_paused(self) -> tuple:
        """Return (is_paused, reason)."""
        return self._paused, self._pause_reason
    
    def pause(self, reason: str):
        """Pause trading due to reconciliation issues."""
        self._paused = True
        self._pause_reason = reason
        logger.warning(f"Trading PAUSED: {reason}")
    
    def resume(self):
        """Resume trading after reconciliation."""
        self._paused = False
        self._pause_reason = ""
        self._issues.clear()
        logger.info("Trading RESUMED - reconciliation complete")
    
    def add_issue(self, issue: str):
        """Add a reconciliation issue."""
        self._issues.append(issue)
        logger.warning(f"Reconciliation issue: {issue}")
    
    def get_issues(self) -> List[str]:
        """Get all reconciliation issues."""
        return self._issues.copy()

    @staticmethod
    def _normalize_symbol(value: Any) -> str:
        text = str(value or "").strip().upper()
        if not text:
            return ""
        if text.startswith("THB_"):
            return text
        if text.endswith("_THB"):
            return f"THB_{text.split('_', 1)[0]}"
        return text

    @classmethod
    def _extract_symbol(cls, order: Any) -> str:
        if not isinstance(order, dict):
            return ""
        return (
            cls._normalize_symbol(order.get("symbol"))
            or cls._normalize_symbol(order.get("_checked_symbol"))
            or cls._normalize_symbol(order.get("pair"))
            or cls._normalize_symbol(order.get("sym"))
        )

    @staticmethod
    def _extract_order_id(order: Any) -> str:
        if not isinstance(order, dict):
            return ""
        value = order.get("order_id") or order.get("id")
        return str(value or "").strip()

    def _fetch_exchange_orders(self, bot_positions: List[Any], api_client: Any) -> List[Any]:
        symbols = sorted({self._extract_symbol(position) for position in bot_positions if self._extract_symbol(position)})
        if not symbols:
            symbols = [""]

        remote_orders: List[Any] = []
        seen_order_ids: set[str] = set()
        for symbol in symbols:
            try:
                rows = api_client.get_open_orders(symbol or None)
            except TypeError:
                rows = api_client.get_open_orders()
            rows = list(rows or [])
            for row in rows:
                order_id = self._extract_order_id(row)
                if order_id and order_id in seen_order_ids:
                    continue
                if order_id:
                    seen_order_ids.add(order_id)
                remote_orders.append(row)
        return remote_orders

    def _replace_issues(self, issues: List[str]) -> None:
        previous = set(self._issues)
        self._issues = list(issues)
        for issue in self._issues:
            if issue not in previous:
                logger.warning("Reconciliation issue: %s", issue)
    
    def check_positions(self, executor, api_client):
        """
        Check if bot positions match exchange positions.
        Pause trading if discrepancies are found.
        """
        try:
            # Get bot's view of positions
            bot_positions = list(executor.get_open_orders() or [])
            issues: List[str] = []

            if len(bot_positions) > 10:
                issues.append(f"Unusually high position count: {len(bot_positions)}")

            remote_orders = self._fetch_exchange_orders(bot_positions, api_client)
            bot_order_ids = {self._extract_order_id(order) for order in bot_positions if self._extract_order_id(order)}
            remote_order_ids = {self._extract_order_id(order) for order in remote_orders if self._extract_order_id(order)}
            if bot_order_ids and remote_order_ids:
                missing_on_exchange = sorted(bot_order_ids - remote_order_ids)
                unexpected_on_exchange = sorted(remote_order_ids - bot_order_ids)
                if missing_on_exchange:
                    issues.append(f"Bot orders missing on exchange: {', '.join(missing_on_exchange[:3])}")
                if unexpected_on_exchange:
                    issues.append(f"Exchange-only open orders detected: {', '.join(unexpected_on_exchange[:3])}")
            elif len(bot_positions) != len(remote_orders):
                issues.append(
                    f"Open-order count mismatch: bot={len(bot_positions)} exchange={len(remote_orders)}"
                )

            self._replace_issues(issues)
            if issues:
                if not self._paused or self._pause_reason != issues[0]:
                    self.pause(issues[0])
            elif self._paused:
                self.resume()
                
        except Exception as e:
            self._replace_issues([f"Position check failed: {e}"])
            if not self._paused or self._pause_reason != self._issues[0]:
                self.pause(self._issues[0])
