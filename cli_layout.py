"""
cli_layout.py — Clean, structured terminal output for the trading bot.

Three primitives:
    * StartupReporter           — banner / phase / step / running readout.
    * log_signal_event()        — one-line event log for trade signals.
    * SuppressRepeatStateFilter — drop repeating state-based log records.

Console output goes to a single stream (stdout by default) with stable column
widths and zero ANSI/emoji noise. File logging (JSON via logger_setup) is
unaffected: the reporter still emits an INFO record per phase boundary so the
JSON audit log stays complete.
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Mapping, Optional, TextIO


__all__ = [
    "StartupReporter",
    "log_signal_event",
    "SuppressRepeatStateFilter",
]


# ─── colour palette ─────────────────────────────────────────────────────────
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"


def _terminal_width(default: int = 80, cap: int = 100) -> int:
    try:
        cols = shutil.get_terminal_size(fallback=(default, 20)).columns
    except Exception:
        cols = default
    return max(60, min(cap, cols))


def _supports_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(stream, "isatty"):
        return False
    try:
        return stream.isatty()
    except Exception:
        return False


# ─── startup reporter ──────────────────────────────────────────────────────
@dataclass
class _PhaseState:
    name: str
    started_at: float = field(default_factory=time.monotonic)
    step_count: int = 0


class StartupReporter:
    """Phase-based, column-aligned startup output.

    Usage:
        reporter = StartupReporter(logger=logging.getLogger("crypto_bot"))
        reporter.banner("CRYPTO TRADING BOT — Binance.th", version="2026.04.27")

        with reporter.phase("Initialization"):
            reporter.step("config",   "bot_config.yaml validated")
            reporter.step("exchange", "api.binance.th reachable", detail="skew 23ms")

        reporter.running(mode="full_auto", pairs=("BTCUSDT","ETHUSDT"), poll_seconds=60)

    Each call also emits a structured INFO record to ``logger`` (if provided),
    so file logs / observability pipelines keep a complete record.
    """

    LABEL_WIDTH = 18

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        stream: Optional[TextIO] = None,
        width: Optional[int] = None,
        use_color: Optional[bool] = None,
    ) -> None:
        self._logger = logger
        self._stream = stream or sys.stdout
        self._width = width or _terminal_width()
        self._color = _supports_color(self._stream) if use_color is None else use_color
        self._lock = threading.Lock()
        self._phase: Optional[_PhaseState] = None
        self._app_started_at = time.monotonic()

    # ── public API ──────────────────────────────────────────────────────
    def banner(self, title: str, *, version: str = "", subtitle: str = "") -> None:
        line = "=" * self._width
        header = title if not version else f"{title}    v{version}"
        meta = subtitle or f"PID {os.getpid()}    started {datetime.now():%Y-%m-%d %H:%M:%S}"
        with self._lock:
            self._write("")
            self._write(self._color_text(line, _CYAN, bold=True))
            self._write(self._color_text(f"  {header}", _BOLD))
            self._write(self._color_text(f"  {meta}", _DIM))
            self._write(self._color_text(line, _CYAN, bold=True))
        self._emit_log("INFO", f"banner | {header} | {meta}")

    @contextlib.contextmanager
    def phase(self, name: str) -> Iterator["StartupReporter"]:
        with self._lock:
            self._phase = _PhaseState(name=name)
            ts = datetime.now().strftime("%H:%M:%S")
            self._write("")
            self._write(self._color_text(f"[{ts}] PHASE  {name}", _CYAN, bold=True))
        self._emit_log("INFO", f"phase.start | {name}")
        try:
            yield self
        finally:
            with self._lock:
                phase = self._phase
                self._phase = None
            if phase is not None:
                elapsed_ms = int((time.monotonic() - phase.started_at) * 1000)
                self._emit_log(
                    "INFO",
                    f"phase.done | {phase.name} | steps={phase.step_count} elapsed_ms={elapsed_ms}",
                )

    def step(self, label: str, status: str, *, detail: str = "", level: str = "INFO") -> None:
        marker = self._level_marker(level)
        label_col = label[: self.LABEL_WIDTH].ljust(self.LABEL_WIDTH)
        body = status if not detail else f"{status}  {self._color_text(detail, _DIM)}"
        line = f"       {marker}  · {label_col}  {body}"
        with self._lock:
            if self._phase is not None:
                self._phase.step_count += 1
            self._write(line)
        self._emit_log(level, f"step | {label}={status}" + (f" | {detail}" if detail else ""))

    def strategy(self, name: str, description: str, *, enabled: bool = True) -> None:
        tag = self._color_text("[ENABLED] ", _GREEN, bold=True) if enabled else self._color_text("[DISABLED]", _DIM)
        label_col = name[: self.LABEL_WIDTH].ljust(self.LABEL_WIDTH)
        desc_col = description.ljust(max(0, self._width - self.LABEL_WIDTH - 24))
        line = f"       INFO  · {label_col}  {desc_col}{tag}"
        with self._lock:
            if self._phase is not None:
                self._phase.step_count += 1
            self._write(line)
        self._emit_log(
            "INFO",
            f"strategy | name={name} enabled={enabled} desc={description}",
        )

    def result(self, summary: str, *, ok: bool = True) -> None:
        color = _GREEN if ok else _RED
        marker = self._color_text("OK  " if ok else "FAIL", color, bold=True)
        with self._lock:
            self._write(f"       {marker}  {summary}")
        self._emit_log("INFO" if ok else "ERROR", f"result | ok={ok} | {summary}")

    def warning(self, summary: str) -> None:
        marker = self._color_text("WARN", _YELLOW, bold=True)
        with self._lock:
            self._write(f"       {marker}  {summary}")
        self._emit_log("WARNING", f"warn | {summary}")

    def running(self, **fields: Any) -> None:
        bar = "=" * self._width
        items = " · ".join(self._render_field(k, v) for k, v in fields.items())
        head = self._color_text("  RUNNING", _GREEN, bold=True)
        with self._lock:
            self._write("")
            self._write(self._color_text(bar, _CYAN, bold=True))
            self._write(f"{head}    {items}    Ctrl+C to stop")
            self._write(self._color_text(bar, _CYAN, bold=True))
            self._write("")
        self._emit_log("INFO", "running | " + " ".join(f"{k}={v}" for k, v in fields.items()))

    # ── helpers ─────────────────────────────────────────────────────────
    def _level_marker(self, level: str) -> str:
        level = level.upper()
        if level in ("ERROR", "CRITICAL"):
            return self._color_text("ERR ", _RED, bold=True)
        if level == "WARNING":
            return self._color_text("WARN", _YELLOW, bold=True)
        return self._color_text("INFO", _GREEN)

    def _color_text(self, text: str, color: str, *, bold: bool = False) -> str:
        if not self._color:
            return text
        prefix = (_BOLD if bold else "") + color
        return f"{prefix}{text}{_RESET}"

    @staticmethod
    def _render_field(key: str, value: Any) -> str:
        if isinstance(value, (list, tuple, set)):
            value = ",".join(str(v) for v in value) or "—"
        return f"{key}={value}"

    def _write(self, text: str) -> None:
        try:
            print(text, file=self._stream, flush=True)
        except Exception:
            pass  # Never let logging itself crash startup.

    def _emit_log(self, level: str, message: str) -> None:
        if self._logger is None:
            return
        try:
            self._logger.log(getattr(logging, level.upper(), logging.INFO), message)
        except Exception:
            pass


# ─── event-based signal log ─────────────────────────────────────────────────
def log_signal_event(
    logger: logging.Logger,
    *,
    symbol: str,
    side: str,
    strategy: str,
    confidence: float,
    trigger: str,
    price: float,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    timeframe: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> None:
    """Emit ONE structured line per trade signal.

    Designed to replace state-spam such as
        "RSI 28.4 oversold"   (printed every iteration)
    with an event-shaped record that fires only on actual trigger transitions
    (e.g. EMA crossover bar, RSI cross-back-above-30, breakout bar).

    Output (single line, fixed columns):

        SIGNAL BTCUSDT  BUY  sniper            tf=15m  conf=0.78
               trigger=ema_cross  px=66412.30  SL=65980.00  TP=67240.50  R:R=1.92
    """
    rr = _risk_reward(side=side, price=price, stop_loss=stop_loss, take_profit=take_profit)

    parts = [
        f"SIGNAL {symbol:<10} {side:<4} {strategy:<18}",
    ]
    if timeframe:
        parts.append(f"tf={timeframe}")
    parts.append(f"conf={confidence:.2f}")
    parts.append(f"trigger={trigger}")
    parts.append(f"px={price:,.2f}")
    if stop_loss is not None:
        parts.append(f"SL={stop_loss:,.2f}")
    if take_profit is not None:
        parts.append(f"TP={take_profit:,.2f}")
    if rr is not None:
        parts.append(f"R:R={rr:.2f}")
    if extra:
        for k, v in extra.items():
            parts.append(f"{k}={v}")

    logger.info(
        " ".join(parts),
        extra={
            "event": "trade_signal",
            "symbol": symbol,
            "side": side,
            "strategy": strategy,
            "confidence": float(confidence),
            "trigger": trigger,
            "price": float(price),
            "stop_loss": float(stop_loss) if stop_loss is not None else None,
            "take_profit": float(take_profit) if take_profit is not None else None,
            "timeframe": timeframe,
            "risk_reward": rr,
        },
    )


def _risk_reward(
    *,
    side: str,
    price: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
) -> Optional[float]:
    if stop_loss is None or take_profit is None or price <= 0:
        return None
    if side.upper() == "BUY":
        risk = price - stop_loss
        reward = take_profit - price
    else:
        risk = stop_loss - price
        reward = price - take_profit
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


# ─── state-spam suppression filter ──────────────────────────────────────────
class SuppressRepeatStateFilter(logging.Filter):
    """Drop log records whose ``extra={'state_key': KEY}`` repeats inside ``ttl_seconds``.

    Use it on the console handler to silence every-iteration state logs while
    leaving the *first* occurrence (and the next one after the TTL) intact.
    Records without ``state_key`` are always passed through.

    Attach once at startup:

        for h in logging.getLogger().handlers:
            if isinstance(h, logging.StreamHandler):
                h.addFilter(SuppressRepeatStateFilter(ttl_seconds=300))
    """

    def __init__(self, ttl_seconds: float = 300.0, max_keys: int = 4096) -> None:
        super().__init__()
        self.ttl = float(ttl_seconds)
        self.max_keys = int(max_keys)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        key = getattr(record, "state_key", None)
        if not key:
            return True
        now = time.monotonic()
        with self._lock:
            last = self._last.get(key)
            if last is not None and (now - last) < self.ttl:
                return False
            self._last[key] = now
            if len(self._last) > self.max_keys:
                # Bound memory: drop the oldest half.
                cutoff = sorted(self._last.values())[len(self._last) // 2]
                self._last = {k: v for k, v in self._last.items() if v >= cutoff}
        return True
