"""Rich-powered terminal command center for the crypto bot."""

from __future__ import annotations

import json
import logging
import math
import re
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, List, Optional

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from helpers import format_exchange_time
from logger_setup import get_shared_console

try:
    from log_formatter import format_log_row as _format_log_row, get_tag as _get_log_tag
except Exception:  # pragma: no cover
    _format_log_row = None  # type: ignore[assignment]
    _get_log_tag = None  # type: ignore[assignment]

try:
    from signal_generator import get_latest_signal_flow_snapshot
except Exception:  # pragma: no cover - defensive: keep dashboard alive if module fails

    def get_latest_signal_flow_snapshot() -> Dict[str, Dict[str, Any]]:
        return {}


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

    _NOISY_INFO_LOGGERS = {"signal_flow", "legacy_bitkub_websocket", "bitkub_websocket", "websocket"}
    _CASH_ASSETS = {"USDT", "THB"}
    _GREEN = "#10b981"
    _MINT = _GREEN
    _RED = "#ef4444"
    _WHITE = "white"
    _EMBER = "#f59e0b"
    _BLUE = "#3b82f6"
    _CYAN = "#22d3ee"
    _PURPLE = "#a78bfa"
    _DIM = "dim"
    _BORDER_DIM = "#555555"
    _PANEL_THEME = {
        "header": (_CYAN, f"bold {_CYAN}"),
        "logs": (_BORDER_DIM, f"bold {_WHITE}"),
        "positions": (_BORDER_DIM, f"bold {_WHITE}"),
        "positions_hot": (_GREEN, f"bold {_GREEN}"),
        "positions_cold": (_RED, f"bold {_RED}"),
        "overview": (_BLUE, f"bold {_BLUE}"),
        "system": (_BORDER_DIM, f"bold {_WHITE}"),
        "signal": (_PURPLE, f"bold {_PURPLE}"),
        "signal_hot": (_GREEN, f"bold {_GREEN}"),
        "signal_cold": (_RED, f"bold {_RED}"),
        "signal_flow": (_CYAN, f"bold {_CYAN}"),
        "events": (_BORDER_DIM, f"bold {_WHITE}"),
        "portfolio": (_EMBER, f"bold {_EMBER}"),
        "portfolio_hot": (_RED, f"bold {_RED}"),
        "portfolio_cold": (_GREEN, f"bold {_GREEN}"),
        "risk": (_EMBER, f"bold {_EMBER}"),
        "footer": (_BORDER_DIM, f"bold {_WHITE}"),
    }
    # SigFlow block: max chars for "Why" (table column max_width should fit this + ellipsis).
    _SIGFLOW_WHY_MAX_LEN = 60

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

    def _terminal_compact_mode(self) -> bool:
        """True when terminal is narrow/short — stacked mobile-friendly layout."""
        term_width, term_height = self._layout_term_dimensions(self.console)
        return term_width < 140 or term_height < 30

    def _terminal_tablet_mode(self) -> bool:
        """True for tablet range (100–139 wide, ≥30 tall) — 2-column layout with wide logs."""
        term_width, term_height = self._layout_term_dimensions(self.console)
        return 100 <= term_width < 140 and term_height >= 30

    @staticmethod
    def _layout_term_dimensions(console: Any) -> tuple[int, int]:
        """Width/height for layout decisions.

        Rich only fixes ``size`` when both ``_width`` and ``_height`` are set; a lone
        ``Console(width=140)`` still reports 80 columns via ``.width``. Prefer stored
        ``_width`` / ``_height`` when present so tests and non-TTY sizing behave.
        """
        if console is None:
            return 120, 30
        fw = getattr(console, "_width", None)
        fh = getattr(console, "_height", None)
        if fw is not None and fh is not None:
            try:
                w = max(int(fw) - int(bool(getattr(console, "legacy_windows", False))), 1)
                return w, max(int(fh), 1)
            except (TypeError, ValueError):
                pass
        if fw is not None:
            try:
                return max(int(fw), 1), max(int(getattr(console, "height", None) or 30), 1)
            except (TypeError, ValueError):
                pass
        try:
            w = int(console.width)
            h = int(console.height)
            return (w if w > 0 else 120), (h if h > 0 else 30)
        except Exception:
            return 120, 30

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
        return isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) in {
            sys.stdout,
            sys.stderr,
        }

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

        raw = record.getMessage().replace("\n", " ").strip()
        if not raw:
            return

        if _format_log_row is not None:
            try:
                row = _format_log_row(record)
                # Override timestamp to exchange timezone (UTC+7) for display consistency
                row["timestamp"] = format_exchange_time(datetime.fromtimestamp(record.created, tz=timezone.utc))
            except Exception:
                ts = format_exchange_time(datetime.fromtimestamp(record.created, tz=timezone.utc))
                tag = str(record.name or "root").split(".")[-1][-4:].upper()
                msg = raw[:150] if len(raw) > 150 else raw
                row = {"timestamp": ts, "level": record.levelname.upper(), "tag": tag, "message": msg, "emoji": ""}
        else:
            ts = format_exchange_time(datetime.fromtimestamp(record.created, tz=timezone.utc))
            tag = str(record.name or "root").split(".")[-1]
            if len(tag) > 14:
                tag = tag[-14:]
            msg = raw[:150] if len(raw) > 150 else raw
            row = {"timestamp": ts, "level": record.levelname.upper(), "tag": tag, "message": msg, "emoji": ""}

        if not self._log_lock.acquire(blocking=False):
            self._dropped_log_count += 1
            if self._dropped_log_count == 1:
                self._safe_stderr_write(
                    "[cli_ui] warning: dashboard log buffer busy; dropping log records until lock is available\n"
                )
            return
        try:
            if self._dropped_log_count > 0:
                dropped_count = self._dropped_log_count
                self._dropped_log_count = 0
                ts = format_exchange_time(datetime.fromtimestamp(record.created, tz=timezone.utc))
                self._log_lines.append(
                    {
                        "timestamp": ts,
                        "level": "WARNING",
                        "tag": "UI  ",
                        "emoji": "⚠️",
                        "message": f"Dropped {dropped_count} log record(s) due to dashboard lock contention",
                    }
                )
            self._log_lines.append(row)
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
            screen=True,
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
        term_width, term_height = self._layout_term_dimensions(self.console)
        compact_mode = self._terminal_compact_mode()
        tablet_mode = self._terminal_tablet_mode()
        footer_size = self._resolve_footer_size(term_width, term_height, footer_mode)

        layout = Layout(name="root")
        if compact_mode:
            layout.split_column(
                Layout(self._build_compact_status_bar(snapshot), size=3, name="header"),
                Layout(name="body"),
                Layout(self._build_footer(snapshot), size=footer_size, name="footer"),
            )
        else:
            layout.split_column(
                Layout(self._build_header(snapshot), size=2, name="header"),
                Layout(name="body"),
                Layout(self._build_footer(snapshot), size=footer_size, name="footer"),
            )
        if tablet_mode:
            # Tablet: 2-column — left: positions+risk+sigflow, right: logs (full height = wider)
            n_pos_tablet = len(list(snapshot.get("positions") or []))
            # summary(1) + pbar(1) + header_row(1) + data_rows + border(2) + padding(3) — min 10, max 15
            pos_size_tablet = max(10, min(15, n_pos_tablet + 8))
            layout["body"].split_row(
                Layout(name="left", ratio=3),
                Layout(name="right", ratio=2),
            )
            layout["left"].split_column(
                Layout(self._build_mobile_position_book(snapshot), size=pos_size_tablet, name="positions"),
                Layout(self._build_mobile_risk_rails_line(snapshot), size=3, name="risk"),
                Layout(self._build_signal_flow_panel(snapshot), ratio=1, name="signal_flow"),
            )
            layout["right"].split_column(
                Layout(self._build_log_stream_panel(snapshot, n_buffer=10), name="logs"),
            )
        elif compact_mode:
            # Mobile: stacked single-column — dynamic position height, portfolio+sigflow+logs fill the rest
            n_pos = len(list(snapshot.get("positions") or []))
            # summary(1) + pbar(1) + header_row(1) + data_rows + border(2) + padding(3) — min 10, max 15
            pos_size = max(10, min(15, n_pos + 8))
            layout["body"].split_column(
                Layout(self._build_mobile_position_book(snapshot), size=pos_size, name="positions"),
                Layout(self._build_mobile_risk_rails_line(snapshot), size=3, name="risk"),
                Layout(self._build_balance_breakdown_panel(snapshot), size=8, name="portfolio"),
                Layout(self._build_signal_flow_compact_new(snapshot), ratio=2, name="signal_flow"),
                Layout(self._build_log_stream_panel(snapshot, n_buffer=16), ratio=2, name="logs"),
            )
        else:
            layout["body"].split_row(
                Layout(name="left", ratio=2),
                Layout(name="center", ratio=6),
                Layout(name="right", ratio=2),
            )
            layout["left"].split_column(
                Layout(self._build_runtime_overview_panel(snapshot), ratio=4, name="overview"),
                Layout(self._build_balance_breakdown_panel(snapshot), ratio=4, name="portfolio"),
                Layout(self._build_recent_events_panel(snapshot), ratio=2, name="events"),
            )
            layout["center"].split_column(
                Layout(self._build_positions_table(snapshot, compact=False), ratio=5, name="positions"),
                Layout(self._build_signal_flow_panel(snapshot), ratio=9, name="signal_flow"),
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
            if self._is_cash_breakdown_line(line):
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
            title=Text.assemble((" ", cls._DIM), (str(title), title_style), (" ", cls._DIM)),
            border_style=border_style,
            title_align="left",
            box=box.ROUNDED,
            padding=(0, 0),
        )

    def _get_filtered_log_rows(self, min_level: str, n: int = 8) -> List[Dict[str, str]]:
        min_level_no = self._level_no(min_level)

        rows = list(self._last_log_rows_snapshot)
        if self._log_lock.acquire(blocking=False):
            try:
                rows = list(self._log_lines)[-max(n, 8):]
                self._last_log_rows_snapshot = list(self._log_lines)[-8:]
            finally:
                self._log_lock.release()

        filtered = [row for row in rows if self._level_no(row.get("level", "INFO")) >= min_level_no]
        return filtered[-n:]

    @staticmethod
    def _normalize_snapshot_for_signature(snapshot: Dict[str, Any]) -> Dict[str, Any]:
        normalized = json.loads(json.dumps(snapshot or {}, sort_keys=True, default=str, ensure_ascii=False))
        normalized.pop("updated_at", None)
        # Exclude chat input from signature so typing doesn't trigger full re-renders
        chat = normalized.get("chat")
        if isinstance(chat, dict):
            chat.pop("input", None)
            chat.pop("suggestions", None)
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
        """Inner renderable rows inside the footer panel (no chat/command UI)."""
        _ = footer_mode
        return 2

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
            target_size = max(min_panel_rows, min(6, safe_height // 5))
        else:
            target_size = max(min_panel_rows, min(5, safe_height // 6))

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
    def _abbrev_signal_flow_step(step: str) -> str:
        label = str(step or "").strip()
        if not label:
            return "-"
        mapping = {
            "Bootstrap": "Start",
            "Sniper:DataCheck": "Data",
            "Sniper:MacroTrend": "Macro",
            "Sniper:MicroTrend": "Micro",
            "Sniper:MACDTrigger": "MACD",
            "Sniper:ATR": "ATR",
            "Sniper:ADX": "ADX",
            "Sniper:Result": "Result",
            "Aggregation": "Agg",
            "SignalCollection": "Collect",
            "GetBestSignal": "Pick",
            "Aggregate:Input": "Input",
            "Sniper:Exception": "Error",
        }
        if label in mapping:
            return mapping[label]
        if label.startswith("Strategy:"):
            rest = label.replace("Strategy:", "", 1).strip() or "?"
            return CLICommandCenter._truncate_inline(rest, 8)
        if label.startswith("RiskCheck:"):
            rest = label.replace("RiskCheck:", "", 1).strip() or "?"
            return CLICommandCenter._truncate_inline(rest, 8)
        if label.startswith("RiskMgr:"):
            rest = label.replace("RiskMgr:", "", 1).strip() or "?"
            return CLICommandCenter._truncate_inline(rest, 8)
        return CLICommandCenter._truncate_inline(label, 9)

    @staticmethod
    def _humanize_signal_flow_reason(step: str, result: str, raw: str, max_len: int = 60) -> str:
        """Short English for one-row-per-pair SigFlow; longer tail still truncated at max_len."""
        st = str(step or "").strip()
        rt = str(result or "").upper().strip()
        s = str(raw or "").strip()
        if not s:
            return "ok" if rt == "PASS" else "—"
        low = s.lower()

        m = re.search(r"Insufficient data \((\d+)/(\d+) bars\)", s, re.I)
        if m:
            return CLICommandCenter._truncate_inline(f"{m.group(1)}/{m.group(2)} bars", max_len)

        if "waiting for first signal cycle" in low or s.strip().lower() == "no diagnostics":
            return "warmup"

        if "cooldown period active" in low:
            return "cooldown"

        if "daily loss limit" in low:
            return "daily loss"

        if "empty signal list" in low or "nothing to aggregate" in low:
            return "no agg"

        if "invalid portfolio value" in low:
            return "port?"

        if st == "Sniper:MacroTrend" or st.endswith("MacroTrend"):
            if rt == "REJECT" or ("buy_ok=false" in low and "sell_ok=false" in low):
                return "no EMA trend"
            return "EMA ok"

        if st == "Sniper:MicroTrend" or st.endswith("MicroTrend"):
            if rt == "REJECT":
                return "off EMA50"
            return "at EMA50"

        if st == "Sniper:MACDTrigger" or st.endswith("MACDTrigger"):
            if rt == "REJECT":
                return "no MACD"
            return "MACD"

        if st == "Sniper:ATR" or st.endswith("ATR"):
            mx = re.search(r"ATR=([0-9.eE+-]+)", s)
            if mx:
                return CLICommandCenter._truncate_inline(f"ATR? {mx.group(1)}", max_len)
            return "ATR?"

        if st == "Sniper:ADX" or ("ADX" in st and "Sniper" in st):
            mx = re.search(r"ADX=([0-9.]+)", s)
            if mx:
                return CLICommandCenter._truncate_inline(f"ADX {mx.group(1)}", max_len)

        if st == "Sniper:Result" or st.endswith("Result"):
            if rt == "PASS":
                return "emit"
            return CLICommandCenter._truncate_inline(s, max_len)

        if st.startswith("RiskCheck:"):
            if rt == "REJECT":
                tail = CLICommandCenter._truncate_inline(s.replace("\n", " "), max(6, max_len - 6))
                return CLICommandCenter._truncate_inline(f"! {tail}", max_len)
            return "risk ok"

        if st.startswith("RiskMgr:"):
            return CLICommandCenter._truncate_inline(s, max_len)

        if st.startswith("Strategy:"):
            strategy_name = st.replace("Strategy:", "", 1).strip()
            strategy_label = strategy_name or "strategy"
            reason_code_match = re.search(r"reason_code=([A-Z0-9_]+)", s, re.I)
            conf_match = re.search(r"conf=([0-9.]+)", s, re.I)
            rr_match = re.search(r"RR=([0-9.]+|N/A)", s, re.I)
            type_match = re.search(r"type=([A-Z]+)", s, re.I)

            if rt == "REJECT":
                if reason_code_match:
                    return CLICommandCenter._truncate_inline(
                        f"{strategy_label} reject: {reason_code_match.group(1)}",
                        max_len,
                    )
                if "generate_signal() returned none" in low:
                    return CLICommandCenter._truncate_inline(
                        f"{strategy_label} reject: no setup",
                        max_len,
                    )
                if "validate_signal() returned false" in low:
                    return CLICommandCenter._truncate_inline(
                        f"{strategy_label} reject: validate false",
                        max_len,
                    )
                return CLICommandCenter._truncate_inline(
                    f"{strategy_label} reject",
                    max_len,
                )

            if rt == "PASS":
                type_part = type_match.group(1).upper() if type_match else "PASS"
                conf_part = f" conf {conf_match.group(1)}" if conf_match else ""
                rr_part = f" rr {rr_match.group(1)}" if rr_match else ""
                return CLICommandCenter._truncate_inline(
                    f"{strategy_label} {type_part}{conf_part}{rr_part}",
                    max_len,
                )

            return CLICommandCenter._truncate_inline(
                f"{strategy_label} {s or 'ok'}",
                max_len,
            )

        return CLICommandCenter._truncate_inline(s, max_len)

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

    def _build_log_stream_panel(self, snapshot: Dict[str, Any], *, max_lines: Optional[int] = None, n_buffer: int = 8) -> Panel:
        ui_cfg = dict(snapshot.get("ui") or {})
        min_level = str(ui_cfg.get("log_level_filter") or "INFO").upper()
        compact = self._terminal_compact_mode()

        if compact:
            # Compact: group up-to-date noise, show last 16 meaningful lines with emoji prefix
            raw_rows = self._get_filtered_log_rows(min_level, n=80)
            rows = self._group_log_rows(raw_rows)[-16:]
            if not rows:
                return self._panel(
                    Text(f"Waiting for logs ({min_level}+)...", style=self._DIM),
                    title="\u2630 Logs",
                    theme="logs",
                )
            lines = [self._log_row_text_compact(row) for row in rows]
            return self._panel(Group(*lines), title="\u2630 Logs", theme="logs")

        rows = self._get_filtered_log_rows(min_level, n=n_buffer)
        if max_lines is not None and max_lines > 0:
            rows = rows[-max_lines:]

        if not rows:
            return self._panel(
                Text(f"Waiting for runtime logs ({min_level}+)...", style=self._DIM),
                title="\u2630 Logs",
                theme="logs",
            )

        lines: List[Text] = []
        for row in rows:
            level = str(row.get("level") or "INFO")
            tag   = str(row.get("tag")   or row.get("logger") or "")[:4].ljust(4)
            emoji = str(row.get("emoji") or "")
            msg   = str(row.get("message") or "-")
            prefix = f"{emoji} " if emoji and not msg.startswith(emoji) else ""
            lines.append(
                Text.assemble(
                    (f"{row.get('timestamp', '-')} ", self._DIM),
                    (f"{level:<4}", self._level_style(level)),
                    (" │ ", self._DIM),
                    (f"{tag}", f"bold {self._CYAN}"),
                    (" │ ", self._DIM),
                    (f"{prefix}{msg}", self._WHITE),
                )
            )

        title = f"\u2630 Logs [{min_level}+]"
        if max_lines is not None:
            title = f"\u2630 Logs (last {max_lines}) [{min_level}+]"
        return self._panel(Group(*lines), title=title, theme="logs")

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

        sep = ("  \u2502  ", self._DIM)  # │ separator
        header = Text.assemble(
            ("\u25c8 ", f"bold {self._CYAN}"),
            (snapshot.get("bot_name", self.bot_name), f"bold {self._WHITE}"),
            sep,
            (mode, mode_style),
            (" \u2022 ", self._DIM),
            (strategy_mode.upper(), f"bold {self._WHITE}"),
            sep,
            ("\u26a0 ", self._risk_style(snapshot.get("risk_level"))),
            risk_text,
            sep,
            (f"{pair_count}", f"bold {self._WHITE}"),
            (" pairs", self._DIM),
            (" \u2022 ", self._DIM),
            (str(open_positions), f"bold {self._GREEN}" if open_positions == 0 else f"bold {self._EMBER}"),
            (" open", self._DIM),
            sep,
            (f"{freshness_icon} ", freshness_style),
            (uptime_str, f"bold {self._GREEN}"),
        )
        return self._panel(Align.left(header), title="Dashboard", theme="header")

    def _build_compact_status_bar(self, snapshot: Dict[str, Any]) -> Panel:
        """Bloomberg-style one-line status strip: ⚡ Bot │ 🟢 LIVE │ 22:49 │ 💰 cash │ 📊 slots │ 📉 pnl%."""
        system = dict(snapshot.get("system") or {})
        positions = list(snapshot.get("positions") or [])
        open_n = len(positions)
        max_pos = max(1, int(float(self._extract_numeric(system.get("max_open_positions"), 6))))
        mode = str(snapshot.get("mode", "-")).upper()
        mode_style = self._mode_style(mode)
        mode_icon = "🟢" if mode == "LIVE" else ("🟡" if "SIM" in mode else "🔴")
        t = str(snapshot.get("updated_at") or "-")
        # Cash: extract numerics from balance strings
        avail_raw = str(system.get("available_balance") or "").strip()
        total_raw = str(system.get("total_balance") or "").strip()
        avail_num = self._extract_numeric(avail_raw, 0.0)
        total_num = self._extract_numeric(total_raw, 0.0)
        try:
            quote_asset = str(self.app._get_quote_asset()).upper()
        except Exception:
            quote_asset = "USDT"
        cash_part = f"{avail_num:.2f}/{total_num:.2f} {quote_asset}"
        # Daily PnL% indicator
        dloss_pct_s = str(system.get("daily_loss_pct") or "-")
        dloss_val, _ = self._extract_fraction(system.get("daily_loss"))
        pnl_icon = "📉" if dloss_val > 0 else "📈"
        pnl_style = f"bold {self._RED}" if dloss_val > 0 else f"bold {self._GREEN}"
        line = Text.assemble(
            ("⚡ ", f"bold {self._CYAN}"),
            (str(snapshot.get("bot_name", self.bot_name) or "Bot"), f"bold {self._WHITE}"),
            (" │ ", self._DIM),
            (f"{mode_icon} ", ""),
            (mode, mode_style),
            (" │ ", self._DIM),
            (t, self._DIM),
            (" │ 💰 ", self._DIM),
            (cash_part, f"bold {self._EMBER}"),
            (" │ 📊 ", self._DIM),
            (f"{open_n}/{max_pos}", f"bold {self._CYAN}"),
            (" │ ", self._DIM),
            (f"{pnl_icon} ", ""),
            (dloss_pct_s, pnl_style),
        )
        return self._panel(Align.left(line), title="Status", theme="header")

    @staticmethod
    def _strip_balance_label_for_status(avail: str, total: str) -> str:
        """Shrink '1,234.56 USDT' style strings for the status bar."""
        a = avail.split("(")[0].strip() if avail else ""
        b = total.split("(")[0].strip() if total else ""
        if a and b:
            return f"{a}/{b}".replace(" ", "")
        return (a or b or "-").replace(" ", "")

    def _build_mobile_position_book(self, snapshot: Dict[str, Any]) -> Panel:
        """Bloomberg position book: emoji + symbol + side + entry→curr + pnl + sl + tp + strat badge."""
        system = dict(snapshot.get("system") or {})
        all_pos = list(snapshot.get("positions") or [])
        positions: List[Dict[str, Any]] = all_pos[:5]
        try:
            quote_asset = str(self.app._get_quote_asset()).upper()
        except Exception:
            quote_asset = "USDT"

        # Portfolio summary header
        pv = self._extract_numeric(system.get("risk_portfolio_value_quote"), 0.0)
        if pv <= 0:
            pv = self._extract_numeric(str(system.get("total_balance", "0")).replace(",", ""), 0.0)
        dloss_amt, dloss_cap = self._extract_fraction(system.get("daily_loss"))
        dloss_pct_s = str(system.get("daily_loss_pct") or "-")
        pnl_vals = [self._safe_float(p.get("pnl_pct")) for p in all_pos]
        valid_pv = [v for v in pnl_vals if v is not None]
        win_n = sum(1 for v in valid_pv if v > 0)
        lose_n = sum(1 for v in valid_pv if v < 0)
        avg_pnl = (sum(valid_pv) / len(valid_pv)) if valid_pv else 0.0
        sign = "-" if dloss_amt > 0 else "+"
        today_part_str = f"{sign}{abs(dloss_amt):.2f} {quote_asset} ({dloss_pct_s})" if dloss_cap or dloss_amt else "-"
        today_style = self._RED if dloss_amt > 0 else self._GREEN
        summary = Text.assemble(
            ("Portfolio: ", self._DIM),
            (f"{pv:.2f} {quote_asset}", f"bold {self._WHITE}"),
            (" │ Today: ", self._DIM),
            (today_part_str, today_style),
            (" │ W/L: ", self._DIM),
            (f"{win_n}/{lose_n}", f"bold {self._CYAN}"),
        )

        # Daily loss progress bar
        pbar_text: Optional[Text] = None
        if dloss_cap > 0:
            used_pct = min(1.0, dloss_amt / dloss_cap)
            filled = int(round(used_pct * 10))
            empty = 10 - filled
            bar_style = (
                f"bold {self._RED}" if used_pct > 0.7
                else (f"bold {self._EMBER}" if used_pct > 0.3 else f"bold {self._GREEN}")
            )
            pbar_text = Text.assemble(
                ("PnL: ", self._DIM),
                ("\u2588" * filled, bar_style),
                ("\u2591" * empty, self._DIM),
                (f" {used_pct * 100:.0f}% of daily limit", self._DIM),
            )

        if not positions:
            theme = self._resolve_positions_theme(avg_pnl)
            blocks: List[Any] = [summary]
            if pbar_text:
                blocks.append(pbar_text)
            blocks.append(Text("No open positions", style=self._DIM))
            return self._panel(Group(*blocks), title="\u25c6 Position Book", theme=theme)

        table = Table(expand=True, show_lines=False, row_styles=["", "on #111111"], padding=(0, 0), pad_edge=False)
        table.add_column("", no_wrap=True, width=2)  # emoji indicator
        table.add_column("Symbol", style=self._WHITE, no_wrap=True, min_width=9)
        table.add_column("Side", justify="center", no_wrap=True, width=4)
        table.add_column("Entry\u2192Curr", justify="right", no_wrap=True, min_width=14)
        table.add_column("PnL%", justify="right", no_wrap=True, width=7)
        table.add_column("SL\u25bc", justify="right", no_wrap=True, width=6)
        table.add_column("TP\u25b2", justify="right", no_wrap=True, width=6)
        table.add_column("Strat", justify="center", no_wrap=True, width=5)

        for pos in positions:
            emoji = self._position_emoji(pos.get("pnl_pct"), pos.get("sl_distance_pct"))
            sym = str(pos.get("symbol", "-")).replace("USDT", "")
            entry = self._format_price_smart(pos.get("entry_price"))
            curr = self._format_price_smart(pos.get("current_price"))
            entry_curr = f"{entry}\u2192{curr}"

            pnl_v = self._safe_float(pos.get("pnl_pct"))
            pnl_str = (
                f"+{pnl_v:.2f}%" if pnl_v is not None and pnl_v > 0
                else (f"{pnl_v:.2f}%" if pnl_v is not None else "-")
            )
            pnl_s = self._pnl_style_rich(pnl_v)

            sl_d = self._safe_float(pos.get("sl_distance_pct"))
            sl_str = f"{sl_d:.1f}%" if sl_d is not None else "-"
            sl_s = self._sl_dist_style(sl_d)

            tp_d = self._safe_float(pos.get("tp_distance_pct"))
            tp_str = (
                f"+{tp_d:.1f}%" if tp_d is not None and tp_d > 0
                else (f"{tp_d:.1f}%" if tp_d is not None else "-")
            )
            tp_s = self._tp_dist_style(tp_d)

            strat_text = self._strategy_label_text(str(pos.get("strategy_source") or "-"))

            table.add_row(
                emoji,
                sym,
                self._side_text(str(pos.get("side", "-"))),
                Text(entry_curr, style=self._DIM),
                Text(pnl_str, style=pnl_s),
                Text(sl_str, style=sl_s),
                Text(tp_str, style=tp_s),
                strat_text,
            )

        theme = self._resolve_positions_theme(avg_pnl)
        blocks2: List[Any] = [summary]
        if pbar_text:
            blocks2.append(pbar_text)
        blocks2.append(table)
        return self._panel(Group(*blocks2), title="\u25c6 Position Book", theme=theme)

    def _build_mobile_risk_rails_line(self, snapshot: Dict[str, Any]) -> Panel:
        """Single-line risk strip: Risk % │ ✓ Ready/⏳ Cooling │ Active │ Expose │ Loss."""
        system = dict(snapshot.get("system") or {})
        positions = list(snapshot.get("positions") or [])
        open_positions = len(positions)
        max_positions = max(1.0, self._extract_numeric(system.get("max_open_positions"), 1.0))
        trade_count = self._extract_numeric(system.get("trade_count"), 0.0)
        max_daily_trades = max(1.0, self._extract_numeric(system.get("max_daily_trades"), 1.0))
        risk_pt = str(system.get("risk_per_trade") or "-")
        cooling = str(system.get("cooling_down") or "No")
        daily_loss_str = str(system.get("daily_loss") or "-").replace("  ", " ")

        # Cooling indicator
        cool_text = (
            Text("\u23f3 Cooling", style=f"bold {self._EMBER}")
            if cooling == "Yes"
            else Text("\u2713 Ready", style=f"bold {self._GREEN}")
        )

        # Loss color based on ratio
        dloss_amt, dloss_cap = self._extract_fraction(system.get("daily_loss"))
        if dloss_cap > 0:
            ratio = dloss_amt / dloss_cap
            if ratio > 0.7:
                loss_style = f"bold {self._RED}"
            elif ratio > 0.3:
                loss_style = f"bold {self._EMBER}"
            else:
                loss_style = f"bold {self._GREEN}"
        else:
            loss_style = self._WHITE

        # Overall warn/bad state
        fresh = str(system.get("freshness") or "fresh").lower()
        mode = str(snapshot.get("mode", "")).upper()
        warn = fresh == "warning" or cooling == "Yes"
        if open_positions >= max_positions * 0.9 or trade_count >= max_daily_trades * 0.9:
            warn = True
        risk_label_style = f"bold {self._EMBER}" if warn else self._WHITE

        line = Text.assemble(
            (f"Risk {risk_pt}", risk_label_style),
            (" \u2502 ", self._DIM),
            cool_text,
            (" \u2502 Active ", self._DIM),
            (f"{int(trade_count)}/{int(max_daily_trades)}", f"bold {self._WHITE}"),
            (" \u2502 Expose ", self._DIM),
            (f"{open_positions}/{int(max_positions)}", f"bold {self._CYAN}"),
            (" \u2502 Loss ", self._DIM),
            (daily_loss_str, loss_style),
        )
        return self._panel(line, title="\u26a0 Risk Rails", theme="risk")

    @staticmethod
    def _sigflow_short_why(reason: str, result: str, *, max_len: int = 28) -> str:
        """Prefer tail token for reject (e.g. SR_GUARD_BLOCKED); truncate long strings."""
        s = str(reason or "").strip()
        upper = str(result or "").upper()
        if "reject:" in s.lower():
            tail = s.split(":")[-1].strip()
            s = tail or s
        elif "_" in s and len(s) > max_len:
            parts = [p for p in s.replace(" ", "_").split("_") if p]
            if parts:
                s = parts[-1]
        if len(s) <= max_len:
            return s
        return s[: max_len - 1] + "\u2026"

    def _build_signal_flow_compact_new(self, snapshot: Dict[str, Any]) -> Panel:
        """Unified per-pair+strategy SigFlow table (always full, no toggle)."""
        try:
            flow_snapshot = get_latest_signal_flow_snapshot()
        except Exception as exc:
            self._safe_stderr_write(f"[cli_ui] signal flow snapshot error: {exc}\n")
            flow_snapshot = {}

        positions = list(snapshot.get("positions") or [])
        pos_by_sym: Dict[str, Dict[str, Any]] = {str(p.get("symbol", "")).upper(): p for p in positions}

        strategy_defs = [
            ("machete_v8b_lite", "〔M〕", f"bold {self._CYAN}"),
            ("simple_scalp_plus", "〔S〕", f"bold {self._EMBER}"),
        ]

        # Collect pairs: use signal_alignment order then flow_snapshot
        pairs_ordered: List[str] = []
        seen_pairs: set = set()
        for row in list(snapshot.get("signal_alignment") or []):
            sym = str(row.get("symbol") or "").upper()
            if sym and sym not in seen_pairs:
                pairs_ordered.append(sym)
                seen_pairs.add(sym)
        for sym in sorted(flow_snapshot.keys()):
            sym_u = str(sym).upper()
            if sym_u not in seen_pairs:
                pairs_ordered.append(sym_u)
                seen_pairs.add(sym_u)

        # Build stats for summary line
        stats: Dict[str, Dict[str, int]] = {k: {"pass": 0, "reject": 0} for k, _, _ in strategy_defs}
        buy_syms: List[str] = []
        sell_syms: List[str] = []

        # Build table
        table = Table(expand=True, show_lines=False, row_styles=["", "on #111111"], padding=(0, 0), pad_edge=False)
        table.add_column("Pair",   no_wrap=True, width=9, style=self._WHITE)
        table.add_column("Strat",  no_wrap=True, width=5, justify="center")
        table.add_column("Signal", no_wrap=True, width=5, justify="center")
        table.add_column("Conf",   no_wrap=True, width=6, justify="right")
        table.add_column("RR",     no_wrap=True, width=5, justify="right")
        table.add_column("State",  no_wrap=True, width=8)
        table.add_column("Reason", no_wrap=False, min_width=20, ratio=1, overflow="ellipsis")

        if not pairs_ordered and not flow_snapshot:
            table.add_row("-", "—", "—", "—", "—", "—", "no SigFlow data")
        else:
            for pair in pairs_ordered:
                flow = flow_snapshot.get(pair, {})
                if not isinstance(flow, dict):
                    flow = {}
                steps_dict = flow.get("steps") or {}
                pos = pos_by_sym.get(pair)
                pnl_pct = self._safe_float((pos or {}).get("pnl_pct"))
                sl_dist = self._safe_float((pos or {}).get("sl_distance_pct"))

                display_pair = pair.replace("USDT", "")
                if pos:
                    row_emoji = self._position_emoji(pnl_pct, sl_dist)
                    pair_cell = Text.assemble((f"{row_emoji} ", ""), (display_pair, self._WHITE))
                    state_text = Text("HOLDING", style=f"bold {self._CYAN}")
                else:
                    pair_cell = Text.assemble(("\u2b1c ", self._DIM), (display_pair, self._DIM))
                    state_text = Text("IDLE", style=self._DIM)

                for strategy_key, badge, badge_style in strategy_defs:
                    step_key = f"Strategy:{strategy_key}"
                    step_data = steps_dict.get(step_key)
                    strat_text = Text(badge, style=badge_style)

                    if not isinstance(step_data, dict):
                        table.add_row(
                            pair_cell, strat_text,
                            Text("—", style=self._DIM),
                            Text("—", style=self._DIM),
                            Text("—", style=self._DIM),
                            state_text,
                            Text("warmup", style=self._DIM),
                        )
                        continue

                    result_raw = str(step_data.get("result") or "").upper()
                    reason_raw = str(step_data.get("reason") or "")

                    sig_type, conf_s, rr_s = self._parse_strategy_signal_fields(reason_raw, result_raw)

                    # Signal cell
                    if sig_type == "BUY":
                        sig_text = Text("BUY", style=f"bold {self._GREEN}")
                    elif sig_type == "SELL":
                        sig_text = Text("SELL", style=f"bold {self._RED}")
                    elif sig_type not in {"—", "-", ""}:
                        sig_text = Text(sig_type, style=self._WHITE)
                    else:
                        sig_text = Text("—", style=self._DIM)

                    conf_text = Text(conf_s, style=self._conf_style(conf_s) if conf_s != "—" else self._DIM)
                    rr_text = Text(rr_s, style=self._rr_style(rr_s) if rr_s not in {"—", "N/A"} else self._DIM)
                    reason_text = self._sigflow_reason_text(reason_raw, result_raw)

                    # Update stats
                    if result_raw == "PASS":
                        stats[strategy_key]["pass"] += 1
                        short = pair.replace("USDT", "").replace("THB_", "")
                        if sig_type == "BUY" and short not in buy_syms:
                            buy_syms.append(short)
                        elif sig_type == "SELL" and short not in sell_syms:
                            sell_syms.append(short)
                    elif result_raw == "REJECT":
                        stats[strategy_key]["reject"] += 1

                    table.add_row(pair_cell, strat_text, sig_text, conf_text, rr_text, state_text, reason_text)

        # Summary line
        summary_parts: List[Any] = []
        for strategy_key, badge, badge_style in strategy_defs:
            st = stats[strategy_key]
            summary_parts.extend([
                (badge, badge_style),
                (f"{st['pass']}\u2713 {st['reject']}\u2717  ", self._DIM),
            ])
        if buy_syms:
            summary_parts.extend([("BUY: ", f"bold {self._GREEN}"), (" ".join(buy_syms[:4]), f"bold {self._GREEN}"), ("  ", "")])
        if sell_syms:
            summary_parts.extend([("SELL: ", f"bold {self._RED}"), (" ".join(sell_syms[:4]), f"bold {self._RED}")])
        summary = Text.assemble(*summary_parts)

        return self._panel(Group(summary, table), title="\u25ec SigFlow", theme="signal_flow")

    def _build_signal_flow_collapsed(self, snapshot: Dict[str, Any]) -> Panel:
        """Summary + PASS pairs only; per-strategy counts over full snapshot."""
        try:
            flow_snapshot = get_latest_signal_flow_snapshot()
        except Exception as exc:
            self._safe_stderr_write(f"[cli_ui] signal flow snapshot error: {exc}\n")
            flow_snapshot = {}
        ui_cfg = dict(snapshot.get("ui") or {})
        full_hint = " [s|sigflow: full]" if not ui_cfg.get("sigflow_full") else ""

        if not isinstance(flow_snapshot, dict) or not flow_snapshot:
            return self._panel(Text("no SigFlow data", style=self._DIM), title=f"\u25ec SigFlow{full_hint}", theme="signal_flow")

        strategy_headers = (
            ("machete_v8b_lite", "Machete"),
            ("simple_scalp_plus", "ScalpPlus"),
        )
        stats: Dict[str, Dict[str, int]] = {
            key: {"pass": 0, "reject": 0, "buy": 0, "sell": 0} for key, _ in strategy_headers
        }
        pass_by_strategy: Dict[str, List[str]] = {key: [] for key, _ in strategy_headers}

        for pair, flow in sorted(flow_snapshot.items(), key=lambda kv: str(kv[0] or "")):
            if not isinstance(flow, dict):
                continue
            steps_dict = flow.get("steps") or {}
            if not isinstance(steps_dict, dict):
                steps_dict = {}
            sym = str(pair or "").strip().upper()
            for strategy_key, _label in strategy_headers:
                step_key = f"Strategy:{strategy_key}"
                step_data = steps_dict.get(step_key)
                if not isinstance(step_data, dict):
                    continue
                result_raw = str(step_data.get("result") or "").upper()
                reason_raw = str(step_data.get("reason") or "")
                if result_raw == "PASS":
                    stats[strategy_key]["pass"] += 1
                    if sym and sym not in pass_by_strategy[strategy_key]:
                        pass_by_strategy[strategy_key].append(sym)
                    ru = reason_raw.upper()
                    if "TYPE=BUY" in ru:
                        stats[strategy_key]["buy"] += 1
                    elif "TYPE=SELL" in ru:
                        stats[strategy_key]["sell"] += 1
                elif result_raw == "REJECT":
                    stats[strategy_key]["reject"] += 1

        summary_bits: List[str] = []
        for strategy_key, label in strategy_headers:
            st = stats[strategy_key]
            summary_bits.append(
                f"{label}: {st['pass']} pass, {st['reject']} block | SELL {st['sell']}, BUY {st['buy']}"
            )
        summary_line = " | ".join(summary_bits)

        pass_lines: List[Text] = []
        term_w, _ = self._layout_term_dimensions(self.console)
        budget = max(24, term_w - 6)
        for strategy_key, label in strategy_headers:
            pairs = pass_by_strategy[strategy_key]
            if not pairs:
                continue
            chunk = ", ".join(pairs)
            if len(chunk) > budget:
                chunk = chunk[: budget - 3] + "..."
            pass_lines.append(
                Text.assemble(
                    (f"\u2713 {label}: ", f"bold {self._GREEN}"),
                    (chunk, self._WHITE),
                )
            )

        blocks: List[Any] = [Text(summary_line, style=self._DIM)]
        blocks.extend(pass_lines if pass_lines else [Text("\u2713 no PASS rows", style=self._DIM)])
        return self._panel(Group(*blocks), title=f"\u25ec SigFlow (compact){full_hint}", theme="signal_flow")

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
        table = Table(expand=True, show_lines=False, row_styles=["", "on #111111"], padding=(0, 0), pad_edge=False)
        table.add_column("Symbol", style=self._WHITE, no_wrap=True)
        table.add_column("Source", style=self._DIM, no_wrap=True)
        table.add_column("Side", justify="center", no_wrap=True)
        table.add_column("Entry", justify="right", style=self._DIM)
        table.add_column("Current", justify="right")
        table.add_column("PnL %", justify="right")
        if compact:
            table.add_column("SL/TP Dist", justify="right")
        else:
            table.add_column("SL / TP", justify="right", style=self._DIM)
            table.add_column("Dist SL/TP", justify="right")

        positions: List[Dict[str, Any]] = list(snapshot.get("positions") or [])
        pnl_values = [self._safe_float(position.get("pnl_pct")) for position in positions]
        valid_pnl_values = [value for value in pnl_values if value is not None]
        avg_pnl_pct = (sum(valid_pnl_values) / len(valid_pnl_values)) if valid_pnl_values else 0.0
        winners = sum(1 for value in valid_pnl_values if value > 0)
        losers = sum(1 for value in valid_pnl_values if value < 0)
        if not positions:
            if compact:
                table.add_row("-", "-", "-", "-", "-", "No open positions", "-")
            else:
                table.add_row("-", "-", "-", "-", "-", "No open positions", "-", "-")
        else:
            for position in positions:
                sltp_text = (
                    f"{self._fmt_price(position.get('stop_loss'))} / {self._fmt_price(position.get('take_profit'))}"
                )
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
                strategy_source = str(position.get("strategy_source") or "-")
                if compact:
                    table.add_row(
                        symbol_display,
                        strategy_source,
                        self._side_text(str(position.get("side", "-"))),
                        self._fmt_price(position.get("entry_price")),
                        self._fmt_price(position.get("current_price")),
                        self._pnl_text(position.get("pnl_pct")),
                        dist_text,
                    )
                else:
                    table.add_row(
                        symbol_display,
                        strategy_source,
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
                ("\u25b8 ", f"bold {self._GREEN}"),
                (f"{len(positions)} open", f"bold {self._EMBER}" if positions else f"bold {self._GREEN}"),
                ("  \u2502  ", self._DIM),
                ("W/L ", self._DIM),
                (
                    f"{winners}/{losers}{win_rate_str}",
                    f"bold {self._GREEN}" if winners >= losers else f"bold {self._RED}",
                ),
                ("  \u2502  ", self._DIM),
                ("PnL ", self._DIM),
                self._pnl_text(avg_pnl_pct),
            )
        )
        summary_lines.append(
            Text.assemble(
                ("  PnL   ", self._DIM),
                self._sparkline_text(
                    self._trend_values("avg_pnl_pct", [avg_pnl_pct]),
                    filled_style=f"bold {self._GREEN}" if avg_pnl_pct >= 0 else f"bold {self._RED}",
                ),
                ("  Open  ", self._DIM),
                self._sparkline_text(
                    self._trend_values("open_positions", [float(len(positions))]), filled_style=f"bold {self._CYAN}"
                ),
            )
        )

        theme = self._resolve_positions_theme(avg_pnl_pct)
        return self._panel(Group(*summary_lines, table), title="\u25c6 Position Book", theme=theme)

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
                ("▸ ", f"bold {self._BLUE}"),
                (str(pair_count), f"bold {self._WHITE}"),
                (" pairs", self._DIM),
                ("  │  ", self._DIM),
                (self._truncate_inline(snapshot.get("strategies") or "idle", 20).upper(), f"bold {self._WHITE}"),
                ("  │  ", self._DIM),
                (str(snapshot.get("updated_at") or "-"), self._DIM),
            )
        )
        lines.append(
            Text.assemble(
                ("▸ ", f"bold {self._BLUE}"),
                ("▲ ", f"bold {self._GREEN}"),
                (f"{buy_signals} buy", f"bold {self._GREEN}"),
                ("  ", ""),
                ("▼ ", f"bold {self._RED}"),
                (f"{sell_signals} sell", f"bold {self._RED}"),
                ("  ", ""),
                ("● ", f"bold {self._EMBER}"),
                (f"{wait_signals} wait", f"bold {self._EMBER}"),
            )
        )
        lines.append(
            Text.assemble(
                ("  Flow  ", self._DIM),
                self._sparkline_text(
                    self._trend_values("buy_signals", [buy_signals])
                    + self._trend_values("sell_signals", [sell_signals])[-1:]
                    + self._trend_values("wait_signals", [wait_signals])[-1:],
                    filled_style=f"bold {self._CYAN}",
                ),
            )
        )
        lines.append(
            Text.assemble(
                ("▸ ", f"bold {self._BLUE}"),
                ("Cash  ", self._DIM),
                self._meter_text(
                    available_balance, total_balance, width=16, filled_style=f"bold {self._EMBER}", suffix="quote"
                ),
            )
        )
        lines.append(
            Text.assemble(
                ("  Trend ", self._DIM),
                self._sparkline_text(
                    self._trend_values("available_balance", [available_balance]), filled_style=f"bold {self._EMBER}"
                ),
            )
        )
        lines.append(
            Text.assemble(
                ("▸ ", f"bold {self._BLUE}"),
                ("Slots ", self._DIM),
                self._meter_text(
                    float(open_positions), max_positions, width=16, filled_style=f"bold {self._WHITE}", decimals=0
                ),
            )
        )
        lines.append(
            Text.assemble(
                ("▸ ", f"bold {self._BLUE}"),
                ("Trade ", self._DIM),
                self._meter_text(
                    trade_count, max_daily_trades, width=16, filled_style=f"bold {self._GREEN}", decimals=0
                ),
            )
        )
        if balance_mix:
            lines.append(
                Text.assemble(
                    ("  Alloc ", self._DIM),
                    self._sparkline_text(
                        [self._extract_allocation_pct(item) for item in balance_mix[:8]],
                        filled_style=f"bold {self._EMBER}",
                    ),
                )
            )

        return self._panel(Group(*lines), title="◈ Trading Matrix", theme="overview")

    def _build_system_status_table(self, snapshot: Dict[str, Any]) -> Panel:
        system = snapshot.get("system", {})
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)

        left_lines = [
            Text.assemble(
                ("▸ ", f"bold {self._CYAN}"),
                ("Market    ", self._DIM),
                (str(system.get("last_market_update", "-")), self._WHITE),
            ),
            Text.assemble(
                ("▸ ", f"bold {self._CYAN}"),
                ("Fresh     ", self._DIM),
                self._freshness_text(system.get("freshness"), system.get("market_age_seconds")),
            ),
            Text.assemble(
                ("▸ ", f"bold {self._CYAN}"),
                ("Latency   ", self._DIM),
                self._api_latency_text(system.get("api_latency")),
            ),
            Text.assemble(
                ("▸ ", f"bold {self._CYAN}"),
                ("WebSocket ", self._DIM),
                self._service_health_text(system.get("websocket_health")),
                (
                    f"  {self._truncate_inline(system.get('websocket_last_error') or '', 48)}",
                    self._RED if str(system.get("websocket_last_error") or "").strip() else self._DIM,
                ),
            ),
            Text.assemble(
                ("▸ ", f"bold {self._CYAN}"),
                ("Balance   ", self._DIM),
                self._service_health_text(system.get("balance_health")),
            ),
            Text.assemble(
                ("▸ ", f"bold {self._CYAN}"),
                ("Candle    ", self._DIM),
                (str(system.get("candle_readiness", "-")), self._WHITE),
            ),
            Text.assemble(("  ", ""), ("Waiting   ", self._DIM), (str(system.get("candle_waiting", "-")), self._DIM)),
        ]

        right_lines = [
            Text.assemble(
                ("▸ ", f"bold {self._EMBER}"),
                ("Available ", self._DIM),
                (str(system.get("available_balance", "-")), f"bold {self._EMBER}"),
            ),
            Text.assemble(
                ("▸ ", f"bold {self._GREEN}"),
                ("Total     ", self._DIM),
                (str(system.get("total_balance", "-")), f"bold {self._GREEN}"),
            ),
            Text.assemble(
                ("▸ ", f"bold {self._CYAN}"),
                ("Floor     ", self._DIM),
                (
                    str(system.get("risk_floor_display") or "-"),
                    (
                        self._RED
                        if system.get("portfolio_meets_trade_floor") is False
                        else (
                            self._GREEN
                            if system.get("portfolio_meets_trade_floor") is True
                            else self._WHITE
                        )
                    ),
                ),
            ),
            Text.assemble(
                ("▸ ", f"bold {self._WHITE}"),
                ("Trades    ", self._DIM),
                (f"{system.get('trade_count', '-')}/{system.get('max_daily_trades', '-')}", self._WHITE),
            ),
            Text.assemble(
                ("▸ ", f"bold {self._RED}"),
                ("Risk/Trd  ", self._DIM),
                (str(system.get("risk_per_trade", "-")), f"bold {self._RED}"),
            ),
            Text.assemble(
                ("▸ ", f"bold {self._WHITE}"),
                ("DailyLoss ", self._DIM),
                (f"{system.get('daily_loss', '-')} ({system.get('daily_loss_pct', '-')})", self._WHITE),
            ),
            Text.assemble(
                ("▸ ", self._DIM),
                ("Cooldown  ", self._DIM),
                (
                    str(system.get("cooling_down", "-")),
                    f"bold {self._EMBER}" if str(system.get("cooling_down", "-")) == "Yes" else f"bold {self._GREEN}",
                ),
            ),
        ]

        grid.add_row(Group(*left_lines), Group(*right_lines))

        # Show degraded mode reason if present
        degraded_reason = snapshot.get("auth_degraded_reason", "")
        if degraded_reason:
            grid.add_row(Text("⚠ Degraded", style=f"bold {self._RED}"), Text(str(degraded_reason), style=self._RED))

        return self._panel(grid, title="⚙ System Bus", theme="system")

    def _build_signal_alignment_panel(self, snapshot: Dict[str, Any]) -> Panel:
        term_w, term_h = self._layout_term_dimensions(self.console)
        show_wait_col = term_w >= 120
        max_pair_rows = max(12, min(22, max(8, term_h - 14)))

        table = Table(expand=True, show_lines=False, row_styles=["", "on #111111"], padding=(0, 0), pad_edge=False)
        table.add_column("Pair", style=self._WHITE, no_wrap=True)
        table.add_column("TF", justify="center", no_wrap=True, style=self._DIM)
        if show_wait_col:
            table.add_column("Wait", no_wrap=True, style=self._DIM)
        table.add_column("M/m/T", justify="center", no_wrap=True)
        table.add_column("Trend", justify="center", no_wrap=True)
        table.add_column("Action", justify="center", no_wrap=True)
        table.add_column("Status", no_wrap=True, max_width=12, style=self._DIM)

        rows = list(snapshot.get("signal_alignment") or [])
        signal_score = self._signal_score(rows)
        buy_signals = sum(1 for row in rows if str(row.get("action") or "").upper() == "BUY")
        sell_signals = sum(1 for row in rows if str(row.get("action") or "").upper() == "SELL")
        wait_signals = sum(1 for row in rows if str(row.get("action") or "").upper() == "WAIT")

        def _trend_cell_style(trend_raw: str) -> str:
            t = str(trend_raw or "MIXED").upper()
            if t in ("BUY", "UP"):
                return f"bold {self._GREEN}"
            if t in ("SELL", "DOWN"):
                return f"bold {self._RED}"
            return f"bold {self._EMBER}"

        if not rows:
            if show_wait_col:
                table.add_row("-", "-", "-", "-", "-", "-", "No pairs")
            else:
                table.add_row("-", "-", "-", "-", "-", "No pairs")
        else:
            for row in rows[:max_pair_rows]:
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
                symbol = self._short_symbol_label(row.get("symbol") or "-")
                tf_cell = str(row.get("tf_ready") or "-")
                status = str(row.get("status") or row.get("pair_state") or "Ready")[:14]
                trend_label = str(row.get("trend") or "MIXED")
                trend_cell = Text(trend_label, style=_trend_cell_style(trend_label))
                mmt = f"{macro}/{micro}/{trigger}"
                wait_cell = str(row.get("wait_detail") or "-")
                if show_wait_col:
                    table.add_row(symbol, tf_cell, wait_cell, mmt, trend_cell, action_text, status)
                else:
                    table.add_row(symbol, tf_cell, mmt, trend_cell, action_text, status)

        summary_lines = [
            Text.assemble(
                ("▸ ", f"bold {self._PURPLE}"),
                ("Quality ", self._DIM),
                (self._signal_quality_label(signal_score), self._signal_quality_style(signal_score)),
                (f" ({signal_score:+.1f})", self._DIM),
                ("  │  ", self._DIM),
                ("▲ ", f"bold {self._GREEN}"),
                (f"{buy_signals}", f"bold {self._GREEN}"),
                ("  ▼ ", f"bold {self._RED}"),
                (f"{sell_signals}", f"bold {self._RED}"),
                ("  ● ", f"bold {self._EMBER}"),
                (f"{wait_signals}", f"bold {self._EMBER}"),
            ),
            Text.assemble(
                ("  Trend ", self._DIM),
                self._sparkline_text(
                    self._trend_values("signal_score", [signal_score]),
                    filled_style=self._signal_quality_style(signal_score),
                ),
            ),
        ]

        return self._panel(Group(*summary_lines, table), title="◎ Signal Radar", theme=self._resolve_signal_theme(rows))

    def _build_signal_flow_panel(self, snapshot: Dict[str, Any]) -> Panel:
        """Render Signal Flow. Compact mode: unified per-pair+strategy table. Wide: per-strategy tables."""
        compact_term = self._terminal_compact_mode()
        if compact_term:
            return self._build_signal_flow_compact_new(snapshot)

        # --- wide-mode path (unchanged) ---
        ui_cfg = dict(snapshot.get("ui") or {})

        try:
            flow_snapshot = get_latest_signal_flow_snapshot()
        except Exception as exc:
            self._safe_stderr_write(f"[cli_ui] signal flow snapshot error: {exc}\n")
            flow_snapshot = {}

        _term_w, term_h = self._layout_term_dimensions(self.console)
        narrow = compact_term
        max_rows = max(6, min(22 if narrow else 24, term_h - (12 if narrow else 16)))

        if not isinstance(flow_snapshot, dict) or not flow_snapshot:
            empty = Table(expand=True, show_lines=False, row_styles=["", "on #111111"], padding=(0, 0), pad_edge=False)
            if narrow:
                why_w = max(12, _term_w - 20)
                empty.add_column("Pair", style=self._WHITE, no_wrap=True, min_width=10)
                empty.add_column("Stp", style=self._DIM, no_wrap=True, width=4)
                empty.add_column("\u2713", justify="center", no_wrap=True, width=2)
                empty.add_column("Why", style=self._DIM, overflow="ellipsis", max_width=why_w)
            else:
                empty.add_column("Pair", style=self._WHITE, max_width=8, no_wrap=True)
                empty.add_column("Step", style=self._DIM, no_wrap=True, max_width=7)
                empty.add_column("\u2713", justify="center", no_wrap=True, width=2)
                empty.add_column("Why", style=self._DIM, ratio=2, overflow="ellipsis", min_width=40, max_width=68)
            empty.add_row("-", "—", "·", "no data")
            title_empty = "\u25ec SigFlow"
            if narrow and ui_cfg.get("sigflow_full"):
                title_empty += " [s: summary]"
            return self._panel(empty, title=title_empty, theme="signal_flow")

        sorted_pairs = sorted(flow_snapshot.items(), key=lambda kv: str(kv[0] or ""))
        total_pairs = len(sorted_pairs)
        page_count = max(1, math.ceil(total_pairs / max_rows)) if max_rows > 0 else 1
        page_index = 0
        if page_count > 1:
            rotate_seconds = max(2, int(self.refresh_interval_seconds * 3))
            page_index = int(datetime.now(timezone.utc).timestamp() // rotate_seconds) % page_count
        start_idx = page_index * max_rows
        end_idx = start_idx + max_rows
        visible_pairs = sorted_pairs[start_idx:end_idx]
        strategy_headers = (
            ("machete_v8b_lite", "MacheteV8bLite"),
            ("simple_scalp_plus", "SimpleScalpPlus"),
        )
        why_max_narrow = max(14, _term_w - 22)

        def _new_strategy_table() -> Table:
            strategy_table = Table(
                expand=True,
                show_lines=False,
                row_styles=["", "on #111111"],
                padding=(0, 0),
                pad_edge=False,
            )
            if narrow:
                strategy_table.add_column("Pair", style=self._WHITE, no_wrap=True, min_width=10)
                strategy_table.add_column("Stp", style=self._DIM, no_wrap=True, width=4)
                strategy_table.add_column("\u2713", justify="center", no_wrap=True, width=2)
                strategy_table.add_column(
                    "Why",
                    style=self._DIM,
                    overflow="ellipsis",
                    max_width=why_max_narrow,
                )
            else:
                strategy_table.add_column("Pair", style=self._WHITE, max_width=8, no_wrap=True)
                strategy_table.add_column("Step", style=self._DIM, no_wrap=True, max_width=7)
                strategy_table.add_column("\u2713", justify="center", no_wrap=True, width=2)
                strategy_table.add_column(
                    "Why",
                    style=self._DIM,
                    ratio=2,
                    overflow="ellipsis",
                    min_width=48,
                    max_width=96,
                )
            return strategy_table

        tables: Dict[str, Table] = {key: _new_strategy_table() for key, _ in strategy_headers}
        stats: Dict[str, Dict[str, int]] = {
            key: {"pass": 0, "reject": 0, "buy": 0, "sell": 0} for key, _ in strategy_headers
        }

        for pair, flow in visible_pairs:
            if not isinstance(flow, dict):
                continue
            updated_at = str(flow.get("updated_at") or "-")
            time_only = updated_at.split(" ", 1)[1] if " " in updated_at else updated_at
            steps_dict = flow.get("steps") or {}
            if not isinstance(steps_dict, dict):
                steps_dict = {}

            sym_full = str(pair or "-").strip().upper()
            if narrow:
                pair_cell = Text(sym_full, style=self._WHITE)
            else:
                symbol_label = self._short_symbol_label(pair or "-")
                pair_cell = Text.assemble((symbol_label, self._WHITE), (" " + time_only, self._DIM))

            st_cell = "St" if narrow else "Strat"

            for strategy_key, _title in strategy_headers:
                step_key = f"Strategy:{strategy_key}"
                step_data = steps_dict.get(step_key)
                if not isinstance(step_data, dict):
                    tables[strategy_key].add_row(pair_cell, st_cell, Text("\u00b7", style=self._DIM), "warmup")
                    continue

                result_raw = str(step_data.get("result") or "").upper()
                reason_raw = str(step_data.get("reason") or "")
                if narrow:
                    why = self._sigflow_short_why(reason_raw, result_raw, max_len=why_max_narrow)
                else:
                    why = self._humanize_signal_flow_reason(
                        step_key, result_raw, reason_raw, max_len=CLICommandCenter._SIGFLOW_WHY_MAX_LEN
                    )

                if result_raw == "PASS":
                    result_cell = Text("\u2713", style=f"bold {self._GREEN}")
                    stats[strategy_key]["pass"] += 1
                    reason_upper = reason_raw.upper()
                    if "TYPE=BUY" in reason_upper:
                        stats[strategy_key]["buy"] += 1
                    elif "TYPE=SELL" in reason_upper:
                        stats[strategy_key]["sell"] += 1
                elif result_raw == "REJECT":
                    result_cell = Text("\u2717", style=f"bold {self._RED}")
                    stats[strategy_key]["reject"] += 1
                elif result_raw == "INFO":
                    result_cell = Text("\u00b7", style=f"bold {self._WHITE}")
                else:
                    result_cell = Text("\u00b7", style=self._DIM)

                tables[strategy_key].add_row(pair_cell, st_cell, result_cell, why)

        blocks: List[Any] = []
        top_summary = Text.assemble(
            ("\u25b8 ", f"bold {self._CYAN}"),
            (f"{total_pairs} pairs", self._DIM),
            (" \u2502 ", self._DIM),
            (f"page {page_index + 1}/{page_count}", self._DIM),
        )
        blocks.append(top_summary)

        for idx, (strategy_key, strategy_label) in enumerate(strategy_headers):
            strategy_stats = stats[strategy_key]
            strategy_summary = Text.assemble(
                ("\u2022 ", self._DIM),
                (strategy_label, f"bold {self._WHITE}"),
                (" \u2502 ", self._DIM),
                ("\u2713", f"bold {self._GREEN}"),
                (str(strategy_stats["pass"]), f"bold {self._GREEN}"),
                (" \u2717", f"bold {self._RED}"),
                (str(strategy_stats["reject"]), f"bold {self._RED}"),
                (" \u2502 SELL ", self._DIM),
                (str(strategy_stats["sell"]), f"bold {self._RED}"),
                ("  BUY ", self._DIM),
                (str(strategy_stats["buy"]), f"bold {self._GREEN}"),
            )
            blocks.append(strategy_summary)
            blocks.append(tables[strategy_key])
            if idx < len(strategy_headers) - 1:
                blocks.append(Text("", style=self._DIM))

        flow_title = "\u25ec SigFlow"
        if narrow and ui_cfg.get("sigflow_full"):
            flow_title += " [s: summary]"
        return self._panel(Group(*blocks), title=flow_title, theme="signal_flow")

    def _build_recent_events_panel(self, snapshot: Dict[str, Any]) -> Panel:
        rows = list(snapshot.get("recent_events") or [])
        if not rows:
            return self._panel(Text("No recent events", style=self._DIM), title="◖ Event Tape", theme="events")

        lines: List[Text] = []
        for row in rows[:3]:
            event_type = str(row.get("type") or "EVT").upper()
            if event_type == "TRADE":
                style = f"bold {self._GREEN}"
            elif "WITHDRAW" in event_type or "LOW" in event_type:
                style = f"bold {self._RED}"
            else:
                style = f"bold {self._EMBER}"
            timestamp = str(row.get("timestamp") or "-")
            message = self._truncate_inline(str(row.get("message") or "-"), 44)
            lines.append(
                Text.assemble((f"{timestamp} ", self._DIM), (f"[{event_type}] ", style), (message, self._WHITE))
            )

        return self._panel(Group(*lines), title="◖ Event Tape", theme="events")

    def _build_balance_breakdown_panel(self, snapshot: Dict[str, Any]) -> Panel:
        system = snapshot.get("system", {})
        breakdown_lines = list(system.get("balance_breakdown") or [])

        table = Table(expand=True, show_header=False, padding=(0, 0), pad_edge=False)
        table.add_column("Holding", style="white")
        table.add_column("Allocation", justify="right")

        summary_lines: List[Text] = []
        if breakdown_lines:
            total_balance = self._extract_numeric(system.get("total_balance"), 0.0)
            top_allocation_pct = max((self._extract_allocation_pct(item) for item in breakdown_lines), default=0.0)
            cash_allocation_pct = 0.0
            for item in breakdown_lines:
                if self._is_cash_breakdown_line(item):
                    cash_allocation_pct = self._extract_allocation_pct(item)
                    break
            summary_lines.append(
                Text.assemble(
                    ("▸ ", f"bold {self._EMBER}"),
                    ("Mix    ", self._DIM),
                    self._sparkline_text(
                        [self._extract_allocation_pct(item) for item in breakdown_lines[:8]],
                        filled_style=f"bold {self._EMBER}",
                    ),
                )
            )
            summary_lines.append(
                Text.assemble(
                    ("  Total ", self._DIM),
                    self._sparkline_text(
                        self._trend_values("total_balance", [total_balance]), filled_style=f"bold {self._GREEN}"
                    ),
                )
            )
            summary_lines.append(
                Text.assemble(
                    ("▸ ", f"bold {self._EMBER}"),
                    ("Conc   ", self._DIM),
                    (
                        f"{top_allocation_pct:.1f}%",
                        f"bold {self._RED}" if top_allocation_pct >= 70.0 else f"bold {self._WHITE}",
                    ),
                    ("  │  ", self._DIM),
                    ("Cash ", self._DIM),
                    (f"{cash_allocation_pct:.1f}%", f"bold {self._EMBER}"),
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
            next(
                (self._extract_allocation_pct(item) for item in breakdown_lines if self._is_cash_breakdown_line(item)),
                0.0,
            ),
        )
        if summary_lines:
            return self._panel(Group(*summary_lines, table), title="▣ Portfolio", theme=portfolio_theme)
        return self._panel(table, title="▣ Portfolio", theme=portfolio_theme)

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
                ("▸ ", f"bold {self._EMBER}"),
                ("Fresh   ", self._DIM),
                self._freshness_text(system.get("freshness"), system.get("market_age_seconds")),
                ("  │  ", self._DIM),
                ("API ", self._DIM),
                (str(system.get("api_latency") or "-"), self._WHITE),
            )
        )
        lines.append(
            Text.assemble(
                ("▸ ", f"bold {self._EMBER}"),
                ("Risk    ", self._DIM),
                (str(system.get("risk_per_trade") or "-"), f"bold {self._RED}"),
                ("  │  ", self._DIM),
                ("Cool ", self._DIM),
                (
                    str(system.get("cooling_down") or "No"),
                    (
                        f"bold {self._EMBER}"
                        if str(system.get("cooling_down") or "No") == "Yes"
                        else f"bold {self._GREEN}"
                    ),
                ),
            )
        )
        lines.append(
            Text.assemble(
                ("▸ ", f"bold {self._EMBER}"),
                ("Loss    ", self._DIM),
                self._meter_text(
                    daily_loss_value, daily_loss_cap, width=16, filled_style=f"bold {self._RED}", suffix="quote"
                ),
            )
        )
        lines.append(
            Text.assemble(
                ("▸ ", f"bold {self._EMBER}"),
                ("Active  ", self._DIM),
                self._meter_text(
                    trade_count, max_daily_trades, width=16, filled_style=f"bold {self._GREEN}", decimals=0
                ),
            )
        )
        lines.append(
            Text.assemble(
                ("▸ ", f"bold {self._EMBER}"),
                ("Expose  ", self._DIM),
                self._meter_text(
                    float(open_positions), max_positions, width=16, filled_style=f"bold {self._WHITE}", decimals=0
                ),
            )
        )
        lines.append(
            Text.assemble(
                ("  Load  ", self._DIM),
                self._sparkline_text(
                    self._trend_values("daily_loss", [daily_loss_value]), filled_style=f"bold {self._RED}"
                ),
            )
        )
        lines.append(
            Text.assemble(
                ("  Pos   ", self._DIM),
                self._sparkline_text(
                    self._trend_values("open_positions", [float(open_positions)]), filled_style=f"bold {self._CYAN}"
                ),
            )
        )

        return self._panel(Group(*lines), title="⚠ Risk Rails", theme="risk")

    def _build_footer(self, snapshot: Dict[str, Any]) -> Panel:
        """Compact status strip: wide pair list + meta; optional pending (no in-panel chat/input)."""
        chat = snapshot.get("chat", {}) or {}
        ui_cfg = dict(snapshot.get("ui") or {})
        pending = chat.get("pending_confirmation") or {}
        footer_mode = str(ui_cfg.get("footer_mode") or "compact").lower()
        log_filter = str(ui_cfg.get("log_level_filter") or "INFO").upper()
        term_width, _term_h = self._layout_term_dimensions(self.console)
        text_budget = max(36, int(term_width) - 10)
        # Leave room for mode | time | log | footer tag after the pair list.
        reserved_suffix = 46
        pairs_budget = max(32, int(term_width) - reserved_suffix)

        meta = Text.assemble(
            ("\u25b8 ", self._CYAN),
            (self._truncate_inline(snapshot.get("pairs", "NONE"), pairs_budget), self._DIM),
            ("  \u2502  ", self._DIM),
            (snapshot.get("mode", "-"), self._mode_style(snapshot.get("mode", "-"))),
            ("  \u2502  ", self._DIM),
            (snapshot.get("updated_at", "-"), self._DIM),
            ("  \u2502  ", self._DIM),
            ("log:", self._DIM),
            (f"{log_filter}+", self._WHITE),
            ("  \u2502  ", self._DIM),
            (footer_mode, self._DIM),
        )
        lines: List[Text] = [meta]

        if pending:
            lines.append(
                Text.assemble(
                    ("Pending: ", self._EMBER),
                    (
                        self._truncate_inline(
                            pending.get("summary") or pending.get("command_text") or "-",
                            text_budget,
                        ),
                        self._EMBER,
                    ),
                )
            )
        elif footer_mode == "verbose":
            st = str(snapshot.get("strategy_mode") or "standard").lower()
            hint = str(chat.get("status") or snapshot.get("commands_hint") or "help | status")
            lines.append(Text.assemble((f"[{st}] ", self._DIM), (self._truncate_inline(hint, text_budget), self._DIM)))
        else:
            hint = str(snapshot.get("commands_hint") or "help | status")
            lines.append(Text(self._truncate_inline(hint, text_budget), style=self._DIM))

        return self._panel(Group(*lines), title="Status", theme="footer")

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
    def _is_cash_asset(asset: Any) -> bool:
        return str(asset or "").strip().upper() in CLICommandCenter._CASH_ASSETS

    @staticmethod
    def _is_cash_breakdown_line(line: Any) -> bool:
        asset = str(line or "").split(" ", 1)[0].strip().upper()
        return CLICommandCenter._is_cash_asset(asset)

    @staticmethod
    def _short_symbol_label(symbol: Any) -> str:
        label = str(symbol or "-").strip().upper()
        if label.startswith("THB_") or label.startswith("USDT_"):
            return label.split("_", 1)[1] or label
        if label.endswith("_THB") or label.endswith("_USDT"):
            return label.rsplit("_", 1)[0] or label
        if label.endswith("USDT") and "_" not in label and len(label) > 4:
            return label[:-4] or label
        return label or "-"

    @staticmethod
    def _balance_breakdown_text(line: Any) -> Text:
        content = str(line or "-")
        if content == "-":
            return Text(content, style=CLICommandCenter._WHITE)

        main_part, separator, suffix = content.partition(" (")
        asset = main_part.split(" ", 1)[0].upper()
        allocation_pct = CLICommandCenter._extract_allocation_pct(content)
        if CLICommandCenter._is_cash_asset(asset):
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
            trend_raw = str(row.get("trend") or "").upper()
            if trend_raw == "UP":
                trend_norm = "BUY"
            elif trend_raw == "DOWN":
                trend_norm = "SELL"
            else:
                trend_norm = trend_raw

            if action == "BUY":
                score += 2.0
            elif action == "SELL":
                score -= 2.0
            elif action == "WAIT":
                score -= 0.5

            if trend_norm == "BUY":
                score += 1.0
            elif trend_norm == "SELL":
                score -= 1.0

            status_plain = str(row.get("status") or "").strip()
            status_u = status_plain.upper()
            combined_u = f"{row.get('status') or ''} {row.get('pair_state') or ''}".upper()
            if "LAG" in combined_u:
                score -= 0.5
            elif action in ("HOLD", "WAIT") and status_u != "READY":
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
            text.append("━" * filled_units, style=filled_style)
        if empty_units:
            text.append("─" * empty_units, style=cls._DIM)
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
        if CLICommandCenter._is_cash_asset(normalized_asset):
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
            text.append("━" * filled_units, style=filled_style)
        if empty_units:
            text.append("─" * empty_units, style=CLICommandCenter._DIM)
        text.append("]", style=CLICommandCenter._DIM)
        text.append(f" {normalized_pct:5.1f}%", style=CLICommandCenter._DIM)
        return text

    _STRATEGY_SHORT: ClassVar[Dict[str, str]] = {
        "machetev8blite": "MchtV8",
        "simplescalpplus": "Scalp+",
    }

    def _abbrev_strategy(self, name: str) -> str:
        """Return a compact display name for a strategy_source string."""
        key = (name or "-").lower().replace(" ", "")
        return self._STRATEGY_SHORT.get(key, (name or "-")[:6])

    @classmethod
    def _strategy_label_text(cls, name: str) -> "Text":
        """Colored strategy badge: 〔M〕cyan, 〔S〕amber, 〔B〕dim, 〔~〕magenta, 〔?〕dim-red."""
        key = str(name or "").lower().replace(" ", "").replace("_", "")
        if "machetev8b" in key or key == "machetev8blite":
            return Text("〔M〕", style=f"bold {cls._CYAN}")
        if "simplescalp" in key or key == "simplescalpplus":
            return Text("〔S〕", style=f"bold {cls._EMBER}")
        if key == "bootstrap":
            return Text("〔B〕", style=cls._DIM)
        if key == "manual":
            return Text("〔~〕", style=f"bold {cls._PURPLE}")
        return Text("〔?〕", style=f"dim {cls._RED}")

    @staticmethod
    def _position_emoji(pnl_pct: Any, sl_dist_pct: Any) -> str:
        """Row emoji: ⚡ SL danger, 🟢 profit, 🔴 big loss, 🟡 small/no loss."""
        try:
            sl = abs(float(sl_dist_pct or 999))
        except (TypeError, ValueError):
            sl = 999.0
        if sl < 0.3:
            return "⚡"
        try:
            pnl = float(pnl_pct or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        if pnl > 0:
            return "🟢"
        if pnl < -0.5:
            return "🔴"
        return "🟡"

    @staticmethod
    def _pnl_style_rich(pnl_pct: Any) -> str:
        """PnL style: bold-green > 0, amber -0.5~0, red < -0.5."""
        try:
            v = float(pnl_pct or 0)
        except (TypeError, ValueError):
            return CLICommandCenter._WHITE
        if v > 0:
            return f"bold {CLICommandCenter._GREEN}"
        if v >= -0.5:
            return f"bold {CLICommandCenter._EMBER}"
        return f"bold {CLICommandCenter._RED}"

    @staticmethod
    def _sl_dist_style(sl_dist_pct: Any) -> str:
        """SL distance style: bold-red < 1%, amber 1–2%, dim > 2%."""
        try:
            v = abs(float(sl_dist_pct or 999))
        except (TypeError, ValueError):
            return CLICommandCenter._DIM
        if v < 1.0:
            return f"bold {CLICommandCenter._RED}"
        if v < 2.0:
            return f"bold {CLICommandCenter._EMBER}"
        return CLICommandCenter._DIM

    @staticmethod
    def _tp_dist_style(tp_dist_pct: Any) -> str:
        """TP distance style: bold-green < 1%, amber 1–3%, dim > 3%."""
        try:
            v = abs(float(tp_dist_pct or 999))
        except (TypeError, ValueError):
            return CLICommandCenter._DIM
        if v < 1.0:
            return f"bold {CLICommandCenter._GREEN}"
        if v < 3.0:
            return f"bold {CLICommandCenter._EMBER}"
        return CLICommandCenter._DIM

    @staticmethod
    def _conf_style(conf: Any) -> str:
        """Confidence style: bold-green ≥0.90, amber 0.70–0.89, dim < 0.70."""
        try:
            v = float(conf or 0)
        except (TypeError, ValueError):
            return CLICommandCenter._DIM
        if v >= 0.90:
            return f"bold {CLICommandCenter._GREEN}"
        if v >= 0.70:
            return f"bold {CLICommandCenter._EMBER}"
        return CLICommandCenter._DIM

    @staticmethod
    def _rr_style(rr: Any) -> str:
        """Risk/reward style: bold-green ≥2.0, amber 1.5–1.9, red < 1.5."""
        s = str(rr or "").upper()
        if s in {"—", "-", "N/A", ""}:
            return CLICommandCenter._DIM
        try:
            v = float(s)
        except (TypeError, ValueError):
            return CLICommandCenter._DIM
        if v >= 2.0:
            return f"bold {CLICommandCenter._GREEN}"
        if v >= 1.5:
            return f"bold {CLICommandCenter._EMBER}"
        return f"bold {CLICommandCenter._RED}"

    @staticmethod
    def _parse_strategy_signal_fields(reason: str, result: str) -> "tuple[str, str, str]":
        """Parse (signal_type, conf, rr) from a Strategy step reason string (PASS only)."""
        reason_s = str(reason or "")
        result_u = str(result or "").upper()
        if result_u != "PASS":
            return "—", "—", "—"
        type_m = re.search(r"type=([A-Z]+)", reason_s, re.I)
        conf_m = re.search(r"\bconf=([0-9.]+)", reason_s, re.I)
        rr_m = re.search(r"\bRR=([0-9.]+|N/A)\b", reason_s, re.I)
        signal_type = type_m.group(1).upper() if type_m else "—"
        conf = conf_m.group(1) if conf_m else "—"
        rr = rr_m.group(1) if rr_m else "—"
        return signal_type, conf, rr

    @classmethod
    def _sigflow_reason_text(cls, reason: str, result: str) -> "Text":
        """Colored Reason cell for the SigFlow unified table."""
        s = str(reason or "").strip()
        result_u = str(result or "").upper()
        if not s:
            return Text("—", style=cls._DIM)
        low = s.lower()
        # ALL_CAPS_UNDERSCORES reason codes → dim red
        if re.match(r"^[A-Z][A-Z0-9_]{2,}$", s) and "_" in s:
            return Text(s[:24], style=f"dim {cls._RED}")
        # NO_SETUP
        if "no_setup" in low or "generate_signal() returned none" in low or "no setup" in low:
            return Text("NO_SETUP", style=cls._DIM)
        # Insufficient bars
        m = re.search(r"insufficient.*?(\d+)/(\d+)", low)
        if m:
            return Text(f"INSUFF ({m.group(1)}/{m.group(2)})", style=f"dim {cls._EMBER}")
        m2 = re.search(r"(\d+)/(\d+) bars", low)
        if m2:
            return Text(f"INSUFF ({m2.group(1)}/{m2.group(2)})", style=f"dim {cls._EMBER}")
        # Warmup
        if "warmup" in low or "first signal cycle" in low or s.lower() == "no diagnostics":
            return Text("warmup", style=cls._DIM)
        # Passing signal → cyan summary
        if result_u == "PASS":
            conf_m = re.search(r"\bconf=([0-9.]+)", s, re.I)
            if conf_m:
                return Text(f"conf={conf_m.group(1)}", style=f"bold {cls._CYAN}")
            tail = cls._truncate_inline(s.split(":")[-1].strip() if ":" in s else s, 20)
            return Text(tail, style=f"bold {cls._CYAN}")
        # SR_GUARD shortcut
        if "sr_guard" in low:
            return Text("SR_GUARD_BLOCKED", style=f"dim {cls._RED}")
        # Cooldown
        if "cooldown" in low:
            return Text("cooldown", style=f"dim {cls._EMBER}")
        # Generic tail
        tail = cls._truncate_inline(s.split(":")[-1].strip() if ":" in s else s, 22)
        return Text(tail, style=cls._DIM)

    @staticmethod
    def _format_price_smart(value: Any) -> str:
        """Smart price format: fewer decimals for large prices."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return "-"
        if v <= 0:
            return "-"
        if v >= 10000:
            return f"{v:,.0f}"
        if v >= 100:
            return f"{v:,.2f}"
        if v >= 1:
            return f"{v:.4f}"
        return f"{v:.6f}"

    @staticmethod
    def _group_log_rows(rows: "List[Dict[str, str]]") -> "List[Dict[str, str]]":
        """Collapse runs of 'up to date' log messages into a single counted line."""
        result: List[Dict[str, str]] = []
        udt_count = 0
        last_udt: Optional[Dict[str, str]] = None
        for row in rows:
            msg = str(row.get("message") or "").lower()
            if "up to date" in msg:
                udt_count += 1
                last_udt = row
            else:
                if udt_count > 0 and last_udt is not None:
                    if udt_count > 1:
                        merged = dict(last_udt)
                        merged["message"] = f"all pairs: up to date \u00d7{udt_count}"
                        result.append(merged)
                    else:
                        result.append(last_udt)
                    udt_count = 0
                    last_udt = None
                result.append(row)
        if udt_count > 0 and last_udt is not None:
            if udt_count > 1:
                merged = dict(last_udt)
                merged["message"] = f"all pairs: up to date \u00d7{udt_count}"
                result.append(merged)
            else:
                result.append(last_udt)
        return result

    @classmethod
    def _log_row_text_compact(cls, row: "Dict[str, str]") -> "Text":
        """Compact log line: TS │ TAG │ EMOJI msg, color-coded by level/content."""
        level   = str(row.get("level") or "INFO").upper()
        msg     = str(row.get("message") or "-")
        ts      = str(row.get("timestamp") or "-")
        tag     = str(row.get("tag") or row.get("logger") or "")[:4].ljust(4)
        emoji   = str(row.get("emoji") or "")
        msg_low = msg.lower()

        # Determine style
        if level in {"ERROR", "CRITICAL"} or any(w in msg_low for w in ("paused", "blocked", "failed", "❗")):
            style = f"bold {cls._RED}"
        elif level == "WARNING" or "⚠" in emoji:
            style = f"bold {cls._EMBER}"
        elif any(e in emoji for e in ("✅", "💰", "🎯", "📩")):
            style = f"bold {cls._GREEN}"
        elif any(e in emoji for e in ("🛑", "🚨", "⛔", "🔴", "❌")):
            style = f"bold {cls._RED}"
        elif any(e in emoji for e in ("📊", "🔄", "📡", "🌐")):
            style = cls._CYAN
        elif "up to date" in msg_low or "🔁" in emoji:
            style = cls._DIM
        else:
            style = cls._WHITE

        # Prefix emoji (avoid double if message already starts with it)
        prefix = f"{emoji} " if emoji and not msg.startswith(emoji) else ""

        return Text.assemble(
            (f"{ts} ", cls._DIM),
            (f"{tag}", f"bold {cls._CYAN}"),
            (" │ ", cls._DIM),
            (f"{prefix}{msg}", style),
        )

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
        if normalized in {
            "OFF",
            "NO DATA",
            "CONNECTING",
            "RECONNECTING",
            "NOT_STARTED",
            "NO_PAIRS",
            "NO_BACKEND",
            "DISABLED",
        }:
            return Text(raw, style=f"bold {CLICommandCenter._EMBER}")
        return Text(raw, style=CLICommandCenter._WHITE)
