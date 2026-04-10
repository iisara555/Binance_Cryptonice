"""
Deep Review Module for Crypto Trading Bot
==========================================
Comprehensive analysis across signals, trades, portfolio, risk, and system health.
"""

import json
import logging
import os
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("crypto-bot.deep_review")

DEFAULT_DB_PATH = "crypto_bot.db"
REVIEW_PERIOD_DAYS = 30


class HealthStatus(Enum):
    EXCELLENT = "excellent"
    GOOD = "good"
    WARNING = "warning"
    CRITICAL = "critical"


# ─────────────────────────────────────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────────────────────────────────────

def get_db_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _get_net_pnl(trade: Dict) -> float:
    return trade.get('net_pnl', trade.get('pnl', 0))


def _get_net_pnl_pct(trade: Dict) -> float:
    return trade.get('net_pnl_pct', trade.get('pnl_pct', 0))


def _get_opened_at(pos: Dict) -> datetime:
    ts = pos.get('opened_at', '')
    try:
        return datetime.fromisoformat(ts) if ts else datetime.now()
    except (ValueError, TypeError):
        return datetime.now()


def calc_sharpe(returns: List[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean, std = statistics.mean(returns), statistics.stdev(returns)
    return (mean / std) if std else 0.0


def calc_sortino(returns: List[float]) -> float:
    if not returns:
        return 0.0
    downside = [r for r in returns if r < 0]
    if not downside:
        return 0.0
    mean, std = statistics.mean(returns), statistics.stdev(downside)
    return (mean / std) if std else 0.0


def calc_max_drawdown(equity: List[float]) -> float:
    if not equity:
        return 0.0
    peak, max_dd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak if peak else 0)
    return max_dd * 100


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MetricScore:
    name: str
    value: float
    min_value: float
    max_value: float
    unit: str = ""
    weight: float = 1.0


@dataclass
class ReviewReport:
    timestamp: datetime
    period_start: datetime
    period_end: datetime
    signal_score: float = 0.0
    trade_score: float = 0.0
    portfolio_score: float = 0.0
    risk_score: float = 0.0
    system_score: float = 0.0
    overall_score: float = 0.0
    overall_status: HealthStatus = HealthStatus.GOOD
    metrics: Dict[str, MetricScore] = field(default_factory=dict)
    issues: List[Dict] = field(default_factory=list)
    recommendations: List[Dict] = field(default_factory=list)
    total_signals: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "period": {
                "start": self.period_start.isoformat(),
                "end": self.period_end.isoformat(),
                "days": (self.period_end - self.period_start).days
            },
            "overall_score": round(self.overall_score, 2),
            "overall_status": self.overall_status.value,
            "category_scores": {
                k: round(v, 2) for k, v in {
                    "signals": self.signal_score,
                    "trades": self.trade_score,
                    "portfolio": self.portfolio_score,
                    "risk": self.risk_score,
                    "system": self.system_score,
                }.items()
            },
            "metrics": {n: {"value": m.value, "unit": m.unit} for n, m in self.metrics.items()},
            "issues": self.issues,
            "recommendations": self.recommendations,
            "summary": {
                "total_signals": self.total_signals,
                "total_trades": self.total_trades,
                "winning_trades": self.winning_trades,
                "losing_trades": self.losing_trades,
                "win_rate": round(self.winning_trades / self.total_trades * 100, 2) if self.total_trades else 0,
            }
        }


# ─────────────────────────────────────────────────────────────────────────────
# Reviewer Classes
# ─────────────────────────────────────────────────────────────────────────────

class SignalReviewer:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path

    def get_signals(self, days: int = REVIEW_PERIOD_DAYS) -> List[Dict]:
        conn = get_db_connection(self.db_path)
        cur = conn.cursor()
        since = (datetime.now() - timedelta(days=days)).isoformat()
        cur.execute("SELECT * FROM signals WHERE timestamp >= ? ORDER BY timestamp DESC", (since,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def analyze(self, signals: List[Dict]) -> Dict:
        if not signals:
            return {"total": 0, "avg_conf": 0.0, "high_conf_rate": 0.0}
        
        confs = [s.get('confidence', 0) for s in signals]
        high_conf = sum(1 for c in confs if c >= 0.75)
        
        return {
            "total": len(signals),
            "avg_conf": statistics.mean(confs) if confs else 0.0,
            "high_conf_rate": high_conf / len(signals),
        }

    def score(self, signals: List[Dict]) -> float:
        if not signals:
            return 50.0
        a = self.analyze(signals)
        score = 100.0 - max(0, (0.5 - a["avg_conf"]) * 50) - max(0, (0.3 - a["high_conf_rate"]) * 40)
        return max(0.0, min(100.0, score))


class TradeReviewer:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path

    def get_trades(self, days: int = REVIEW_PERIOD_DAYS) -> List[Dict]:
        conn = get_db_connection(self.db_path)
        cur = conn.cursor()
        since = (datetime.now() - timedelta(days=days)).isoformat()
        cur.execute("SELECT * FROM closed_trades WHERE closed_at >= ? ORDER BY closed_at DESC", (since,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def analyze(self, trades: List[Dict]) -> Dict:
        if not trades:
            return {"total": 0, "win_rate": 0.0, "avg_pnl": 0.0, "avg_pnl_pct": 0.0, "total_pnl": 0.0}
        
        pnls = [_get_net_pnl(t) for t in trades]
        pnl_pcts = [_get_net_pnl_pct(t) for t in trades]
        winning = [p for p in pnls if p > 0]
        
        return {
            "total": len(trades),
            "winning": len(winning),
            "losing": len(trades) - len(winning),
            "win_rate": len(winning) / len(trades) * 100,
            "avg_pnl": statistics.mean(pnls),
            "avg_pnl_pct": statistics.mean(pnl_pcts),
            "total_pnl": sum(pnls),
        }

    def advanced_metrics(self, trades: List[Dict]) -> Dict:
        if not trades:
            return {"sharpe": 0.0, "sortino": 0.0, "max_dd": 0.0, "profit_factor": 0.0}
        
        equity = [0.0]
        for t in trades:
            equity.append(equity[-1] + _get_net_pnl(t))
        
        returns = [_get_net_pnl_pct(t) / 100 for t in trades if _get_net_pnl_pct(t)]
        wins = [_get_net_pnl(t) for t in trades if _get_net_pnl(t) > 0]
        losses = [abs(_get_net_pnl(t)) for t in trades if _get_net_pnl(t) < 0]
        
        return {
            "sharpe": calc_sharpe(returns),
            "sortino": calc_sortino(returns),
            "max_dd": calc_max_drawdown(equity),
            "profit_factor": sum(wins) / sum(losses) if losses else 0.0,
        }

    def score(self, trades: List[Dict]) -> float:
        a = self.analyze(trades)
        if not a["total"]:
            return 50.0
        
        score = 100.0
        if a["win_rate"] < 40:
            score -= (40 - a["win_rate"]) * 1.5
        elif a["win_rate"] < 50:
            score -= (50 - a["win_rate"]) * 0.5
        elif a["win_rate"] >= 60:
            score += 5
        
        if a["avg_pnl"] < 0:
            score -= abs(a["avg_pnl"]) * 2
        
        return max(0.0, min(100.0, score))


class PortfolioReviewer:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path

    def get_positions(self) -> List[Dict]:
        conn = get_db_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT * FROM positions ORDER BY opened_at DESC")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def analyze(self, positions: List[Dict]) -> Dict:
        if not positions:
            return {"total": 0, "total_exposure": 0.0, "oldest_age": 0}
        
        exposure = sum(p.get('remaining_amount', 0) * p.get('entry_price', 0) for p in positions)
        ages = [_get_opened_at(p) for p in positions]
        
        return {
            "total": len(positions),
            "total_exposure": exposure,
            "oldest_age": max((datetime.now() - a).total_seconds() for a in ages) if ages else 0,
        }

    def score(self, positions: List[Dict]) -> float:
        a = self.analyze(positions)
        score = 100.0
        
        if a["total"] > 5:
            score -= (a["total"] - 5) * 3
        elif a["total"] == 0:
            score -= 5
        
        if a["oldest_age"] > 86400 * 7:
            score -= 15
        elif a["oldest_age"] > 86400 * 3:
            score -= 8
        
        return max(0.0, min(100.0, score))


class RiskReviewer:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path

    def get_trades(self, days: int = REVIEW_PERIOD_DAYS) -> List[Dict]:
        conn = get_db_connection(self.db_path)
        cur = conn.cursor()
        since = (datetime.now() - timedelta(days=days)).isoformat()
        cur.execute("SELECT * FROM closed_trades WHERE closed_at >= ? ORDER BY closed_at DESC", (since,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def analyze(self, trades: List[Dict]) -> Dict:
        if not trades:
            return {"total": 0, "sl_rate": 0.0, "tp_rate": 0.0, "tp_sl_ratio": 0.0}
        
        sl = sum(1 for t in trades if t.get('trigger') == 'stop_loss')
        tp = sum(1 for t in trades if t.get('trigger') == 'take_profit')
        total = len(trades)
        
        return {
            "total": total,
            "sl_hits": sl,
            "tp_hits": tp,
            "sl_rate": sl / total * 100,
            "tp_rate": tp / total * 100,
            "tp_sl_ratio": tp / sl if sl > 0 else float('inf'),
        }

    def score(self, trades: List[Dict]) -> float:
        a = self.analyze(trades)
        score = 100.0
        
        if a["sl_rate"] > 50:
            score -= (a["sl_rate"] - 50) * 2
        elif a["sl_rate"] < 30:
            score += 5
        
        tp_sl = a["tp_sl_ratio"]
        if tp_sl < 1:
            score -= 15
        elif tp_sl >= 2:
            score += 10
        
        return max(0.0, min(100.0, score))


class SystemReviewer:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path

    def metrics(self, days: int = REVIEW_PERIOD_DAYS) -> Dict:
        conn = get_db_connection(self.db_path)
        cur = conn.cursor()
        since = (datetime.now() - timedelta(days=days)).isoformat()
        
        cur.execute("SELECT COUNT(*) FROM signals WHERE timestamp >= ?", (since,))
        signals = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM closed_trades WHERE closed_at >= ?", (since,))
        trades = cur.fetchone()[0]
        
        try:
            cur.execute("SELECT COUNT(*) FROM orders WHERE created_at >= ? AND status = 'error'", (since,))
            errors = cur.fetchone()[0]
        except sqlite3.OperationalError:
            errors = 0
        
        conn.close()
        return {"signals": signals, "trades": trades, "errors": errors, "error_rate": errors / signals * 100 if signals else 0}

    def score(self, days: int = REVIEW_PERIOD_DAYS) -> float:
        m = self.metrics(days)
        score = 100.0
        
        if m["error_rate"] > 5:
            score -= 30
        elif m["error_rate"] > 2:
            score -= 15
        
        if m["signals"] < 10:
            score -= 10
        
        if os.path.exists(self.db_path):
            db_mb = os.path.getsize(self.db_path) / (1024 * 1024)
            if db_mb > 500:
                score -= 10
        
        return max(0.0, min(100.0, score))


# ─────────────────────────────────────────────────────────────────────────────
# Deep Review Engine
# ─────────────────────────────────────────────────────────────────────────────

class DeepReviewEngine:
    def __init__(self, db_path: str = DEFAULT_DB_PATH, days: int = REVIEW_PERIOD_DAYS, config: Dict = None):
        self.db_path = db_path
        self.days = days
        self.config = config or {}
        self.signals = SignalReviewer(db_path)
        self.trades = TradeReviewer(db_path)
        self.portfolio = PortfolioReviewer(db_path)
        self.risk = RiskReviewer(db_path)
        self.system = SystemReviewer(db_path)
        logger.info(f"DeepReviewEngine initialized | DB: {db_path} | Period: {days} days")

    def generate_report(self) -> ReviewReport:
        now = datetime.now()
        period_start = now - timedelta(days=self.days)
        
        sig_data = self.signals.get_signals(self.days)
        trade_data = self.trades.get_trades(self.days)
        pos_data = self.portfolio.get_positions()
        
        sig_analysis = self.signals.analyze(sig_data)
        trade_analysis = self.trades.analyze(trade_data)
        risk_analysis = self.risk.analyze(trade_data)
        sys_metrics = self.system.metrics(self.days)
        adv_metrics = self.trades.advanced_metrics(trade_data)
        
        report = ReviewReport(
            timestamp=now,
            period_start=period_start,
            period_end=now,
            signal_score=self.signals.score(sig_data),
            trade_score=self.trades.score(trade_data),
            portfolio_score=self.portfolio.score(pos_data),
            risk_score=self.risk.score(trade_data),
            system_score=self.system.score(self.days),
            total_signals=len(sig_data),
            total_trades=trade_analysis["total"],
            winning_trades=trade_analysis.get("winning", 0),
            losing_trades=trade_analysis.get("losing", 0),
        )
        
        # Weighted overall score
        weights = {"signal": 0.15, "trade": 0.30, "portfolio": 0.20, "risk": 0.25, "system": 0.10}
        report.overall_score = (
            report.signal_score * weights["signal"] +
            report.trade_score * weights["trade"] +
            report.portfolio_score * weights["portfolio"] +
            report.risk_score * weights["risk"] +
            report.system_score * weights["system"]
        )
        
        # Status
        if report.overall_score >= 80:
            report.overall_status = HealthStatus.EXCELLENT
        elif report.overall_score >= 65:
            report.overall_status = HealthStatus.GOOD
        elif report.overall_score >= 50:
            report.overall_status = HealthStatus.WARNING
        else:
            report.overall_status = HealthStatus.CRITICAL
        
        # Metrics
        report.metrics = {
            "total_signals": MetricScore("Total Signals", len(sig_data), 100, float('inf'), "signals"),
            "avg_confidence": MetricScore("Avg Confidence", sig_analysis["avg_conf"] * 100, 50, 100, "%"),
            "win_rate": MetricScore("Win Rate", trade_analysis["win_rate"], 50, 100, "%"),
            "profit_factor": MetricScore("Profit Factor", adv_metrics["profit_factor"], 1.5, float('inf'), "ratio"),
            "sharpe": MetricScore("Sharpe Ratio", adv_metrics["sharpe"], 1.0, float('inf'), "ratio"),
            "max_drawdown": MetricScore("Max Drawdown", adv_metrics["max_dd"], 0, 20, "%"),
            "sl_hit_rate": MetricScore("SL Hit Rate", risk_analysis["sl_rate"], 0, 40, "%"),
            "open_positions": MetricScore("Open Positions", len(pos_data), 0, 5, "positions"),
            "error_rate": MetricScore("Error Rate", sys_metrics["error_rate"], 0, 2, "%"),
        }
        
        # Issues
        report.issues = []
        if trade_analysis["win_rate"] < 40:
            report.issues.append({"severity": "high", "title": "Low Win Rate", "desc": f"{trade_analysis['win_rate']:.1f}% (target: >50%)"})
        if risk_analysis["sl_rate"] > 50:
            report.issues.append({"severity": "high", "title": "Excessive SL Hits", "desc": f"{risk_analysis['sl_rate']:.1f}% (target: <40%)"})
        if sig_analysis["avg_conf"] < 0.5:
            report.issues.append({"severity": "medium", "title": "Low Signal Confidence", "desc": f"{sig_analysis['avg_conf']*100:.1f}%"})
        if not trade_analysis["total"]:
            report.issues.append({"severity": "medium", "title": "No Trades Executed", "desc": "Check signal quality or risk rules"})
        
        # Recommendations
        report.recommendations = []
        if trade_analysis["win_rate"] < 50:
            report.recommendations.append({"priority": "high", "title": "Improve Win Rate", "action": "Adjust entry criteria or reduce position size"})
        if sig_analysis["avg_conf"] < 0.6:
            report.recommendations.append({"priority": "medium", "title": "Strengthen Signals", "action": "Increase min_confidence threshold"})
        if risk_analysis["sl_rate"] > 40:
            report.recommendations.append({"priority": "high", "title": "Widen Stop Loss", "action": "Increase ATR multiplier for SL"})
        if adv_metrics["profit_factor"] < 1.5:
            report.recommendations.append({"priority": "medium", "title": "Improve Risk/Reward", "action": "Review TP levels or use trailing stops"})
        
        logger.info(f"Deep Review Complete | Score: {report.overall_score:.1f} | Status: {report.overall_status.value}")
        return report

    def save_report(self, report: ReviewReport, filepath: str = None) -> str:
        if not filepath:
            filepath = f"deep_review_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filepath, 'w') as f:
            json.dump(report.to_dict(), f, indent=2)
        return filepath

    def print_summary(self, report: ReviewReport):
        print("\n" + "=" * 60)
        print("DEEP REVIEW SUMMARY")
        print("=" * 60)
        print(f"Period: {report.period_start.strftime('%Y-%m-%d')} to {report.period_end.strftime('%Y-%m-%d')}")
        print(f"Overall Score: {report.overall_score:.1f}/100 [{report.overall_status.value.upper()}]")
        print()
        print("Category Scores:")
        print(f"  Signals:   {report.signal_score:.1f}/100")
        print(f"  Trades:    {report.trade_score:.1f}/100")
        print(f"  Portfolio: {report.portfolio_score:.1f}/100")
        print(f"  Risk:      {report.risk_score:.1f}/100")
        print(f"  System:    {report.system_score:.1f}/100")
        print()
        print(f"Trading Summary:")
        print(f"  Total Signals: {report.total_signals}")
        print(f"  Total Trades:  {report.total_trades}")
        if report.total_trades:
            print(f"  Win Rate:       {report.winning_trades}/{report.total_trades} ({report.winning_trades/report.total_trades*100:.1f}%)")
        
        if report.issues:
            print(f"\nIssues ({len(report.issues)}):")
            for i in report.issues:
                print(f"  [{i['severity'].upper()}] {i['title']}: {i['desc']}")
        
        if report.recommendations:
            print(f"\nRecommendations ({len(report.recommendations)}):")
            for r in report.recommendations:
                print(f"  [{r['priority'].upper()}] {r['title']}: {r['action']}")
        
        print("=" * 60)


def quick_health_check(db_path: str = DEFAULT_DB_PATH) -> Dict:
    health = {"status": "unknown", "timestamp": datetime.now().isoformat(), "checks": {}}
    try:
        conn = get_db_connection(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM signals")
        signals = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM positions")
        positions = cur.fetchone()[0]
        conn.close()
        health["checks"]["database"] = {"status": "ok", "signals": signals, "positions": positions}
        health["status"] = "operational" if signals > 0 else "no_data"
    except Exception as e:
        health["status"] = "error"
        health["checks"]["database"] = {"status": "error", "error": str(e)}
    return health


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Deep Review - Bot analysis")
    parser.add_argument("--days", type=int, default=30, help="Review period (days)")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="Database path")
    parser.add_argument("--output", type=str, help="Output file path")
    parser.add_argument("--quick", action="store_true", help="Quick health check")
    args = parser.parse_args()
    
    if args.quick:
        print(json.dumps(quick_health_check(args.db), indent=2))
    else:
        engine = DeepReviewEngine(db_path=args.db, days=args.days)
        report = engine.generate_report()
        engine.print_summary(report)
        if args.output:
            engine.save_report(report, args.output)
