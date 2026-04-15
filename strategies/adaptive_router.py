"""
Adaptive Strategy Router
Auto-switches strategy mode based on multi-dimensional market analysis.
Includes hysteresis protection to prevent rapid mode switching.
"""

import time
import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

logger = logging.getLogger("crypto-bot.adaptive_router")


@dataclass
class MarketAnalysis:
    """Multi-dimensional market analysis result."""
    symbol: str
    timestamp: datetime
    
    # Individual dimensions
    trend_direction: str  # "UP", "DOWN", "SIDEWAYS"
    trend_strength: float  # 0-100 (ADX)
    volatility_pct: float  # ATR as % of close price
    volume_ratio: float  # current_volume / avg_volume
    btc_correlation: float  # -1 to 1
    
    # Derived metrics
    market_condition: str  # "STRONG_UP", "WEAK_UP", "STRONG_DOWN", "VOLATILE", "RANGING"
    
    # Metadata for diagnostics
    adx_value: float = 0.0
    atr_value: float = 0.0
    ema_50: float = 0.0
    ema_200: float = 0.0
    current_price: float = 0.0
    avg_volume_20d: float = 0.0
    current_volume: float = 0.0
    btc_price: float = 0.0
    
    def __repr__(self) -> str:
        return (
            f"MarketAnalysis({self.symbol}): "
            f"Trend={self.trend_direction}(ADX={self.trend_strength:.1f}), "
            f"Vol={self.volatility_pct:.2f}%, "
            f"VolumeRatio={self.volume_ratio:.2f}, "
            f"BTC_Corr={self.btc_correlation:.2f}, "
            f"Condition={self.market_condition}"
        )


@dataclass
class ModeDecision:
    """Result of auto mode switching decision."""
    recommended_mode: str
    reasoning: str
    market_analysis: MarketAnalysis
    should_switch: bool
    switch_reason: str = ""
    confidence: float = 0.0


class AdaptiveStrategyRouter:
    """
    Routes strategy mode based on market conditions.
    Analyzes: trend strength (ADX), volatility (ATR%), volume ratio, BTC correlation.
    """
    
    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        db: Optional[Any] = None,
        api_client: Optional[Any] = None,
    ):
        self.config = config or {}
        self.db = db
        self.api_client = api_client
        
        # Auto-switch config
        auto_switch_cfg = self.config.get("auto_mode_switch", {})
        self.enabled = auto_switch_cfg.get("enabled", False)
        self.check_interval_seconds = auto_switch_cfg.get("check_interval_seconds", 300)
        self.min_switch_interval_seconds = auto_switch_cfg.get("min_switch_interval_seconds", 1800)
        self.persistence_threshold = auto_switch_cfg.get("persistence_threshold", 3)
        
        # Market analysis config
        market_cfg = self.config.get("market_analysis", {})
        self.adx_thresholds = market_cfg.get("adx_thresholds", {
            "strong_trend": 40,
            "weak_trend": 25,
        })
        self.volatility_thresholds = market_cfg.get("volatility_thresholds", {
            "high_pct": 3.0,
            "low_pct": 1.0,
        })
        self.volume_thresholds = market_cfg.get("volume_thresholds", {
            "high_ratio": 1.5,
            "low_ratio": 0.7,
        })
        
        # BTC correlation config
        btc_cfg = self.config.get("btc_correlation", {})
        self.btc_correlation_enabled = btc_cfg.get("enabled", True)
        self.btc_lookback_bars = btc_cfg.get("lookback_bars", 100)
        self.btc_min_correlation_strong = btc_cfg.get("min_correlation_strong", 0.7)
        self.btc_min_correlation_moderate = btc_cfg.get("min_correlation_moderate", 0.5)
        
        # State tracking
        self._last_check_time = 0.0
        self._last_switch_time = 0.0
        self._current_mode = "standard"
        self._decision_history: List[str] = []  # Last N mode recommendations
        self._last_analysis: Optional[MarketAnalysis] = None
        self._btc_correlation_warning_keys: set[str] = set()
        
        if not self.enabled:
            logger.info("[AdaptiveRouter] Auto mode switching is DISABLED in config")
        else:
            logger.info("[AdaptiveRouter] Auto mode switching ENABLED")
            logger.info(
                f"  Check interval: {self.check_interval_seconds}s, "
                f"Min switch interval: {self.min_switch_interval_seconds}s, "
                f"Persistence threshold: {self.persistence_threshold}"
            )

    def set_current_mode(self, mode: str) -> None:
        """Update the current mode state."""
        self._current_mode = str(mode or "standard").lower()

    def _warn_btc_correlation_issue(self, issue_key: str, message: str, *args: Any) -> None:
        if issue_key in self._btc_correlation_warning_keys:
            return
        self._btc_correlation_warning_keys.add(issue_key)
        logger.warning(message, *args)

    def analyze_market_dimensions(
        self,
        symbol: str,
        timeframe: str,
        data: Optional[Any] = None,
    ) -> Optional[MarketAnalysis]:
        """
        Analyze market in multiple dimensions.
        Returns MarketAnalysis or None if data unavailable.
        
        Requires data with OHLCV columns and sufficient history for indicators.
        """
        if data is None or len(data) < 100:
            logger.debug(f"[AdaptiveRouter] Insufficient data for {symbol}: {len(data) if data is not None else 0} bars")
            return None
        
        try:
            from indicators import TechnicalIndicators
        except ImportError:
            logger.warning("[AdaptiveRouter] TechnicalIndicators import failed")
            return None
        
        try:
            # Extract OHLCV
            close = data['close']
            high = data['high']
            low = data['low']
            volume = data.get('volume', None)
            
            current_price = float(close.iloc[-1])
            current_volume = float(volume.iloc[-1]) if volume is not None else 0.0
            
            # Calculate indicators (use pandas Series methods)
            adx = TechnicalIndicators.calculate_adx(high, low, close, period=14)
            atr = TechnicalIndicators.calculate_atr(high, low, close, period=14)
            ema_50 = TechnicalIndicators.calculate_ema(close, period=50)
            ema_200 = TechnicalIndicators.calculate_ema(close, period=200)
            
            # Latest values
            current_adx = float(adx.iloc[-1]) if not adx.empty else 25.0
            current_atr = float(atr.iloc[-1]) if not atr.empty else 0.0
            current_ema50 = float(ema_50.iloc[-1]) if not ema_50.empty else current_price
            current_ema200 = float(ema_200.iloc[-1]) if not ema_200.empty else current_price
            
            # Volatility as ATR %
            volatility_pct = (current_atr / current_price * 100) if current_price > 0 else 0.0
            
            # Volume ratio (current vs 20-day average)
            if volume is not None and len(volume) >= 20:
                avg_volume_20d = float(volume.iloc[-20:].mean())
                volume_ratio = (current_volume / avg_volume_20d) if avg_volume_20d > 0 else 1.0
            else:
                avg_volume_20d = current_volume
                volume_ratio = 1.0
            
            # Trend direction based on EMA
            if current_ema50 > current_ema200:
                trend_direction = "UP"
            elif current_ema50 < current_ema200:
                trend_direction = "DOWN"
            else:
                trend_direction = "SIDEWAYS"
            
            # Trend strength from ADX
            trend_strength = current_adx
            
            # BTC correlation (if enabled)
            btc_correlation = 0.0
            btc_price = 0.0
            if self.btc_correlation_enabled and self.api_client:
                try:
                    btc_close = self._get_btc_price_series(
                        lookback_bars=self.btc_lookback_bars,
                        timeframe=timeframe,
                    )
                    if btc_close is not None and len(btc_close) >= 2:
                        # Normalize to same length
                        min_len = min(len(close), len(btc_close))
                        local_close = close.iloc[-min_len:].reset_index(drop=True)
                        btc_close_series = local_close.copy()
                        btc_close_series[:] = btc_close[-min_len:]

                        corr_value = float(local_close.corr(btc_close_series))
                        if math.isfinite(corr_value):
                            btc_correlation = corr_value
                        else:
                            self._warn_btc_correlation_issue(
                                f"nonfinite:{timeframe}",
                                "[AdaptiveRouter] BTC correlation unavailable for %s: non-finite correlation on timeframe %s",
                                symbol,
                                timeframe,
                            )
                            btc_correlation = 0.0
                        btc_price = float(btc_close[-1])
                    elif self.btc_correlation_enabled:
                        self._warn_btc_correlation_issue(
                            f"insufficient:{timeframe}",
                            "[AdaptiveRouter] BTC correlation unavailable for %s: insufficient THB_BTC history on timeframe %s",
                            symbol,
                            timeframe,
                        )
                except Exception as e:
                    self._warn_btc_correlation_issue(
                        f"exception:{timeframe}",
                        "[AdaptiveRouter] BTC correlation calculation failed for %s on %s: %s",
                        symbol,
                        timeframe,
                        e,
                    )
                    btc_correlation = 0.0
            
            # Classify market condition
            market_condition = self._classify_condition(
                trend_direction, trend_strength, volatility_pct, volume_ratio
            )
            
            analysis = MarketAnalysis(
                symbol=symbol,
                timestamp=datetime.now(),
                trend_direction=trend_direction,
                trend_strength=trend_strength,
                volatility_pct=volatility_pct,
                volume_ratio=volume_ratio,
                btc_correlation=btc_correlation,
                market_condition=market_condition,
                adx_value=current_adx,
                atr_value=current_atr,
                ema_50=current_ema50,
                ema_200=current_ema200,
                current_price=current_price,
                avg_volume_20d=avg_volume_20d,
                current_volume=current_volume,
                btc_price=btc_price,
            )
            
            self._last_analysis = analysis
            logger.debug(f"[AdaptiveRouter] Analyzed {symbol}: {analysis}")
            return analysis
            
        except Exception as e:
            logger.error(f"[AdaptiveRouter] Market analysis failed for {symbol}: {e}", exc_info=True)
            return None

    def _get_btc_price_series(self, lookback_bars: int, timeframe: str = "15m") -> Optional[List[float]]:
        """Fetch BTC close-price history for correlation calculations."""
        if not self.api_client:
            return None
        try:
            candles = self.api_client.get_candle(
                symbol="THB_BTC",
                timeframe=str(timeframe or "15m"),
                limit=max(int(lookback_bars or 2), 2),
            )
            if not isinstance(candles, dict) or candles.get("error") not in (0, None):
                self._warn_btc_correlation_issue(
                    f"payload:{timeframe}",
                    "[AdaptiveRouter] BTC correlation unavailable: malformed THB_BTC candle response for timeframe %s",
                    timeframe,
                )
                return None

            rows = candles.get("result") or []
            closes: List[float] = []
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) < 5:
                    continue
                try:
                    close_value = float(row[4])
                except (TypeError, ValueError):
                    continue
                if math.isfinite(close_value):
                    closes.append(close_value)
            if len(closes) < 2:
                self._warn_btc_correlation_issue(
                    f"closes:{timeframe}",
                    "[AdaptiveRouter] BTC correlation unavailable: THB_BTC candle series too short on timeframe %s",
                    timeframe,
                )
                return None
            return closes
        except Exception as e:
            self._warn_btc_correlation_issue(
                f"fetch:{timeframe}",
                "[AdaptiveRouter] BTC correlation unavailable: THB_BTC candle fetch failed on timeframe %s",
                timeframe,
            )
            return None

    def _classify_condition(
        self,
        trend: str,
        adx: float,
        volatility_pct: float,
        volume_ratio: float,
    ) -> str:
        """Classify market condition based on multiple factors."""
        strong_adx_threshold = self.adx_thresholds["strong_trend"]
        weak_adx_threshold = self.adx_thresholds["weak_trend"]
        high_vol = self.volatility_thresholds["high_pct"]
        low_vol = self.volatility_thresholds["low_pct"]
        high_vol_ratio = self.volume_thresholds["high_ratio"]
        
        # Classify by trend strength and direction
        if adx > strong_adx_threshold:
            if trend == "UP":
                return "STRONG_UP"
            elif trend == "DOWN":
                return "STRONG_DOWN"
            else:
                return "STRONG_RANGING"
        elif adx > weak_adx_threshold:
            if trend == "UP":
                return "WEAK_UP"
            elif trend == "DOWN":
                return "WEAK_DOWN"
            else:
                return "WEAK_RANGING"
        else:
            if volatility_pct > high_vol and volume_ratio > high_vol_ratio:
                return "VOLATILE"
            else:
                return "RANGING"

    def classify_market_and_recommend_mode(
        self,
        analysis: MarketAnalysis,
    ) -> str:
        """
        Classify market and recommend strategy mode.
        
        Returns: "standard", "trend_only", "scalping", "sniper"
        """
        trend = analysis.trend_direction
        adx = analysis.trend_strength
        volatility = analysis.volatility_pct
        volume_ratio = analysis.volume_ratio
        condition = analysis.market_condition
        
        strong_adx = self.adx_thresholds["strong_trend"]
        weak_adx = self.adx_thresholds["weak_trend"]
        high_vol = self.volatility_thresholds["high_pct"]
        
        # Mode selection logic
        reasoning = ""
        
        # TREND_ONLY: Strong trend detected
        if adx > strong_adx:
            if trend == "UP":
                return "trend_only"
            elif trend == "DOWN":
                # Strong downtrend = high volatility opportunity but risky
                # Could use scalping for shorts or trend_only with tight stops
                return "trend_only"
        
        # SCALPING: High volatility + high volume + no clear trend (or trending)
        if volatility > high_vol and volume_ratio > 1.3 and adx < strong_adx:
            return "scalping"
        
        # SNIPER: Low volatility + weakly ranging
        if volatility < self.volatility_thresholds["low_pct"] and adx < weak_adx:
            return "sniper"
        
        # STANDARD: Default multi-strategy approach
        return "standard"

    def should_switch_mode(self, new_mode: str) -> bool:
        """
        Check if should switch to new_mode given hysteresis constraints.
        
        Returns True only if:
        1. New mode differs from current
        2. Cooldown period (min_switch_interval) has elapsed
        3. Recommendation has persisted for persistence_threshold checks
        """
        if new_mode == self._current_mode:
            return False
        
        now = time.time()
        
        # Check cooldown
        if now - self._last_switch_time < self.min_switch_interval_seconds:
            elapsed = now - self._last_switch_time
            remaining = self.min_switch_interval_seconds - elapsed
            logger.debug(
                f"[AdaptiveRouter] Mode switch blocked by cooldown. "
                f"Last switch {elapsed:.0f}s ago, need {remaining:.0f}s more"
            )
            return False
        
        # Check persistence
        self._decision_history.append(new_mode)
        # Keep only recent history
        if len(self._decision_history) > max(self.persistence_threshold * 2, 10):
            self._decision_history = self._decision_history[-10:]
        
        # Count how many recent decisions agree with new_mode
        recent_count = self._decision_history[-self.persistence_threshold:]
        agreement_count = sum(1 for decision in recent_count if decision == new_mode)
        
        if agreement_count >= self.persistence_threshold:
            logger.info(
                f"[AdaptiveRouter] Persistence check PASSED: "
                f"{agreement_count}/{self.persistence_threshold} checks agree on mode={new_mode}"
            )
            return True
        else:
            logger.debug(
                f"[AdaptiveRouter] Persistence check FAILED: "
                f"only {agreement_count}/{self.persistence_threshold} checks agree on mode={new_mode}"
            )
            return False

    def auto_switch_mode(self, symbol: str, data: Optional[Any] = None) -> ModeDecision:
        """
        Main entry point for auto mode switching.
        
        Analyzes market, classifies condition, checks hysteresis, recommends mode switch.
        
        Returns ModeDecision with recommendation and switch flag.
        """
        if not self.enabled:
            return ModeDecision(
                recommended_mode=self._current_mode,
                reasoning="Auto mode switching is disabled",
                market_analysis=None,
                should_switch=False,
            )
        
        now = time.time()
        
        # Skip if checked too recently
        if now - self._last_check_time < self.check_interval_seconds:
            return ModeDecision(
                recommended_mode=self._current_mode,
                reasoning=f"Check interval not elapsed ({self.check_interval_seconds}s)",
                market_analysis=self._last_analysis,
                should_switch=False,
            )
        
        self._last_check_time = now
        
        # Analyze market
        analysis = self.analyze_market_dimensions(symbol, "15m", data)
        if analysis is None:
            return ModeDecision(
                recommended_mode=self._current_mode,
                reasoning="Could not analyze market (insufficient data)",
                market_analysis=None,
                should_switch=False,
            )
        
        # Get recommendation
        recommended_mode = self.classify_market_and_recommend_mode(analysis)
        
        # Check if should switch
        should_switch = self.should_switch_mode(recommended_mode)
        
        # Build decision
        reasoning = f"{analysis.market_condition} condition → recommending {recommended_mode}"
        
        decision = ModeDecision(
            recommended_mode=recommended_mode,
            reasoning=reasoning,
            market_analysis=analysis,
            should_switch=should_switch,
            switch_reason="Hysteresis check passed" if should_switch else "Hysteresis protection active",
            confidence=min(analysis.trend_strength / 100.0, 1.0),
        )
        
        # Log decision
        if should_switch:
            logger.warning(
                f"[AdaptiveRouter] MODE SWITCH: {self._current_mode} → {recommended_mode} | {reasoning}"
            )
            self._last_switch_time = now
            self._current_mode = recommended_mode
        else:
            logger.debug(f"[AdaptiveRouter] {reasoning} (no switch)")
        
        return decision
