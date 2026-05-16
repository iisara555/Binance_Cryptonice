"""Multi-timeframe analysis utilities backed by the prices table."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from indicators import TechnicalIndicators
from strategy_base import SignalType

logger = logging.getLogger(__name__)


def _normalize_timestamp_series(series: pd.Series) -> pd.Series:
    """Convert mixed naive/aware timestamps into consistently naive UTC values."""
    normalized = pd.to_datetime(series, errors="coerce", utc=True)
    if hasattr(normalized.dt, "tz_localize"):
        normalized = normalized.dt.tz_localize(None)
    return normalized


class Timeframe(Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


TIMEFRAME_MINUTES = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}


@dataclass
class TimeframeData:
    """OHLCV data snapshot for a specific timeframe."""

    timeframe: str
    candles: pd.DataFrame = field(default_factory=pd.DataFrame)
    latest_close: float = 0.0
    latest_timestamp: Optional[datetime] = None
    candle_count: int = 0
    has_data: bool = False

    def __post_init__(self) -> None:
        if self.candles is None or self.candles.empty:
            self.candles = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
            self.has_data = False
            self.candle_count = 0
            self.latest_close = 0.0
            self.latest_timestamp = None
            return

        candles = self.candles.copy()
        if "timestamp" in candles.columns:
            candles["timestamp"] = _normalize_timestamp_series(candles["timestamp"])
            candles = candles.dropna(subset=["timestamp"])
        candles = candles.sort_values("timestamp").reset_index(drop=True)
        self.candles = candles
        self.candle_count = len(candles)
        self.has_data = self.candle_count > 0
        if self.has_data:
            latest = candles.iloc[-1]
            self.latest_close = float(latest.get("close") or 0.0)
            self.latest_timestamp = latest.get("timestamp")


@dataclass
class TimeframeSignal:
    """Signal generated from one timeframe."""

    timeframe: str
    signal_type: SignalType
    confidence: float
    trend_strength: float = 0.0
    indicators: Dict[str, float] = field(default_factory=dict)
    reason: str = ""


@dataclass
class MultiTimeframeResult:
    """Combined result from multiple timeframes."""

    pair: str
    timestamp: datetime
    timeframes: Dict[str, TimeframeData]
    signals: Dict[str, TimeframeSignal] = field(default_factory=dict)
    aligned_signal: SignalType = SignalType.HOLD
    aligned_confidence: float = 0.0
    trend_alignment: float = 0.0
    higher_timeframe_trend: Optional[SignalType] = None
    higher_timeframe_confidence: float = 0.0
    consensus_count: int = 0
    consensus_strength: float = 0.0


class MultiTimeframeCollector:
    """Read multiple timeframe candles from the database."""

    def __init__(self, pair: str, timeframes: List[str], db=None):
        self.pair = str(pair or "").upper()
        self.timeframes = [str(timeframe).strip() for timeframe in (timeframes or []) if str(timeframe).strip()]
        self.db = db

    def collect(self, pair: str, timeframes: List[str]) -> bool:
        self.pair = str(pair or self.pair).upper()
        self.timeframes = [
            str(timeframe).strip() for timeframe in (timeframes or self.timeframes) if str(timeframe).strip()
        ]
        return bool(self.fetch_from_db())

    def fetch_from_db(self, limit: int = 100) -> Dict[str, TimeframeData]:
        results: Dict[str, TimeframeData] = {}
        for timeframe in self.timeframes:
            df = self._get_timeframe_df(timeframe, limit)
            results[timeframe] = TimeframeData(timeframe=timeframe, candles=df)
        return results

    def _get_timeframe_df(self, timeframe: str, limit: int) -> pd.DataFrame:
        if self.db is None or not hasattr(self.db, "get_candles"):
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        try:
            candles = self.db.get_candles(self.pair, interval=timeframe, limit=limit)
        except Exception as exc:
            logger.debug("Failed to load %s candles for %s: %s", timeframe, self.pair, exc)
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        if candles is None or candles.empty:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        frame = candles.copy()
        expected_columns = ["timestamp", "open", "high", "low", "close", "volume"]
        missing = [column for column in expected_columns if column not in frame.columns]
        for column in missing:
            frame[column] = 0.0
        frame["timestamp"] = _normalize_timestamp_series(frame["timestamp"])
        frame = frame.dropna(subset=["timestamp"])
        return frame[expected_columns].sort_values("timestamp").reset_index(drop=True)


class MultiTimeframeAnalyzer:
    """Analyze multiple timeframes and aggregate the result."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        pair: str = "",
        indicator_cache: Optional[Dict[Tuple[Any, ...], Tuple[float, Dict[str, float]]]] = None,
        indicator_cache_lock: Optional[threading.Lock] = None,
        indicator_cache_ttl: float = 0.0,
        indicator_cache_max_size: int = 0,
    ):
        self.config = dict(config or {})
        self.pair = str(pair or "").upper()
        self.alignment_threshold = float(self.config.get("alignment_threshold", 0.6) or 0.6)
        self.tf_weights = {
            str(timeframe): float(weight) for timeframe, weight in (self.config.get("tf_weights") or {}).items()
        }
        self.higher_timeframes = [
            str(timeframe).strip()
            for timeframe in (self.config.get("higher_timeframes") or ["1h", "4h", "1d"])
            if str(timeframe).strip()
        ]
        self._indicator_cache = indicator_cache
        self._indicator_cache_lock = indicator_cache_lock
        self._indicator_cache_ttl = max(float(indicator_cache_ttl or 0.0), 0.0)
        self._indicator_cache_max_size = max(int(indicator_cache_max_size or 0), 0)

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return float(default)
        if pd.isna(numeric):
            return float(default)
        return numeric

    def _select_indicator_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        if len(frame) >= 36:
            return frame.iloc[:-1].copy()
        return frame.copy()

    def _make_indicator_cache_key(self, timeframe: str, frame: pd.DataFrame) -> Optional[Tuple[Any, ...]]:
        if frame.empty:
            return None
        last_row = frame.iloc[-1]
        return (
            self.pair,
            str(timeframe),
            last_row.get("timestamp"),
            len(frame),
            self._safe_float(last_row.get("close")),
            self._safe_float(last_row.get("volume"), 1.0),
        )

    def _get_cached_indicator_snapshot(self, cache_key: Optional[Tuple[Any, ...]]) -> Optional[Dict[str, float]]:
        if cache_key is None or self._indicator_cache is None or self._indicator_cache_lock is None:
            return None
        now = datetime.now(timezone.utc).timestamp()
        with self._indicator_cache_lock:
            cached = self._indicator_cache.get(cache_key)
            if not cached:
                return None
            cached_at, snapshot = cached
            if self._indicator_cache_ttl > 0 and now - cached_at >= self._indicator_cache_ttl:
                self._indicator_cache.pop(cache_key, None)
                return None
            return dict(snapshot)

    def _store_cached_indicator_snapshot(
        self,
        cache_key: Optional[Tuple[Any, ...]],
        snapshot: Dict[str, float],
    ) -> Dict[str, float]:
        if cache_key is None or self._indicator_cache is None or self._indicator_cache_lock is None:
            return snapshot
        with self._indicator_cache_lock:
            if self._indicator_cache_max_size > 0 and len(self._indicator_cache) >= self._indicator_cache_max_size:
                oldest_key = min(self._indicator_cache.items(), key=lambda item: item[1][0])[0]
                self._indicator_cache.pop(oldest_key, None)
            self._indicator_cache[cache_key] = (datetime.now(timezone.utc).timestamp(), dict(snapshot))
        return snapshot

    def _compute_indicator_snapshot(self, frame: pd.DataFrame) -> Dict[str, float]:
        close = frame["close"].astype(float)
        high = frame["high"].astype(float)
        low = frame["low"].astype(float)
        volume = frame["volume"].astype(float)

        ema_fast = close.ewm(span=9, adjust=False).mean().iloc[-1]
        ema_slow = close.ewm(span=21, adjust=False).mean().iloc[-1]
        rsi = TechnicalIndicators.calculate_rsi(close).iloc[-1]
        macd, macd_signal, macd_hist = TechnicalIndicators.calculate_macd(close)
        adx = TechnicalIndicators.calculate_adx(high, low, close).iloc[-1]
        atr = TechnicalIndicators.calculate_atr(high, low, close).iloc[-1]
        volume_ma = volume.rolling(window=20).mean().iloc[-1]

        return {
            "ema_fast": self._safe_float(ema_fast),
            "ema_slow": self._safe_float(ema_slow),
            "rsi": self._safe_float(rsi, 50.0),
            "macd": self._safe_float(macd.iloc[-1]),
            "macd_signal": self._safe_float(macd_signal.iloc[-1]),
            "macd_hist": self._safe_float(macd_hist.iloc[-1]),
            "adx": self._safe_float(adx),
            "atr": self._safe_float(atr),
            "volume_ma": self._safe_float(volume_ma),
        }

    def _get_indicator_snapshot(self, timeframe: str, frame: pd.DataFrame) -> Dict[str, float]:
        indicator_frame = self._select_indicator_frame(frame)
        cache_key = self._make_indicator_cache_key(timeframe, indicator_frame)
        cached = self._get_cached_indicator_snapshot(cache_key)
        if cached is not None:
            return cached
        snapshot = self._compute_indicator_snapshot(indicator_frame)
        return self._store_cached_indicator_snapshot(cache_key, snapshot)

    def analyze(self, data: Dict[str, TimeframeData]) -> Dict[str, TimeframeSignal]:
        signals: Dict[str, TimeframeSignal] = {}
        for timeframe, timeframe_data in (data or {}).items():
            signals[timeframe] = self._analyze_timeframe(timeframe, timeframe_data)
        return signals

    def aggregate_signals(self, signals: Dict[str, TimeframeSignal]) -> Tuple[SignalType, float, float]:
        actionable = {
            timeframe: signal
            for timeframe, signal in (signals or {}).items()
            if signal.signal_type in (SignalType.BUY, SignalType.SELL)
        }
        if not actionable:
            return SignalType.HOLD, 0.0, 0.0

        buy_score = 0.0
        sell_score = 0.0
        total_score = 0.0
        for timeframe, signal in actionable.items():
            weight = self._get_weight(timeframe)
            weighted_score = max(signal.confidence, 0.0) * weight
            total_score += weighted_score
            if signal.signal_type == SignalType.BUY:
                buy_score += weighted_score
            elif signal.signal_type == SignalType.SELL:
                sell_score += weighted_score

        if total_score <= 0:
            return SignalType.HOLD, 0.0, 0.0

        aligned_signal = SignalType.BUY if buy_score >= sell_score else SignalType.SELL
        dominant_score = max(buy_score, sell_score)
        confidence = dominant_score / total_score
        alignment = confidence

        if alignment < self.alignment_threshold:
            return SignalType.HOLD, confidence, alignment

        return aligned_signal, confidence, alignment

    def get_higher_timeframe_bias(self, signals: Dict[str, TimeframeSignal]) -> Tuple[Optional[SignalType], float]:
        relevant = [
            signal
            for timeframe, signal in (signals or {}).items()
            if timeframe in self.higher_timeframes and signal.signal_type in (SignalType.BUY, SignalType.SELL)
        ]
        if not relevant:
            return None, 0.0

        buy_score = sum(signal.confidence for signal in relevant if signal.signal_type == SignalType.BUY)
        sell_score = sum(signal.confidence for signal in relevant if signal.signal_type == SignalType.SELL)
        total = buy_score + sell_score
        if total <= 0:
            return None, 0.0

        signal_type = SignalType.BUY if buy_score >= sell_score else SignalType.SELL
        confidence = max(buy_score, sell_score) / total
        return signal_type, confidence

    def _analyze_timeframe(self, timeframe: str, data: TimeframeData) -> TimeframeSignal:
        if not data or not data.has_data or data.candle_count < 35:
            return TimeframeSignal(
                timeframe=timeframe,
                signal_type=SignalType.HOLD,
                confidence=0.0,
                reason="insufficient_data",
            )

        frame = data.candles
        close = frame["close"].astype(float)
        volume = frame["volume"].astype(float)
        indicators = self._get_indicator_snapshot(timeframe, frame)

        ema_fast = indicators.get("ema_fast", 0.0)
        ema_slow = indicators.get("ema_slow", 0.0)
        rsi = indicators.get("rsi", 50.0)
        adx = indicators.get("adx", 0.0)
        atr = indicators.get("atr", 0.0)
        latest_macd_hist = indicators.get("macd_hist", 0.0)
        volume_ma = indicators.get("volume_ma", 0.0)
        latest_close = float(close.iloc[-1])
        volume_ratio = (
            float(volume.iloc[-1] / volume_ma) if volume_ma and not pd.isna(volume_ma) and volume_ma > 0 else 1.0
        )

        trend_strength = float(adx) if adx else 0.0
        signal_type = SignalType.HOLD
        reason = "neutral"

        bullish = latest_close > ema_slow and ema_fast > ema_slow and rsi >= 55 and latest_macd_hist >= 0
        bearish = latest_close < ema_slow and ema_fast < ema_slow and rsi <= 45 and latest_macd_hist <= 0

        if bullish:
            signal_type = SignalType.BUY
            reason = "bullish_alignment"
        elif bearish:
            signal_type = SignalType.SELL
            reason = "bearish_alignment"

        trend_component = min(abs(ema_fast - ema_slow) / max(abs(latest_close), 1e-9) * 12.0, 0.3)
        rsi_component = min(abs(float(rsi) - 50.0) / 50.0 * 0.35, 0.35)
        macd_component = min(abs(latest_macd_hist) / max(abs(latest_close), 1e-9) * 800.0, 0.2)
        volume_component = min(max(volume_ratio - 1.0, 0.0) * 0.1, 0.1)
        confidence = min(0.95, 0.35 + trend_component + rsi_component + macd_component + volume_component)
        if signal_type == SignalType.HOLD:
            confidence = min(confidence, 0.49)

        indicators = {
            "rsi": float(rsi),
            "ema_fast": float(ema_fast),
            "ema_slow": float(ema_slow),
            "macd": float(indicators.get("macd", 0.0)),
            "macd_signal": float(indicators.get("macd_signal", 0.0)),
            "macd_hist": latest_macd_hist,
            "adx": float(adx),
            "atr": float(atr),
            "volume_ratio": float(volume_ratio),
        }
        return TimeframeSignal(
            timeframe=timeframe,
            signal_type=signal_type,
            confidence=float(confidence),
            trend_strength=trend_strength,
            indicators=indicators,
            reason=reason,
        )

    def _get_weight(self, timeframe: str) -> float:
        configured = self.tf_weights.get(str(timeframe))
        if configured is not None:
            return max(float(configured), 0.01)
        minutes = TIMEFRAME_MINUTES.get(str(timeframe), 60)
        return max(minutes / 60.0, 0.1)


class MultiTimeframeSignalGenerator:
    """Decide whether aggregated multi-timeframe signals are tradeable."""

    def __init__(self, base_generator=None, config: Optional[Dict[str, Any]] = None):
        self.base_generator = base_generator
        self.config = dict(config or {})
        self.alignment_threshold = float(self.config.get("alignment_threshold", 0.6) or 0.6)
        self.require_htf_confirmation = bool(self.config.get("require_htf_confirmation", False))

    def should_trade(self, data: Optional[MultiTimeframeResult]) -> Tuple[bool, str]:
        if data is None:
            return False, "No multi-timeframe data"
        if data.aligned_signal == SignalType.HOLD:
            return False, "No aligned multi-timeframe signal"
        if data.aligned_confidence < self.alignment_threshold:
            return False, (
                f"Alignment confidence {data.aligned_confidence:.2f} below threshold {self.alignment_threshold:.2f}"
            )
        if self.require_htf_confirmation:
            if data.higher_timeframe_trend is None:
                return False, "Higher timeframe confirmation unavailable"
            if data.higher_timeframe_trend != data.aligned_signal:
                return False, "Higher timeframe bias disagrees with aligned signal"
        return True, "Aligned across configured timeframes"

    def generate(self, data: Dict[str, TimeframeData]) -> Optional[MultiTimeframeResult]:
        if not data:
            return None
        analyzer = MultiTimeframeAnalyzer(self.config)
        result = MultiTimeframeResult(pair="", timestamp=datetime.now(timezone.utc), timeframes=data)
        result.signals = analyzer.analyze(data)
        result.aligned_signal, result.aligned_confidence, result.trend_alignment = analyzer.aggregate_signals(
            result.signals
        )
        result.higher_timeframe_trend, result.higher_timeframe_confidence = analyzer.get_higher_timeframe_bias(
            result.signals
        )
        return result
