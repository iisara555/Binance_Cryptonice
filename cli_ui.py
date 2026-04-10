"""Rich-powered terminal command center for the crypto bot."""

from __future__ import annotations

import re
import math
from typing import Any, Dict, List, Optional

from rich.align import Align
from rich.console import Group
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from logger_setup import get_shared_console


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

        # Adaptive footer size: cap at ~1/3 of terminal height, min 5
        term_height = self.console.height if self.console else 30
        term_width = self.console.width if self.console else 120
        compact_mode = term_width < 140 or term_height < 30
        footer_size = max(5, min(11, term_height // 3))

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
                Layout(self._build_recent_events_panel(snapshot), ratio=1, name="events"),
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
                Layout(self._build_recent_events_panel(snapshot), ratio=1, name="events"),
            )
        return layout

    def _build_header(self, snapshot: Dict[str, Any]) -> Panel:
        mode = snapshot.get("mode", "UNKNOWN")
        mode_style = self._mode_style(mode)
        risk_text = Text(str(snapshot.get("risk_level", "UNKNOWN")), style=snapshot.get("risk_style", "bold white"))
        header = Text.assemble(
            (snapshot.get("bot_name", self.bot_name), "bold cyan"),
            "    ",
            ("Mode: ", "bold white"),
            (mode, mode_style),
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
        table.add_column("M/m/T", justify="center")
        table.add_column("Trend", justify="center")
        table.add_column("Trigger", justify="center")
        table.add_column("Action", justify="center")

        rows = list(snapshot.get("signal_alignment") or [])
        if not rows:
            table.add_row("-", "-", "-", "-", "-")
        else:
            for row in rows[:8]:
                action = str(row.get("action") or "HOLD").upper()
                if action == "BUY":
                    action_text = Text("BUY", style="bold green")
                elif action == "SELL":
                    action_text = Text("SELL", style="bold red")
                else:
                    action_text = Text("HOLD", style="bold yellow")
                macro = str(row.get("macro") or "N/A")[0:1]
                micro = str(row.get("micro") or "N/A")[0:1]
                trigger = str(row.get("trigger") or "N/A")[0:1]
                table.add_row(
                    str(row.get("symbol") or "-"),
                    f"{macro}/{micro}/{trigger}",
                    str(row.get("trend") or "MIXED"),
                    str(row.get("trigger_side") or "NONE"),
                    action_text,
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
        history = list(chat.get("history") or [])
        pending = chat.get("pending_confirmation") or {}
        suggestions = list(chat.get("suggestions") or [])

        meta = Text.assemble(
            ("Pairs: ", "bold white"),
            (snapshot.get("pairs", "NONE"), "cyan"),
            "    ",
            ("Strategies: ", "bold white"),
            (snapshot.get("strategies", "-"), "magenta"),
            "    ",
            ("Updated: ", "bold white"),
            (snapshot.get("updated_at", "-"), "white"),
        )

        lines: List[Text] = [meta]
        lines.append(Text.assemble(("Status: ", "bold white"), (str(chat.get("status") or snapshot.get("commands_hint") or "help"), "bright_black")))

        if pending:
            lines.append(
                Text.assemble(
                    ("Pending: ", "bold yellow"),
                    (str(pending.get("summary") or pending.get("command_text") or "-"), "yellow"),
                )
            )

        if history:
            for item in history:
                role = str(item.get("role") or "bot").lower()
                label = "You" if role == "user" else "Bot"
                style = "bold cyan" if role == "user" else "bold green"
                lines.append(Text.assemble((f"{label}: ", style), (str(item.get("message") or "-"), "white")))
        else:
            lines.append(Text("Bot: Type a command below and press Enter", style="white"))

        if suggestions:
            lines.append(
                Text.assemble(
                    ("Suggestions: ", "bold white"),
                    (" | ".join(str(item) for item in suggestions), "bright_black"),
                )
            )

        lines.append(
            Text.assemble(
                ("Input> ", "bold yellow"),
                (str(chat.get("input") or ""), "yellow"),
                ("_", "yellow"),
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
