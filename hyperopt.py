"""
Hyperopt — Parameter Optimization (SPEC_07)
============================================

Grid-search + walk-forward parameter optimisation for the Crypto_Sniper bot.
Inspired by Freqtrade's hyperopt module, but simplified to avoid the cost and
complexity of full Bayesian optimisation.

Workflow
--------
1. Load historical candles from the bot's SQLite database.
2. For each parameter combination in ``PARAM_SPACE`` (per mode), run a
   vectorised backtest on each walk-forward window.
3. Average each combination's metrics across windows and compute a composite
   ``score`` (Sharpe 40% / Win-Rate 30% / Max-DD 20% / Profit-Factor 10%).
4. Sort by score, return the top-N, and optionally write the best parameters
   back into ``bot_config.yaml`` (with a timestamped backup).
5. Save full ranked output to ``hyperopt_results/{symbol}_{mode}_{date}.json``.

Notes
-----
* This module re-implements the backtest loop locally instead of calling
  ``BacktestingValidator._run_backtest`` so we can vectorise the indicator
  computation (one pass per parameter set) and stay independent from any
  refactor of that file (cursorrules: DO NOT TOUCH backtesting_validation.py).
* SL / TP follow the bot's convention: ATR-aware stop with a percentage floor,
  and a fixed-percentage take-profit. Round-trip fees (Binance.th 0.2%) are
  applied to every trade so optimised parameters are realistic.

CLI usage
---------
    python hyperopt.py --symbol BTCUSDT --mode scalping \\
        --start 2024-01-01 --end 2024-12-31 --splits 5 --top 5

    python hyperopt.py --all --start 2024-01-01 --end 2024-12-31
"""

# --- NEW: SPEC_07 Hyperopt ---

from __future__ import annotations

import argparse
import itertools
import json
import logging
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT: Path = Path(__file__).parent
DEFAULT_CONFIG_PATH: Path = PROJECT_ROOT / "bot_config.yaml"
DEFAULT_RESULTS_DIR: Path = PROJECT_ROOT / "hyperopt_results"

BINANCE_TH_ROUND_TRIP_FEE: float = 0.002  # 0.1% × 2 sides
DEFAULT_INITIAL_CAPITAL: float = 10_000.0
DEFAULT_TIMEFRAME: str = "15m"
DEFAULT_ATR_PERIOD: int = 14
MIN_CANDLES_REQUIRED: int = 500
MIN_TRADES_FOR_SCORE: int = 10


# Parameter grids per mode. Loosely matches the SPEC_07 brief; tweak here to
# expand/contract the search space without changing any other code.
PARAM_SPACE: Dict[str, Dict[str, List[Any]]] = {
    "scalping": {
        "fast_ema": [5, 7, 9, 12],
        "slow_ema": [17, 21, 26, 34],
        "rsi_period": [7, 9, 14],
        "rsi_oversold": [28, 30, 34, 38],
        "rsi_overbought": [62, 66, 70, 72],
        "stop_loss_pct": [0.8, 1.0, 1.2, 1.5],
        "take_profit_pct": [2.0, 2.5, 3.0, 3.5],
        "atr_multiplier": [1.0, 1.2, 1.5, 1.8],
    },
    "trend_only": {
        "fast_ema": [9, 12, 15, 20],
        "slow_ema": [26, 34, 50, 65],
        "stop_loss_pct": [3.0, 4.0, 4.5, 5.0],
        "take_profit_pct": [8.0, 10.0, 12.0, 15.0],
        "atr_multiplier": [1.5, 1.8, 2.0, 2.5],
    },
}


@dataclass
class OptimizationResult:
    """Final ranked output for a single parameter combination."""

    params: Dict[str, Any]

    sharpe_ratio: float
    win_rate: float  # 0.0–1.0 fraction
    total_return: float  # percentage (e.g. 12.5 = +12.5%)

    max_drawdown: float  # percentage (e.g. 7.2 = 7.2% peak-to-trough)
    profit_factor: float
    total_trades: int
    avg_trade_pct: float  # mean per-trade net return (percentage)

    score: float

    splits_evaluated: int = 0
    bars_evaluated: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Indicator helpers (vectorised — one pass per parameter set)
# ---------------------------------------------------------------------------


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=max(int(span), 1), adjust=False).mean()


def _rsi(series: pd.Series, period: int) -> pd.Series:
    period = max(int(period), 2)
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0).clip(0.0, 100.0)


def _atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = DEFAULT_ATR_PERIOD,
) -> pd.Series:
    period = max(int(period), 2)
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _bollinger(
    series: pd.Series,
    period: int = 20,
    std_dev: float = 2.0,
) -> Tuple[pd.Series, pd.Series]:
    rolling_mean = series.rolling(window=period).mean()
    rolling_std = series.rolling(window=period).std(ddof=0)
    upper = rolling_mean + (rolling_std * std_dev)
    lower = rolling_mean - (rolling_std * std_dev)
    return upper, lower


# ---------------------------------------------------------------------------
# HyperoptRunner
# ---------------------------------------------------------------------------


class HyperoptRunner:
    """Grid-search + walk-forward optimiser over ``PARAM_SPACE``.

    Args:
        db: A ``database.Database``-compatible object exposing
            ``get_candles(symbol, interval, start_time, end_time)``. Optional
            in unit-test contexts; required to actually run an optimisation.
        config: Optional parsed ``bot_config.yaml``; only used for CLI defaults
            (initial capital, fee override, timeframe).
    """

    def __init__(
        self,
        db: Any = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.db = db
        self.config: Dict[str, Any] = dict(config or {})

        hyperopt_cfg = self.config.get("hyperopt", {}) or {}
        self.timeframe: str = str(hyperopt_cfg.get("timeframe", DEFAULT_TIMEFRAME))
        self.fee_pct: float = float(hyperopt_cfg.get("fee_pct", BINANCE_TH_ROUND_TRIP_FEE))
        self.initial_capital: float = float(hyperopt_cfg.get("initial_capital", DEFAULT_INITIAL_CAPITAL))
        self.atr_period: int = int(hyperopt_cfg.get("atr_period", DEFAULT_ATR_PERIOD))

    # ────────────────────────── Public API ──────────────────────────

    def run(
        self,
        symbol: str,
        mode: str,
        start_date: str,
        end_date: str,
        n_splits: int = 5,
        top_n: int = 5,
        progress_every: int = 50,
    ) -> List[OptimizationResult]:
        """Optimise one (symbol, mode) pair.

        Returns the top-N combinations sorted by composite score (descending).
        """
        param_grid = PARAM_SPACE.get(mode)
        if not param_grid:
            logger.error("[Hyperopt] Unknown mode '%s' — supported: %s", mode, sorted(PARAM_SPACE.keys()))
            return []

        combinations = self._generate_combinations(param_grid)
        if not combinations:
            logger.error("[Hyperopt] No valid parameter combinations for mode %s", mode)
            return []

        # Pre-load candles once; walk-forward slices it n_splits ways.
        candles = self._load_candles(symbol, self.timeframe, start_date, end_date)
        if candles is None or len(candles) < MIN_CANDLES_REQUIRED:
            logger.error(
                "[Hyperopt] Insufficient candles for %s %s (have %d, need %d) — "
                "did the data collector backfill this range?",
                symbol,
                self.timeframe,
                0 if candles is None else len(candles),
                MIN_CANDLES_REQUIRED,
            )
            return []

        logger.info(
            "[Hyperopt] %s/%s: testing %d combinations × %d splits " "(%d candles, %s → %s)",
            symbol,
            mode,
            len(combinations),
            n_splits,
            len(candles),
            start_date,
            end_date,
        )

        started_at = time.time()
        results: List[OptimizationResult] = []

        for i, params in enumerate(combinations):
            if progress_every > 0 and i and (i % progress_every == 0):
                elapsed = time.time() - started_at
                pct = (i / len(combinations)) * 100.0
                logger.info(
                    "[Hyperopt] Progress: %d/%d (%.1f%%) — %.1fs elapsed",
                    i,
                    len(combinations),
                    pct,
                    elapsed,
                )

            wf_scores = self._walk_forward(candles, mode, params, n_splits)
            if not wf_scores:
                continue

            avg_result = self._average_results(params, wf_scores)
            if avg_result is not None:
                results.append(avg_result)

        elapsed_total = time.time() - started_at
        logger.info(
            "[Hyperopt] Completed %d/%d combinations in %.1fs (%.2fs avg)",
            len(results),
            len(combinations),
            elapsed_total,
            elapsed_total / max(len(combinations), 1),
        )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[: max(int(top_n), 1)]

    def apply_best_params(
        self,
        result: OptimizationResult,
        mode: str,
        config_path: Optional[Path] = None,
    ) -> Tuple[Path, Path]:
        """Write the winning parameters into ``bot_config.yaml``.

        A timestamped backup is written next to the original file before any
        changes are persisted. Returns ``(updated_path, backup_path)``.
        """
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("PyYAML is required to apply hyperopt results to bot_config.yaml") from exc

        target = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        if not target.exists():
            raise FileNotFoundError(f"Config not found at {target}")

        backup = target.with_name(f"{target.stem}.backup.{datetime.now():%Y%m%d_%H%M%S}{target.suffix}")
        shutil.copy(target, backup)
        logger.info("[Hyperopt] Backup saved → %s", backup)

        with open(target, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        strategy_cfg = cfg.setdefault("strategy_mode", {}).setdefault(mode, {})
        for key, value in result.params.items():
            strategy_cfg[key] = value

        # Keep the parallel mode_indicator_profiles section in sync where it
        # already exists — only known keys are mirrored to avoid surprising
        # the SignalGenerator with foreign fields.
        profile_cfg = cfg.get("mode_indicator_profiles", {}).get(mode)
        if isinstance(profile_cfg, dict):
            for key in ("stop_loss_pct", "take_profit_pct", "atr_multiplier"):
                if key in result.params:
                    profile_cfg[key] = result.params[key]

        with open(target, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        logger.info("[Hyperopt] Applied best params for %s → %s", mode, target)
        return target, backup

    def save_results_json(
        self,
        symbol: str,
        mode: str,
        results: List[OptimizationResult],
        start_date: str,
        end_date: str,
        results_dir: Optional[Path] = None,
    ) -> Path:
        """Dump the ranked results to ``hyperopt_results/{symbol}_{mode}_{date}.json``."""
        out_dir = Path(results_dir) if results_dir else DEFAULT_RESULTS_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{symbol.upper()}_{mode}_{stamp}.json"
        path = out_dir / filename

        payload = {
            "symbol": symbol.upper(),
            "mode": mode,
            "timeframe": self.timeframe,
            "start_date": start_date,
            "end_date": end_date,
            "fee_pct": self.fee_pct,
            "initial_capital": self.initial_capital,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
            "results": [r.to_dict() for r in results],
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)

        logger.info("[Hyperopt] Results saved → %s", path)
        return path

    # ────────────────────────── Internals ──────────────────────────

    @staticmethod
    def _generate_combinations(
        param_grid: Mapping[str, Iterable[Any]],
    ) -> List[Dict[str, Any]]:
        """Cartesian product with structural pruning.

        Drops combinations that violate ordering invariants, e.g.
        ``fast_ema >= slow_ema`` or ``rsi_oversold >= rsi_overbought``.
        """
        keys = list(param_grid.keys())
        values = [list(param_grid[k]) for k in keys]

        out: List[Dict[str, Any]] = []
        for combo in itertools.product(*values):
            params = dict(zip(keys, combo))

            fast = params.get("fast_ema")
            slow = params.get("slow_ema")
            if fast is not None and slow is not None and fast >= slow:
                continue

            oversold = params.get("rsi_oversold")
            overbought = params.get("rsi_overbought")
            if oversold is not None and overbought is not None and oversold >= overbought:
                continue

            sl = params.get("stop_loss_pct")
            tp = params.get("take_profit_pct")
            if sl is not None and tp is not None and tp <= sl:
                continue  # require positive risk:reward

            out.append(params)
        return out

    def _load_candles(
        self,
        symbol: str,
        timeframe: str,
        start_date: str,
        end_date: str,
    ) -> Optional[pd.DataFrame]:
        if self.db is None:
            logger.error("[Hyperopt] No database handle — cannot load candles")
            return None

        try:
            start_dt = _parse_date(start_date)
            end_dt = _parse_date(end_date, end_of_day=True)
        except ValueError as exc:
            logger.error("[Hyperopt] %s", exc)
            return None

        df = self.db.get_candles(
            symbol=symbol,
            interval=timeframe,
            start_time=start_dt,
            end_time=end_dt,
        )

        if df is None or df.empty:
            return None

        cols_needed = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = cols_needed - set(df.columns)
        if missing:
            logger.error("[Hyperopt] Candle frame missing required columns: %s", sorted(missing))
            return None

        df = df.sort_values("timestamp").reset_index(drop=True)
        # Defensive numeric coercion (some rows may have None volume)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
        return df

    def _walk_forward(
        self,
        candles: pd.DataFrame,
        mode: str,
        params: Dict[str, Any],
        n_splits: int,
    ) -> List[Dict[str, Any]]:
        """Slice the candle frame into ``n_splits`` windows and backtest each
        out-of-sample window. Strategy is rule-based so there is no train step;
        every window after the first acts as out-of-sample test data.
        """
        n_splits = max(int(n_splits), 2)
        window_size = len(candles) // n_splits
        if window_size < 100:
            return []

        scores: List[Dict[str, Any]] = []
        for i in range(n_splits - 1):
            test_start = (i + 1) * window_size
            test_end = min(test_start + window_size, len(candles))
            test_candles = candles.iloc[test_start:test_end].reset_index(drop=True)

            if len(test_candles) < 100:
                continue

            metrics = self._backtest_with_params(test_candles, mode, params)
            if metrics is not None:
                metrics["bars"] = len(test_candles)
                scores.append(metrics)
        return scores

    def _backtest_with_params(
        self,
        candles: pd.DataFrame,
        mode: str,
        params: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if mode == "scalping":
            signals = self._compute_scalping_signals(candles, params)
        elif mode == "trend_only":
            signals = self._compute_trend_only_signals(candles, params)
        else:
            return None

        if signals is None or signals.empty:
            return None

        return self._simulate(signals, params)

    # ────────── Vectorised signal generators (per mode) ──────────

    def _compute_scalping_signals(
        self,
        candles: pd.DataFrame,
        params: Dict[str, Any],
    ) -> Optional[pd.DataFrame]:
        """EMA-cross + RSI + Bollinger-band bounce — mirrors ScalpingStrategy."""
        fast = int(params.get("fast_ema", 9))
        slow = int(params.get("slow_ema", 21))
        rsi_period = int(params.get("rsi_period", 7))
        rsi_oversold = float(params.get("rsi_oversold", 34))
        rsi_overbought = float(params.get("rsi_overbought", 66))
        bb_period = int(params.get("bollinger_period", 20))
        bb_std = float(params.get("bollinger_std", 2.0))

        if len(candles) < max(slow, bb_period, rsi_period) + 5:
            return None

        close = candles["close"].astype(float)

        ema_fast = _ema(close, fast)
        ema_slow = _ema(close, slow)
        rsi = _rsi(close, rsi_period)
        upper, lower = _bollinger(close, bb_period, bb_std)
        atr = _atr(candles["high"].astype(float), candles["low"].astype(float), close, self.atr_period)

        prev_fast = ema_fast.shift(1)
        prev_slow = ema_slow.shift(1)
        prev_close = close.shift(1)
        prev_lower = lower.shift(1)
        prev_upper = upper.shift(1)

        bullish_cross = (prev_fast <= prev_slow) & (ema_fast > ema_slow)
        bearish_cross = (prev_fast >= prev_slow) & (ema_fast < ema_slow)
        lower_bounce = (prev_close <= prev_lower) & (close > lower)
        upper_reject = (prev_close >= prev_upper) & (close < upper)

        signal = pd.Series(["HOLD"] * len(candles), index=candles.index, dtype=object)
        buy_mask = bullish_cross & (rsi <= rsi_oversold) & lower_bounce
        sell_mask = bearish_cross & (rsi >= rsi_overbought) & upper_reject
        signal[buy_mask] = "BUY"
        signal[sell_mask] = "SELL"

        df = candles[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        df["atr"] = atr
        df["signal"] = signal
        return df

    def _compute_trend_only_signals(
        self,
        candles: pd.DataFrame,
        params: Dict[str, Any],
    ) -> Optional[pd.DataFrame]:
        """EMA-cross trend filter — closer to mode_indicator_profiles.trend_only."""
        fast = int(params.get("fast_ema", 12))
        slow = int(params.get("slow_ema", 26))

        if len(candles) < slow + 5:
            return None

        close = candles["close"].astype(float)
        high = candles["high"].astype(float)
        low = candles["low"].astype(float)

        ema_fast = _ema(close, fast)
        ema_slow = _ema(close, slow)
        atr = _atr(high, low, close, self.atr_period)

        prev_fast = ema_fast.shift(1)
        prev_slow = ema_slow.shift(1)

        bullish_cross = (prev_fast <= prev_slow) & (ema_fast > ema_slow)
        bearish_cross = (prev_fast >= prev_slow) & (ema_fast < ema_slow)

        # Trend filter: only act when price agrees with the slow EMA direction
        bullish_trend = close > ema_slow
        bearish_trend = close < ema_slow

        signal = pd.Series(["HOLD"] * len(candles), index=candles.index, dtype=object)
        signal[bullish_cross & bullish_trend] = "BUY"
        signal[bearish_cross & bearish_trend] = "SELL"

        df = candles[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        df["atr"] = atr
        df["signal"] = signal
        return df

    # ────────── Trade simulator (long-only, spot) ──────────

    def _simulate(
        self,
        signals: pd.DataFrame,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Step through bars and book trades against the bot's SL/TP rules."""
        sl_pct = float(params.get("stop_loss_pct", 1.0))
        tp_pct = float(params.get("take_profit_pct", 3.0))
        atr_mult = float(params.get("atr_multiplier", 1.2))
        fee_pct = self.fee_pct

        equity = self.initial_capital
        peak_equity = equity
        max_dd_frac = 0.0

        in_position = False
        entry_price = 0.0
        sl_price = 0.0
        tp_price = 0.0
        entry_idx = -1

        trades: List[Dict[str, Any]] = []

        highs = signals["high"].to_numpy(dtype=float)
        lows = signals["low"].to_numpy(dtype=float)
        closes = signals["close"].to_numpy(dtype=float)
        atrs = signals["atr"].to_numpy(dtype=float)
        signal_arr = signals["signal"].to_numpy()

        n = len(signals)
        # Skip the very first bars where indicators are NaN; start when we have
        # at least one prior close to enable cross detection.
        for i in range(1, n):
            if in_position:
                exit_price: Optional[float] = None
                exit_reason: Optional[str] = None

                # SL has priority over TP (worst-case assumption when both are
                # touched on the same bar).
                if lows[i] <= sl_price:
                    exit_price = sl_price
                    exit_reason = "SL"
                elif highs[i] >= tp_price:
                    exit_price = tp_price
                    exit_reason = "TP"
                elif signal_arr[i] == "SELL":
                    exit_price = closes[i]
                    exit_reason = "SIGNAL"

                if exit_price is not None:
                    gross = (exit_price - entry_price) / entry_price
                    net = gross - fee_pct
                    equity *= 1.0 + net
                    trades.append(
                        {
                            "entry": entry_price,
                            "exit": exit_price,
                            "pnl_pct": net,
                            "bars": i - entry_idx,
                            "reason": exit_reason,
                        }
                    )
                    in_position = False

                    peak_equity = max(peak_equity, equity)
                    if peak_equity > 0:
                        dd = (peak_equity - equity) / peak_equity
                        max_dd_frac = max(max_dd_frac, dd)
            else:
                if signal_arr[i] == "BUY":
                    entry_price = float(closes[i])
                    if not (entry_price > 0):
                        continue
                    atr_val = float(atrs[i]) if not np.isnan(atrs[i]) else 0.0
                    sl_distance_pct = sl_pct
                    if atr_val > 0:
                        atr_pct = (atr_val * atr_mult) / entry_price * 100.0
                        sl_distance_pct = max(sl_pct, atr_pct)

                    sl_price = entry_price * (1.0 - sl_distance_pct / 100.0)
                    tp_price = entry_price * (1.0 + tp_pct / 100.0)
                    entry_idx = i
                    in_position = True

        # Close any dangling position at the final close so it shows up in stats.
        if in_position and n > 0:
            last_close = float(closes[-1])
            gross = (last_close - entry_price) / entry_price
            net = gross - fee_pct
            equity *= 1.0 + net
            trades.append(
                {
                    "entry": entry_price,
                    "exit": last_close,
                    "pnl_pct": net,
                    "bars": (n - 1) - entry_idx,
                    "reason": "EOT",
                }
            )
            peak_equity = max(peak_equity, equity)
            if peak_equity > 0:
                dd = (peak_equity - equity) / peak_equity
                max_dd_frac = max(max_dd_frac, dd)

        return self._summarise_trades(trades, equity, max_dd_frac, signals)

    def _summarise_trades(
        self,
        trades: List[Dict[str, Any]],
        final_equity: float,
        max_dd_frac: float,
        signals: pd.DataFrame,
    ) -> Dict[str, Any]:
        n_trades = len(trades)
        if n_trades == 0:
            return {
                "sharpe_ratio": 0.0,
                "win_rate": 0.0,
                "total_return": 0.0,
                "max_drawdown": 0.0,
                "profit_factor": 0.0,
                "total_trades": 0,
                "avg_trade_pct": 0.0,
            }

        returns = np.array([t["pnl_pct"] for t in trades], dtype=float)
        wins = returns[returns > 0]
        losses = returns[returns <= 0]

        gross_profit = float(wins.sum()) if wins.size else 0.0
        gross_loss = float(-losses.sum()) if losses.size else 0.0
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            profit_factor = float("inf")
        else:
            profit_factor = 0.0

        # Sharpe — annualise by trades-per-year inferred from the test window.
        try:
            ts = pd.to_datetime(signals["timestamp"], errors="coerce")
            span_days = max((ts.iloc[-1] - ts.iloc[0]).total_seconds() / 86400.0, 1.0)
        except Exception:
            span_days = 1.0

        trades_per_year = n_trades * (365.0 / span_days)
        std = float(returns.std(ddof=1)) if n_trades > 1 else 0.0
        if std > 0 and trades_per_year > 0:
            sharpe = float(returns.mean() / std * np.sqrt(trades_per_year))
        else:
            sharpe = 0.0

        # Cap pathological infinity results so they don't poison averaging.
        if not np.isfinite(profit_factor):
            profit_factor = 5.0

        total_return_pct = (final_equity - self.initial_capital) / self.initial_capital * 100.0

        return {
            "sharpe_ratio": float(sharpe),
            "win_rate": float(len(wins) / n_trades),
            "total_return": float(total_return_pct),
            "max_drawdown": float(max_dd_frac * 100.0),  # percentage
            "profit_factor": float(profit_factor),
            "total_trades": int(n_trades),
            "avg_trade_pct": float(returns.mean() * 100.0),
        }

    # ────────── Aggregation + scoring ──────────

    def _average_results(
        self,
        params: Dict[str, Any],
        wf_scores: List[Dict[str, Any]],
    ) -> Optional[OptimizationResult]:
        if not wf_scores:
            return None

        def _avg(key: str) -> float:
            vals = [float(s.get(key, 0.0)) for s in wf_scores]
            return float(np.mean(vals)) if vals else 0.0

        sharpe = _avg("sharpe_ratio")
        win_rate = _avg("win_rate")
        total_return = _avg("total_return")
        max_dd = _avg("max_drawdown")
        profit_factor = _avg("profit_factor")
        avg_trade_pct = _avg("avg_trade_pct")
        total_trades = int(np.mean([int(s.get("total_trades", 0)) for s in wf_scores]))
        bars_evaluated = int(np.sum([int(s.get("bars", 0)) for s in wf_scores]))

        averaged = {
            "sharpe_ratio": sharpe,
            "win_rate": win_rate,
            "total_return": total_return,
            "max_drawdown": max_dd,
            "profit_factor": profit_factor,
            "total_trades": total_trades,
            "avg_trade_pct": avg_trade_pct,
        }
        score = self._calculate_score(averaged)

        return OptimizationResult(
            params=dict(params),
            sharpe_ratio=round(sharpe, 4),
            win_rate=round(win_rate, 4),
            total_return=round(total_return, 4),
            max_drawdown=round(max_dd, 4),
            profit_factor=round(profit_factor, 4),
            total_trades=int(total_trades),
            avg_trade_pct=round(avg_trade_pct, 4),
            score=score,
            splits_evaluated=len(wf_scores),
            bars_evaluated=bars_evaluated,
        )

    @staticmethod
    def _calculate_score(metrics: Mapping[str, Any]) -> float:
        """Composite score: Sharpe 40% / WinRate 30% / MaxDD 20% / PF 10%.

        The drawdown and profit-factor terms are normalised so that a 20% DD
        contributes 0 and a profit factor ≥ 3.0 maxes out at the cap.
        """
        sharpe = float(metrics.get("sharpe_ratio", 0.0))
        win_rate = float(metrics.get("win_rate", 0.0))
        max_dd = float(metrics.get("max_drawdown", 100.0))
        pf = float(metrics.get("profit_factor", 0.0))
        trades = int(metrics.get("total_trades", 0))

        if trades < MIN_TRADES_FOR_SCORE:
            return -999.0

        dd_term = 1.0 - min(max(max_dd, 0.0) / 20.0, 1.0)
        pf_term = min(max(pf, 0.0) / 3.0, 1.0)

        score = sharpe * 0.40 + win_rate * 0.30 + dd_term * 0.20 + pf_term * 0.10
        return round(float(score), 4)


# ---------------------------------------------------------------------------
# Pretty printer (console "box" output described in SPEC_07)
# ---------------------------------------------------------------------------


def _format_results_table(
    symbol: str,
    mode: str,
    start_date: str,
    end_date: str,
    combinations_tested: int,
    n_splits: int,
    results: List[OptimizationResult],
) -> str:
    width = 78
    border_top = "+" + "=" * (width - 2) + "+"
    border_mid = "+" + "-" * (width - 2) + "+"

    def line(text: str = "") -> str:
        body = text.ljust(width - 2)
        if len(body) > width - 2:
            body = body[: width - 2]
        return f"|{body}|"

    out = [
        border_top,
        line(f" HYPEROPT RESULTS"),
        line(f" Symbol: {symbol.upper()}  |  Mode: {mode}  |  " f"Period: {start_date} -> {end_date}"),
        line(f" Combinations tested: {combinations_tested}  |  " f"Walk-forward splits: {n_splits}"),
        border_mid,
        line(" RANK   SCORE   SHARPE  WIN%    MAX_DD  PF    TRADES"),
    ]

    if not results:
        out.append(line(" (no results — try widening the search window)"))
    else:
        for rank, res in enumerate(results, start=1):
            out.append(
                line(
                    f" #{rank:<4} {res.score:>5.3f}  {res.sharpe_ratio:>5.2f}  "
                    f"{res.win_rate * 100:>5.1f}%  "
                    f"{-abs(res.max_drawdown):>5.1f}%  "
                    f"{res.profit_factor:>4.2f}  {res.total_trades:>4d}"
                )
            )
            param_str = _format_params(res.params)
            # Wrap long parameter strings across multiple table rows.
            for chunk in _wrap_text(param_str, width - 6):
                out.append(line("       " + chunk))

    out.append(border_top)
    return "\n".join(out)


def _format_params(params: Mapping[str, Any]) -> str:
    parts: List[str] = []
    for key, val in params.items():
        if isinstance(val, float):
            parts.append(f"{key}={val:g}")
        else:
            parts.append(f"{key}={val}")
    return ", ".join(parts)


def _wrap_text(text: str, max_width: int) -> List[str]:
    if max_width <= 0:
        return [text]
    chunks: List[str] = []
    current = ""
    for word in text.split(", "):
        token = word + ", "
        if len(current) + len(token) > max_width and current:
            chunks.append(current.rstrip(", "))
            current = token
        else:
            current += token
    if current:
        chunks.append(current.rstrip(", "))
    return chunks or [text]


def _write_stdout_safe(text: str) -> None:
    """Write ``text`` to stdout, tolerating Windows code-page limitations."""
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
        sys.stdout.write(text.encode("ascii", errors="replace").decode("ascii"))
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_date(raw: str, end_of_day: bool = False) -> datetime:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Empty date string")
    try:
        if "T" in raw:
            dt = datetime.fromisoformat(raw)
        else:
            dt = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Invalid date '{raw}': {exc}") from exc

    if end_of_day and dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt


def _load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        logger.warning("[Hyperopt] Config not found at %s; using defaults", path)
        return {}
    try:
        import yaml
    except ImportError:
        logger.error("[Hyperopt] PyYAML not installed — cannot load %s", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.error("[Hyperopt] Failed to load config %s: %s", path, exc)
        return {}


def _resolve_pairs(
    explicit_symbol: Optional[str],
    config: Dict[str, Any],
    use_all: bool,
) -> List[str]:
    if explicit_symbol and not use_all:
        return [explicit_symbol.upper()]

    pairs = list(config.get("data", {}).get("pairs") or [])
    if pairs:
        return [str(p).upper() for p in pairs]

    # Fallback to the canonical Binance.th set listed in cursorrules.
    return ["BTCUSDT", "ETHUSDT", "BNBUSDT", "DOGEUSDT"]


def _prompt_yes_no(prompt: str) -> bool:
    try:
        ans = input(prompt).strip().lower()
    except EOFError:
        return False
    return ans in {"y", "yes"}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hyperopt",
        description="Crypto_Sniper grid-search + walk-forward optimiser (SPEC_07).",
    )
    parser.add_argument("--symbol", type=str, default=None, help="Trading pair, e.g. BTCUSDT (omit when --all is set)")
    parser.add_argument(
        "--mode",
        type=str,
        default="scalping",
        choices=sorted(PARAM_SPACE.keys()) + ["all"],
        help="Strategy mode to optimise",
    )
    parser.add_argument("--start", type=str, required=True, help="Window start (YYYY-MM-DD, UTC)")
    parser.add_argument("--end", type=str, required=True, help="Window end (YYYY-MM-DD, UTC)")
    parser.add_argument("--splits", type=int, default=5, help="Walk-forward window count (default 5)")
    parser.add_argument("--top", type=int, default=5, help="Number of top combinations to keep (default 5)")
    parser.add_argument("--all", action="store_true", help="Run every pair from config.data.pairs against every mode")
    parser.add_argument("--config", type=str, default=str(DEFAULT_CONFIG_PATH), help="Path to bot_config.yaml")
    parser.add_argument("--db", type=str, default=None, help="Override SQLite database path")
    parser.add_argument(
        "--results-dir", type=str, default=str(DEFAULT_RESULTS_DIR), help="Output directory for ranked JSON results"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Apply the best parameters to bot_config.yaml without prompting"
    )
    parser.add_argument(
        "--no-prompt", action="store_true", help="Skip the interactive 'Apply best params? [y/n]' prompt"
    )
    parser.add_argument("--no-save", action="store_true", help="Skip writing the JSON results file")
    parser.add_argument("--quiet", action="store_true", help="Suppress the ranked table output to stdout")
    parser.add_argument("--log-level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not args.all and not args.symbol:
        parser.error("Either --symbol or --all must be supplied")

    config = _load_config(Path(args.config))

    try:
        from database import get_database
    except Exception as exc:
        logger.error("[Hyperopt] Cannot import database module: %s", exc)
        return 2

    db = get_database(args.db)

    runner = HyperoptRunner(db=db, config=config)

    pairs = _resolve_pairs(args.symbol, config, args.all)
    modes = sorted(PARAM_SPACE.keys()) if (args.all or args.mode == "all") else [args.mode]

    logger.info(
        "[Hyperopt] Plan: pairs=%s modes=%s window=%s..%s splits=%d top=%d",
        pairs,
        modes,
        args.start,
        args.end,
        args.splits,
        args.top,
    )

    overall_status = 0
    for symbol in pairs:
        for mode in modes:
            try:
                # Recount to print expected combination count alongside each header.
                combinations = runner._generate_combinations(PARAM_SPACE[mode])
                results = runner.run(
                    symbol=symbol,
                    mode=mode,
                    start_date=args.start,
                    end_date=args.end,
                    n_splits=args.splits,
                    top_n=args.top,
                )
            except Exception as exc:
                logger.exception("[Hyperopt] %s/%s crashed: %s", symbol, mode, exc)
                overall_status = 1
                continue

            if not results:
                logger.warning("[Hyperopt] No results produced for %s/%s", symbol, mode)
                continue

            if not args.quiet:
                table = _format_results_table(
                    symbol=symbol,
                    mode=mode,
                    start_date=args.start,
                    end_date=args.end,
                    combinations_tested=len(combinations),
                    n_splits=args.splits,
                    results=results,
                )
                _write_stdout_safe(table + "\n")

            if not args.no_save:
                try:
                    runner.save_results_json(
                        symbol=symbol,
                        mode=mode,
                        results=results,
                        start_date=args.start,
                        end_date=args.end,
                        results_dir=Path(args.results_dir),
                    )
                except Exception as exc:
                    logger.error("[Hyperopt] Failed to save results: %s", exc)
                    overall_status = 1

            best = results[0]
            should_apply = args.apply
            if not should_apply and not args.no_prompt and not args.all:
                should_apply = _prompt_yes_no(f"\nApply best params for {symbol}/{mode}? [y/n]: ")

            if should_apply:
                try:
                    runner.apply_best_params(best, mode, Path(args.config))
                    logger.info(
                        "[Hyperopt] Applied %s/%s best params (score=%.3f)",
                        symbol,
                        mode,
                        best.score,
                    )
                except Exception as exc:
                    logger.error("[Hyperopt] Failed to apply params: %s", exc)
                    overall_status = 1
            else:
                logger.info(
                    "[Hyperopt] Skipping config update for %s/%s " "(use --apply to persist)",
                    symbol,
                    mode,
                )

    return overall_status


if __name__ == "__main__":
    raise SystemExit(main())
