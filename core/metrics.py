"""
Prometheus Metrics Module
=======================
Prometheus metrics collection for monitoring trading bot performance.
Provides counters, gauges, and histograms for observability.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class MetricType(Enum):
    """Metric types supported"""

    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    SUMMARY = "summary"


@dataclass
class Metric:
    """Base metric definition"""

    name: str
    description: str
    metric_type: MetricType
    labels: Dict[str, str] = field(default_factory=dict)
    value: float = 0.0
    created_at: datetime = field(default_factory=datetime.now)


class PrometheusMetrics:
    """
    Lightweight Prometheus metrics collector.

    Features:
    - Counters for discrete events (orders, trades, errors)
    - Gauges for current values (balance, positions)
    - Histograms for distributions (latency, duration)
    - Exportable to Prometheus scrape format

    Usage:
        metrics = PrometheusMetrics()

        # Counters
        metrics.increment_counter("orders_placed_total", labels={"symbol": "BTC_THB"})
        metrics.increment_counter("orders_filled_total", labels={"side": "buy"})

        # Gauges
        metrics.set_gauge("portfolio_value_thb", 100000.0)
        metrics.set_gauge("open_positions", 3)

        # Histograms
        metrics.observe_histogram("order_latency_seconds", 0.5)

        # Export for Prometheus
        output = metrics.export()
    """

    def __init__(self):
        self._counters: Dict[str, float] = {}
        self._counter_labels: Dict[str, Dict[tuple, float]] = {}
        self._gauges: Dict[str, float] = {}
        self._gauge_labels: Dict[str, Dict[tuple, float]] = {}
        self._histograms: Dict[str, list] = {}
        self._histogram_labels: Dict[str, Dict[tuple, list]] = {}
        self._metadata: Dict[str, str] = {}
        self._last_export: float = time.time()

        logger.info("PrometheusMetrics initialized")

    def _make_label_tuple(self, labels: Dict[str, str]) -> tuple:
        """Convert label dict to sortable tuple"""
        return tuple(sorted(labels.items()))

    def _format_labels(self, labels: Dict[str, str]) -> str:
        """Format labels for Prometheus output"""
        if not labels:
            return ""
        return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"

    # ── Counter Operations ────────────────────────────────────────────────────

    def increment_counter(self, name: str, value: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        """
        Increment a counter metric.

        Args:
            name: Metric name (e.g., 'orders_total')
            value: Amount to increment (default: 1)
            labels: Optional label dict (e.g., {'symbol': 'BTC_THB', 'side': 'buy'})
        """
        if labels is None:
            self._counters[name] = self._counters.get(name, 0.0) + value
        else:
            if name not in self._counter_labels:
                self._counter_labels[name] = {}
            label_tuple = self._make_label_tuple(labels)
            self._counter_labels[name][label_tuple] = self._counter_labels[name].get(label_tuple, 0.0) + value

    def get_counter(self, name: str, labels: Optional[Dict[str, str]] = None) -> float:
        """Get current counter value"""
        if labels is None:
            return self._counters.get(name, 0.0)
        if name in self._counter_labels:
            label_tuple = self._make_label_tuple(labels)
            return self._counter_labels[name].get(label_tuple, 0.0)
        return 0.0

    # ── Gauge Operations ──────────────────────────────────────────────────────

    def set_gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """
        Set a gauge metric value.

        Args:
            name: Metric name (e.g., 'portfolio_value')
            value: New value
            labels: Optional label dict
        """
        if labels is None:
            self._gauges[name] = value
        else:
            if name not in self._gauge_labels:
                self._gauge_labels[name] = {}
            label_tuple = self._make_label_tuple(labels)
            self._gauge_labels[name][label_tuple] = value

    def get_gauge(self, name: str, labels: Optional[Dict[str, str]] = None) -> float:
        """Get current gauge value"""
        if labels is None:
            return self._gauges.get(name, 0.0)
        if name in self._gauge_labels:
            label_tuple = self._make_label_tuple(labels)
            return self._gauge_labels[name].get(label_tuple, 0.0)
        return 0.0

    def increment_gauge(self, name: str, value: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        """Increment a gauge value"""
        current = self.get_gauge(name, labels)
        self.set_gauge(name, current + value, labels)

    def decrement_gauge(self, name: str, value: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        """Decrement a gauge value"""
        current = self.get_gauge(name, labels)
        self.set_gauge(name, current - value, labels)

    # ── Histogram Operations ─────────────────────────────────────────────────

    def observe_histogram(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """
        Observe a value for histogram.

        Args:
            name: Metric name (e.g., 'order_latency_seconds')
            value: Observed value
            labels: Optional label dict
        """
        if labels is None:
            if name not in self._histograms:
                self._histograms[name] = []
            self._histograms[name].append(value)
            # Keep last 1000 observations
            if len(self._histograms[name]) > 1000:
                self._histograms[name] = self._histograms[name][-1000:]
        else:
            if name not in self._histogram_labels:
                self._histogram_labels[name] = {}
            label_tuple = self._make_label_tuple(labels)
            if label_tuple not in self._histogram_labels[name]:
                self._histogram_labels[name][label_tuple] = []
            self._histogram_labels[name][label_tuple].append(value)
            if len(self._histogram_labels[name][label_tuple]) > 1000:
                self._histogram_labels[name][label_tuple] = self._histogram_labels[name][label_tuple][-1000:]

    def get_histogram_stats(self, name: str, labels: Optional[Dict[str, str]] = None) -> Dict[str, float]:
        """Get histogram statistics (count, sum, mean, p50, p95, p99)"""
        if labels is None:
            values = self._histograms.get(name, [])
        else:
            if name in self._histogram_labels:
                label_tuple = self._make_label_tuple(labels)
                values = self._histogram_labels[name].get(label_tuple, [])
            else:
                values = []

        if not values:
            return {"count": 0, "sum": 0, "mean": 0, "p50": 0, "p95": 0, "p99": 0}

        sorted_values = sorted(values)
        n = len(sorted_values)

        return {
            "count": n,
            "sum": sum(sorted_values),
            "mean": sum(sorted_values) / n,
            "p50": sorted_values[int(n * 0.50)] if n > 0 else 0,
            "p95": sorted_values[int(n * 0.95)] if n > 0 else 0,
            "p99": sorted_values[int(n * 0.99)] if n > 0 else 0,
            "min": sorted_values[0],
            "max": sorted_values[-1],
        }

    # ── Metadata ─────────────────────────────────────────────────────────────

    def set_help(self, name: str, description: str) -> None:
        """Set metric help/description"""
        self._metadata[f"{name}_help"] = description

    # ── Export ────────────────────────────────────────────────────────────────

    def export(self) -> str:
        """
        Export all metrics in Prometheus text format.

        Returns:
            String in Prometheus scrape format
        """
        lines = []
        timestamp = int(time.time() * 1000)

        # Export counters
        for name, value in sorted(self._counters.items()):
            help_text = self._metadata.get(f"{name}_help", "")
            if help_text:
                lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {value} {timestamp}")

        # Export labeled counters
        for name, label_values in sorted(self._counter_labels.items()):
            help_text = self._metadata.get(f"{name}_help", "")
            if help_text:
                lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            for label_tuple, value in sorted(label_values.items()):
                labels = dict(label_tuple)
                label_str = self._format_labels(labels)
                lines.append(f"{name}{label_str} {value} {timestamp}")

        # Export gauges
        for name, value in sorted(self._gauges.items()):
            help_text = self._metadata.get(f"{name}_help", "")
            if help_text:
                lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value} {timestamp}")

        # Export labeled gauges
        for name, label_values in sorted(self._gauge_labels.items()):
            help_text = self._metadata.get(f"{name}_help", "")
            if help_text:
                lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            for label_tuple, value in sorted(label_values.items()):
                labels = dict(label_tuple)
                label_str = self._format_labels(labels)
                lines.append(f"{name}{label_str} {value} {timestamp}")

        # Export histograms
        for name in sorted(self._histograms.keys()):
            stats = self.get_histogram_stats(name)
            help_text = self._metadata.get(f"{name}_help", "")
            if help_text:
                lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} histogram")
            # Bucket boundaries
            for bucket in [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]:
                bucket_count = sum(1 for v in self._histograms[name] if v <= bucket)
                lines.append(f'{name}_bucket{{le="{bucket}"}} {bucket_count} {timestamp}')
            lines.append(f'{name}_bucket{{le="+Inf"}} {stats["count"]} {timestamp}')
            lines.append(f"{name}_sum {stats['sum']} {timestamp}")
            lines.append(f"{name}_count {stats['count']} {timestamp}")

        self._last_export = time.time()
        return "\n".join(lines)

    def reset(self) -> None:
        """Reset all metrics"""
        self._counters.clear()
        self._counter_labels.clear()
        self._gauges.clear()
        self._gauge_labels.clear()
        self._histograms.clear()
        self._histogram_labels.clear()
        logger.info("PrometheusMetrics reset")

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of all metrics"""
        return {
            "total_counters": len(self._counters) + sum(len(v) for v in self._counter_labels.values()),
            "total_gauges": len(self._gauges) + sum(len(v) for v in self._gauge_labels.values()),
            "total_histograms": len(self._histograms) + sum(len(v) for v in self._histogram_labels.values()),
            "last_export_seconds_ago": time.time() - self._last_export,
        }


# ── Predefined Trading Metrics ───────────────────────────────────────────────


def create_trading_metrics(metrics: PrometheusMetrics) -> None:
    """
    Initialize predefined trading metrics with descriptions.

    Call this once at startup to register all metric metadata.
    """
    # Order metrics
    metrics.set_help("orders_placed_total", "Total number of orders placed")
    metrics.set_help("orders_filled_total", "Total number of orders filled")
    metrics.set_help("orders_cancelled_total", "Total number of orders cancelled")
    metrics.set_help("orders_failed_total", "Total number of failed orders")

    # Latency metrics
    metrics.set_help("order_latency_seconds", "Order placement latency in seconds")
    metrics.set_help("api_latency_seconds", "API request latency in seconds")
    metrics.set_help("websocket_latency_seconds", "WebSocket message processing latency")

    # Balance metrics
    metrics.set_help("portfolio_value_thb", "Total portfolio value in THB")
    metrics.set_help("available_balance_thb", "Available THB balance")
    metrics.set_help("reserved_balance_thb", "Reserved balance in orders")

    # Position metrics
    metrics.set_help("open_positions", "Number of open positions")
    metrics.set_help("position_value_thb", "Total value of open positions")

    # PnL metrics
    metrics.set_help("daily_pnl_thb", "Daily profit/loss in THB")
    metrics.set_help("total_pnl_thb", "Total profit/loss in THB")
    metrics.set_help("win_rate", "Percentage of winning trades")

    # Circuit breaker metrics
    metrics.set_help("circuit_breaker_state", "Circuit breaker state (0=closed, 1=open, 2=half)")
    metrics.set_help("api_errors_total", "Total API errors")

    # Signal metrics
    metrics.set_help("signals_generated_total", "Total signals generated")
    metrics.set_help("signals_executed_total", "Total signals that led to execution")

    logger.info("Trading metrics initialized")


# ── Global Metrics Instance ─────────────────────────────────────────────────

_global_metrics: Optional[PrometheusMetrics] = None


def get_metrics() -> PrometheusMetrics:
    """Get or create global metrics instance"""
    global _global_metrics
    if _global_metrics is None:
        _global_metrics = PrometheusMetrics()
    return _global_metrics


# ── Convenience Functions ────────────────────────────────────────────────────


def record_order_placed(symbol: str, side: str, amount: float) -> None:
    """Record an order placement"""
    m = get_metrics()
    m.increment_counter("orders_placed_total", labels={"symbol": symbol, "side": side})
    m.increment_gauge("open_positions", 1, labels={"symbol": symbol})


def record_order_filled(symbol: str, side: str, filled_price: float, filled_amount: float) -> None:
    """Record an order fill"""
    m = get_metrics()
    m.increment_counter("orders_filled_total", labels={"symbol": symbol, "side": side})
    m.decrement_gauge("open_positions", 1, labels={"symbol": symbol})


def record_trade_pnl(pnl_thb: float, is_win: bool) -> None:
    """Record trade PnL"""
    m = get_metrics()
    m.increment_counter("trades_total", labels={"result": "win" if is_win else "loss"})
    m.increment_gauge("total_pnl_thb", pnl_thb)


def record_api_latency(endpoint: str, latency_seconds: float) -> None:
    """Record API request latency"""
    m = get_metrics()
    m.observe_histogram("api_latency_seconds", latency_seconds, labels={"endpoint": endpoint})


def record_circuit_breaker_state(state: str) -> None:
    """Record circuit breaker state"""
    state_map = {"closed": 0, "open": 1, "half_open": 2}
    m = get_metrics()
    m.set_gauge("circuit_breaker_state", state_map.get(state, 0))
