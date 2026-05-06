"""
Single load of signal / strategy knobs per generator instance (strategy domain).
Keeps aggregation gates and MTF caches aligned without scattered config lookups.
"""

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class StrategyRuntimeConfig:
    min_confidence: float
    max_risk_score: float
    min_strategies_agree: int
    max_positions: int
    max_daily_trades: int
    independent_strategy_execution: bool
    strategy_perf_lookback_days: int
    signal_cache_ttl: float
    signal_cache_max_size: int
    mtf_cache_ttl_seconds: float
    mtf_cache_max_size: int
    mtf_indicator_cache_ttl_seconds: float
    mtf_indicator_cache_max_size: int

    @classmethod
    def from_bot_config(cls, config: Dict[str, Any]) -> "StrategyRuntimeConfig":
        cfg = dict(config or {})
        strategies_top = dict(cfg.get("strategies", {}) or {})
        mtf = dict(cfg.get("multi_timeframe", {}) or {})

        return cls(
            # Read from strategies sub-dict (where these keys live in the YAML)
            min_confidence=float(strategies_top.get("min_confidence", 0.40)),
            max_risk_score=float(cfg.get("max_risk_score", 70)),
            min_strategies_agree=int(strategies_top.get("min_strategies_agree", 1)),
            max_positions=int(cfg.get("max_open_positions", 3)),
            max_daily_trades=int(cfg.get("max_daily_trades", 10)),
            independent_strategy_execution=bool(strategies_top.get("independent_strategy_execution", False)),
            strategy_perf_lookback_days=int(cfg.get("strategy_perf_lookback_days", 30)),
            signal_cache_ttl=float(cfg.get("signal_cache_ttl", 30.0) or 30.0),
            signal_cache_max_size=int(cfg.get("signal_cache_max_size", 100) or 100),
            mtf_cache_ttl_seconds=float(mtf.get("cache_ttl_seconds", 10.0) or 10.0),
            mtf_cache_max_size=int(mtf.get("cache_max_size", 100) or 100),
            mtf_indicator_cache_ttl_seconds=float(mtf.get("indicator_cache_ttl_seconds", 300.0) or 300.0),
            mtf_indicator_cache_max_size=int(mtf.get("indicator_cache_max_size", 500) or 500),
        )
