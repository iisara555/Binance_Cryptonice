"""Rich-powered terminal command center for the crypto bot."""

from __future__ import annotations

import json
import logging
import sys
import re
import math
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from rich.align import Align
from rich import box
from rich.console import Group
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from helpers import format_bitkub_time
from logger_setup import get_shared_console


class _UILogBufferHandler(logging.Handler):
    """Push emitted log records into the dashboard ring buffer."""

    def __init__(self, sink) -> None:
        super().__init__(level=logging.NOTSET)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink(record)
        except Exception as exc:
            CLICommandCenter._safe_stderr_write(f"[cli_ui] log sink failure: {exc}\n")
            self.handleError(record)


class CLICommandCenter:
    """Render a live terminal dashboard using Rich."""

    _NOISY_INFO_LOGGERS = {"signal_flow", "bitkub_websocket", "websocket"}
    _GREEN = "#14b8a6"
    _MINT = _GREEN
    _RED = "#ef4444"
    _WHITE = "white"
    _EMBER = "#f59e0b"
    _DIM = "dim"
    _PANEL_THEME = {
        "header": (_WHITE, f"bold {_WHITE}"),
        "logs": (_WHITE, f"bold {_WHITE}"),
        "positions": (_WHITE, f"bold {_WHITE}"),
        "positions_hot": (_GREEN, f"bold {_GREEN}"),
        "positions_cold": (_RED, f"bold {_RED}"),
        "overview": (_WHITE, f"bold {_WHITE}"),
        "system": (_WHITE, f"bold {_WHITE}"),
        "signal": (_WHITE, f"bold {_WHITE}"),
        "signal_hot": (_GREEN, f"bold {_GREEN}"),
        "signal_cold": (_RED, f"bold {_RED}"),
        "events": (_WHITE, f"bold {_WHITE}"),
        "portfolio": (_WHITE, f"bold {_WHITE}"),
        "portfolio_hot": (_RED, f"bold {_RED}"),
        "portfolio_cold": (_GREEN, f"bold {_GREEN}"),
        "risk": (_EMBER, f"bold {_EMBER}"),
        "footer": (_WHITE, f"bold {_WHITE}"),
    }

    def __init__(
        self,
        app: Any,
        bot_name: str = "Crypto Bot V1",
        refresh_interval_seconds: float = 2.0,
        console: Optional[Console] = None,
    ) -> None:
        self.app = app
        self.bot_name = bot_name
        self.refresh_interval_seconds = max(1.0, float(refresh_interval_seconds))
        self.console = console or get_shared_console() or Console(stderr=True, soft_wrap=True)
        self._start_time = datetime.now(timezone.utc)
        self._log_lines: deque[Dict[str, str]] = deque(maxlen=140)
        self._last_log_rows_snapshot: List[Dict[str, str]] = []
        self._log_lock = threading.Lock()
        self._log_handler: Optional[_UILogBufferHandler] = None
        self._muted_console_handlers: List[tuple[logging.Handler, int]] = []
        self._footer_size_cache: Dict[tuple[int, int, str], int] = {}
        self._dropped_log_count = 0
        self._trend_history: Dict[str, deque[float]] = {
            "available_balance": deque(maxlen=24),
            "total_balance": deque(maxlen=24),
            "open_positions": deque(maxlen=24),
            "trade_count": deque(maxlen=24),
            "daily_loss": deque(maxlen=24),
            "buy_signals": deque(maxlen=24),
            "sell_signals": deque(maxlen=24),
            "wait_signals": deque(maxlen=24),
            "avg_pnl_pct": deque(maxlen=24),
            "signal_score": deque(maxlen=24),
            "top_allocation_pct": deque(maxlen=24),
            "cash_allocation_pct": deque(maxlen=24),
        }

    @staticmethod
    def _safe_stderr_write(message: str) -> None:
        try:
            sys.stderr.write(message)
        except Exception:
            return

    def start_log_capture(self) -> None:
        """Start mirroring runtime logs into an in-memory ring buffer for UI rendering."""
        if self._log_handler is not None:
            return
        handler = _UILogBufferHandler(self._append_log_record)
        root = logging.getLogger()
        root.addHandler(handler)
        self._log_handler = handler
        self._mute_console_handlers()

    def stop_log_capture(self) -> None:
        """Detach runtime log mirroring handler."""
        handler = self._log_handler
        if handler is None:
            return
        root = logging.getLogger()
        try:
            root.removeHandler(handler)
        except Exception as exc:
            self._safe_stderr_write(f"[cli_ui] failed to remove log capture handler: {exc}\n")
        self._log_handler = None
        self._restore_console_handlers()

    @staticmethod
    def _is_live_console_handler(handler: logging.Handler) -> bool:
        if handler.__class__.__name__ == "RichHandler":
            return True
        return isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) in {sys.stdout, sys.stderr}

    def _mute_console_handlers(self) -> None:
        if self._muted_console_handlers:
            return
        root = logging.getLogger()
        for handler in list(root.handlers):
            if handler is self._log_handler:
                continue
            if not self._is_live_console_handler(handler):
                continue
            self._muted_console_handlers.append((handler, int(handler.level)))
            handler.setLevel(logging.CRITICAL + 1)

    def _restore_console_handlers(self) -> None:
        for handler, original_level in self._muted_console_handlers:
            try:
                handler.setLevel(original_level)
            except Exception as exc:
                self._safe_stderr_write(f"[cli_ui] failed to restore console handler level: {exc}\n")
        self._muted_console_handlers.clear()

    @classmethod
    def _should_capture_log_record(cls, record: logging.LogRecord) -> bool:
        if record.name == __name__:
            return False
        logger_leaf = str(record.name or "root").split(".")[-1]
        if logger_leaf in cls._NOISY_INFO_LOGGERS and record.levelno < logging.WARNING:
            return False
        return True

    def _append_log_record(self, record: logging.LogRecord) -> None:
        if not self._should_capture_log_record(record):
            return

        rendered = record.getMessage().replace("\n", " ").strip()
        if not rendered:
            return

        timestamp = format_bitkub_time(datetime.fromtimestamp(record.created, tz=timezone.utc))
        logger_name = str(record.name or "root").split(".")[-1]
        if len(logger_name) > 14:
            logger_name = logger_name[-14:]
        if len(rendered) > 150:
            rendered = f"{rendered[:147]}..."

        if not self._log_lock.acquire(blocking=False):
            self._dropped_log_count += 1
            if self._dropped_log_count == 1:
                self._safe_stderr_write("[cli_ui] warning: dashboard log buffer busy; dropping log records until lock is available\n")
            return
        try:
            if self._dropped_log_count > 0:
                dropped_count = self._dropped_log_count
                self._dropped_log_count = 0
                self._log_lines.append(
                    {
                        "timestamp": timestamp,
                        "level": "WARNING",
                        "logger": "cli_ui",
                        "message": f"Dropped {dropped_count} log record(s) due to dashboard lock contention",
                    }
                )
            self._log_lines.append(
                {
                    "timestamp": timestamp,
                    "level": record.levelname.upper(),
                    "logger": logger_name,
                    "message": rendered,
                }
            )
            self._last_log_rows_snapshot = list(self._log_lines)[-8:]
        finally:
            self._log_lock.release()

    def create_live(self) -> Live:
        """Create the Live context used by the main runtime loop."""
        return Live(
            self.render(),
            console=self.console,
            auto_refresh=False,
            transient=False,
            screen=False,
            vertical_overflow="crop",
        )

    def capture_render_state(self) -> tuple[Dict[str, Any], str]:
        """Return the latest snapshot plus a stable signature for redraw suppression."""
        snapshot = self.app.get_cli_snapshot(bot_name=self.bot_name)
        return snapshot, self._build_render_signature(snapshot)

    def render(self, snapshot: Optional[Dict[str, Any]] = None) -> Layout:
        """Build the latest dashboard layout from the app snapshot."""
        try:
            return self._render_inner(snapshot)
        except Exception as exc:
            self._safe_stderr_write(f"[cli_ui] render error: {exc}\n")
            layout = Layout(name="root")
            layout.update(Panel(Text(f"Dashboard render error: {exc}", style=f"bold {self._RED}"), title="ERROR"))
            return layout

    def _render_inner(self, snapshot: Optional[Dict[str, Any]] = None) -> Layout:
        snapshot = snapshot or self.app.get_cli_snapshot(bot_name=self.bot_name)
        self._record_metric_history(snapshot)
        ui_cfg = dict(snapshot.get("ui") or {})
        footer_mode = str(ui_cfg.get("footer_mode") or "compact").lower()

        # Adaptive footer size: compact chat area while preserving input visibility.
        term_height = self.console.height if self.console else 30
        term_width = self.console.width if self.console else 120
        compact_mode = term_width < 140 or term_height < 30
        footer_size = self._resolve_footer_size(term_width, term_height, footer_mode)

        layout = Layout(name="root")
        layout.split_column(
            Layout(self._build_header(snapshot), size=3, name="header"),
            Layout(name="body"),
            Layout(self._build_footer(snapshot), size=footer_size, name="footer"),
        )
        if compact_mode:
            layout["body"].split_column(
                Layout(self._build_runtime_overview_panel(snapshot), ratio=2, name="overview"),
                Layout(self._build_positions_table(snapshot, compact=True), ratio=4, name="positions"),
                Layout(self._build_signal_alignment_panel(snapshot), ratio=4, name="alignment"),
                Layout(self._build_risk_rails_panel(snapshot), ratio=3, name="risk"),
                Layout(self._build_log_stream_panel(snapshot), ratio=2, name="logs"),
            )
        else:
            layout["body"].split_row(
                Layout(name="left", ratio=3),
                Layout(name="center", ratio=4),
                Layout(name="right", ratio=3),
            )
            layout["left"].split_column(
                Layout(self._build_runtime_overview_panel(snapshot), ratio=3, name="overview"),
                Layout(self._build_balance_breakdown_panel(snapshot), ratio=4, name="portfolio"),
                Layout(self._build_recent_events_panel(snapshot), ratio=3, name="events"),
            )
            layout["center"].split_column(
                Layout(self._build_positions_table(snapshot, compact=False), ratio=5, name="positions"),
                Layout(self._build_signal_alignment_panel(snapshot), ratio=5, name="alignment"),
            )
            layout["right"].split_column(
                Layout(self._build_risk_rails_panel(snapshot), ratio=3, name="risk"),
                Layout(self._build_system_status_table(snapshot), ratio=4, name="system"),
                Layout(self._build_log_stream_panel(snapshot), ratio=3, name="logs"),
            )
        return layout

    def _record_metric_history(self, snapshot: Dict[str, Any]) -> None:
        system = dict(snapshot.get("system") or {})
        positions = list(snapshot.get("positions") or [])
        signal_rows = list(snapshot.get("signal_alignment") or [])
        balance_breakdown = list(system.get("balance_breakdown") or [])
        pnl_values = [self._safe_float(position.get("pnl_pct")) for position in positions]
        valid_pnl_values = [value for value in pnl_values if value is not None]
        avg_pnl_pct = (sum(valid_pnl_values) / len(valid_pnl_values)) if valid_pnl_values else 0.0
        signal_score = self._signal_score(signal_rows)
        top_allocation_pct = 0.0
        cash_allocation_pct = 0.0
        for line in balance_breakdown:
            allocation_pct = self._extract_allocation_pct(line)
            top_allocation_pct = max(top_allocation_pct, allocation_pct)
            if str(line or "").upper().startswith("THB "):
                cash_allocation_pct = allocation_pct

        values = {
            "available_balance": self._extract_numeric(system.get("available_balance"), 0.0),
            "total_balance": self._extract_numeric(system.get("total_balance"), 0.0),
            "open_positions": float(len(positions)),
            "trade_count": self._extract_numeric(system.get("trade_count"), 0.0),
            "daily_loss": self._extract_fraction(system.get("daily_loss"))[0],
            "buy_signals": float(sum(1 for row in signal_rows if str(row.get("action") or "").upper() == "BUY")),
            "sell_signals": float(sum(1 for row in signal_rows if str(row.get("action") or "").upper() == "SELL")),
            "wait_signals": float(sum(1 for row in signal_rows if str(row.get("action") or "").upper() == "WAIT")),
            "avg_pnl_pct": avg_pnl_pct,
            "signal_score": signal_score,
            "top_allocation_pct": top_allocation_pct,
            "cash_allocation_pct": cash_allocation_pct,
        }

        for key, value in values.items():
            self._trend_history[key].append(float(value or 0.0))

    def _trend_values(self, key: str, fallback: Optional[List[float]] = None) -> List[float]:
        values = list(self._trend_history.get(key) or [])
        if values:
            return values
        return list(fallback or [])

    @classmethod
    def _panel(cls, renderable: Any, title: str, theme: str) -> Panel:
        border_style, title_style = cls._PANEL_THEME.get(theme, ("white", "bold white"))
        return Panel(
            renderable,
            title=Text.assemble(("[ ", cls._DIM), (str(title), title_style), (" ]", cls._DIM)),
            border_style=border_style,
            title_align="left",
            box=box.SQUARE,
            padding=(0, 1),
        )

    def _get_filtered_log_rows(self, min_level: str) -> List[Dict[str, str]]:
        min_level_no = self._level_no(min_level)

        rows = list(self._last_log_rows_snapshot)
        if self._log_lock.acquire(blocking=False):
            try:
                rows = list(self._log_lines)[-8:]
                self._last_log_rows_snapshot = rows
            finally:
                self._log_lock.release()

        return [row for row in rows if self._level_no(row.get("level", "INFO")) >= min_level_no]

    @staticmethod
    def _normalize_snapshot_for_signature(snapshot: Dict[str, Any]) -> Dict[str, Any]:
        normalized = json.loads(json.dumps(snapshot or {}, sort_keys=True, default=str, ensure_ascii=False))
        normalized.pop("updated_at", None)
        system = normalized.get("system")
        if isinstance(system, dict):
            system.pop("market_age_seconds", None)
        return normalized

    def _build_render_signature(self, snapshot: Dict[str, Any]) -> str:
        ui_cfg = dict(snapshot.get("ui") or {})
        min_level = str(ui_cfg.get("log_level_filter") or "INFO").upper()
        payload = {
            "snapshot": self._normalize_snapshot_for_signature(snapshot),
            "logs": self._get_filtered_log_rows(min_level),
            "term_width": self.console.width if self.console else 120,
            "term_height": self.console.height if self.console else 30,
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)

    @staticmethod
    def _footer_content_budget(footer_mode: str) -> int:
        return 7

    def _resolve_footer_size(self, term_width: int, term_height: int, footer_mode: str) -> int:
        normalized_mode = str(footer_mode or "compact").lower()
        safe_width = max(1, int(term_width or 120))
        safe_height = max(1, int(term_height or 30))
        cache_key = (safe_width, safe_height, normalized_mode)
        cached_size = self._footer_size_cache.get(cache_key)
        if cached_size is not None:
            return cached_size

        min_content_rows = self._footer_content_budget(normalized_mode)
        min_panel_rows = min_content_rows + 2  # top/bottom panel borders
        if normalized_mode == "verbose":
            target_size = max(8, min(11, safe_height // 3))
        else:
            target_size = max(6, min(8, safe_height // 4))

        resolved = max(min_panel_rows, target_size)
        self._footer_size_cache[cache_key] = resolved
        return resolved

    @staticmethod
    def _truncate_inline(value: Any, max_chars: int, preserve_tail: bool = False) -> str:
        text = str(value or "")
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        if max_chars <= 3:
            return text[:max_chars]
        if preserve_tail:
            return f"...{text[-(max_chars - 3):]}"
        return f"{text[:max_chars - 3]}..."

    @staticmethod
    def _level_style(level: str) -> str:
        value = str(level or "").upper()
        if value in {"CRITICAL", "FATAL"}:
            return f"bold {CLICommandCenter._RED}"
        if value == "ERROR":
            return f"bold {CLICommandCenter._RED}"
        if value == "WARNING":
            return f"bold {CLICommandCenter._EMBER}"
        if value == "INFO":
            return f"bold {CLICommandCenter._WHITE}"
        return CLICommandCenter._DIM

    @staticmethod
    def _level_no(value: str) -> int:
        order = {
            "DEBUG": 10,
            "INFO": 20,
            "WARNING": 30,
            "ERROR": 40,
            "CRITICAL": 50,
        }
        return order.get(str(value or "").upper(), 20)

    def _build_log_stream_panel(self, snapshot: Dict[str, Any]) -> Panel:
        ui_cfg = dict(snapshot.get("ui") or {})
        min_level = str(ui_cfg.get("log_level_filter") or "INFO").upper()
        rows = self._get_filtered_log_rows(min_level)

        if not rows:
            return self._panel(
                Text(f"Waiting for runtime logs ({min_level}+)...", style=self._DIM),
                title="Terminal Log Stream",
                theme="logs",
            )

        lines: List[Text] = []
        for row in rows:
            level = str(row.get("level") or "INFO")
            lines.append(
                Text.assemble(
                    (f"{row.get('timestamp', '-')} ", self._DIM),
                    (f"{level:<8}", self._level_style(level)),
                    (f" {row.get('logger', '-'):<14}", f"bold {self._WHITE}"),
                    (f" {row.get('message', '-')}", self._WHITE),
                )
            )

        return self._panel(Group(*lines), title=f"Terminal Log Stream [{min_level}+ ]", theme="logs")

    def _build_header(self, snapshot: Dict[str, Any]) -> Panel:
        mode = snapshot.get("mode", "UNKNOWN")
        strategy_mode = str(snapshot.get("strategy_mode") or "standard").lower()
        mode_style = self._mode_style(mode)
        risk_text = Text(str(snapshot.get("risk_level", "UNKNOWN")), style=self._risk_style(snapshot.get("risk_level")))
        pair_count = self._pair_count(snapshot)
        open_positions = len(list(snapshot.get("positions") or []))
        # Uptime
        uptime_delta = datetime.now(timezone.utc) - self._start_time
        total_secs = int(uptime_delta.total_seconds())
        days, remainder = divmod(total_secs, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        uptime_str = f"{days}d {hours:02d}h {minutes:02d}m" if days else f"{hours}h {minutes:02d}m"
        # Data freshness for header
        system = snapshot.get("system", {}) or {}
        freshness_state = str(system.get("freshness") or "fresh").lower()
        freshness_icon = "\u25cf"  # ●
        if freshness_state == "critical":
            freshness_style = f"bold {self._RED}"
        elif freshness_state == "warning":
            freshness_style = f"bold {self._EMBER}"
        else:
            freshness_style = f"bold {self._GREEN}"

        header = Text.assemble(
            ("> ", f"bold {self._WHITE}"),
            (snapshot.get("bot_name", self.bot_name), f"bold {self._WHITE}"),
            (" :: ", self._DIM),
            ("MODE ", f"bold {self._WHITE}"),
            (mode, mode_style),
            (" :: ", self._DIM),
            ("STRATEGY ", f"bold {self._WHITE}"),
            (strategy_mode, f"bold {self._WHITE}"),
            (" :: ", self._DIM),
            ("RISK ", f"bold {self._WHITE}"),
            risk_text,
            (" :: ", self._DIM),
            ("PAIRS ", f"bold {self._WHITE}"),
            (str(pair_count), f"bold {self._WHITE}"),
            (" :: ", self._DIM),
            ("OPEN ", f"bold {self._WHITE}"),
            (str(open_positions), f"bold {self._GREEN}" if open_positions == 0 else f"bold {self._EMBER}"),
            (" :: ", self._DIM),
            (f"{freshness_icon} ", freshness_style),
            ("UP ", f"bold {self._WHITE}"),
            (uptime_str, f"bold {self._GREEN}"),
        )
        return self._panel(Align.left(header), title="Terminal Deck", theme="header")

    @staticmethod
    def _mode_style(mode: str) -> str:
        normalized = str(mode or "").strip().upper()
        if normalized == "LIVE":
            return f"bold {CLICommandCenter._GREEN}"
        if normalized in {"SEMI AUTO", "SIMULATION"}:
            return f"bold {CLICommandCenter._EMBER}"
        if normalized in {"READ ONLY", "DEGRADED"}:
            return f"bold {CLICommandCenter._RED}"
        return f"bold {CLICommandCenter._WHITE}"

    @staticmethod
    def _risk_style(risk_level: Any) -> str:
        value = str(risk_level or "").upper()
        if value in {"HIGH", "CRITICAL", "SEVERE"}:
            return f"bold {CLICommandCenter._RED}"
        if value in {"MEDIUM", "ELEVATED", "MODERATE"}:
            return f"bold {CLICommandCenter._EMBER}"
        if value in {"LOW", "OK", "NORMAL"}:
            return f"bold {CLICommandCenter._GREEN}"
        return f"bold {CLICommandCenter._WHITE}"

    def _build_positions_table(self, snapshot: Dict[str, Any], compact: bool = False) -> Panel:
        table = Table(expand=True)
        table.add_column("Symbol", style=f"bold {self._WHITE}")
        table.add_column("Side", justify="center")
        table.add_column("Entry", justify="right")
        table.add_column("Current", justify="right")
        table.add_column("PnL %", justify="right")
        if compact:
            table.add_column("SL/TP Dist", justify="right")
        else:
            table.add_column("SL / TP", justify="right")
            table.add_column("Dist SL/TP", justify="right")

        positions: List[Dict[str, Any]] = list(snapshot.get("positions") or [])
        pnl_values = [self._safe_float(position.get("pnl_pct")) for position in positions]
        valid_pnl_values = [value for value in pnl_values if value is not None]
        avg_pnl_pct = (sum(valid_pnl_values) / len(valid_pnl_values)) if valid_pnl_values else 0.0
        winners = sum(1 for value in valid_pnl_values if value > 0)
        losers = sum(1 for value in valid_pnl_values if value < 0)
        if not positions:
            if compact:
                table.add_row("-", "-", "-", "-", "No open positions", "-")
            else:
                table.add_row("-", "-", "-", "-", "No open positions", "-", "-")
        else:
            for position in positions:
                sltp_text = f"{self._fmt_price(position.get('stop_loss'))} / {self._fmt_price(position.get('take_profit'))}"
                sl_dist = self._fmt_distance_pct(position.get("sl_distance_pct"))
                tp_dist = self._fmt_distance_pct(position.get("tp_distance_pct"))
                dist_text = Text.assemble(("SL ", self._DIM), sl_dist, (" | TP ", self._DIM), tp_dist)
                # Bootstrap source tag: DB=persisted, TS=trade_state, EST=ticker estimate
                bsrc = str(position.get("bootstrap_source") or "")
                src_tag = ""
                if bsrc == "persisted_position":
                    src_tag = " [DB]"
                elif bsrc == "trade_state":
                    src_tag = " [TS]"
                elif bsrc == "estimated_from_ticker":
                    src_tag = " [EST]"
                symbol_display = str(position.get("symbol", "-")) + src_tag
                if compact:
                    table.add_row(
                        symbol_display,
                        self._side_text(str(position.get("side", "-"))),
                        self._fmt_price(position.get("entry_price")),
                        self._fmt_price(position.get("current_price")),
                        self._pnl_text(position.get("pnl_pct")),
                        dist_text,
                    )
                else:
                    table.add_row(
                        symbol_display,
                        self._side_text(str(position.get("side", "-"))),
                        self._fmt_price(position.get("entry_price")),
                        self._fmt_price(position.get("current_price")),
                        self._pnl_text(position.get("pnl_pct")),
                        sltp_text,
                        dist_text,
                    )

        summary_lines: List[Text] = []
        total_wl = winners + losers
        win_rate_str = f" ({100 * winners / total_wl:.0f}%)" if total_wl > 0 else ""
        summary_lines.append(
            Text.assemble(
                ("BOOK ", "bold white"),
                (f"open {len(positions)}", f"bold {self._EMBER}" if positions else f"bold {self._GREEN}"),
                ("  W/L ", "bold white"),
                (f"{winners}/{losers}{win_rate_str}", f"bold {self._GREEN}" if winners >= losers else f"bold {self._RED}"),
            )
        )
        summary_lines.append(
            Text.assemble(
                ("BOOK PNL ", "bold white"),
                self._pnl_text(avg_pnl_pct),
            )
        )
        summary_lines.append(
            Text.assemble(
                ("PNL TREND ", "bold white"),
                self._sparkline_text(self._trend_values("avg_pnl_pct", [avg_pnl_pct]), filled_style=f"bold {self._GREEN}" if avg_pnl_pct >= 0 else f"bold {self._RED}"),
            )
        )
        summary_lines.append(
            Text.assemble(
                ("OPEN TREND ", "bold white"),
                self._sparkline_text(self._trend_values("open_positions", [float(len(positions))]), filled_style=f"bold {self._WHITE}"),
            )
        )

        theme = self._resolve_positions_theme(avg_pnl_pct)
        return self._panel(Group(*summary_lines, table), title="Position Book", theme=theme)

    def _build_runtime_overview_panel(self, snapshot: Dict[str, Any]) -> Panel:
        system = snapshot.get("system", {}) or {}
        positions = list(snapshot.get("positions") or [])
        signal_rows = list(snapshot.get("signal_alignment") or [])
        balance_mix = list(system.get("balance_breakdown") or [])
        pair_count = self._pair_count(snapshot)
        open_positions = len(positions)
        max_positions = max(1.0, self._extract_numeric(system.get("max_open_positions"), 1.0))
        trade_count = self._extract_numeric(system.get("trade_count"), 0.0)
        max_daily_trades = max(1.0, self._extract_numeric(system.get("max_daily_trades"), 1.0))
        available_balance = self._extract_numeric(system.get("available_balance"), 0.0)
        total_balance = max(available_balance, self._extract_numeric(system.get("total_balance"), 0.0))
        buy_signals = sum(1 for row in signal_rows if str(row.get("action") or "").upper() == "BUY")
        sell_signals = sum(1 for row in signal_rows if str(row.get("action") or "").upper() == "SELL")
        wait_signals = sum(1 for row in signal_rows if str(row.get("action") or "").upper() == "WAIT")

        lines: List[Text] = []
        lines.append(
            Text.assemble(
                ("PAIRSET ", "bold white"),
                (str(pair_count), f"bold {self._WHITE}"),
                ("  STRAT ", "bold white"),
                (self._truncate_inline(snapshot.get("strategies") or "idle", 22), f"bold {self._WHITE}"),
                ("  UPD ", "bold white"),
                (str(snapshot.get("updated_at") or "-"), self._WHITE),
            )
        )
        lines.append(
            Text.assemble(
                ("SIGNALS ", "bold white"),
                (f"B {buy_signals}", f"bold {self._GREEN}"),
                (" / ", self._DIM),
                (f"S {sell_signals}", f"bold {self._RED}"),
                (" / ", self._DIM),
                (f"W {wait_signals}", f"bold {self._EMBER}"),
            )
        )
        lines.append(
            Text.assemble(
                ("FLOW ", "bold white"),
                self._sparkline_text(
                    self._trend_values("buy_signals", [buy_signals])
                    + self._trend_values("sell_signals", [sell_signals])[-1:]
                    + self._trend_values("wait_signals", [wait_signals])[-1:],
                    filled_style=f"bold {self._WHITE}",
                ),
            )
        )
        lines.append(
            Text.assemble(
                ("CASH ", "bold white"),
                self._meter_text(available_balance, total_balance, width=16, filled_style=f"bold {self._EMBER}", suffix="THB"),
            )
        )
        lines.append(
            Text.assemble(
                ("CASH TREND ", "bold white"),
                self._sparkline_text(self._trend_values("available_balance", [available_balance]), filled_style=f"bold {self._EMBER}"),
            )
        )
        lines.append(
            Text.assemble(
                ("SLOTS ", "bold white"),
                self._meter_text(float(open_positions), max_positions, width=16, filled_style=f"bold {self._WHITE}", decimals=0),
            )
        )
        lines.append(
            Text.assemble(
                ("TRADES ", "bold white"),
                self._meter_text(trade_count, max_daily_trades, width=16, filled_style=f"bold {self._GREEN}", decimals=0),
            )
        )
        if balance_mix:
            lines.append(
                Text.assemble(
                    ("ALLOC ", "bold white"),
                    self._sparkline_text(
                        [self._extract_allocation_pct(item) for item in balance_mix[:8]],
                        filled_style=f"bold {self._EMBER}",
                    ),
                )
            )

        return self._panel(Group(*lines), title="Trading Matrix", theme="overview")

    def _build_system_status_table(self, snapshot: Dict[str, Any]) -> Panel:
        system = snapshot.get("system", {})
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)

        left_lines = [
            Text.assemble(("Last Market Update ", "bold white"), (str(system.get("last_market_update", "-")), self._WHITE)),
            Text.assemble(("Data Freshness ", "bold white"), self._freshness_text(system.get("freshness"), system.get("market_age_seconds"))),
            Text.assemble(("API Latency ", "bold white"), self._api_latency_text(system.get("api_latency"))),
            Text.assemble(("WebSocket ", "bold white"), self._service_health_text(system.get("websocket_health"))),
            Text.assemble(("Balances ", "bold white"), self._service_health_text(system.get("balance_health"))),
            Text.assemble(("Candle Readiness ", "bold white"), (str(system.get("candle_readiness", "-")), self._WHITE)),
            Text.assemble(("Candle Waiting ", "bold white"), (str(system.get("candle_waiting", "-")), self._DIM)),
        ]

        right_lines = [
            Text.assemble(("Available Balance ", "bold white"), (str(system.get("available_balance", "-")), f"bold {self._EMBER}")),
            Text.assemble(("Total Balance ", "bold white"), (str(system.get("total_balance", "-")), f"bold {self._GREEN}")),
            Text.assemble(("Today's Trades ", "bold white"), (f"{system.get('trade_count', '-')}/{system.get('max_daily_trades', '-')}", self._WHITE)),
            Text.assemble(("Risk/Trade ", "bold white"), (str(system.get("risk_per_trade", "-")), f"bold {self._RED}")),
            Text.assemble(("Daily Loss ", "bold white"), (f"{system.get('daily_loss', '-')} ({system.get('daily_loss_pct', '-')})", self._WHITE)),
            Text.assemble(("Cooldown ", "bold white"), (str(system.get("cooling_down", "-")), f"bold {self._EMBER}" if str(system.get("cooling_down", "-")) == "Yes" else f"bold {self._GREEN}")),
        ]

        grid.add_row(Group(*left_lines), Group(*right_lines))

        # Show degraded mode reason if present
        degraded_reason = snapshot.get("auth_degraded_reason", "")
        if degraded_reason:
            grid.add_row(Text("⚠ Degraded", style=f"bold {self._RED}"), Text(str(degraded_reason), style=self._RED))

        return self._panel(grid, title="System Bus", theme="system")

    def _build_signal_alignment_panel(self, snapshot: Dict[str, Any]) -> Panel:
        table = Table(expand=True, show_lines=False)
        table.add_column("Pair", style=f"bold {self._WHITE}", no_wrap=True)
        table.add_column("TF", justify="center", no_wrap=True)
        table.add_column("Wait", no_wrap=True)
        table.add_column("M/m/T", justify="center", no_wrap=True)
        table.add_column("Trend", justify="center", no_wrap=True)
        table.add_column("Action", justify="center", no_wrap=True)
        table.add_column("Status", no_wrap=True, max_width=12)

        rows = list(snapshot.get("signal_alignment") or [])
        signal_score = self._signal_score(rows)
        buy_signals = sum(1 for row in rows if str(row.get("action") or "").upper() == "BUY")
        sell_signals = sum(1 for row in rows if str(row.get("action") or "").upper() == "SELL")
        wait_signals = sum(1 for row in rows if str(row.get("action") or "").upper() == "WAIT")
        if not rows:
            table.add_row("-", "-", "-", "-", "-", "-", "No pairs")
        else:
            for row in rows[:12]:
                action = str(row.get("action") or "HOLD").upper()
                if action == "BUY":
                    action_text = Text("▲ BUY", style=f"bold {self._GREEN}")
                elif action == "SELL":
                    action_text = Text("▼ SELL", style=f"bold {self._RED}")
                elif action == "WAIT":
                    action_text = Text("■ WAIT", style=f"bold {self._EMBER}")
                else:
                    action_text = Text("• HOLD", style=f"bold {self._WHITE}")
                macro_value = str(row.get("macro") or "N/A")
                micro_value = str(row.get("micro") or "N/A")
                trigger_value = str(row.get("trigger") or "N/A")
                macro = "-" if macro_value.upper() == "N/A" else macro_value[0:1]
                micro = "-" if micro_value.upper() == "N/A" else micro_value[0:1]
                trigger = "-" if trigger_value.upper() == "N/A" else trigger_value[0:1]
                # Shorten symbol: THB_BTC -> BTC
                symbol = str(row.get("symbol") or "-")
                if symbol.startswith("THB_"):
                    symbol = symbol[4:]
                # Truncate status
                status = str(row.get("status") or row.get("pair_state") or "Ready")[:12]
                trend_style = f"bold {self._GREEN}" if str(row.get("trend") or "").upper() == "UP" else f"bold {self._RED}" if str(row.get("trend") or "").upper() == "DOWN" else f"bold {self._EMBER}"
                table.add_row(
                    symbol,
                    str(row.get("tf_ready") or "-"),
                    str(row.get("wait_detail") or "-"),
                    f"{macro}/{micro}/{trigger}",
                    Text(str(row.get("trend") or "MIXED"), style=trend_style),
                    action_text,
                    status,
                )

        summary_lines = [
            Text.assemble(
                ("QUALITY ", "bold white"),
                (self._signal_quality_label(signal_score), self._signal_quality_style(signal_score)),
                (f" ({signal_score:+.1f})", self._DIM),
            ),
            Text.assemble(
                ("FLOW MIX ", "bold white"),
                (f"B {buy_signals}", f"bold {self._GREEN}"),
                (" / ", self._DIM),
                (f"S {sell_signals}", f"bold {self._RED}"),
                (" / ", self._DIM),
                (f"W {wait_signals}", f"bold {self._EMBER}"),
            ),
            Text.assemble(
                ("SIGNAL TREND ", "bold white"),
                self._sparkline_text(
                    self._trend_values("signal_score", [signal_score]),
                    filled_style=self._signal_quality_style(signal_score),
                ),
            ),
        ]

        return self._panel(Group(*summary_lines, table), title="Signal Radar", theme=self._resolve_signal_theme(rows))

    def _build_recent_events_panel(self, snapshot: Dict[str, Any]) -> Panel:
        rows = list(snapshot.get("recent_events") or [])
        if not rows:
            return self._panel(Text("No recent events", style=self._DIM), title="Event Tape", theme="events")

        lines: List[Text] = []
        for row in rows[:5]:
            event_type = str(row.get("type") or "EVT").upper()
            if event_type == "TRADE":
                style = f"bold {self._GREEN}"
            elif "WITHDRAW" in event_type or "LOW" in event_type:
                style = f"bold {self._RED}"
            else:
                style = f"bold {self._EMBER}"
            timestamp = str(row.get("timestamp") or "-")
            message = self._truncate_inline(str(row.get("message") or "-"), 58)
            lines.append(Text.assemble((f"{timestamp} ", self._DIM), (f"[{event_type}] ", style), (message, self._WHITE)))

        return self._panel(Group(*lines), title="Event Tape", theme="events")

    def _build_balance_breakdown_panel(self, snapshot: Dict[str, Any]) -> Panel:
        system = snapshot.get("system", {})
        breakdown_lines = list(system.get("balance_breakdown") or [])

        table = Table(expand=True, show_header=False)
        table.add_column("Holding", style="bold white")
        table.add_column("Allocation", justify="right")

        summary_lines: List[Text] = []
        if breakdown_lines:
            total_balance = self._extract_numeric(system.get("total_balance"), 0.0)
            top_allocation_pct = max((self._extract_allocation_pct(item) for item in breakdown_lines), default=0.0)
            cash_allocation_pct = 0.0
            for item in breakdown_lines:
                if str(item or "").upper().startswith("THB "):
                    cash_allocation_pct = self._extract_allocation_pct(item)
                    break
            summary_lines.append(
                Text.assemble(
                    ("MIX ", "bold white"),
                    self._sparkline_text(
                        [self._extract_allocation_pct(item) for item in breakdown_lines[:8]],
                        filled_style=f"bold {self._EMBER}",
                    ),
                )
            )
            summary_lines.append(
                Text.assemble(
                    ("TOTAL TREND ", "bold white"),
                    self._sparkline_text(self._trend_values("total_balance", [total_balance]), filled_style=f"bold {self._GREEN}"),
                )
            )
            summary_lines.append(
                Text.assemble(
                    ("CONCENTRATION ", "bold white"),
                    (f"{top_allocation_pct:.2f}%", f"bold {self._RED}" if top_allocation_pct >= 70.0 else f"bold {self._WHITE}"),
                    ("  TREND ", "bold white"),
                    self._sparkline_text(self._trend_values("top_allocation_pct", [top_allocation_pct]), filled_style=f"bold {self._RED}" if top_allocation_pct >= 70.0 else f"bold {self._WHITE}"),
                )
            )
            summary_lines.append(
                Text.assemble(
                    ("CASH MIX ", "bold white"),
                    (f"{cash_allocation_pct:.2f}%", f"bold {self._EMBER}"),
                    ("  TREND ", "bold white"),
                    self._sparkline_text(self._trend_values("cash_allocation_pct", [cash_allocation_pct]), filled_style=f"bold {self._EMBER}"),
                )
            )

        if not breakdown_lines:
            table.add_row(Text("No balance breakdown", style=self._WHITE), Text("-", style=self._WHITE))
        else:
            for line in breakdown_lines:
                allocation_pct = self._extract_allocation_pct(line)
                asset = str(line or "-").split(" ", 1)[0].upper()
                table.add_row(
                    self._balance_breakdown_text(line),
                    self._allocation_bar_text(allocation_pct, asset=asset),
                )

        portfolio_theme = self._resolve_portfolio_theme(
            max((self._extract_allocation_pct(item) for item in breakdown_lines), default=0.0),
            next((self._extract_allocation_pct(item) for item in breakdown_lines if str(item or "").upper().startswith("THB ")), 0.0),
        )
        if summary_lines:
            return self._panel(Group(*summary_lines, table), title="Portfolio Breakdown // Stack", theme=portfolio_theme)
        return self._panel(table, title="Portfolio Breakdown // Stack", theme=portfolio_theme)

    def _build_risk_rails_panel(self, snapshot: Dict[str, Any]) -> Panel:
        system = snapshot.get("system", {}) or {}
        positions = list(snapshot.get("positions") or [])
        open_positions = len(positions)
        max_positions = max(1.0, self._extract_numeric(system.get("max_open_positions"), 1.0))
        trade_count = self._extract_numeric(system.get("trade_count"), 0.0)
        max_daily_trades = max(1.0, self._extract_numeric(system.get("max_daily_trades"), 1.0))
        daily_loss_value, daily_loss_cap = self._extract_fraction(system.get("daily_loss"))
        daily_loss_cap = max(1.0, daily_loss_cap)

        lines: List[Text] = []
        lines.append(
            Text.assemble(
                ("FRESH ", "bold white"),
                self._freshness_text(system.get("freshness"), system.get("market_age_seconds")),
                ("  API ", "bold white"),
                (str(system.get("api_latency") or "-"), self._WHITE),
            )
        )
        lines.append(
            Text.assemble(
                ("RISK/TRADE ", "bold white"),
                (str(system.get("risk_per_trade") or "-"), f"bold {self._RED}"),
                ("  COOL ", "bold white"),
                (str(system.get("cooling_down") or "No"), f"bold {self._EMBER}" if str(system.get("cooling_down") or "No") == "Yes" else f"bold {self._GREEN}"),
            )
        )
        lines.append(
            Text.assemble(
                ("LOSS ", "bold white"),
                self._meter_text(daily_loss_value, daily_loss_cap, width=16, filled_style=f"bold {self._RED}", suffix="THB"),
            )
        )
        lines.append(
            Text.assemble(
                ("ACTIVITY ", "bold white"),
                self._meter_text(trade_count, max_daily_trades, width=16, filled_style=f"bold {self._GREEN}", decimals=0),
            )
        )
        lines.append(
            Text.assemble(
                ("EXPOSURE ", "bold white"),
                self._meter_text(float(open_positions), max_positions, width=16, filled_style=f"bold {self._WHITE}", decimals=0),
            )
        )
        lines.append(
            Text.assemble(
                ("LOAD ", "bold white"),
                self._sparkline_text(self._trend_values("daily_loss", [daily_loss_value]), filled_style=f"bold {self._RED}"),
            )
        )
        lines.append(
            Text.assemble(
                ("POSITION TREND ", "bold white"),
                self._sparkline_text(self._trend_values("open_positions", [float(open_positions)]), filled_style=f"bold {self._WHITE}"),
            )
        )

        return self._panel(Group(*lines), title="Risk Rails", theme="risk")

    def _build_footer(self, snapshot: Dict[str, Any]) -> Panel:
        chat = snapshot.get("chat", {}) or {}
        ui_cfg = dict(snapshot.get("ui") or {})
        history = list(chat.get("history") or [])
        pending = chat.get("pending_confirmation") or {}
        suggestions = list(chat.get("suggestions") or [])
        footer_mode = str(ui_cfg.get("footer_mode") or "compact").lower()
        log_filter = str(ui_cfg.get("log_level_filter") or "INFO").upper()
        term_width = self.console.width if self.console else 120
        text_budget = max(28, int(term_width) - 26)
        pairs_budget = max(18, min(40, text_budget // 2))
        footer_budget = self._footer_content_budget(footer_mode)

        meta = Text.assemble(
            ("PAIRS ", f"bold {self._WHITE}"),
            (self._truncate_inline(snapshot.get("pairs", "NONE"), pairs_budget), self._WHITE),
            ("  ::  ", self._DIM),
            ("MODE ", f"bold {self._WHITE}"),
            (snapshot.get("mode", "-"), self._mode_style(snapshot.get("mode", "-"))),
            ("  ::  ", self._DIM),
            ("UPD ", f"bold {self._WHITE}"),
            (snapshot.get("updated_at", "-"), self._WHITE),
            ("  ::  ", self._DIM),
            ("LOG ", f"bold {self._WHITE}"),
            (f"{log_filter}+", self._WHITE),
            ("  ::  ", self._DIM),
            ("FOOTER ", f"bold {self._WHITE}"),
            (footer_mode, self._EMBER),
        )

        lines: List[Text] = [meta]
        status_text = str(chat.get("status") or snapshot.get("commands_hint") or "help")
        if pending:
            status_text = f"{status_text} | Pending confirm"
        status_text = f"[{str(snapshot.get('strategy_mode') or 'standard').lower()}] {status_text}"
        lines.append(
            Text.assemble(
                ("STATUS ", f"bold {self._WHITE}"),
                (self._truncate_inline(status_text, text_budget), self._WHITE),
            )
        )

        context_lines: List[Text] = []

        if pending:
            pending_line = Text.assemble(
                ("Pending: ", f"bold {self._EMBER}"),
                (self._truncate_inline(pending.get("summary") or pending.get("command_text") or "-", text_budget), self._EMBER),
            )
            context_lines.append(pending_line)
        elif footer_mode == "verbose":
            context_lines.append(Text("Pending: -", style=self._DIM))

        rendered_history: List[Text] = []
        if history:
            history_limit = 2
            for item in history[-history_limit:]:
                role = str(item.get("role") or "bot").lower()
                label = "You" if role == "user" else "Bot"
                style = f"bold {self._WHITE}"
                rendered_history.append(
                    Text.assemble(
                        (f"{label}: ", style),
                        (self._truncate_inline(item.get("message") or "-", text_budget), self._WHITE),
                    )
                )
        else:
            rendered_history.append(Text("Bot: Type command + Enter", style=self._WHITE))

        context_lines.extend(rendered_history)

        suggestion_limit = 5 if footer_mode == "verbose" else 3
        if suggestions:
            compact_suggestions = [str(item) for item in suggestions[:suggestion_limit]]
            context_lines.append(
                Text.assemble(
                    ("Suggestions: ", "bold white"),
                    (self._truncate_inline(" | ".join(compact_suggestions), text_budget), self._DIM),
                )
            )
        elif footer_mode == "verbose":
            context_lines.append(Text("Suggestions: -", style=self._DIM))

        body_slots = max(1, footer_budget - 3)
        if len(context_lines) > body_slots:
            if footer_mode == "verbose" and context_lines:
                preserved_head = [context_lines[0]]
                remaining_slots = max(0, body_slots - 1)
                preserved_tail = context_lines[-remaining_slots:] if remaining_slots else []
                context_lines = preserved_head + preserved_tail
            elif pending and context_lines:
                context_lines = [context_lines[0]]
            else:
                context_lines = context_lines[-body_slots:]

        while len(context_lines) < body_slots:
            context_lines.append(Text(" ", style=self._DIM))

        lines.extend(context_lines[:body_slots])

        lines.append(
            Text.assemble(
                ("Input> ", f"bold {self._EMBER}"),
                (self._truncate_inline(str(chat.get("input") or "") + "_", text_budget, preserve_tail=True), self._EMBER),
            )
        )
        return self._panel(Group(*lines), title="Command Chat", theme="footer")

    @staticmethod
    def _fmt_price(value: Any) -> str:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return "-"
        if numeric == 0:
            return "-"
        return f"{numeric:,.4f}"

    @staticmethod
    def _pnl_text(value: Any) -> Text:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return Text("-", style=CLICommandCenter._WHITE)
        style = f"bold {CLICommandCenter._GREEN}" if numeric >= 0 else f"bold {CLICommandCenter._RED}"
        prefix = "+" if numeric > 0 else ""
        return Text(f"{prefix}{numeric:.2f}%", style=style)

    @staticmethod
    def _side_text(side: str) -> Text:
        normalized = side.strip().lower()
        if normalized in {"buy", "long"}:
            return Text("LONG", style=f"bold {CLICommandCenter._GREEN}")
        if normalized in {"sell", "short"}:
            return Text("SHORT", style=f"bold {CLICommandCenter._RED}")
        return Text(normalized.upper() or "-", style=CLICommandCenter._WHITE)

    @staticmethod
    def _balance_breakdown_text(line: Any) -> Text:
        content = str(line or "-")
        if content == "-":
            return Text(content, style=CLICommandCenter._WHITE)

        main_part, separator, suffix = content.partition(" (")
        asset = main_part.split(" ", 1)[0].upper()
        allocation_pct = CLICommandCenter._extract_allocation_pct(content)
        if asset == "THB":
            main_style = f"bold {CLICommandCenter._EMBER}"
        elif allocation_pct >= 50.0:
            main_style = f"bold {CLICommandCenter._GREEN}"
        elif allocation_pct >= 20.0:
            main_style = f"bold {CLICommandCenter._MINT}"
        else:
            main_style = f"bold {CLICommandCenter._WHITE}"
        text = Text()
        text.append(main_part, style=main_style)
        if separator:
            text.append(f" ({suffix}", style=CLICommandCenter._DIM)
        return text

    @staticmethod
    def _extract_allocation_pct(content: str) -> float:
        match = re.search(r"\(([-+]?[0-9]+(?:\.[0-9]+)?)%\)", str(content or ""))
        if not match:
            return 0.0
        try:
            return float(match.group(1))
        except ValueError:
            return 0.0

    @staticmethod
    def _extract_numeric(value: Any, default: float = 0.0) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        matches = re.findall(r"[-+]?[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?", str(value or ""))
        if not matches:
            return float(default)
        try:
            return float(matches[0].replace(",", ""))
        except ValueError:
            return float(default)

    @staticmethod
    def _extract_fraction(value: Any) -> tuple[float, float]:
        matches = re.findall(r"[-+]?[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?", str(value or ""))
        if len(matches) < 2:
            baseline = CLICommandCenter._extract_numeric(value, 0.0)
            return baseline, max(1.0, baseline)
        try:
            left = float(matches[0].replace(",", ""))
            right = float(matches[1].replace(",", ""))
            return left, right
        except ValueError:
            baseline = CLICommandCenter._extract_numeric(value, 0.0)
            return baseline, max(1.0, baseline)

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _pair_count(snapshot: Dict[str, Any]) -> int:
        pairs_value = str(snapshot.get("pairs") or "").strip()
        if not pairs_value or pairs_value == "NONE":
            return 0
        return len([item for item in pairs_value.split(",") if item.strip()])

    @staticmethod
    def _resolve_positions_theme(avg_pnl_pct: float) -> str:
        if avg_pnl_pct > 0.05:
            return "positions_hot"
        if avg_pnl_pct < -0.05:
            return "positions_cold"
        return "positions"

    @staticmethod
    def _resolve_signal_theme(rows: List[Dict[str, Any]]) -> str:
        score = CLICommandCenter._signal_score(rows)
        if score > 1.0:
            return "signal_hot"
        if score < -1.0:
            return "signal_cold"
        return "signal"

    @staticmethod
    def _resolve_portfolio_theme(top_allocation_pct: float, cash_allocation_pct: float) -> str:
        if top_allocation_pct >= 70.0:
            return "portfolio_hot"
        if cash_allocation_pct >= 35.0:
            return "portfolio_cold"
        return "portfolio"

    @staticmethod
    def _signal_score(rows: List[Dict[str, Any]]) -> float:
        score = 0.0
        for row in rows:
            action = str(row.get("action") or "").upper()
            trend = str(row.get("trend") or "").upper()
            status = str(row.get("status") or row.get("pair_state") or "").upper()
            if action == "BUY":
                score += 2.0
            elif action == "SELL":
                score -= 2.0
            elif action == "WAIT":
                score -= 0.5
            if trend == "UP":
                score += 1.0
            elif trend == "DOWN":
                score -= 1.0
            if "WAIT" in status or "LAG" in status:
                score -= 0.5
        return score

    @staticmethod
    def _signal_quality_label(score: float) -> str:
        if score > 1.0:
            return "BULLISH"
        if score < -1.0:
            return "DEFENSIVE"
        return "NEUTRAL"

    @staticmethod
    def _signal_quality_style(score: float) -> str:
        if score > 1.0:
            return f"bold {CLICommandCenter._GREEN}"
        if score < -1.0:
            return f"bold {CLICommandCenter._RED}"
        return f"bold {CLICommandCenter._EMBER}"

    @staticmethod
    def _format_meter_value(value: float, decimals: int = 2) -> str:
        if decimals <= 0:
            return f"{int(round(value))}"
        return f"{value:.{decimals}f}"

    @staticmethod
    def _sparkline_text(values: List[float], filled_style: str = "bold white") -> Text:
        glyphs = "▁▂▃▄▅▆▇█"
        cleaned: List[float] = []
        for item in values:
            try:
                cleaned.append(max(0.0, float(item or 0.0)))
            except (TypeError, ValueError):
                cleaned.append(0.0)
        if not cleaned:
            return Text("-", style=CLICommandCenter._DIM)
        peak = max(cleaned)
        if peak <= 0:
            return Text(glyphs[0] * len(cleaned), style=CLICommandCenter._DIM)

        text = Text()
        for value in cleaned:
            ratio = max(0.0, min(1.0, value / peak))
            index = min(len(glyphs) - 1, int(round(ratio * (len(glyphs) - 1))))
            glyph = glyphs[index]
            style = filled_style if value > 0 else CLICommandCenter._DIM
            text.append(glyph, style=style)
        return text

    @classmethod
    def _meter_text(
        cls,
        value: float,
        capacity: float,
        *,
        width: int = 18,
        filled_style: str = "bold white",
        decimals: int = 2,
        suffix: str = "",
    ) -> Text:
        safe_capacity = max(1.0, float(capacity or 0.0))
        safe_value = max(0.0, float(value or 0.0))
        normalized = max(0.0, min(1.0, safe_value / safe_capacity))
        filled_units = int(round(normalized * width))
        if safe_value > 0:
            filled_units = max(1, filled_units)
        filled_units = min(width, filled_units)
        empty_units = max(0, width - filled_units)

        text = Text()
        text.append("[", style=cls._DIM)
        if filled_units:
            text.append("█" * filled_units, style=filled_style)
        if empty_units:
            text.append("░" * empty_units, style=cls._DIM)
        text.append("] ", style=cls._DIM)
        text.append(cls._format_meter_value(safe_value, decimals=decimals), style=cls._WHITE)
        text.append("/", style=cls._DIM)
        text.append(cls._format_meter_value(safe_capacity, decimals=decimals), style=cls._WHITE)
        if suffix:
            text.append(f" {suffix}", style=cls._DIM)
        return text

    @staticmethod
    def _allocation_bar_text(allocation_pct: float, asset: str = "") -> Text:
        normalized_pct = max(0.0, min(100.0, float(allocation_pct or 0.0)))
        filled_units = math.ceil(normalized_pct / 5.0) if normalized_pct > 0 else 0
        if normalized_pct > 0:
            filled_units = max(1, filled_units)
        filled_units = min(20, filled_units)
        empty_units = max(0, 20 - filled_units)

        normalized_asset = str(asset or "").upper()
        if normalized_asset == "THB":
            filled_style = f"bold {CLICommandCenter._EMBER}"
        elif normalized_pct >= 50.0:
            filled_style = f"bold {CLICommandCenter._GREEN}"
        elif normalized_pct >= 20.0:
            filled_style = f"bold {CLICommandCenter._MINT}"
        else:
            filled_style = f"bold {CLICommandCenter._WHITE}"

        text = Text()
        text.append("[", style=CLICommandCenter._DIM)
        if filled_units:
            text.append("#" * filled_units, style=filled_style)
        if empty_units:
            text.append("-" * empty_units, style=CLICommandCenter._DIM)
        text.append("]", style=CLICommandCenter._DIM)
        text.append(f" {normalized_pct:5.2f}%", style=CLICommandCenter._DIM)
        return text

    @staticmethod
    def _fmt_distance_pct(value: Any) -> Text:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return Text("-", style=CLICommandCenter._WHITE)
        if numeric > 0:
            return Text(f"+{numeric:.2f}%", style=f"bold {CLICommandCenter._GREEN}")
        if numeric < 0:
            return Text(f"{numeric:.2f}%", style=f"bold {CLICommandCenter._RED}")
        return Text("0.00%", style=f"bold {CLICommandCenter._EMBER}")

    @staticmethod
    def _freshness_text(state: Any, age_seconds: Any) -> Text:
        label = str(state or "fresh").lower()
        suffix = ""
        try:
            if age_seconds is not None:
                suffix = f" ({int(age_seconds)}s)"
        except (TypeError, ValueError):
            suffix = ""

        if label == "critical":
            return Text(f"STALE{suffix}", style=f"bold {CLICommandCenter._RED}")
        if label == "warning":
            return Text(f"DELAYED{suffix}", style=f"bold {CLICommandCenter._EMBER}")
        return Text(f"FRESH{suffix}", style=f"bold {CLICommandCenter._GREEN}")

    @staticmethod
    def _api_latency_text(value: Any) -> Text:
        raw = str(value or "-")
        try:
            ms = int(raw.replace("ms", "").replace(" ", "").strip())
        except (TypeError, ValueError):
            return Text(raw, style=CLICommandCenter._WHITE)
        if ms >= 2000:
            return Text(f"{ms} ms", style=f"bold {CLICommandCenter._RED}")
        if ms >= 800:
            return Text(f"{ms} ms", style=f"bold {CLICommandCenter._EMBER}")
        return Text(f"{ms} ms", style=f"bold {CLICommandCenter._GREEN}")

    @staticmethod
    def _service_health_text(value: Any) -> Text:
        raw = str(value or "-")
        normalized = raw.upper()
        if normalized.startswith("OK") or normalized in {"CONNECTED", "FRESH"}:
            return Text(raw, style=f"bold {CLICommandCenter._GREEN}")
        if normalized.startswith("STALE") or normalized in {"STOPPED", "DISCONNECTED", "FAILED"}:
            return Text(raw, style=f"bold {CLICommandCenter._RED}")
        if normalized in {"OFF", "NO DATA", "CONNECTING", "RECONNECTING", "NOT_STARTED"}:
            return Text(raw, style=f"bold {CLICommandCenter._EMBER}")
        return Text(raw, style=CLICommandCenter._WHITE)
