"""
Signal Generator
รวม signals จากหลาย strategy, คำนวณ confidence, และ filter risk
Multi-timeframe analysis สำหรับยืนยัน trend จากหลาย timeframe
"""
import time
import logging
import threading
import copy
import hashlib
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, TYPE_CHECKING
from dataclasses import dataclass, field

logger = logging.getLogger("crypto-bot.signal")
diag_logger = logging.getLogger("crypto-bot.signal_flow")

_SIGNAL_FLOW_LOCK = threading.Lock()
_LATEST_SIGNAL_FLOW: Dict[str, Dict[str, Any]] = {}


def get_latest_signal_flow_snapshot() -> Dict[str, Dict[str, Any]]:
    """Return a copy of the latest per-pair signal flow diagnostics."""
    with _SIGNAL_FLOW_LOCK:
        return copy.deepcopy(_LATEST_SIGNAL_FLOW)

def ensure_signal_flow_record(pair: str, reason: str = "Waiting for first signal cycle") -> Dict[str, Any]:
    """Ensure a per-pair signal-flow record exists during warmup phases."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    normalized_pair = str(pair or "").upper()
    normalized_reason = str(reason or "").strip() or "Waiting for first signal cycle"

    with _SIGNAL_FLOW_LOCK:
        record = _LATEST_SIGNAL_FLOW.setdefault(
            normalized_pair,
            {
                "updated_at": ts,
                "steps": {},
            },
        )
        record["updated_at"] = ts
        steps = record.setdefault("steps", {})
        bootstrap = steps.get("Bootstrap")
        if not isinstance(bootstrap, dict):
            steps["Bootstrap"] = {
                "result": "INFO",
                "reason": normalized_reason,
                "timestamp": ts,
            }
        elif not str(bootstrap.get("reason") or "").strip():
            bootstrap["reason"] = normalized_reason
            bootstrap["timestamp"] = ts

        return copy.deepcopy(record)

def _diag(pair: str, step: str, result: str, reason: str = ""):
    """Emit a standardised [SIGNAL_FLOW] diagnostic line."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    normalized_pair = str(pair or "").upper()
    with _SIGNAL_FLOW_LOCK:
        record = _LATEST_SIGNAL_FLOW.setdefault(
            normalized_pair,
            {
                "updated_at": ts,
                "steps": {},
            },
        )
        record["updated_at"] = ts
        record_steps = record.setdefault("steps", {})
        record_steps[str(step or "")] = {
            "result": str(result or ""),
            "reason": str(reason or ""),
            "timestamp": ts,
        }

    diag_logger.info(
        f"[SIGNAL_FLOW] {ts} | {pair} | Step: {step} | Result: {result}"
        + (f" | Reason: {reason}" if reason else "")
    )

from strategy_base import (
    StrategyBase, TradingSignal, SignalType, 
    SignalConfidence, MarketCondition, detect_market_condition
)
from strategies.trend_following import TrendFollowingStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.breakout import BreakoutStrategy
from strategies.scalping import ScalpingStrategy
from strategies.sniper import SniperStrategy
from strategies.machete_v8b_lite import MacheteV8bLite
from strategies.simple_scalp_plus import SimpleScalpPlus
from indicators import TechnicalIndicators

# Multi-timeframe imports
try:
    from multi_timeframe import (
        MultiTimeframeCollector, MultiTimeframeAnalyzer,
        MultiTimeframeSignalGenerator, TimeframeData,
        TimeframeSignal, MultiTimeframeResult
    )
    MTF_AVAILABLE = True
except ImportError:
    MTF_AVAILABLE = False
    logger.warning("Multi-timeframe module not available")

if TYPE_CHECKING:
    from multi_timeframe import (
        MultiTimeframeCollector, MultiTimeframeAnalyzer,
        MultiTimeframeSignalGenerator, TimeframeData,
        TimeframeSignal, MultiTimeframeResult
    )


@dataclass
class AggregatedSignal:
    """Signal ที่รวมจากหลาย strategies
    
    Note: signal_type must use strategy_base.SignalType enum values (uppercase: BUY/SELL/HOLD)
    for consistency across all signal paths (strategy, ML, ensemble, multi-timeframe).
    """
    symbol: str
    signal_type: SignalType
    combined_confidence: float
    
    # Source signals
    signals: List[TradingSignal] = field(default_factory=list)
    
    # Aggregated data — aligned with risk_management SL/TP conventions
    avg_price: float = 0.0
    avg_stop_loss: float = 0.0
    avg_take_profit: float = 0.0
    avg_risk_reward: float = 0.0
    
    # Strategy breakdown
    strategy_votes: Dict[str, int] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    
    # Risk assessment
    risk_score: float = 0.0        # 0-100, lower is safer
    market_condition: MarketCondition = MarketCondition.RANGING
    trade_rationale: str = ""     # Human-readable reason for this trade signal
    _mtf_rationale: str = ""      # MTF alignment rationale suffix
    
    @property
    def confidence_level(self) -> SignalConfidence:
        if self.combined_confidence < 0.4:
            return SignalConfidence.LOW
        elif self.combined_confidence < 0.7:
            return SignalConfidence.MEDIUM
        return SignalConfidence.HIGH
    
    @property
    def is_aligned(self) -> bool:
        """ทุก strategy เห็นด้วยกับ direction เดียวกัน"""
        if not self.signals:
            return False
        return len(self.strategy_votes) == 1


@dataclass
class SignalRiskCheck:
    """ผลลัพธ์จากการ check risk (renamed from RiskCheckResult to avoid duplicate with risk_management.py)"""
    passed: bool
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    risk_score: float = 0.0


class SignalGenerator:
    """
    รวม signals จากทุก strategy, คำนวณ confidence, และ filter by risk
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        sniper_cfg = dict(self.config.get("sniper", {}) or {})
        
        # Initialize strategies
        self.strategies: Dict[str, StrategyBase] = {
            "trend_following": TrendFollowingStrategy(
                self.config.get("trend_following", {})
            ),
            "mean_reversion": MeanReversionStrategy(
                self.config.get("mean_reversion", {})
            ),
            "breakout": BreakoutStrategy(
                self.config.get("breakout", {})
            ),
            "scalping": ScalpingStrategy(
                self.config.get("scalping", {})
            ),
            "sniper": SniperStrategy(
                sniper_cfg,
                indicators=TechnicalIndicators,
            ),
            "machete_v8b_lite": MacheteV8bLite(
                self.config.get("machete_v8b_lite", {}),
            ),
            "simple_scalp_plus": SimpleScalpPlus(
                self.config.get("simple_scalp_plus", {}),
            ),
        }
        # Trusted strategies for default aggregation. The legacy "trend_following",
        # "mean_reversion", "breakout", and "scalping" entries are NOT included by
        # default — historically they fired continuously while a condition held
        # (the root cause of the 5.3% win rate). They are still loaded above so
        # that mode profiles in bot_config.yaml can opt them in explicitly.
        self._aggregate_strategy_names = [
            "sniper",
            "machete_v8b_lite",
            "simple_scalp_plus",
        ]
        
        # Risk parameters
        strategies_top = dict(self.config.get("strategies", {}) or {})
        self.risk_config = {
            "min_confidence": self.config.get("min_confidence", 0.5),
            "max_risk_score": self.config.get("max_risk_score", 70),
            "min_strategies_agree": self.config.get("min_strategies_agree", 2),
            "max_positions": self.config.get("max_open_positions", 3),
            "max_daily_trades": self.config.get("max_daily_trades", 10),
            # When True: each strategy's signal is aggregated alone (OR). Agreement gate is skipped
            # so e.g. SimpleScalp+ can trade without Sniper firing the same bar (and vice versa).
            "independent_strategy_execution": bool(
                strategies_top.get("independent_strategy_execution", False)
            ),
        }

        # Signal cache: key=(symbol, data_hash), value=(signals, timestamp)
        self._signal_cache: Dict[str, tuple] = {}
        self._signal_cache_lock = threading.Lock()
        self._signal_cache_ttl: float = 30.0  # seconds
        self._signal_cache_max_size: int = 100  # Max entries before eviction
        
        # State
        self._open_positions: List[Dict] = []
        self._daily_trade_count: int = 0
        self._last_reset: datetime = datetime.now()

        # Dynamic strategy weighting (populated by set_database)
        self._db = None
        self._strategy_perf_cache: Dict[str, Dict[str, Any]] = {}
        self._strategy_perf_cache_at: float = 0.0
        self._strategy_perf_cache_ttl: float = 300.0  # refresh every 5 minutes
        self._strategy_perf_lookback_days: int = int(self.config.get("strategy_perf_lookback_days", 30))
        self._mtf_cache: Dict[str, tuple] = {}
        self._mtf_cache_lock = threading.Lock()
        self._mtf_cache_ttl: float = float((self.config.get("multi_timeframe", {}) or {}).get("cache_ttl_seconds", 10.0) or 10.0)
        self._mtf_cache_max_size: int = int((self.config.get("multi_timeframe", {}) or {}).get("cache_max_size", 100) or 100)
        self._mtf_indicator_cache: Dict[tuple, tuple] = {}
        self._mtf_indicator_cache_lock = threading.Lock()
        self._mtf_indicator_cache_ttl: float = float((self.config.get("multi_timeframe", {}) or {}).get("indicator_cache_ttl_seconds", 300.0) or 300.0)
        self._mtf_indicator_cache_max_size: int = int((self.config.get("multi_timeframe", {}) or {}).get("indicator_cache_max_size", 500) or 500)

    def set_database(self, db) -> None:
        """Inject the database handle for dynamic strategy performance lookups."""
        self._db = db

    def get_active_strategies_for_mode(self, mode: str) -> List[str]:
        """Resolve active strategy names from mode profiles with safe fallbacks."""
        mode_name = str(mode or "standard").strip().lower() or "standard"
        mode_profiles = dict(self.config.get("mode_indicator_profiles", {}) or {})
        profile = dict(mode_profiles.get(mode_name, {}) or {})
        configured = profile.get("active_strategies")
        if not isinstance(configured, list):
            configured = None
        if configured is None:
            configured = self.config.get("strategies", {}).get("enabled", [])
        if not isinstance(configured, list):
            configured = []

        active: List[str] = []
        seen: set[str] = set()
        for strategy_name in configured:
            normalized = str(strategy_name or "").strip().lower()
            if not normalized or normalized in seen:
                continue
            if normalized not in self.strategies:
                logger.debug("Skipping unknown strategy %s in mode %s", normalized, mode_name)
                continue
            seen.add(normalized)
            active.append(normalized)

        if active:
            return active
        return list(self._aggregate_strategy_names)

    def refresh_risk_config_for_mode(self, mode: str) -> None:
        """Align risk gate thresholds with ``mode_indicator_profiles`` then global ``risk`` / ``strategies``."""
        mode_name = str(mode or "standard").strip().lower() or "standard"
        profiles = dict(self.config.get("mode_indicator_profiles", {}) or {})
        profile = dict(profiles.get(mode_name, {}) or {})
        strategies_cfg = dict(self.config.get("strategies", {}) or {})
        risk_cfg = dict(self.config.get("risk", {}) or {})

        if "min_confidence" in profile:
            self.risk_config["min_confidence"] = float(profile["min_confidence"])
        elif "min_confidence" in strategies_cfg:
            self.risk_config["min_confidence"] = float(strategies_cfg["min_confidence"])

        if "min_strategies_agree" in profile:
            self.risk_config["min_strategies_agree"] = int(profile["min_strategies_agree"])
        elif "min_strategies_agree" in strategies_cfg:
            self.risk_config["min_strategies_agree"] = int(strategies_cfg["min_strategies_agree"])

        if "independent_strategy_execution" in profile:
            self.risk_config["independent_strategy_execution"] = bool(profile["independent_strategy_execution"])
        elif "independent_strategy_execution" in strategies_cfg:
            self.risk_config["independent_strategy_execution"] = bool(
                strategies_cfg["independent_strategy_execution"]
            )

        if "max_risk_score" in profile:
            self.risk_config["max_risk_score"] = float(profile["max_risk_score"])

        if "max_open_positions" in risk_cfg:
            self.risk_config["max_positions"] = int(risk_cfg["max_open_positions"])
        if "max_daily_trades" in risk_cfg:
            self.risk_config["max_daily_trades"] = int(risk_cfg["max_daily_trades"])

    def _make_mtf_cache_key(self, pair: str, timeframes: List[str], db: Any) -> str:
        raw = f"{str(pair or '').upper()}|{','.join(str(tf) for tf in timeframes)}|{type(db).__name__}:{id(db)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _get_cached_mtf_result(self, cache_key: str):
        now = time.time()
        with self._mtf_cache_lock:
            cached = self._mtf_cache.get(cache_key)
            if not cached:
                return None
            result, cached_at = cached
            if now - cached_at >= self._mtf_cache_ttl:
                self._mtf_cache.pop(cache_key, None)
                return None
            return copy.deepcopy(result)

    def _store_cached_mtf_result(self, cache_key: str, result) -> None:
        with self._mtf_cache_lock:
            if len(self._mtf_cache) >= self._mtf_cache_max_size:
                oldest_key = min(self._mtf_cache.items(), key=lambda item: item[1][1])[0]
                self._mtf_cache.pop(oldest_key, None)
            self._mtf_cache[cache_key] = (copy.deepcopy(result), time.time())

    def _get_strategy_performance(self) -> Dict[str, Dict[str, Any]]:
        """Return cached per-strategy performance stats, refreshing every 5 min."""
        now = time.time()
        if now - self._strategy_perf_cache_at < self._strategy_perf_cache_ttl and self._strategy_perf_cache:
            return self._strategy_perf_cache
        if self._db and hasattr(self._db, "get_strategy_performance"):
            try:
                self._strategy_perf_cache = self._db.get_strategy_performance(
                    days=self._strategy_perf_lookback_days,
                )
                self._strategy_perf_cache_at = now
            except Exception as exc:
                logger.debug("Failed to load strategy performance: %s", exc)
        return self._strategy_perf_cache

    def generate_signals(
        self,
        data: pd.DataFrame,
        symbol: str,
        use_strategies: Optional[List[str]] = None
    ) -> List[AggregatedSignal]:
        """
        Generate signals from all or specified strategies.

        Uses a 30-second cache keyed on the last few candle closes' hash
        to avoid recalculating when data hasn't changed.

        Args:
            data: OHLCV DataFrame
            symbol: Trading pair
            use_strategies: List of strategy names to use (None = all)

        Returns:
            List of AggregatedSignal objects
        """
        import hashlib

        # Reset daily count if new day
        self._reset_daily_state()

        if not isinstance(data, pd.DataFrame) or data.empty:
            logger.debug("generate_signals skipped: empty OHLCV | symbol=%s", symbol)
            return []

        # Build a cache key from symbol + OHLCV hash (SHA-256, 24 chars)
        try:
            tail_data = data[['open', 'high', 'low', 'close', 'volume']].tail(5)
            serialized_tail = str(tail_data.to_json() or "")
            data_hash = hashlib.sha256(serialized_tail.encode("utf-8")).hexdigest()[:24]
        except Exception:
            data_hash = "unknown"

        cache_key = f"{symbol}:{data_hash}"
        now = time.time()

        # Return cached result if fresh
        with self._signal_cache_lock:
            if cache_key in self._signal_cache:
                cached_signals, cached_at = self._signal_cache[cache_key]
                if now - cached_at < self._signal_cache_ttl:
                    return cached_signals

        # Evict oldest entries if cache is full
        with self._signal_cache_lock:
            if len(self._signal_cache) >= self._signal_cache_max_size:
                # Remove oldest 20%
                evict_count = max(len(self._signal_cache) // 5, 1)
                sorted_keys = sorted(
                    self._signal_cache.keys(),
                    key=lambda k: self._signal_cache[k][1]
                )
                for key in sorted_keys[:evict_count]:
                    del self._signal_cache[key]

        # Detect market condition
        market_condition = detect_market_condition(data["close"].tolist() if isinstance(data, pd.DataFrame) else data)

        # Collect signals from each strategy
        all_signals: List[TradingSignal] = []

        strategies_to_use = use_strategies or list(self._aggregate_strategy_names)

        for name in strategies_to_use:
            if name not in self.strategies:
                continue

            strategy = self.strategies[name]

            try:
                signal = strategy.generate_signal(data, symbol)
                if signal:
                    # Validate signal
                    if strategy.validate_signal(signal, data):
                        all_signals.append(signal)
                        _diag(symbol, f"Strategy:{name}", "PASS",
                              f"type={signal.signal_type.value}, conf={signal.confidence:.3f}, "
                              f"price={signal.price:.2f}, RR={signal.risk_reward_ratio or 'N/A'}")
                    else:
                        _diag(symbol, f"Strategy:{name}", "REJECT",
                              f"validate_signal() returned False (type={signal.signal_type.value}, "
                              f"conf={signal.confidence:.3f})")
                else:
                    _diag(symbol, f"Strategy:{name}", "REJECT",
                          "generate_signal() returned None (no setup detected)")
            except Exception as e:
                logger.warning(
                    "Signal strategy error | pair=%s strategy=%s %s: %s",
                    symbol,
                    name,
                    type(e).__name__,
                    e,
                )
                _diag(symbol, f"Strategy:{name}", "REJECT", f"{type(e).__name__}: {e}")

        _diag(symbol, "SignalCollection", "INFO",
              f"Total raw signals collected: {len(all_signals)} from {len(strategies_to_use)} strategies")
        
        # Generate aggregated signals with MTF Convergence Phase 2
        aggregated = self._aggregate_signals(all_signals, market_condition, symbol, data)

        if not aggregated:
            if all_signals:
                raw_mix: Dict[str, int] = {}
                for raw_signal in all_signals:
                    key = raw_signal.signal_type.value
                    raw_mix[key] = raw_mix.get(key, 0) + 1
                logger.warning(
                    "[SignalDrop] %s | raw_signals=%d | mix=%s | reason=no actionable aggregated signals",
                    symbol,
                    len(all_signals),
                    ", ".join(f"{key}={value}" for key, value in sorted(raw_mix.items())) or "none",
                )
            _diag(symbol, "Aggregation", "REJECT",
                  "No actionable aggregated signals (all HOLD or empty)")
        else:
            for agg in aggregated:
                _diag(symbol, "Aggregation", "PASS",
                      f"type={agg.signal_type.value}, combined_conf={agg.combined_confidence:.3f}, "
                      f"risk_score={agg.risk_score:.0f}, strategies={list(agg.strategy_votes.keys())}")
        
        # Cache the result
        with self._signal_cache_lock:
            self._signal_cache[cache_key] = (aggregated, now)
        
        return aggregated

    # ── Sniper: Dual EMA + MACD "Full Bull Alignment" ─────────────────

    def generate_sniper_signal(
        self,
        data: pd.DataFrame,
        symbol: str,
    ) -> List[AggregatedSignal]:
        """Generate sniper signals by delegating the strategy logic to SniperStrategy."""
        self._reset_daily_state()

        strategy = self.strategies.get("sniper")
        if strategy is None:
            return []

        if not isinstance(data, pd.DataFrame) or data.empty:
            logger.debug("generate_sniper_signal skipped: empty OHLCV | symbol=%s", symbol)
            return []

        try:
            raw_signal = strategy.generate_signal(data, symbol)
        except Exception as e:
            logger.warning(
                "Signal strategy error | pair=%s strategy=sniper %s: %s",
                symbol,
                type(e).__name__,
                e,
            )
            _diag(symbol, "Sniper:Exception", "REJECT", f"{type(e).__name__}: {e}")
            return []
        diagnostics = getattr(strategy, "get_last_diagnostics", lambda: {})()
        for step in (
            "Sniper:DataCheck",
            "Sniper:MacroTrend",
            "Sniper:MicroTrend",
            "Sniper:MACDTrigger",
            "Sniper:ATR",
            "Sniper:Result",
        ):
            record = diagnostics.get(step)
            if record:
                _diag(symbol, step, record.get("result", ""), record.get("reason", ""))

        if not raw_signal:
            return []

        market_condition = detect_market_condition(data["close"].tolist() if isinstance(data, pd.DataFrame) else data)
        agg = AggregatedSignal(
            symbol=symbol,
            signal_type=raw_signal.signal_type,
            combined_confidence=raw_signal.confidence,
            signals=[raw_signal],
            avg_price=raw_signal.price,
            avg_stop_loss=float(raw_signal.stop_loss or 0.0),
            avg_take_profit=float(raw_signal.take_profit or 0.0),
            avg_risk_reward=float(raw_signal.risk_reward_ratio or 0.0),
            strategy_votes={raw_signal.strategy_name: 1},
            market_condition=market_condition,
            risk_score=self._calculate_risk_score([raw_signal], market_condition),
        )
        agg.trade_rationale = str((raw_signal.metadata or {}).get("trade_rationale") or self._generate_trade_rationale(agg))
        return [agg]

    def _make_aggregate_for_direction(
        self,
        type_signals: List[TradingSignal],
        sig_type: SignalType,
        market_condition: MarketCondition,
        symbol: str,
        data: pd.DataFrame,
    ) -> Optional[AggregatedSignal]:
        """Build one AggregatedSignal for a set of raw signals sharing the same direction."""
        if sig_type == SignalType.HOLD or not type_signals:
            return None

        strategy_weights = self._calculate_strategy_weights(type_signals)
        weight_sum = sum(strategy_weights.values())
        if weight_sum <= 0:
            return None
        weighted_conf = sum(
            s.confidence * strategy_weights.get(s.strategy_name, 1.0) for s in type_signals
        ) / weight_sum

        if len(type_signals) >= 3:
            weighted_conf = min(0.95, weighted_conf + 0.1)
        elif len(type_signals) >= 2:
            weighted_conf = min(0.90, weighted_conf + 0.05)

        adj_conf = self._adjust_for_market_condition(weighted_conf, type_signals, market_condition)

        prices = [s.price for s in type_signals]
        stop_losses = [s.stop_loss for s in type_signals if s.stop_loss]
        take_profits = [s.take_profit for s in type_signals if s.take_profit]
        rrs = [s.risk_reward_ratio for s in type_signals if s.risk_reward_ratio]

        votes: Dict[str, int] = {}
        for s in type_signals:
            votes[s.strategy_name] = votes.get(s.strategy_name, 0) + 1

        risk_score = self._calculate_risk_score(type_signals, market_condition)

        mtf_rationale = ""
        try:
            macro_trend = None
            htf_used = None

            if self._db and hasattr(self._db, "get_candles"):
                for htf_interval in ("1h", "4h"):
                    try:
                        htf_df = self._db.get_candles(symbol, interval=htf_interval, limit=30)
                        if htf_df is not None and hasattr(htf_df, "__len__") and len(htf_df) >= 20:
                            htf_closes = (
                                htf_df["close"].astype(float)
                                if hasattr(htf_df, "columns")
                                else pd.Series([float(r.close) for r in htf_df])
                            )
                            ema_fast = htf_closes.ewm(span=9, adjust=False).mean().iloc[-1]
                            ema_slow = htf_closes.ewm(span=21, adjust=False).mean().iloc[-1]
                            macro_trend = SignalType.BUY if ema_fast > ema_slow else SignalType.SELL
                            htf_used = htf_interval
                            break
                    except Exception:
                        continue

            if macro_trend is None and len(data) >= 60 and "close" in data.columns:
                ema_60 = data["close"].ewm(span=60, adjust=False).mean().iloc[-1]
                current_price = data["close"].iloc[-1]
                macro_trend = SignalType.BUY if current_price > ema_60 else SignalType.SELL
                htf_used = "ema60_approx"

            if macro_trend is not None:
                if sig_type != macro_trend:
                    logger.debug(
                        "[MTF Filter] Micro %s contradicts %s Macro %s -> Halving confidence",
                        sig_type.name,
                        htf_used,
                        macro_trend.name,
                    )
                    adj_conf *= 0.5
                    risk_score += 25
                    mtf_rationale = f" | ⚠️ MTF Misaligned ({htf_used})"
                else:
                    mtf_rationale = f" | ✅ MTF Aligned ({htf_used})"
        except Exception as e:
            logger.debug(f"[MTF] Filter skipped: {e}")

        agg_signal = AggregatedSignal(
            symbol=symbol,
            signal_type=sig_type,
            combined_confidence=adj_conf,
            signals=type_signals,
            avg_price=float(np.mean(prices)),
            avg_stop_loss=float(np.mean(stop_losses)) if stop_losses else 0.0,
            avg_take_profit=float(np.mean(take_profits)) if take_profits else 0.0,
            avg_risk_reward=float(np.mean(rrs)) if rrs else 0.0,
            strategy_votes=votes,
            market_condition=market_condition,
            risk_score=risk_score,
        )
        agg_signal._mtf_rationale = mtf_rationale
        return agg_signal
    
    def _aggregate_signals(
        self, 
        signals: List[TradingSignal],
        market_condition: MarketCondition,
        symbol: str,
        data: pd.DataFrame
    ) -> List[AggregatedSignal]:
        """Aggregate signals by direction and calculate combined confidence.

        With ``independent_strategy_execution``, emit one candidate per raw signal (OR),
        so strategies with different bar counts / styles do not need to fire on the same bar.
        """
        
        if not signals:
            _diag(symbol, "Aggregate:Input", "REJECT", "Empty signal list — nothing to aggregate")
            return []

        if self.risk_config.get("independent_strategy_execution"):
            results_ind: List[AggregatedSignal] = []
            for raw in signals:
                if raw.signal_type == SignalType.HOLD:
                    continue
                built = self._make_aggregate_for_direction(
                    [raw], raw.signal_type, market_condition, symbol, data
                )
                if built is not None:
                    results_ind.append(built)
            results_ind.sort(key=lambda x: x.combined_confidence, reverse=True)
            for agg_sig in results_ind:
                agg_sig.trade_rationale = self._generate_trade_rationale(agg_sig)
            _diag(
                symbol,
                "Aggregation",
                "INFO",
                f"independent_execution: {len(results_ind)} candidate(s) from {len(signals)} raw",
            )
            return results_ind

        by_type: Dict[SignalType, List[TradingSignal]] = {
            SignalType.BUY: [],
            SignalType.SELL: [],
            SignalType.HOLD: [],
        }
        
        for sig in signals:
            by_type[sig.signal_type].append(sig)
        
        results = []
        
        for sig_type, type_signals in by_type.items():
            built = self._make_aggregate_for_direction(
                type_signals, sig_type, market_condition, symbol, data
            )
            if built is not None:
                results.append(built)
        
        results.sort(key=lambda x: x.combined_confidence, reverse=True)
        
        for agg_sig in results:
            agg_sig.trade_rationale = self._generate_trade_rationale(agg_sig)
        
        return results
    
    def _generate_trade_rationale(self, signal: AggregatedSignal) -> str:
        """Generate human-readable rationale for why this trade signal was triggered.
        
        Format: [Trade Triggered] BUY/SELL | Source: indicator1 (score), indicator2 (score) | ML: XX% | Total Score: X%
        """
        if not signal.signals:
            return "[No signals]"
        
        # Get direction
        direction = signal.signal_type.value.upper()
        
        # Collect indicator scores
        indicator_scores = []
        for sig in signal.signals:
            strategy_name = sig.strategy_name.upper()
            conf_pct = sig.confidence * 100
            indicator_scores.append(f"{strategy_name}({conf_pct:.0f}%)")
        
        # Combined strategy confidence score
        total_conf = signal.combined_confidence * 100
        
        # Build rationale string
        sources = ", ".join(indicator_scores)
        rationale = f"[Trade Triggered] {direction} | Source: {sources} | Score: {total_conf:.0f}%"
        
        # Add market condition
        if signal.market_condition != MarketCondition.RANGING:
            rationale += f" | Market: {signal.market_condition.value}"
        
        # Add alignment info
        num_strategies = len(signal.signals)
        rationale += f" | {num_strategies} strategy/ies aligned"
        
        # Add MTF Phase 2 logic if attached
        if hasattr(signal, '_mtf_rationale'):
            rationale += signal._mtf_rationale
            
        return rationale
    
    def _calculate_strategy_weights(self, signals: List[TradingSignal]) -> Dict[str, float]:
        """Calculate weights using historical win rate + current signal quality.

        When per-strategy performance data is available (from closed trade
        history), the historical win_rate is blended into the weight so that
        recently-successful strategies carry more influence on the aggregated
        confidence.  When no history exists the system falls back to the
        original heuristic (base + confidence + RR bonus).
        """
        perf = self._get_strategy_performance()

        weights = {}
        for sig in signals:
            # Base weight
            w = 1.0

            # Higher weight for higher confidence
            w += sig.confidence * 0.5

            # Higher weight for better RR
            if sig.risk_reward_ratio and sig.risk_reward_ratio > 2:
                w += 0.3

            # Dynamic component: blend historical win rate (0.0-1.0)
            strat_stats = perf.get(sig.strategy_name)
            if strat_stats and strat_stats.get("total", 0) >= 5:
                win_rate = float(strat_stats.get("win_rate", 0.5))
                # Scale: win_rate 0.5 → +0.0, 1.0 → +1.0, 0.0 → -0.5
                w += (win_rate - 0.5) * 2.0
                _diag("GLOBAL", "StratWeight:Dynamic", "INFO",
                      f"{sig.strategy_name} win_rate={win_rate:.2f} "
                      f"(n={strat_stats['total']}) -> w={w:.2f}")

            weights[sig.strategy_name] = max(0.1, w)  # Floor to prevent zero/negative

        return weights
    
    @staticmethod
    def _coerce_market_condition(condition: Any) -> MarketCondition:
        """Coerce a string or MarketCondition into a MarketCondition enum.

        SRG-4 fix: external sources (ensemble, config, serialised state) may
        pass the market condition as a plain string.  Comparing a bare string
        against MarketCondition members with ``==`` always returns False, so
        the suitability lookup silently falls through.  This helper
        normalises the value to a proper enum member before any comparison.
        """
        if isinstance(condition, MarketCondition):
            return condition
        try:
            return MarketCondition(str(condition).upper())
        except (ValueError, KeyError):
            return MarketCondition.SIDEWAY

    def _adjust_for_market_condition(
        self, 
        confidence: float,
        signals: List[TradingSignal],
        condition: Any
    ) -> float:
        """Adjust confidence based on market condition"""
        condition = self._coerce_market_condition(condition)

        strategy_names = [s.strategy_name for s in signals]
        
        # Define which strategies work best in which conditions.
        # SRG-4 fix: include BULL/BEAR/SIDEWAY — the values that
        # detect_market_condition() actually returns — alongside the
        # granular aliases so every condition maps to the right set.
        condition_suitability = {
            MarketCondition.BULL: ["trend_following", "breakout"],
            MarketCondition.TRENDING_UP: ["trend_following", "breakout"],
            MarketCondition.BEAR: ["trend_following", "breakout", "scalping"],
            MarketCondition.TRENDING_DOWN: ["trend_following", "breakout", "scalping"],
            MarketCondition.SIDEWAY: ["mean_reversion", "scalping"],
            MarketCondition.RANGING: ["mean_reversion", "scalping"],
            MarketCondition.VOLATILE: ["breakout", "scalping"],
            MarketCondition.LOW_VOLUME: ["mean_reversion"],
        }
        
        suitable = condition_suitability.get(condition, [])
        matching = sum(1 for s in strategy_names if s in suitable)
        
        # If most strategies are suitable for this condition, boost confidence
        if matching >= len(signals) * 0.5:
            return min(0.95, confidence * 1.1)
        
        # If few or none match, reduce confidence
        if matching == 0:
            return confidence * 0.7
        
        return confidence
    
    def _calculate_risk_score(
        self, 
        signals: List[TradingSignal],
        market_condition: Any
    ) -> float:
        """Calculate risk score 0-100 (lower = safer)"""
        market_condition = self._coerce_market_condition(market_condition)

        score = 30  # Base risk
        
        # Check disagreement between signals
        prices = [s.price for s in signals]
        # FIX VULN-02: Added parentheses for correct operator precedence
        # Was: max - (min / mean) due to / having higher precedence
        # Now: (max - min) / mean - correct price range calculation
        price_range = (max(prices) - min(prices)) / np.mean(prices) if len(prices) > 1 else 0
        score += price_range * 100  # Price disagreement risk
        
        # Market condition risk
        if market_condition == MarketCondition.VOLATILE:
            score += 25
        elif market_condition == MarketCondition.LOW_VOLUME:
            score += 15
        
        # Low confidence risk
        avg_conf = np.mean([s.confidence for s in signals])
        if avg_conf < 0.5:
            score += 20
        elif avg_conf < 0.6:
            score += 10
        
        # Poor risk-reward risk
        avg_rr = np.mean([s.risk_reward_ratio for s in signals if s.risk_reward_ratio])
        if avg_rr < 1.5:
            score += 15
        elif avg_rr < 1.0:
            score += 25
        
        return float(min(100.0, max(0.0, float(score))))
    
    def check_risk(
        self, 
        signal: AggregatedSignal,
        portfolio: Dict[str, Any]
    ) -> SignalRiskCheck:
        """
        Check if signal passes risk management rules
        
        Args:
            signal: AggregatedSignal to check
            portfolio: Portfolio state dict with keys like 'balance', 'positions', etc.
            
        Returns:
            SignalRiskCheck with pass/fail and reasons
        """
        result = SignalRiskCheck(passed=True)
        pair = signal.symbol

        _diag(pair, "RiskCheck:Begin", "INFO",
              f"type={signal.signal_type.value}, conf={signal.combined_confidence:.3f}, "
              f"risk_score={signal.risk_score:.0f}, RR={signal.avg_risk_reward:.2f}, "
              f"strategies={list(signal.strategy_votes.keys())}")
        
        # 1. Minimum confidence check
        if signal.combined_confidence < self.risk_config["min_confidence"]:
            result.passed = False
            reason = (f"Confidence {signal.combined_confidence:.2f} below minimum "
                      f"{self.risk_config['min_confidence']}")
            result.reasons.append(reason)
            _diag(pair, "RiskCheck:Confidence", "REJECT", reason)
        else:
            _diag(pair, "RiskCheck:Confidence", "PASS",
                  f"{signal.combined_confidence:.2f} >= {self.risk_config['min_confidence']}")
        
        # 2. Risk score check
        if signal.risk_score > self.risk_config["max_risk_score"]:
            result.passed = False
            reason = (f"Risk score {signal.risk_score:.0f} exceeds maximum "
                      f"{self.risk_config['max_risk_score']}")
            result.reasons.append(reason)
            _diag(pair, "RiskCheck:RiskScore", "REJECT", reason)
        elif signal.risk_score > 50:
            result.warnings.append(f"Elevated risk score: {signal.risk_score:.0f}")
            _diag(pair, "RiskCheck:RiskScore", "PASS",
                  f"risk_score={signal.risk_score:.0f} (warning: elevated)")
        else:
            _diag(pair, "RiskCheck:RiskScore", "PASS",
                  f"risk_score={signal.risk_score:.0f} <= {self.risk_config['max_risk_score']}")
        
        # 3. Strategy agreement check (skipped in independent OR mode)
        if self.risk_config.get("independent_strategy_execution"):
            _diag(
                pair,
                "RiskCheck:StrategyAgreement",
                "PASS",
                "independent_strategy_execution — per-strategy candidates; min_agree gate skipped",
            )
        elif len(signal.strategy_votes) < self.risk_config["min_strategies_agree"]:
            result.passed = False
            reason = (f"Only {len(signal.strategy_votes)} strategies agree, need "
                      f"{self.risk_config['min_strategies_agree']}+")
            result.reasons.append(reason)
            _diag(pair, "RiskCheck:StrategyAgreement", "REJECT", reason)
        else:
            _diag(pair, "RiskCheck:StrategyAgreement", "PASS",
                  f"{len(signal.strategy_votes)} strategies agree "
                  f"(min {self.risk_config['min_strategies_agree']})")
        
        # 4. Max open positions check
        current_positions = len(self._open_positions)
        if current_positions >= self.risk_config["max_positions"]:
            result.passed = False
            reason = f"Max positions ({self.risk_config['max_positions']}) reached"
            result.reasons.append(reason)
            _diag(pair, "RiskCheck:MaxPositions", "REJECT", reason)
        else:
            _diag(pair, "RiskCheck:MaxPositions", "PASS",
                  f"{current_positions}/{self.risk_config['max_positions']} positions open")
        
        # 5. Daily trade limit
        if self._daily_trade_count >= self.risk_config["max_daily_trades"]:
            result.passed = False
            reason = f"Daily trade limit ({self.risk_config['max_daily_trades']}) reached"
            result.reasons.append(reason)
            _diag(pair, "RiskCheck:DailyLimit", "REJECT", reason)
        else:
            _diag(pair, "RiskCheck:DailyLimit", "PASS",
                  f"{self._daily_trade_count}/{self.risk_config['max_daily_trades']} trades today")
        
        # 6. Position sizing check
        balance = portfolio.get("balance", 0)
        suggested_size = self._get_position_size(signal, balance)
        current_price = signal.avg_price  # THB per 1 BTC — same units as plan entry_price
        position_value_thb = suggested_size * current_price
        if balance > 0 and position_value_thb > balance * 0.2:  # Max 20% per trade
            pct = (position_value_thb / balance * 100) if balance > 0 else 0
            result.warnings.append(
                f"Position size ({pct:.1f}% of balance) is large"
            )
            _diag(pair, "RiskCheck:PositionSize", "PASS",
                  f"WARNING — position {pct:.1f}% of balance (>20%)")
        else:
            _diag(pair, "RiskCheck:PositionSize", "PASS",
                  f"position_value={position_value_thb:.2f}, balance={balance:.2f}")
        
        # 7. Risk-reward ratio check — aligned with MIN_RISK_REWARD_RATIO from config
        # Using 1.3 as minimum (from config.py MIN_RISK_REWARD_RATIO)
        from config import MIN_RISK_REWARD_RATIO
        if signal.avg_risk_reward < MIN_RISK_REWARD_RATIO:
            result.passed = False
            reason = (f"Risk-reward ratio {signal.avg_risk_reward:.2f} below minimum "
                      f"{MIN_RISK_REWARD_RATIO}")
            result.reasons.append(reason)
            _diag(pair, "RiskCheck:RiskReward", "REJECT", reason)
        elif signal.avg_risk_reward < 1.5:
            result.warnings.append(
                f"Suboptimal risk-reward ratio: {signal.avg_risk_reward:.2f}"
            )
            _diag(pair, "RiskCheck:RiskReward", "PASS",
                  f"R:R={signal.avg_risk_reward:.2f} (warning: suboptimal, min={MIN_RISK_REWARD_RATIO})")
        else:
            _diag(pair, "RiskCheck:RiskReward", "PASS",
                  f"R:R={signal.avg_risk_reward:.2f} >= {MIN_RISK_REWARD_RATIO}")
        
        # 8. Market condition warning
        if signal.market_condition == MarketCondition.VOLATILE:
            result.warnings.append("Volatile market - increased risk")
        elif signal.market_condition == MarketCondition.LOW_VOLUME:
            result.warnings.append("Low volume - may have slippage")
        
        result.risk_score = signal.risk_score

        # ── Final verdict ──
        if result.passed:
            _diag(pair, "RiskCheck:Final", "PASS",
                  "All 8 risk gates passed — signal approved for trading")
        else:
            _diag(pair, "RiskCheck:Final", "REJECT",
                  f"Blocked by: {'; '.join(result.reasons)}")
        
        return result
    
    def _get_position_size(self, signal: AggregatedSignal, portfolio_value: float) -> float:
        """BTC (base) size implied by the same risk structure as RiskManager (warning only).

        RiskManager uses: risk_based_quantity = risk_amount / risk_per_unit (BTC),
        then notional THB = quantity * entry_price. Using ``1.0`` here incorrectly
        treated the position as 1.0 BTC, blowing up the % of balance.
        """
        entry = float(signal.avg_price or 0.0)
        sl = float(signal.avg_stop_loss or 0.0)
        if portfolio_value <= 0 or entry <= 0 or sl <= 0:
            return 0.0
        max_risk_pct = min(float(self.config.get("max_risk_per_trade_pct", 1.5)), 1.0)
        max_pos_pct = float(self.config.get("max_position_per_trade_pct", 10.0))
        hard_cap_thb = portfolio_value * (max_pos_pct / 100.0)
        risk_amount = portfolio_value * (max_risk_pct / 100.0)
        risk_per_unit = abs(entry - sl)
        if risk_per_unit <= 0:
            return 0.0
        risk_based_quantity = risk_amount / risk_per_unit
        suggested_investment_thb = min(risk_based_quantity * entry, hard_cap_thb)
        return suggested_investment_thb / entry if entry > 0 else 0.0
    
    def _reset_daily_state(self):
        """Reset daily counters if new day"""
        now = datetime.now()
        if now.date() > self._last_reset.date():
            self._daily_trade_count = 0
            self._last_reset = now
    
    def sync_state(self, open_positions_count: int, daily_trades_count: int) -> None:
        """Synchronise internal counters with real trading state.

        Must be called by the trading loop immediately before generate_signals()
        and check_risk() so that max-positions and daily-trade-limit gates
        reflect the live portfolio rather than permanently-zero defaults.

        Args:
            open_positions_count: Number of currently open positions.
            daily_trades_count:   Number of trades executed today.
        """
        # Replace the stub list with one of the correct length; only len() is used
        # downstream, so the exact content of each element is irrelevant.
        self._open_positions = [{"id": f"_synced_{i}"} for i in range(open_positions_count)]
        self._daily_trade_count = daily_trades_count

    def record_trade(self, trade: Dict[str, Any]):
        """Record completed trade for tracking"""
        self._open_positions.append(trade)
        self._daily_trade_count += 1
    
    def close_position(self, position_id: str):
        """Remove closed position from tracking"""
        self._open_positions = [
            p for p in self._open_positions if p.get("id") != position_id
        ]
    
    def get_best_signal(
        self, 
        data: pd.DataFrame, 
        symbol: str,
        portfolio: Dict[str, Any],
        use_strategies: Optional[List[str]] = None
    ) -> Optional[AggregatedSignal]:
        """
        Get the best filtered signal that passes risk checks
        """
        signals = self.generate_signals(data, symbol, use_strategies)

        if not signals:
            logger.info("[SignalDrop] %s | no aggregated signals generated", symbol)
            _diag(symbol, "GetBestSignal", "REJECT",
                  "No signals generated at all")
            return None

        rejected_summaries: List[str] = []
        for signal in signals:
            risk_result = self.check_risk(signal, portfolio)
            if risk_result.passed:
                _diag(symbol, "GetBestSignal", "PASS",
                      f"type={signal.signal_type.value}, conf={signal.combined_confidence:.3f}")
                return signal

            rejected_summaries.append(
                f"{signal.signal_type.value}@{signal.combined_confidence:.3f}: "
                f"{' ; '.join(risk_result.reasons) if risk_result.reasons else 'no explicit reason'}"
            )

        logger.warning(
            "[SignalDrop] %s | aggregated=%d | all candidates failed risk checks | %s",
            symbol,
            len(signals),
            " | ".join(rejected_summaries) if rejected_summaries else "no details",
        )
        
        _diag(symbol, "GetBestSignal", "REJECT",
              f"All {len(signals)} signal(s) failed risk checks")
        return None
    
    def get_all_filtered_signals(
        self,
        data: pd.DataFrame,
        symbol: str,
        portfolio: Dict[str, Any],
        use_strategies: Optional[List[str]] = None
    ) -> List[tuple]:
        """
        Get all signals with their risk check results
        Returns list of (signal, SignalRiskCheck) tuples
        """
        signals = self.generate_signals(data, symbol, use_strategies)
        
        results = []
        for signal in signals:
            risk_result = self.check_risk(signal, portfolio)
            results.append((signal, risk_result))
        
        # Sort by confidence
        results.sort(key=lambda x: x[0].combined_confidence, reverse=True)
        
        return results

    # ==================== Multi-Timeframe Methods ====================

    def generate_mtf_signals(
        self,
        pair: str,
        timeframes: Optional[List[str]] = None,
        db=None
    ) -> Optional[MultiTimeframeResult]:
        """
        Generate signals using multi-timeframe analysis
        
        Args:
            pair: Trading pair (e.g., 'THB_BTC')
            timeframes: List of timeframes to analyze
            db: Database instance
            
        Returns:
            MultiTimeframeResult or None if MTF not available
        """
        if not MTF_AVAILABLE:
            logger.warning("Multi-timeframe module not available")
            return None

        if timeframes is None:
            timeframes = self.config.get('multi_timeframe', {}).get(
                'timeframes', ['1m', '5m', '15m', '1h']
            )

        resolved_timeframes: List[str] = list(timeframes) if timeframes else ['1m', '5m', '15m', '1h']
        cache_key = self._make_mtf_cache_key(pair, resolved_timeframes, db)
        cached = self._get_cached_mtf_result(cache_key)
        if cached is not None:
            return cached

        # Collect data for all timeframes
        mtf_collector = MultiTimeframeCollector(pair, resolved_timeframes, db)

        # Fetch from database
        mtf_data = mtf_collector.fetch_from_db(limit=250)

        if not mtf_data or all(not d.has_data for d in mtf_data.values()):
            logger.warning(f"No data available for {pair} MTF analysis")
            self._store_cached_mtf_result(cache_key, None)
            return None

        # Use the base analyzer
        mtf_config = self.config.get('multi_timeframe', {})
        analyzer = MultiTimeframeAnalyzer(
            mtf_config,
            pair=pair,
            indicator_cache=self._mtf_indicator_cache,
            indicator_cache_lock=self._mtf_indicator_cache_lock,
            indicator_cache_ttl=self._mtf_indicator_cache_ttl,
            indicator_cache_max_size=self._mtf_indicator_cache_max_size,
        )

        # Generate result
        result = MultiTimeframeResult(
            pair=pair,
            timestamp=datetime.now(timezone.utc),
            timeframes=mtf_data
        )

        # Analyze each timeframe
        signals = analyzer.analyze(mtf_data)
        result.signals = signals

        # Aggregate
        aligned, conf, alignment = analyzer.aggregate_signals(signals)
        result.aligned_signal = aligned
        result.aligned_confidence = conf
        result.trend_alignment = alignment

        # Higher timeframe bias
        htf_trend, htf_conf = analyzer.get_higher_timeframe_bias(signals)
        result.higher_timeframe_trend = htf_trend
        result.higher_timeframe_confidence = htf_conf

        # Consensus
        if signals:
            signal_types = [s.signal_type for s in signals.values()]
            most_common = max(set(signal_types), key=signal_types.count)
            result.consensus_count = signal_types.count(most_common)
            result.consensus_strength = result.consensus_count / len(signals)

        self._store_cached_mtf_result(cache_key, result)
        return copy.deepcopy(result)

    def get_mtf_signal(
        self,
        pair: str,
        timeframes: Optional[List[str]] = None,
        portfolio: Optional[Dict[str, Any]] = None,
        db=None,
        mtf_result: Optional[MultiTimeframeResult] = None,
    ) -> Optional[TradingSignal]:
        """
        Get a trading signal from multi-timeframe analysis
        that passes risk checks
        
        Args:
            pair: Trading pair
            timeframes: Timeframes to analyze
            portfolio: Portfolio state for risk checks
            db: Database instance
            mtf_result: Optional precomputed multi-timeframe analysis result
            
        Returns:
            TradingSignal or None
        """
        if not MTF_AVAILABLE:
            return None

        if mtf_result is None:
            mtf_result = self.generate_mtf_signals(pair, timeframes, db)

        if not mtf_result:
            return None

        # Create MTF signal generator for validation
        mtf_config = self.config.get('multi_timeframe', {})
        mtf_gen = MultiTimeframeSignalGenerator(self, mtf_config)

        # Check if should trade
        should_trade, reason = mtf_gen.should_trade(mtf_result)

        if not should_trade:
            logger.debug(f"[{pair}] MTF No trade: {reason}")
            return None

        # Build signals dict for MultiTimeframeSignalGenerator
        mtf_data = mtf_result.timeframes

        # Get primary price
        primary_tf = mtf_data.get('15m') or mtf_data.get('1h')
        if not primary_tf:
            for tf_data in mtf_data.values():
                if tf_data.has_data:
                    primary_tf = tf_data
                    break

        if not primary_tf:
            return None

        price = primary_tf.latest_close

        # Calculate SL/TP
        sl_pct = mtf_config.get('stop_loss_pct', 2.0) / 100
        tp_pct = mtf_config.get('take_profit_pct', 4.0) / 100

        if mtf_result.aligned_signal == SignalType.BUY:
            stop_loss = price * (1 - sl_pct)
            take_profit = price * (1 + tp_pct)
        else:
            stop_loss = price * (1 + sl_pct)
            take_profit = price * (1 - tp_pct)

        risk = abs(price - stop_loss)
        reward = abs(take_profit - price)
        rr_ratio = reward / risk if risk > 0 else 0

        metadata = {
            'source': 'multi_timeframe',
            'timeframes_used': list(mtf_result.signals.keys()),
            'trend_alignment': mtf_result.trend_alignment,
            'consensus_strength': mtf_result.consensus_strength,
            'higher_timeframe_trend': (
                mtf_result.higher_timeframe_trend.value 
                if mtf_result.higher_timeframe_trend else None
            ),
            'htf_confidence': mtf_result.higher_timeframe_confidence,
            'signals_detail': {
                tf: {
                    'type': sig.signal_type.value,
                    'confidence': sig.confidence,
                    'trend_strength': sig.trend_strength,
                    'rsi': sig.indicators.get('rsi', 0),
                    'adx': sig.indicators.get('adx', 0),
                    'macd_hist': sig.indicators.get('macd_hist', 0),
                    'volume_ratio': sig.indicators.get('volume_ratio', 0),
                    'reason': sig.reason,
                }
                for tf, sig in mtf_result.signals.items()
            }
        }

        signal = TradingSignal(
            strategy_name='multi_timeframe',
            symbol=pair,
            signal_type=mtf_result.aligned_signal,
            confidence=mtf_result.aligned_confidence,
            price=price,
            timestamp=datetime.now(timezone.utc),
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_reward_ratio=rr_ratio,
            metadata=metadata
        )

        return signal
