"""
Weekly Performance Review — SPEC_08B
=====================================

Computes weekly trading stats from SQLite (`closed_trades` table),
sends a Telegram summary, and saves a Markdown report.
Inspired by Nate Herk's weekly-review workflow.

Usage (CLI):
    python weekly_review.py --manual                 # last 7 days
    python weekly_review.py --start 2026-04-19 --end 2026-04-26
    python weekly_review.py --manual --no-send       # skip Telegram
    python weekly_review.py --manual --no-save       # skip markdown file

Usage (programmatic):
    reviewer = WeeklyReviewer(db, config, alert_system)
    stats = reviewer.run_review()                    # last 7 days
    stats = reviewer.run_review(week_start, week_end)
"""

# --- NEW: SPEC_08B Weekly Review ---

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "bot_config.yaml"
DEFAULT_REVIEW_SUBDIR = "weekly_reviews"

GRADE_STARS: Dict[str, str] = {
    "A": "⭐⭐⭐⭐⭐",
    "B": "⭐⭐⭐⭐",
    "C": "⭐⭐⭐",
    "D": "⭐⭐",
    "F": "⭐",
    "N/A": "",
}


# ────────────────────────────────────────────────────────────────────────────
# Dataclass
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class WeeklyStats:
    """Aggregate weekly performance snapshot."""

    week_start: datetime
    week_end: datetime
    quote_currency: str = "USDT"

    starting_equity: float = 0.0
    ending_equity: float = 0.0
    week_return_amt: float = 0.0
    week_return_pct: float = 0.0

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0

    best_trade: Dict[str, Any] = field(default_factory=dict)
    worst_trade: Dict[str, Any] = field(default_factory=dict)
    avg_trade_pct: float = 0.0

    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0

    trades_by_trigger: Dict[str, int] = field(default_factory=dict)
    pnl_by_trigger: Dict[str, float] = field(default_factory=dict)
    trades_by_strategy: Dict[str, int] = field(default_factory=dict)
    pnl_by_strategy: Dict[str, float] = field(default_factory=dict)

    grade: str = "F"
    grade_score: int = 0
    grade_reasons: List[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────────────
# Reviewer
# ────────────────────────────────────────────────────────────────────────────


class WeeklyReviewer:
    """
    Compute weekly trading performance from `closed_trades`,
    send a Telegram summary, and save a Markdown file.

    The class is fully synchronous (matches `alerts.AlertSystem` and
    `database.Database` APIs). For asyncio runtimes call `run_review_async`
    which wraps the work in `asyncio.to_thread`.
    """

    def __init__(self, db, config: Optional[Dict[str, Any]] = None, alert_system=None):
        self.db = db
        self.config = config or {}
        self.alerts = alert_system

        review_cfg = dict(self.config.get("weekly_review", {}) or {})
        self.enabled = bool(review_cfg.get("enabled", True))
        self.day_of_week = int(review_cfg.get("day_of_week", 6))
        self.hour_utc = int(review_cfg.get("hour_utc", 17))
        self.benchmark = str(review_cfg.get("benchmark", "BTC")).upper()
        self.save_to_file = bool(review_cfg.get("save_to_file", True))
        self.send_telegram_flag = bool(review_cfg.get("send_telegram", True))
        self.file_path_template = str(review_cfg.get("file_path", f"{DEFAULT_REVIEW_SUBDIR}/review_{{date}}.md"))

        portfolio_cfg = dict(self.config.get("portfolio", {}) or {})
        self.quote_currency = str(
            review_cfg.get("quote_currency") or portfolio_cfg.get("quote_currency") or "USDT"
        ).upper()
        self.initial_balance = float(portfolio_cfg.get("initial_balance", 1000.0))

    # ── Public API ──────────────────────────────────────────────────────

    def run_review(
        self,
        week_start: Optional[datetime] = None,
        week_end: Optional[datetime] = None,
        *,
        send_telegram: Optional[bool] = None,
        save_to_file: Optional[bool] = None,
    ) -> WeeklyStats:
        """Compute stats and (optionally) send Telegram + save markdown."""
        if week_end is None:
            week_end = datetime.now(timezone.utc)
        if week_start is None:
            week_start = week_end - timedelta(days=7)

        stats = self.compute_weekly_stats(week_start, week_end)

        do_send = self.send_telegram_flag if send_telegram is None else bool(send_telegram)
        do_save = self.save_to_file if save_to_file is None else bool(save_to_file)

        if do_save:
            try:
                path = self.save_markdown(stats)
                if path:
                    logger.info("[WeeklyReview] Saved %s", path)
            except Exception as exc:
                logger.error("[WeeklyReview] Failed to save markdown: %s", exc, exc_info=True)

        if do_send and self.alerts is not None:
            try:
                ok = self.send_telegram(stats)
                if ok:
                    logger.info(
                        "[WeeklyReview] Telegram summary sent (grade=%s, return=%+.2f%%)",
                        stats.grade,
                        stats.week_return_pct,
                    )
                else:
                    logger.warning("[WeeklyReview] Telegram summary not sent (rate-limited or disabled)")
            except Exception as exc:
                logger.error("[WeeklyReview] Failed to send Telegram: %s", exc, exc_info=True)

        return stats

    async def run_review_async(
        self,
        week_start: Optional[datetime] = None,
        week_end: Optional[datetime] = None,
        **kwargs: Any,
    ) -> WeeklyStats:
        """Async wrapper — runs `run_review` in a worker thread."""
        import asyncio

        return await asyncio.to_thread(self.run_review, week_start, week_end, **kwargs)

    def compute_weekly_stats(
        self,
        week_start: datetime,
        week_end: datetime,
    ) -> WeeklyStats:
        """Pull closed trades in the window and compute every metric."""
        trades = self._fetch_closed_trades(week_start, week_end)

        ending_equity = self._estimate_current_equity()
        net_pnl_total = sum(self._safe(t, "net_pnl") for t in trades)
        starting_equity = max(ending_equity - net_pnl_total, 0.01)
        week_return_amt = net_pnl_total
        week_return_pct = week_return_amt / starting_equity * 100.0 if starting_equity > 0 else 0.0

        winners = [t for t in trades if self._safe(t, "net_pnl") > 0]
        losers = [t for t in trades if self._safe(t, "net_pnl") <= 0]
        win_rate = (len(winners) / len(trades) * 100.0) if trades else 0.0

        gross_profit = sum(self._safe(t, "net_pnl") for t in winners)
        gross_loss = abs(sum(self._safe(t, "net_pnl") for t in losers))
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            profit_factor = float("inf")
        else:
            profit_factor = 0.0

        best = max(trades, key=lambda t: self._safe(t, "net_pnl_pct")) if trades else {}
        worst = min(trades, key=lambda t: self._safe(t, "net_pnl_pct")) if trades else {}
        avg_pct = sum(self._safe(t, "net_pnl_pct") for t in trades) / len(trades) if trades else 0.0

        max_dd = self._compute_max_drawdown(trades, starting_equity)
        sharpe = self._compute_sharpe(trades, starting_equity)

        signal_lookup = self._fetch_strategy_lookup(week_start, week_end)

        trades_by_trigger: Dict[str, int] = {}
        pnl_by_trigger: Dict[str, float] = {}
        trades_by_strategy: Dict[str, int] = {}
        pnl_by_strategy: Dict[str, float] = {}

        for t in trades:
            trigger = (self._safe(t, "trigger", "UNKNOWN", default_str="UNKNOWN") or "UNKNOWN").upper()
            trades_by_trigger[trigger] = trades_by_trigger.get(trigger, 0) + 1
            pnl_by_trigger[trigger] = pnl_by_trigger.get(trigger, 0.0) + self._safe(t, "net_pnl")

            symbol = str(self._safe(t, "symbol", "", default_str="") or "").upper()
            opened_at = self._safe_dt(t, "opened_at") or self._safe_dt(t, "closed_at")
            strategy = self._lookup_strategy(signal_lookup, symbol, opened_at) or "unknown"
            trades_by_strategy[strategy] = trades_by_strategy.get(strategy, 0) + 1
            pnl_by_strategy[strategy] = pnl_by_strategy.get(strategy, 0.0) + self._safe(t, "net_pnl")

        grade, score, reasons = self._calculate_grade(
            win_rate=win_rate,
            profit_factor=profit_factor,
            week_return_pct=week_return_pct,
            max_dd=max_dd,
            trade_count=len(trades),
        )

        return WeeklyStats(
            week_start=week_start,
            week_end=week_end,
            quote_currency=self.quote_currency,
            starting_equity=starting_equity,
            ending_equity=ending_equity,
            week_return_amt=week_return_amt,
            week_return_pct=week_return_pct,
            total_trades=len(trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate=win_rate,
            profit_factor=profit_factor,
            best_trade=self._build_trade_dict(best),
            worst_trade=self._build_trade_dict(worst),
            avg_trade_pct=avg_pct,
            max_drawdown_pct=max_dd,
            sharpe_ratio=sharpe,
            trades_by_trigger=trades_by_trigger,
            pnl_by_trigger=pnl_by_trigger,
            trades_by_strategy=trades_by_strategy,
            pnl_by_strategy=pnl_by_strategy,
            grade=grade,
            grade_score=score,
            grade_reasons=reasons,
        )

    def send_telegram(self, stats: WeeklyStats) -> bool:
        """Send a formatted summary via the existing alert system."""
        if self.alerts is None:
            logger.debug("[WeeklyReview] No alert system attached; skipping Telegram")
            return False

        message = self.format_telegram_message(stats)

        try:
            from alerts import AlertLevel

            level = AlertLevel.SUMMARY
        except Exception:
            level = "summary"

        try:
            return bool(self.alerts.send(level, message))
        except Exception as exc:
            logger.error("[WeeklyReview] AlertSystem.send failed: %s", exc, exc_info=True)
            return False

    def save_markdown(self, stats: WeeklyStats) -> Optional[Path]:
        """Render markdown and write to disk, returning the resulting path."""
        date_str = stats.week_end.strftime("%Y-%m-%d")
        rel_path = self.file_path_template.format(date=date_str)
        path = Path(rel_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path

        path.parent.mkdir(parents=True, exist_ok=True)
        content = self.format_markdown(stats)
        path.write_text(content, encoding="utf-8")
        return path

    # ── Formatters ──────────────────────────────────────────────────────

    def format_markdown(self, stats: WeeklyStats) -> str:
        cur = stats.quote_currency
        fmt_amt = lambda v: f"{'+' if v >= 0 else ''}{v:,.2f}"
        fmt_pct = lambda v: f"{'+' if v >= 0 else ''}{v:.2f}%"
        pf_str = self._format_profit_factor(stats.profit_factor)

        lines: List[str] = [
            f"# Weekly Review — {stats.week_start.strftime('%Y-%m-%d')} → {stats.week_end.strftime('%Y-%m-%d')}",
            "",
            "## 📊 Performance Summary",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Starting Portfolio | {stats.starting_equity:,.2f} {cur} |",
            f"| Ending Portfolio   | {stats.ending_equity:,.2f} {cur} |",
            f"| Week Return        | **{fmt_amt(stats.week_return_amt)} {cur} ({fmt_pct(stats.week_return_pct)})** |",
            f"| Total Trades       | {stats.total_trades} |",
            f"| Win Rate           | {stats.win_rate:.1f}% ({stats.winning_trades}W / {stats.losing_trades}L) |",
            f"| Profit Factor      | {pf_str} |",
        ]

        if stats.best_trade:
            bt = stats.best_trade
            lines.append(
                f"| Best Trade         | {bt.get('symbol', '?')} {fmt_pct(bt.get('pnl_pct', 0.0))} ({fmt_amt(bt.get('pnl_amt', 0.0))} {cur}) |"
            )
        if stats.worst_trade:
            wt = stats.worst_trade
            lines.append(
                f"| Worst Trade        | {wt.get('symbol', '?')} {fmt_pct(wt.get('pnl_pct', 0.0))} ({fmt_amt(wt.get('pnl_amt', 0.0))} {cur}) |"
            )

        lines.extend(
            [
                f"| Avg Trade          | {fmt_pct(stats.avg_trade_pct)} |",
                f"| Max Drawdown       | -{stats.max_drawdown_pct:.2f}% |",
                f"| Sharpe Ratio       | {stats.sharpe_ratio:.2f} |",
                "",
            ]
        )

        if stats.trades_by_trigger:
            lines += ["## 🎯 By Exit Trigger", "", "| Trigger | Trades | PnL |", "|---------|--------|-----|"]
            for trig in sorted(stats.trades_by_trigger.keys()):
                n = stats.trades_by_trigger[trig]
                pnl = stats.pnl_by_trigger.get(trig, 0.0)
                lines.append(f"| {trig} | {n} | {fmt_amt(pnl)} {cur} |")
            lines.append("")

        if stats.trades_by_strategy and any(k != "unknown" for k in stats.trades_by_strategy):
            lines += [
                "## 📈 By Strategy (best-effort)",
                "",
                "| Strategy | Trades | PnL |",
                "|----------|--------|-----|",
            ]
            for strat in sorted(stats.trades_by_strategy.keys()):
                n = stats.trades_by_strategy[strat]
                pnl = stats.pnl_by_strategy.get(strat, 0.0)
                lines.append(f"| {strat} | {n} | {fmt_amt(pnl)} {cur} |")
            lines.append("")

        lines += [
            "## 📝 Grade Reasons",
            "",
        ]
        if stats.grade_reasons:
            for reason in stats.grade_reasons:
                lines.append(f"- {reason}")
        else:
            lines.append("- (no reasons recorded)")
        lines.append("")

        lines += [
            f"## Overall Grade: **{stats.grade}** {GRADE_STARS.get(stats.grade, '')}".rstrip(),
            "",
            f"_Score: {stats.grade_score} / 9_  ",
            f"_Period: {stats.week_start.isoformat(timespec='seconds')} → {stats.week_end.isoformat(timespec='seconds')} (UTC)_",
            "",
        ]

        return "\n".join(lines)

    def format_telegram_message(self, stats: WeeklyStats) -> str:
        """HTML-formatted summary for Telegram (matches alerts.py style)."""
        try:
            from alerts import escape_html as _escape_html
        except Exception:
            import html as _html

            def _escape_html(value: Any) -> str:
                return _html.escape(str(value or ""), quote=False)

        cur = _escape_html(stats.quote_currency)
        sign_amt = lambda v: f"{'+' if v >= 0 else ''}{v:,.2f}"
        sign_pct = lambda v: f"{'+' if v >= 0 else ''}{v:.2f}%"
        pf_str = self._format_profit_factor(stats.profit_factor)
        stars = GRADE_STARS.get(stats.grade, "")

        lines: List[str] = [
            "📊 <b>Weekly Review</b>",
            f"<i>{stats.week_start.strftime('%b %d')} – {stats.week_end.strftime('%b %d, %Y')}</i>",
            "─" * 22,
            f"Portfolio: <code>{stats.ending_equity:,.2f}</code> {cur}",
            f"Return: <b>{sign_amt(stats.week_return_amt)} {cur}</b> " f"(<b>{sign_pct(stats.week_return_pct)}</b>)",
            "",
            f"Trades: <code>{stats.total_trades}</code> " f"({stats.winning_trades}W / {stats.losing_trades}L)",
            f"Win Rate: <code>{stats.win_rate:.1f}%</code>",
            f"Profit Factor: <code>{pf_str}</code>",
            f"Max DD: <code>-{stats.max_drawdown_pct:.2f}%</code>",
            f"Sharpe: <code>{stats.sharpe_ratio:.2f}</code>",
        ]

        if stats.best_trade:
            bt = stats.best_trade
            lines.append(
                f"Best: <code>{_escape_html(bt.get('symbol', '?'))}</code> " f"{sign_pct(bt.get('pnl_pct', 0.0))}"
            )
        if stats.worst_trade:
            wt = stats.worst_trade
            lines.append(
                f"Worst: <code>{_escape_html(wt.get('symbol', '?'))}</code> " f"{sign_pct(wt.get('pnl_pct', 0.0))}"
            )

        if stats.trades_by_trigger:
            top_triggers = sorted(stats.trades_by_trigger.items(), key=lambda kv: kv[1], reverse=True)[:3]
            lines.append("")
            lines.append("Triggers: " + ", ".join(f"{_escape_html(k)}×{v}" for k, v in top_triggers))

        lines.append("")
        lines.append(f"<b>Grade: {_escape_html(stats.grade)}</b> {stars}".rstrip())

        return "\n".join(lines)

    # ── Grading ─────────────────────────────────────────────────────────

    @staticmethod
    def _calculate_grade(
        *,
        win_rate: float,
        profit_factor: float,
        week_return_pct: float,
        max_dd: float,
        trade_count: int,
    ) -> Tuple[str, int, List[str]]:
        """
        Grade A-F per Nate Herk's heuristic adapted for crypto.

        Score breakdown (max 9):
            win_rate      ≥65→3, ≥55→2, ≥45→1, else 0
            profit_factor ≥2.0→3, ≥1.5→2, ≥1.2→1, else 0
            week_return   ≥3%→2, ≥1%→1, ≥0%→0, else -1
            max_dd        <3%→1, <8%→0, else -1
        """
        reasons: List[str] = []

        if trade_count < 3:
            reasons.append("ข้อมูลน้อยเกินไป (< 3 trades) — grade not assigned")
            return "N/A", 0, reasons

        score = 0

        if win_rate >= 65:
            score += 3
            reasons.append(f"✅ Win rate {win_rate:.1f}% (excellent)")
        elif win_rate >= 55:
            score += 2
            reasons.append(f"✅ Win rate {win_rate:.1f}% (good)")
        elif win_rate >= 45:
            score += 1
            reasons.append(f"⚠️ Win rate {win_rate:.1f}% (average)")
        else:
            reasons.append(f"❌ Win rate {win_rate:.1f}% (poor)")

        pf_display = "∞" if profit_factor == float("inf") else f"{profit_factor:.2f}"
        if profit_factor >= 2.0:
            score += 3
            reasons.append(f"✅ Profit factor {pf_display} (excellent)")
        elif profit_factor >= 1.5:
            score += 2
            reasons.append(f"✅ Profit factor {pf_display} (good)")
        elif profit_factor >= 1.2:
            score += 1
            reasons.append(f"⚠️ Profit factor {pf_display} (ok)")
        else:
            reasons.append(f"❌ Profit factor {pf_display} (poor)")

        if week_return_pct >= 3:
            score += 2
            reasons.append(f"✅ Week return {week_return_pct:+.2f}% (excellent)")
        elif week_return_pct >= 1:
            score += 1
            reasons.append(f"✅ Week return {week_return_pct:+.2f}% (positive)")
        elif week_return_pct >= 0:
            reasons.append(f"⚠️ Week return {week_return_pct:+.2f}% (breakeven)")
        else:
            score -= 1
            reasons.append(f"❌ Week return {week_return_pct:+.2f}% (negative)")

        if max_dd < 3:
            score += 1
            reasons.append(f"✅ Max drawdown {max_dd:.2f}% (controlled)")
        elif max_dd < 8:
            reasons.append(f"⚠️ Max drawdown {max_dd:.2f}% (acceptable)")
        else:
            score -= 1
            reasons.append(f"❌ Max drawdown {max_dd:.2f}% (high)")

        if score >= 8:
            grade = "A"
        elif score >= 6:
            grade = "B"
        elif score >= 4:
            grade = "C"
        elif score >= 2:
            grade = "D"
        else:
            grade = "F"

        return grade, score, reasons

    # ── Data fetching ───────────────────────────────────────────────────

    def _fetch_closed_trades(self, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        """Fetch closed trades in [start, end) as plain dicts (session-detached)."""
        if self.db is None:
            return []
        try:
            from models import ClosedTrade
        except Exception as exc:
            logger.error("[WeeklyReview] Cannot import ClosedTrade model: %s", exc)
            return []

        try:
            session = self.db.get_session()
        except Exception as exc:
            logger.error("[WeeklyReview] Cannot open DB session: %s", exc)
            return []

        try:
            rows = (
                session.query(ClosedTrade)
                .filter(
                    ClosedTrade.closed_at >= start,
                    ClosedTrade.closed_at < end,
                )
                .order_by(ClosedTrade.closed_at.asc())
                .all()
            )
            return [
                {
                    "id": r.id,
                    "symbol": r.symbol,
                    "side": r.side,
                    "amount": r.amount or 0.0,
                    "entry_price": r.entry_price or 0.0,
                    "exit_price": r.exit_price or 0.0,
                    "entry_cost": r.entry_cost or 0.0,
                    "gross_exit": r.gross_exit or 0.0,
                    "entry_fee": r.entry_fee or 0.0,
                    "exit_fee": r.exit_fee or 0.0,
                    "total_fees": r.total_fees or 0.0,
                    "net_pnl": r.net_pnl or 0.0,
                    "net_pnl_pct": r.net_pnl_pct or 0.0,
                    "trigger": r.trigger or "UNKNOWN",
                    "price_source": r.price_source or "",
                    "opened_at": r.opened_at,
                    "closed_at": r.closed_at,
                }
                for r in rows
            ]
        except Exception as exc:
            logger.error("[WeeklyReview] Failed to query closed_trades: %s", exc, exc_info=True)
            return []
        finally:
            session.close()

    def _fetch_strategy_lookup(self, start: datetime, end: datetime) -> Dict[str, List[Tuple[datetime, str]]]:
        """
        Build {symbol_upper: [(timestamp, strategy), ...]} sorted by timestamp.
        Used for best-effort strategy attribution per closed trade.
        """
        if self.db is None:
            return {}
        try:
            from models import Signal
        except Exception:
            return {}

        # Look slightly before start to catch BUY signals that opened
        # positions which then closed inside the window.
        lookup_from = start - timedelta(days=2)

        try:
            session = self.db.get_session()
        except Exception:
            return {}

        try:
            rows = (
                session.query(Signal)
                .filter(
                    Signal.timestamp >= lookup_from,
                    Signal.timestamp <= end,
                    Signal.strategy.isnot(None),
                    Signal.strategy != "",
                )
                .order_by(Signal.timestamp.asc())
                .all()
            )
            lookup: Dict[str, List[Tuple[datetime, str]]] = {}
            for r in rows:
                pair_up = str(r.pair or "").upper()
                if not pair_up:
                    continue
                lookup.setdefault(pair_up, []).append((r.timestamp, str(r.strategy)))
            return lookup
        except Exception as exc:
            logger.debug("[WeeklyReview] Strategy lookup unavailable: %s", exc)
            return {}
        finally:
            session.close()

    @staticmethod
    def _lookup_strategy(
        lookup: Dict[str, List[Tuple[datetime, str]]],
        symbol: str,
        opened_at: Optional[datetime],
    ) -> Optional[str]:
        """Find the most recent BUY signal for `symbol` at or before `opened_at`."""
        if not lookup or not symbol or not opened_at:
            return None
        rows = lookup.get(symbol.upper())
        if not rows:
            return None
        chosen: Optional[str] = None
        # rows are sorted ascending by timestamp
        for ts, strat in rows:
            if ts is None:
                continue
            if ts <= opened_at:
                chosen = strat
            else:
                break
        return chosen

    def _estimate_current_equity(self) -> float:
        """
        Best-effort current portfolio value used to derive starting equity.
        Order of preference:
            1) portfolio_state.json (PortfolioManager snapshot)
            2) balance_monitor_state.json (BalanceMonitor cache)
            3) initial_balance + cumulative realized PnL from DB
        """
        candidates = [
            (PROJECT_ROOT / "portfolio_state.json", ("total_portfolio_value", "current_balance", "balance")),
            (PROJECT_ROOT / "balance_monitor_state.json", ("total_value_thb", "balance_thb", "balance")),
        ]
        for path, keys in candidates:
            try:
                if not path.exists():
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for key in keys:
                    val = data.get(key) if isinstance(data, dict) else None
                    if isinstance(val, (int, float)) and val > 0:
                        return float(val)
            except Exception as exc:
                logger.debug("[WeeklyReview] Skip equity source %s: %s", path.name, exc)

        cumulative = 0.0
        if self.db is not None:
            try:
                cumulative = float(self.db.get_total_pnl() or 0.0)
            except Exception:
                cumulative = 0.0
        return float(self.initial_balance) + cumulative

    # ── Stat helpers ────────────────────────────────────────────────────

    def _compute_max_drawdown(self, trades: List[Dict[str, Any]], starting_equity: float) -> float:
        """
        Trade-level running drawdown — % retracement from peak equity.
        Not a true intraday portfolio drawdown (we don't snapshot equity)
        but a reasonable proxy when no portfolio history is available.
        """
        if not trades or starting_equity <= 0:
            return 0.0

        equity = float(starting_equity)
        peak = equity
        max_dd_pct = 0.0
        for t in sorted(trades, key=lambda x: self._safe_dt(x, "closed_at") or datetime.min):
            equity += self._safe(t, "net_pnl")
            peak = max(peak, equity)
            if peak > 0:
                dd_pct = (peak - equity) / peak * 100.0
                if dd_pct > max_dd_pct:
                    max_dd_pct = dd_pct
        return max_dd_pct

    def _compute_sharpe(self, trades: List[Dict[str, Any]], starting_equity: float) -> float:
        """
        Daily-bucket Sharpe annualized × √365 (crypto trades 7 days/week).
        Returns 0.0 with fewer than 2 active days or zero variance.
        """
        if len(trades) < 2 or starting_equity <= 0:
            return 0.0

        daily_pnl: Dict[Any, float] = defaultdict(float)
        for t in trades:
            dt = self._safe_dt(t, "closed_at")
            if dt is None:
                continue
            daily_pnl[dt.date()] += self._safe(t, "net_pnl", 0.0)

        if len(daily_pnl) < 2:
            return 0.0

        daily_returns = [v / starting_equity for v in daily_pnl.values()]
        try:
            mean_r = statistics.mean(daily_returns)
            std_r = statistics.stdev(daily_returns)
        except statistics.StatisticsError:
            return 0.0

        if std_r == 0:
            return 0.0
        return (mean_r / std_r) * (365**0.5)

    @staticmethod
    def _format_profit_factor(pf: float) -> str:
        if pf == float("inf"):
            return "∞ (no losses)"
        if pf <= 0:
            return "0.00"
        return f"{pf:.2f}"

    def _build_trade_dict(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a raw trade row into the {symbol, pnl_pct, pnl_amt} shape."""
        if not trade:
            return {}
        return {
            "symbol": str(self._safe(trade, "symbol", "", default_str="") or "?"),
            "pnl_pct": float(self._safe(trade, "net_pnl_pct", 0.0)),
            "pnl_amt": float(self._safe(trade, "net_pnl", 0.0)),
            "trigger": str(self._safe(trade, "trigger", "", default_str="") or ""),
            "closed_at": self._safe_dt(trade, "closed_at"),
        }

    @staticmethod
    def _safe(obj: Any, key: str, default: Any = 0.0, *, default_str: Optional[str] = None) -> Any:
        """Read attribute or dict key, treating None as default."""
        if obj is None:
            return default if default_str is None else default_str
        if isinstance(obj, dict):
            val = obj.get(key, default)
        else:
            val = getattr(obj, key, default)
        if val is None:
            return default if default_str is None else default_str
        return val

    @staticmethod
    def _safe_dt(obj: Any, key: str) -> Optional[datetime]:
        """Coerce a datetime-like field to a naive datetime in UTC."""
        if obj is None:
            return None
        if isinstance(obj, dict):
            val = obj.get(key)
        else:
            val = getattr(obj, key, None)
        if val is None:
            return None
        if isinstance(val, datetime):
            if val.tzinfo is not None:
                return val.astimezone(timezone.utc).replace(tzinfo=None)
            return val
        try:
            return datetime.fromisoformat(str(val))
        except Exception:
            return None


# ────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ────────────────────────────────────────────────────────────────────────────


def _load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        logger.warning("[WeeklyReview] Config not found at %s; using defaults", path)
        return {}
    try:
        import yaml
    except ImportError:
        logger.error("[WeeklyReview] PyYAML not installed — cannot load %s", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.error("[WeeklyReview] Failed to load config %s: %s", path, exc)
        return {}


def _parse_date(raw: str) -> datetime:
    """Accept YYYY-MM-DD or full ISO string; treat naive as UTC."""
    raw = raw.strip()
    try:
        if "T" in raw:
            dt = datetime.fromisoformat(raw)
        else:
            dt = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(f"Invalid date '{raw}': {exc}") from exc
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _write_stdout_safe(text: str) -> None:
    """
    Write `text` to stdout, tolerating Windows consoles whose default
    code page (cp1252) cannot represent emoji / arrow characters.
    """
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    except UnicodeEncodeError:
        pass

    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        encoded = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        sys.stdout.write(encoded)
        sys.stdout.flush()
    except Exception:
        try:
            buffer = getattr(sys.stdout, "buffer", None)
            if buffer is not None:
                buffer.write(text.encode("utf-8", errors="replace"))
                buffer.flush()
                return
        except Exception:
            pass
        sys.stdout.write(text.encode("ascii", errors="replace").decode("ascii"))
        sys.stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="weekly_review",
        description="Crypto bot weekly performance review (SPEC_08B).",
    )
    parser.add_argument(
        "--manual", action="store_true", help="Run review for the trailing 7 days (default if --start/--end omitted)"
    )
    parser.add_argument("--start", type=str, default=None, help="Period start (YYYY-MM-DD). UTC.")
    parser.add_argument("--end", type=str, default=None, help="Period end (YYYY-MM-DD or ISO). UTC.")
    parser.add_argument("--config", type=str, default=str(DEFAULT_CONFIG_PATH), help="Path to bot_config.yaml")
    parser.add_argument("--db", type=str, default=None, help="Override SQLite database path")
    parser.add_argument("--no-send", action="store_true", help="Do not send Telegram summary")
    parser.add_argument("--no-save", action="store_true", help="Do not write the markdown report to disk")
    parser.add_argument("--quiet", action="store_true", help="Suppress markdown stdout preview")
    parser.add_argument("--log-level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = _load_config(Path(args.config))

    try:
        from database import get_database
    except Exception as exc:
        logger.error("Cannot import database module: %s", exc)
        return 2
    db = get_database(args.db)

    alerts_obj = None
    if not args.no_send:
        try:
            from alerts import AlertSystem

            alerts_obj = AlertSystem()
        except Exception as exc:
            logger.warning("[WeeklyReview] AlertSystem unavailable (%s); Telegram disabled", exc)

    reviewer = WeeklyReviewer(db, config, alerts_obj)

    if args.start or args.end:
        if not (args.start and args.end):
            parser.error("--start and --end must be provided together")
        week_start = _parse_date(args.start)
        week_end = _parse_date(args.end)
    else:
        week_end = datetime.now(timezone.utc)
        week_start = week_end - timedelta(days=7)

    if week_end <= week_start:
        parser.error("--end must be later than --start")

    logger.info(
        "[WeeklyReview] Period %s -> %s (UTC)",
        week_start.isoformat(timespec="seconds"),
        week_end.isoformat(timespec="seconds"),
    )

    stats = reviewer.run_review(
        week_start=week_start,
        week_end=week_end,
        send_telegram=not args.no_send,
        save_to_file=not args.no_save,
    )

    if not args.quiet:
        _write_stdout_safe(reviewer.format_markdown(stats) + "\n")

    summary = (
        f"[WeeklyReview] grade={stats.grade} score={stats.grade_score}/9 "
        f"return={stats.week_return_pct:+.2f}% trades={stats.total_trades} "
        f"win_rate={stats.win_rate:.1f}% pf={reviewer._format_profit_factor(stats.profit_factor)} "
        f"max_dd={stats.max_drawdown_pct:.2f}%"
    )
    logger.info(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
