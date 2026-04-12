"""Rich-powered terminal command center for the crypto bot."""

from __future__ import annotations

import logging
import re
import math
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from rich.align import Align
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
        except Exception:
            self.handleError(record)


class CLICommandCenter:
    """Render a live terminal dashboard using Rich."""

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
        self._log_lines: deque[Dict[str, str]] = deque(maxlen=140)
        self._log_lock = threading.Lock()
        self._log_handler: Optional[_UILogBufferHandler] = None

    def start_log_capture(self) -> None:
        """Start mirroring runtime logs into an in-memory ring buffer for UI rendering."""
        if self._log_handler is not None:
            return
        handler = _UILogBufferHandler(self._append_log_record)
        root = logging.getLogger()
        root.addHandler(handler)
        self._log_handler = handler

    def stop_log_capture(self) -> None:
        """Detach runtime log mirroring handler."""
        handler = self._log_handler
        if handler is None:
            return
        root = logging.getLogger()
        try:
            root.removeHandler(handler)
        except Exception:
            pass
        self._log_handler = None

    def _append_log_record(self, record: logging.LogRecord) -> None:
        if record.name == __name__:
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

        with self._log_lock:
            self._log_lines.append(
                {
                    "timestamp": timestamp,
                    "level": record.levelname.upper(),
                    "logger": logger_name,
                    "message": rendered,
                }
            )

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

    def render(self) -> Layout:
        """Build the latest dashboard layout from the app snapshot."""
        snapshot = self.app.get_cli_snapshot(bot_name=self.bot_name)
        ui_cfg = dict(snapshot.get("ui") or {})
        footer_mode = str(ui_cfg.get("footer_mode") or "compact").lower()

        # Adaptive footer size: compact chat area while preserving input visibility.
        term_height = self.console.height if self.console else 30
        term_width = self.console.width if self.console else 120
        compact_mode = term_width < 140 or term_height < 30
        if footer_mode == "verbose":
            footer_size = max(7, min(11, term_height // 3))
        else:
            footer_size = max(4, min(8, term_height // 4))

        layout = Layout(name="root")
        layout.split_column(
            Layout(self._build_header(snapshot), size=3, name="header"),
            Layout(name="body"),
            Layout(self._build_footer(snapshot), size=footer_size, name="footer"),
        )
        if compact_mode:
            layout["body"].split_column(
                Layout(self._build_positions_table(snapshot, compact=True), ratio=3, name="positions"),
                Layout(self._build_system_status_table(snapshot), ratio=2, name="system"),
                Layout(self._build_signal_alignment_panel(snapshot), ratio=2, name="alignment"),
                Layout(self._build_balance_breakdown_panel(snapshot), ratio=2, name="portfolio"),
                Layout(self._build_log_stream_panel(snapshot), ratio=2, name="logs"),
            )
        else:
            layout["body"].split_row(
                Layout(self._build_positions_table(snapshot, compact=False), ratio=3, name="positions"),
                Layout(name="sidebar", ratio=2),
            )
            layout["sidebar"].split_column(
                Layout(self._build_system_status_table(snapshot), ratio=2, name="system"),
                Layout(self._build_signal_alignment_panel(snapshot), ratio=2, name="alignment"),
                Layout(self._build_balance_breakdown_panel(snapshot), ratio=3, name="portfolio"),
                Layout(self._build_log_stream_panel(snapshot), ratio=2, name="logs"),
            )
        return layout

    @staticmethod
    def _level_style(level: str) -> str:
        value = str(level or "").upper()
        if value in {"CRITICAL", "FATAL"}:
            return "bold white on red"
        if value == "ERROR":
            return "bold red"
        if value == "WARNING":
            return "bold yellow"
        if value == "INFO":
            return "bold cyan"
        return "bright_black"

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
        min_level_no = self._level_no(min_level)

        with self._log_lock:
            rows = list(self._log_lines)[-8:]

        rows = [row for row in rows if self._level_no(row.get("level", "INFO")) >= min_level_no]

        if not rows:
            return Panel(
                Text(f"Waiting for runtime logs ({min_level}+)...", style="bright_black"),
                title="Terminal Log Stream",
                border_style="bright_black",
            )

        lines: List[Text] = []
        for row in rows:
            level = str(row.get("level") or "INFO")
            lines.append(
                Text.assemble(
                    (f"{row.get('timestamp', '-')} ", "bright_black"),
                    (f"{level:<8}", self._level_style(level)),
                    (f" {row.get('logger', '-'):<14}", "magenta"),
                    (f" {row.get('message', '-')}", "white"),
                )
            )

        return Panel(Group(*lines), title=f"Terminal Log Stream [{min_level}+ ]", border_style="blue")

    def _build_header(self, snapshot: Dict[str, Any]) -> Panel:
        mode = snapshot.get("mode", "UNKNOWN")
        strategy_mode = str(snapshot.get("strategy_mode") or "standard").lower()
        mode_style = self._mode_style(mode)
        risk_text = Text(str(snapshot.get("risk_level", "UNKNOWN")), style=snapshot.get("risk_style", "bold white"))
        header = Text.assemble(
            (snapshot.get("bot_name", self.bot_name), "bold cyan"),
            "    ",
            ("Mode: ", "bold white"),
            (mode, mode_style),
            "    ",
            ("Strategy: ", "bold white"),
            (strategy_mode, "bold magenta"),
            "    ",
            ("Risk: ", "bold white"),
            risk_text,
        )
        return Panel(Align.left(header), border_style="blue")

    @staticmethod
    def _mode_style(mode: str) -> str:
        normalized = str(mode or "").strip().upper()
        if normalized == "LIVE":
            return "bold green"
        if normalized in {"SEMI AUTO", "SIMULATION"}:
            return "bold yellow"
        if normalized in {"READ ONLY", "DEGRADED"}:
            return "bold red"
        return "bold white"

    def _build_positions_table(self, snapshot: Dict[str, Any], compact: bool = False) -> Panel:
        table = Table(expand=True)
        table.add_column("Symbol", style="bold cyan")
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
                dist_text = Text.assemble(("SL ", "bright_black"), sl_dist, (" | TP ", "bright_black"), tp_dist)
                if compact:
                    table.add_row(
                        str(position.get("symbol", "-")),
                        self._side_text(str(position.get("side", "-"))),
                        self._fmt_price(position.get("entry_price")),
                        self._fmt_price(position.get("current_price")),
                        self._pnl_text(position.get("pnl_pct")),
                        dist_text,
                    )
                else:
                    table.add_row(
                        str(position.get("symbol", "-")),
                        self._side_text(str(position.get("side", "-"))),
                        self._fmt_price(position.get("entry_price")),
                        self._fmt_price(position.get("current_price")),
                        self._pnl_text(position.get("pnl_pct")),
                        sltp_text,
                        dist_text,
                    )

        return Panel(table, title="Open Positions", border_style="magenta")

    def _build_system_status_table(self, snapshot: Dict[str, Any]) -> Panel:
        table = Table(expand=True, show_header=False)
        table.add_column("Metric", style="bold white")
        table.add_column("Value", justify="right")

        system = snapshot.get("system", {})
        for label, value in (
            ("Last Market Update", system.get("last_market_update", "-")),
            ("Data Freshness", self._freshness_text(system.get("freshness"), system.get("market_age_seconds"))),
            ("API Latency", system.get("api_latency", "-")),
            ("Available Balance", system.get("available_balance", "-")),
            ("Total Balance", system.get("total_balance", "-")),
            ("Today's Trades", f"{system.get('trade_count', '-')}/{system.get('max_daily_trades', '-')}"),
            ("Risk/Trade", system.get("risk_per_trade", "-")),
            ("Daily Loss", f"{system.get('daily_loss', '-')} ({system.get('daily_loss_pct', '-')})"),
            ("Max Positions", system.get("max_open_positions", "-")),
            ("Cooldown", system.get("cooling_down", "-")),
        ):
            table.add_row(label, str(value))

        # Show degraded mode reason if present
        degraded_reason = snapshot.get("auth_degraded_reason", "")
        if degraded_reason:
            table.add_row(
                Text("⚠ Degraded", style="bold red"),
                Text(str(degraded_reason), style="red"),
            )

        return Panel(table, title="System Status", border_style="green")

    def _build_signal_alignment_panel(self, snapshot: Dict[str, Any]) -> Panel:
        table = Table(expand=True)
        table.add_column("Pair", style="bold cyan")
        table.add_column("TF", justify="center")
        table.add_column("Upd", justify="center")
        table.add_column("M/m/T", justify="center")
        table.add_column("Trend", justify="center")
        table.add_column("Trigger", justify="center")
        table.add_column("Action", justify="center")
        table.add_column("Status", overflow="fold")

        rows = list(snapshot.get("signal_alignment") or [])
        if not rows:
            table.add_row("-", "-", "-", "-", "-", "-", "No runtime trading pairs")
        else:
            for row in rows[:8]:
                action = str(row.get("action") or "HOLD").upper()
                if action == "BUY":
                    action_text = Text("BUY", style="bold green")
                elif action == "SELL":
                    action_text = Text("SELL", style="bold red")
                elif action == "WAIT":
                    action_text = Text("WAIT", style="bold cyan")
                else:
                    action_text = Text("HOLD", style="bold yellow")
                macro_value = str(row.get("macro") or "N/A")
                micro_value = str(row.get("micro") or "N/A")
                trigger_value = str(row.get("trigger") or "N/A")
                macro = "-" if macro_value.upper() == "N/A" else macro_value[0:1]
                micro = "-" if micro_value.upper() == "N/A" else micro_value[0:1]
                trigger = "-" if trigger_value.upper() == "N/A" else trigger_value[0:1]
                table.add_row(
                    str(row.get("symbol") or "-"),
                    str(row.get("tf_ready") or "-"),
                    str(row.get("market_update") or "-"),
                    f"{macro}/{micro}/{trigger}",
                    str(row.get("trend") or "MIXED"),
                    str(row.get("trigger_side") or "NONE"),
                    action_text,
                    str(row.get("status") or row.get("pair_state") or "Ready"),
                )

        return Panel(table, title="Signal Alignment", border_style="cyan")

    def _build_recent_events_panel(self, snapshot: Dict[str, Any]) -> Panel:
        rows = list(snapshot.get("recent_events") or [])
        if not rows:
            return Panel(Text("No recent events", style="bright_black"), title="Recent Events", border_style="bright_black")

        lines: List[Text] = []
        for row in rows[:5]:
            event_type = str(row.get("type") or "EVT").upper()
            if event_type == "TRADE":
                style = "bold green"
            elif "WITHDRAW" in event_type or "LOW" in event_type:
                style = "bold red"
            else:
                style = "bold cyan"
            timestamp = str(row.get("timestamp") or "-")
            message = str(row.get("message") or "-")
            lines.append(Text.assemble((f"{timestamp} ", "bright_black"), (f"[{event_type}] ", style), (message, "white")))

        return Panel(Group(*lines), title="Recent Events", border_style="bright_black")

    def _build_balance_breakdown_panel(self, snapshot: Dict[str, Any]) -> Panel:
        system = snapshot.get("system", {})
        breakdown_lines = list(system.get("balance_breakdown") or [])

        table = Table(expand=True, show_header=False)
        table.add_column("Holding", style="bold white")
        table.add_column("Allocation", justify="right")

        if not breakdown_lines:
            table.add_row(Text("No balance breakdown", style="white"), Text("-", style="white"))
        else:
            for line in breakdown_lines:
                allocation_pct = self._extract_allocation_pct(line)
                asset = str(line or "-").split(" ", 1)[0].upper()
                table.add_row(
                    self._balance_breakdown_text(line),
                    self._allocation_bar_text(allocation_pct, asset=asset),
                )

        return Panel(table, title="Portfolio Breakdown", border_style="yellow")

    def _build_footer(self, snapshot: Dict[str, Any]) -> Panel:
        chat = snapshot.get("chat", {}) or {}
        ui_cfg = dict(snapshot.get("ui") or {})
        history = list(chat.get("history") or [])
        pending = chat.get("pending_confirmation") or {}
        suggestions = list(chat.get("suggestions") or [])
        footer_mode = str(ui_cfg.get("footer_mode") or "compact").lower()
        log_filter = str(ui_cfg.get("log_level_filter") or "INFO").upper()

        meta = Text.assemble(
            ("Pairs ", "bold white"),
            (snapshot.get("pairs", "NONE"), "cyan"),
            "  ",
            ("Mode ", "bold white"),
            (snapshot.get("mode", "-"), "green"),
            "  ",
            ("Upd ", "bold white"),
            (snapshot.get("updated_at", "-"), "white"),
            "  ",
            ("Log ", "bold white"),
            (f"{log_filter}+", "blue"),
            "  ",
            ("Footer ", "bold white"),
            (footer_mode, "yellow"),
        )

        lines: List[Text] = [meta]
        status_text = str(chat.get("status") or snapshot.get("commands_hint") or "help")
        if pending:
            status_text = f"{status_text} | Pending confirm"
        status_text = f"[{str(snapshot.get('strategy_mode') or 'standard').lower()}] {status_text}"
        lines.append(Text.assemble(("Status ", "bold white"), (status_text, "bright_black")))

        if pending:
            lines.append(
                Text.assemble(
                    ("Pending ", "bold yellow"),
                    (str(pending.get("summary") or pending.get("command_text") or "-"), "yellow"),
                )
            )

        history_limit = 4 if footer_mode == "verbose" else 2
        if history:
            for item in history[-history_limit:]:
                role = str(item.get("role") or "bot").lower()
                label = "You" if role == "user" else "Bot"
                style = "bold cyan" if role == "user" else "bold green"
                lines.append(Text.assemble((f"{label}: ", style), (str(item.get("message") or "-"), "white")))
        else:
            lines.append(Text("Bot: Type command + Enter", style="white"))

        suggestion_limit = 5 if footer_mode == "verbose" else 3
        if suggestions:
            compact_suggestions = [str(item) for item in suggestions[:suggestion_limit]]
            lines.append(
                Text.assemble(
                    ("Tips ", "bold white"),
                    (" | ".join(compact_suggestions), "bright_black"),
                )
            )

        lines.append(
            Text.assemble(
                ("> ", "bold yellow"),
                (str(chat.get("input") or ""), "yellow"),
            )
        )
        return Panel(Group(*lines), title="Command Chat", border_style="bright_black")

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
            return Text("-", style="white")
        style = "bold green" if numeric >= 0 else "bold red"
        prefix = "+" if numeric > 0 else ""
        return Text(f"{prefix}{numeric:.2f}%", style=style)

    @staticmethod
    def _side_text(side: str) -> Text:
        normalized = side.strip().lower()
        if normalized in {"buy", "long"}:
            return Text("LONG", style="bold green")
        if normalized in {"sell", "short"}:
            return Text("SHORT", style="bold red")
        return Text(normalized.upper() or "-", style="white")

    @staticmethod
    def _balance_breakdown_text(line: Any) -> Text:
        content = str(line or "-")
        if content == "-":
            return Text(content, style="white")

        main_part, separator, suffix = content.partition(" (")
        asset = main_part.split(" ", 1)[0].upper()
        allocation_pct = CLICommandCenter._extract_allocation_pct(content)
        if asset == "THB":
            main_style = "bold yellow"
        elif allocation_pct >= 50.0:
            main_style = "bold bright_green"
        elif allocation_pct >= 20.0:
            main_style = "bold cyan"
        else:
            main_style = "bold white"
        text = Text()
        text.append(main_part, style=main_style)
        if separator:
            text.append(f" ({suffix}", style="bright_black")
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
    def _allocation_bar_text(allocation_pct: float, asset: str = "") -> Text:
        normalized_pct = max(0.0, min(100.0, float(allocation_pct or 0.0)))
        filled_units = math.ceil(normalized_pct / 5.0) if normalized_pct > 0 else 0
        if normalized_pct > 0:
            filled_units = max(1, filled_units)
        filled_units = min(20, filled_units)
        empty_units = max(0, 20 - filled_units)

        normalized_asset = str(asset or "").upper()
        if normalized_asset == "THB":
            filled_style = "bold yellow"
        elif normalized_pct >= 50.0:
            filled_style = "bold bright_green"
        elif normalized_pct >= 20.0:
            filled_style = "bold cyan"
        else:
            filled_style = "bold white"

        text = Text()
        text.append("[", style="bright_black")
        if filled_units:
            text.append("#" * filled_units, style=filled_style)
        if empty_units:
            text.append("-" * empty_units, style="bright_black")
        text.append("]", style="bright_black")
        text.append(f" {normalized_pct:5.2f}%", style="bright_black")
        return text

    @staticmethod
    def _fmt_distance_pct(value: Any) -> Text:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return Text("-", style="white")
        if numeric > 0:
            return Text(f"+{numeric:.2f}%", style="bold green")
        if numeric < 0:
            return Text(f"{numeric:.2f}%", style="bold red")
        return Text("0.00%", style="bold yellow")

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
            return Text(f"STALE{suffix}", style="bold red")
        if label == "warning":
            return Text(f"DELAYED{suffix}", style="bold yellow")
        return Text(f"FRESH{suffix}", style="bold green")
