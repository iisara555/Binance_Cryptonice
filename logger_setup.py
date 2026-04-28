"""Advanced logging configuration for Crypto Bot V1.

Provides a single logging stack for terminal output, structured file logs,
and lightweight metrics collection.  Includes automatic cleanup of old
rotated log files that exceed retention limits.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from rich.console import Console
    from rich.logging import RichHandler

    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - fallback when rich is not installed yet
    Console = None  # type: ignore[assignment]
    RichHandler = None  # type: ignore[assignment]
    _RICH_AVAILABLE = False


# ── Default constants (overridable via YAML config) ──────────────────────────
MAX_LOG_SIZE = 100 * 1024 * 1024  # 100 MB per file
BACKUP_COUNT = 10  # rotated copies for size-based
DEBUG_RETENTION_DAYS = 30  # keep daily debug logs this many days
ERROR_RETENTION_DAYS = 90  # keep error.log backups this many days
TRADE_RETENTION_DAYS = 90  # keep trades.log backups this many days
WATCHDOG_MAX_SIZE = 10 * 1024 * 1024  # 10 MB for watchdog.log
WATCHDOG_BACKUP_COUNT = 3

LOG_DIRECTORY = "logs"
ERROR_LOG_FILE = "error.log"
DEBUG_LOG_FILE = "debug.log"
ACCESS_LOG_FILE = "access.log"
TRADE_LOG_FILE = "trades.log"
METRICS_LOG_FILE = "metrics.log"
WATCHDOG_LOG_FILE = "watchdog.log"

_global_logger: Optional[logging.Logger] = None
_shared_console: Optional[Any] = None

_LEVEL_BADGES = {
    "DEBUG": "DBG",
    "INFO": "INF",
    "WARNING": "WRN",
    "ERROR": "ERR",
    "CRITICAL": "CRT",
}

_LEVEL_COLORS = {
    "DEBUG": "\033[38;5;110m",
    "INFO": "\033[38;5;114m",
    "WARNING": "\033[38;5;221m",
    "ERROR": "\033[38;5;203m",
    "CRITICAL": "\033[1;38;5;199m",
}

_LOGGER_ALIASES = {
    "root": "core",
    "__main__": "app",
    "main": "app",
    "trading_bot": "bot",
    "data_collector": "collector",
    "telegram_bot": "telegram",
    "alerts": "alerts",
    "database": "database",
    "signal_generator": "signal",
}


def _ansi_enabled(stream: Optional[Any] = None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    target = stream or sys.stderr
    return bool(getattr(target, "isatty", lambda: False)())


def _normalize_component(logger_name: str) -> str:
    if not logger_name:
        return "core"
    if logger_name in _LOGGER_ALIASES:
        return _LOGGER_ALIASES[logger_name]
    tail = logger_name.rsplit(".", maxsplit=1)[-1]
    return _LOGGER_ALIASES.get(tail, tail)


def _format_multiline(text: str, indent: str) -> str:
    lines = str(text).splitlines() or [""]
    if len(lines) == 1:
        return lines[0]
    return lines[0] + "\n" + "\n".join(f"{indent}{line}" for line in lines[1:])


def _build_banner(title: str) -> str:
    width = max(48, min(96, shutil.get_terminal_size(fallback=(80, 20)).columns))
    border = "=" * width
    return f"{border}\n{title.center(width)}\n{border}"


class StructuredFormatter(logging.Formatter):
    """JSON formatter for machine-readable log files."""

    def format(self, record: logging.LogRecord) -> str:
        entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "component": _normalize_component(record.name),
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "thread_id": record.thread,
            "thread_name": record.threadName,
        }
        if hasattr(record, "metrics"):
            entry["metrics"] = record.metrics
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


class HumanReadableFormatter(logging.Formatter):
    """Compact terminal formatter with consistent columns and optional colors."""

    def __init__(self, use_color: bool = True):
        super().__init__()
        self.use_color = use_color
        self.reset = "\033[0m"

    def _format_level(self, level_name: str) -> str:
        badge = _LEVEL_BADGES.get(level_name, level_name[:3].upper())
        if not self.use_color:
            return badge
        color = _LEVEL_COLORS.get(level_name, "")
        return f"{color}{badge}{self.reset}"

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        component = _normalize_component(record.name)[:16].ljust(16)
        level = self._format_level(record.levelname)
        message = _format_multiline(record.getMessage(), indent=" " * 35)
        rendered = f"{timestamp} | {level:<12} | {component} | {message}"

        if record.levelno >= logging.WARNING:
            rendered += f" ({record.filename}:{record.lineno})"

        if record.exc_info:
            exc = self.formatException(record.exc_info)
            rendered += "\n" + _format_multiline(exc, indent=" " * 35)

        return rendered


class MetricsCollector:
    """Collects simple counters and histograms and flushes them into logs."""

    def __init__(self, metrics_logger: Optional[logging.Logger] = None):
        self.logger = metrics_logger or logging.getLogger("metrics")
        self.counters: Dict[str, int] = {}
        self.gauges: Dict[str, float] = {}
        self.histograms: Dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._last_flush = time.time()
        self.flush_interval = 60

    def increment(self, name: str, value: int = 1, tags: Optional[Dict[str, Any]] = None):
        _ = tags
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + value
            self._check_flush()

    def gauge(self, name: str, value: float, tags: Optional[Dict[str, Any]] = None):
        _ = tags
        with self._lock:
            self.gauges[name] = value
            self._check_flush()

    def histogram(self, name: str, value: float, tags: Optional[Dict[str, Any]] = None):
        _ = tags
        with self._lock:
            self.histograms.setdefault(name, []).append(value)
            self._check_flush()

    def timing(self, name: str, duration_ms: float, tags: Optional[Dict[str, Any]] = None):
        self.histogram(f"{name}_duration_ms", duration_ms, tags)

    def _check_flush(self):
        if time.time() - self._last_flush >= self.flush_interval:
            self.flush()

    def flush(self):
        with self._lock:
            metrics_data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "counters": self.counters.copy(),
                "gauges": self.gauges.copy(),
                "histograms": {},
            }

            for name, values in self.histograms.items():
                if not values:
                    continue
                sorted_values = sorted(values)
                metrics_data["histograms"][name] = {
                    "count": len(sorted_values),
                    "min": min(sorted_values),
                    "max": max(sorted_values),
                    "avg": sum(sorted_values) / len(sorted_values),
                    "p50": sorted_values[int(len(sorted_values) * 0.5)],
                    "p95": sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * 0.95))],
                    "p99": sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * 0.99))],
                }

            self.logger.info("Metrics snapshot", extra={"metrics": metrics_data})
            self.counters.clear()
            self.histograms.clear()
            self._last_flush = time.time()


def _reset_logger(logger: logging.Logger):
    for handler in list(logger.handlers):
        logger.removeHandler(handler)


def _configure_category_logger(name: str, handler: Optional[logging.Handler]):
    logger = logging.getLogger(name)
    _reset_logger(logger)
    if handler is None:
        logger.propagate = True
        return logger
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger


def _apply_library_defaults():
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


def get_shared_console() -> Optional[Any]:
    """Return the shared Rich console used by logging and live UI."""
    global _shared_console
    if not _RICH_AVAILABLE:
        return None
    if _shared_console is None:
        _shared_console = Console(stderr=True, soft_wrap=True)
    return _shared_console


# ── Log cleanup / rotation helpers ───────────────────────────────────────────


def _cleanup_old_timed_logs(log_dir: str, base_name: str, retention_days: int) -> int:
    """Remove TimedRotatingFileHandler rotated files older than *retention_days*.

    Returns the number of files removed.
    """
    removed = 0
    pattern = os.path.join(log_dir, f"{base_name}.*")
    cutoff = time.time() - (retention_days * 86_400)
    for path in glob.glob(pattern):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def _cleanup_old_rotating_logs(log_dir: str, base_name: str, keep: int) -> int:
    """Remove RotatingFileHandler backups beyond *keep* count.

    RotatingFileHandler names files as ``base.1``, ``base.2``, etc.
    This removes any numbered backup whose index exceeds *keep*.
    """
    removed = 0
    pat = re.compile(re.escape(base_name) + r"\.(\d+)$")
    for fname in os.listdir(log_dir):
        m = pat.match(fname)
        if m and int(m.group(1)) > keep:
            try:
                os.remove(os.path.join(log_dir, fname))
                removed += 1
            except OSError:
                pass
    return removed


def _cleanup_service_logs(log_dir: str, retention_days: int = 30) -> int:
    """Remove old Windows service logs from logs/services/."""
    services_dir = os.path.join(log_dir, "services")
    if not os.path.isdir(services_dir):
        return 0
    removed = 0
    cutoff = time.time() - (retention_days * 86_400)
    for fname in os.listdir(services_dir):
        fpath = os.path.join(services_dir, fname)
        try:
            if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
                removed += 1
        except OSError:
            pass
    return removed


def cleanup_old_logs(
    log_directory: str = LOG_DIRECTORY,
    debug_retention_days: int = DEBUG_RETENTION_DAYS,
    error_backup_count: int = BACKUP_COUNT,
    trade_backup_count: int = BACKUP_COUNT,
    metrics_backup_count: int = BACKUP_COUNT,
    service_retention_days: int = 30,
) -> Dict[str, int]:
    """Run all log cleanup routines.  Safe to call at startup.

    Returns a dict with counts of files removed per category.
    """
    result: Dict[str, int] = {}
    if not os.path.isdir(log_directory):
        return result

    result["debug"] = _cleanup_old_timed_logs(log_directory, DEBUG_LOG_FILE, debug_retention_days)
    result["error"] = _cleanup_old_rotating_logs(log_directory, ERROR_LOG_FILE, error_backup_count)
    result["trades"] = _cleanup_old_rotating_logs(log_directory, TRADE_LOG_FILE, trade_backup_count)
    result["metrics"] = _cleanup_old_rotating_logs(log_directory, METRICS_LOG_FILE, metrics_backup_count)
    result["services"] = _cleanup_service_logs(log_directory, service_retention_days)

    # Clean watchdog log at project root (one level up from logs/)
    project_root = os.path.dirname(os.path.abspath(log_directory))
    wd_path = os.path.join(project_root, WATCHDOG_LOG_FILE)
    if os.path.isfile(wd_path):
        try:
            size = os.path.getsize(wd_path)
            if size > WATCHDOG_MAX_SIZE * (WATCHDOG_BACKUP_COUNT + 1):
                # Truncate to keep only the last portion
                with open(wd_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(max(0, size - WATCHDOG_MAX_SIZE))
                    f.readline()  # skip partial line
                    tail = f.read()
                with open(wd_path, "w", encoding="utf-8") as f:
                    f.write(tail)
                result["watchdog_truncated"] = 1
        except OSError:
            pass

    total = sum(result.values())
    if total > 0:
        _log = logging.getLogger("logger_setup")
        _log.info("Log cleanup: removed/truncated %d items %s", total, result)
    return result


def load_logging_config(yaml_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Extract logging settings from the bot_config.yaml ``logging:`` section.

    Falls back to module-level defaults for any missing key.
    """
    cfg = (yaml_config or {}).get("logging", {}) if yaml_config else {}
    return {
        "log_level": cfg.get("log_level", "INFO"),
        "enable_console": cfg.get("enable_console", True),
        "enable_files": cfg.get("enable_files", True),
        "max_log_size_mb": cfg.get("max_log_size_mb", MAX_LOG_SIZE // (1024 * 1024)),
        "backup_count": cfg.get("backup_count", BACKUP_COUNT),
        "debug_retention_days": cfg.get("debug_retention_days", DEBUG_RETENTION_DAYS),
        "metrics_flush_interval": cfg.get("metrics_flush_interval", 60),
        "cleanup_on_startup": cfg.get("cleanup_on_startup", True),
    }


def setup_colorized_logger(name: str = "bot", level: str = "INFO", project_root: str = None) -> logging.Logger:
    """Backward-compatible console logger setup for legacy call sites."""
    root_logger, _ = setup_logging(log_level=level, enable_console=True, enable_files=False)
    if project_root:
        root_logger.debug("Logger initialized for project root: %s", project_root)
    return logging.getLogger(name)


def log_section(title: str, logger: Optional[logging.Logger] = None):
    target = logger or _global_logger or logging.getLogger()
    target.info("\n%s", _build_banner(title))


def log_success(message: str, logger: Optional[logging.Logger] = None):
    target = logger or _global_logger or logging.getLogger()
    target.info("OK  %s", message)


def log_error(message: str, logger: Optional[logging.Logger] = None):
    target = logger or _global_logger or logging.getLogger()
    target.error("ERR %s", message)


def setup_logging(
    log_level: str = "INFO",
    enable_console: bool = True,
    enable_files: bool = True,
    log_directory: str = LOG_DIRECTORY,
    max_log_size_mb: Optional[int] = None,
    backup_count: Optional[int] = None,
    debug_retention_days: Optional[int] = None,
    cleanup_on_startup: bool = True,
    console: Optional[Any] = None,
    use_rich_console: bool = True,
) -> tuple[logging.Logger, MetricsCollector]:
    """Configure root, file, and console logging for the application.

    Parameters
    ----------
    max_log_size_mb : int, optional
        Override MAX_LOG_SIZE for RotatingFileHandlers (in MB).
    backup_count : int, optional
        Override BACKUP_COUNT for RotatingFileHandlers.
    debug_retention_days : int, optional
        Override DEBUG_RETENTION_DAYS for daily debug log cleanup.
    cleanup_on_startup : bool
        If True, run cleanup_old_logs() at startup to purge stale files.
    """
    global _global_logger

    effective_max_bytes = (max_log_size_mb or (MAX_LOG_SIZE // (1024 * 1024))) * 1024 * 1024
    effective_backup = backup_count or BACKUP_COUNT
    effective_debug_retention = debug_retention_days or DEBUG_RETENTION_DAYS

    resolved_level = getattr(logging, str(log_level).upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(resolved_level)
    _reset_logger(root_logger)

    trade_handler: Optional[logging.Handler] = None
    metrics_handler: Optional[logging.Handler] = None

    if enable_files:
        os.makedirs(log_directory, exist_ok=True)

        error_handler = RotatingFileHandler(
            filename=os.path.join(log_directory, ERROR_LOG_FILE),
            maxBytes=effective_max_bytes,
            backupCount=effective_backup,
            encoding="utf-8",
            delay=True,
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(StructuredFormatter())

        debug_handler = TimedRotatingFileHandler(
            filename=os.path.join(log_directory, DEBUG_LOG_FILE),
            when="midnight",
            interval=1,
            backupCount=effective_debug_retention,
            encoding="utf-8",
            delay=True,
            utc=True,
        )
        debug_handler.setLevel(logging.DEBUG)
        debug_handler.setFormatter(StructuredFormatter())

        trade_handler = RotatingFileHandler(
            filename=os.path.join(log_directory, TRADE_LOG_FILE),
            maxBytes=effective_max_bytes,
            backupCount=effective_backup,
            encoding="utf-8",
            delay=True,
        )
        trade_handler.setLevel(logging.INFO)
        trade_handler.setFormatter(StructuredFormatter())

        metrics_handler = RotatingFileHandler(
            filename=os.path.join(log_directory, METRICS_LOG_FILE),
            maxBytes=effective_max_bytes,
            backupCount=effective_backup,
            encoding="utf-8",
            delay=True,
        )
        metrics_handler.setLevel(logging.INFO)
        metrics_handler.setFormatter(StructuredFormatter())

        root_logger.addHandler(error_handler)
        root_logger.addHandler(debug_handler)

    if enable_console:
        if use_rich_console and _RICH_AVAILABLE:
            rich_console = console or get_shared_console()
            console_handler = RichHandler(
                console=rich_console,
                rich_tracebacks=True,
                tracebacks_show_locals=False,
                show_path=False,
                omit_repeated_times=False,
                log_time_format="%H:%M:%S",
                markup=False,
            )
            console_handler.setLevel(resolved_level)
            console_handler.setFormatter(logging.Formatter("%(message)s"))
        else:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(resolved_level)
            console_handler.setFormatter(HumanReadableFormatter(use_color=_ansi_enabled(console_handler.stream)))
        root_logger.addHandler(console_handler)

    _configure_category_logger("trades", trade_handler)
    metrics_logger = _configure_category_logger("metrics", metrics_handler)

    metrics_collector = MetricsCollector(metrics_logger)
    _global_logger = root_logger

    _apply_library_defaults()

    root_logger.info(
        "Logging initialized | level=%s | files=%s | console=%s",
        logging.getLevelName(resolved_level),
        enable_files,
        enable_console,
    )
    if enable_files:
        root_logger.info("Log directory ready at %s", os.path.abspath(log_directory))

    # Run cleanup of old rotated logs at startup
    if enable_files and cleanup_on_startup:
        try:
            cleanup_old_logs(
                log_directory=log_directory,
                debug_retention_days=effective_debug_retention,
                error_backup_count=effective_backup,
                trade_backup_count=effective_backup,
                metrics_backup_count=effective_backup,
            )
        except Exception:
            root_logger.debug("Log cleanup skipped due to error", exc_info=True)

    return root_logger, metrics_collector


LOGGING_CONFIG_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Logging Configuration",
    "description": "Logging and metrics collection configuration schema",
    "type": "object",
    "properties": {
        "log_level": {
            "type": "string",
            "enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            "default": "INFO",
            "description": "Minimum logging level to capture",
        },
        "enable_console": {
            "type": "boolean",
            "default": True,
            "description": "Enable console logging output",
        },
        "max_log_size_mb": {
            "type": "integer",
            "minimum": 10,
            "maximum": 1000,
            "default": 100,
            "description": "Maximum size per log file before rotation (MB)",
        },
        "backup_count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "default": 10,
            "description": "Number of rotated log files to retain",
        },
        "metrics_flush_interval": {
            "type": "integer",
            "minimum": 10,
            "maximum": 3600,
            "default": 60,
            "description": "Metrics collection flush interval (seconds)",
        },
        "structured_logging": {
            "type": "boolean",
            "default": True,
            "description": "Enable JSON structured logging for file outputs",
        },
    },
    "required": [],
}
