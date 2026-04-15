"""Tests for Adaptive Strategy Router."""

import logging
import pytest
import pandas as pd
import numpy as np
from datetime import datetime
from unittest.mock import Mock
from strategies.adaptive_router import AdaptiveStrategyRouter, MarketAnalysis, ModeDecision


@pytest.fixture
def sample_config():
    """Create sample config for router."""
    return {
        "auto_mode_switch": {
            "enabled": True,
            "check_interval_seconds": 60,
            "min_switch_interval_seconds": 300,
            "persistence_threshold": 2,
        },
        "market_analysis": {
            "adx_thresholds": {
                "strong_trend": 40,
                "weak_trend": 25,
            },
            "volatility_thresholds": {
                "high_pct": 3.0,
                "low_pct": 1.0,
            },
            "volume_thresholds": {
                "high_ratio": 1.5,
                "low_ratio": 0.7,
            },
        },
        "btc_correlation": {
            "enabled": False,
        },
    }


@pytest.fixture
def router(sample_config):
    """Create a router instance."""
    return AdaptiveStrategyRouter(config=sample_config)


@pytest.fixture
def sample_ohlcv_data():
    """Create sample OHLCV data for testing."""
    n_bars = 250
    base_price = 100.0
    dates = pd.date_range(start="2024-01-01", periods=n_bars, freq="1H")
    
    # Create synthetic uptrend with increasing volatility
    trend = np.linspace(0, 5, n_bars)
    noise = np.random.normal(0, 0.5, n_bars)
    prices = base_price + trend + noise
    
    data = pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.uniform(-0.5, 0, n_bars),
        "high": prices + np.random.uniform(0, 1, n_bars),
        "low": prices - np.random.uniform(0, 1, n_bars),
        "close": prices,
        "volume": np.random.uniform(100, 500, n_bars),
    })
    
    # Ensure high > low and high >= close and low <= close
    data["high"] = data[["open", "high", "close"]].max(axis=1) + np.random.uniform(0, 0.5, n_bars)
    data["low"] = data[["open", "low", "close"]].min(axis=1) - np.random.uniform(0, 0.5, n_bars)
    
    return data


class TestAdaptiveRouterInitialization:
    """Test router initialization."""
    
    def test_router_initialization_disabled(self):
        """Test router with auto mode switch disabled."""
        config = {"auto_mode_switch": {"enabled": False}}
        router = AdaptiveStrategyRouter(config=config)
        
        assert not router.enabled
    
    def test_router_initialization_enabled(self, router):
        """Test router with auto mode switch enabled."""
        assert router.enabled
        assert router.enabled
        assert router.check_interval_seconds == 60
        assert router.min_switch_interval_seconds == 300
        assert router.persistence_threshold == 2
    
    def test_current_mode_setting(self, router):
        """Test setting current mode."""
        router.set_current_mode("trend_only")
        assert router._current_mode == "trend_only"


class TestMarketAnalysis:
    """Test market dimension analysis."""
    
    def test_market_analysis_creation(self):
        """Test MarketAnalysis dataclass creation."""
        now = datetime.now()
        analysis = MarketAnalysis(
            symbol="THB_BTC",
            timestamp=now,
            trend_direction="UP",
            trend_strength=45.0,
            volatility_pct=2.5,
            volume_ratio=1.2,
            btc_correlation=0.75,
            market_condition="STRONG_UP",
        )
        
        assert analysis.symbol == "THB_BTC"
        assert analysis.trend_direction == "UP"
        assert analysis.trend_strength == 45.0
        assert 0 <= analysis.trend_strength <= 100
    
    def test_market_analysis_repr(self):
        """Test MarketAnalysis string representation."""
        analysis = MarketAnalysis(
            symbol="THB_BTC",
            timestamp=datetime.now(),
            trend_direction="UP",
            trend_strength=45.0,
            volatility_pct=2.5,
            volume_ratio=1.2,
            btc_correlation=0.75,
            market_condition="STRONG_UP",
        )
        
        repr_str = repr(analysis)
        assert "THB_BTC" in repr_str
        assert "STRONG_UP" in repr_str


class TestModeClassification:
    """Test mode classification logic."""
    
    def test_strong_uptrend_recommends_trend_only(self, router):
        """Test that strong uptrend recommends TREND_ONLY mode."""
        analysis = MarketAnalysis(
            symbol="THB_BTC",
            timestamp=datetime.now(),
            trend_direction="UP",
            trend_strength=50.0,  # High ADX
            volatility_pct=1.5,
            volume_ratio=1.0,
            btc_correlation=0.0,
            market_condition="STRONG_UP",
        )
        
        mode = router.classify_market_and_recommend_mode(analysis)
        assert mode == "trend_only"
    
    def test_high_volatility_high_volume_recommends_scalping(self, router):
        """Test that high volatility with high volume recommends SCALPING."""
        analysis = MarketAnalysis(
            symbol="THB_BTC",
            timestamp=datetime.now(),
            trend_direction="SIDEWAYS",
            trend_strength=20.0,  # Low ADX
            volatility_pct=4.0,  # High volatility
            volume_ratio=1.6,  # High volume
            btc_correlation=0.0,
            market_condition="VOLATILE",
        )
        
        mode = router.classify_market_and_recommend_mode(analysis)
        assert mode == "scalping"
    
    def test_low_volatility_recommends_sniper(self, router):
        """Test that low volatility recommends SNIPER mode."""
        analysis = MarketAnalysis(
            symbol="THB_BTC",
            timestamp=datetime.now(),
            trend_direction="SIDEWAYS",
            trend_strength=15.0,  # Low ADX
            volatility_pct=0.5,  # Low volatility
            volume_ratio=0.8,
            btc_correlation=0.0,
            market_condition="RANGING",
        )
        
        mode = router.classify_market_and_recommend_mode(analysis)
        assert mode == "sniper"
    
    def test_default_to_standard_mode(self, router):
        """Test default fallback to STANDARD mode."""
        analysis = MarketAnalysis(
            symbol="THB_BTC",
            timestamp=datetime.now(),
            trend_direction="UP",
            trend_strength=30.0,  # Medium ADX
            volatility_pct=1.8,   # Medium volatility
            volume_ratio=1.2,     # Medium volume
            btc_correlation=0.0,
            market_condition="WEAK_UP",
        )
        
        mode = router.classify_market_and_recommend_mode(analysis)
        assert mode == "standard"


class TestHysteresisProtection:
    """Test hysteresis protection against rapid mode switching."""
    
    def test_mode_switch_blocked_by_cooldown(self, router):
        """Test that mode switch is blocked during cooldown period."""
        router.set_current_mode("scalping")
        router._last_switch_time = 0  # Very old
        
        # First two calls should be allowed (persistence threshold = 2)
        result = router.should_switch_mode("trend_only")
        assert result is False  # First call, decision history not yet persistent
        
        # Second call same mode should trigger switch
        result = router.should_switch_mode("trend_only")
        assert result is True  # Now persistent
        
        # Immediately after switch, cooldown should block next switch
        result = router.should_switch_mode("standard")
        assert result is False  # Blocked by cooldown
    
    def test_persistence_check(self, router):
        """Test that mode change requires persistence."""
        router.set_current_mode("scalping")
        router._last_switch_time = 0  # Allow switching
        
        # Single recommendation should not trigger switch
        result = router.should_switch_mode("trend_only")
        assert result is False
        
        # Two recommendations should trigger switch (threshold=2)
        result = router.should_switch_mode("trend_only")
        assert result is True


class TestModeDecision:
    """Test ModeDecision structure."""
    
    def test_mode_decision_structure(self):
        """Test ModeDecision dataclass."""
        decision = ModeDecision(
            recommended_mode="trend_only",
            reasoning="Strong uptrend detected",
            market_analysis=None,
            should_switch=True,
            switch_reason="Hysteresis check passed",
            confidence=0.85,
        )
        
        assert decision.recommended_mode == "trend_only"
        assert decision.should_switch is True
        assert decision.confidence == 0.85


class TestAutoSwitchMode:
    """Test main auto_switch_mode method."""
    
    def test_auto_switch_disabled_returns_no_switch(self):
        """Test that disabled router returns no switch."""
        config = {"auto_mode_switch": {"enabled": False}}
        router = AdaptiveStrategyRouter(config=config)
        router.set_current_mode("scalping")
        
        decision = router.auto_switch_mode("THB_BTC")
        assert decision.should_switch is False


class TestBtcCorrelationSeries:
    """Test BTC correlation path uses historical series, not single ticker point."""

    def test_get_btc_price_series_from_candles(self, sample_config):
        config = dict(sample_config)
        config["btc_correlation"] = {"enabled": True, "lookback_bars": 5}

        api_client = Mock()
        api_client.get_candle.return_value = {
            "error": 0,
            "result": [
                [1, 100.0, 101.0, 99.0, 100.5, 10.0],
                [2, 100.5, 102.0, 100.0, 101.5, 11.0],
                [3, 101.5, 103.0, 101.0, 102.5, 12.0],
                [4, 102.5, 104.0, 102.0, 103.5, 13.0],
                [5, 103.5, 105.0, 103.0, 104.5, 14.0],
            ],
        }

        router = AdaptiveStrategyRouter(config=config, api_client=api_client)
        series = router._get_btc_price_series(lookback_bars=5, timeframe="15m")

        assert series == [100.5, 101.5, 102.5, 103.5, 104.5]
        api_client.get_candle.assert_called_once()

    def test_analyze_market_dimensions_computes_finite_btc_correlation(self, sample_ohlcv_data, sample_config, monkeypatch):
        config = dict(sample_config)
        config["btc_correlation"] = {"enabled": True, "lookback_bars": 120}

        from indicators import TechnicalIndicators

        if not hasattr(TechnicalIndicators, "calculate_ema"):
            monkeypatch.setattr(
                TechnicalIndicators,
                "calculate_ema",
                staticmethod(lambda series, period=50: series.ewm(span=period, adjust=False).mean()),
                raising=False,
            )

        n = len(sample_ohlcv_data)
        base = np.linspace(50000.0, 52000.0, n)
        api_client = Mock()
        api_client.get_candle.return_value = {
            "error": 0,
            "result": [
                [int(i), float(v), float(v), float(v), float(v), 1000.0]
                for i, v in enumerate(base, start=1)
            ],
        }

        router = AdaptiveStrategyRouter(config=config, api_client=api_client)
        analysis = router.analyze_market_dimensions("THB_ETH", "15m", sample_ohlcv_data)

        assert analysis is not None
        assert np.isfinite(analysis.btc_correlation)
        assert -1.0 <= analysis.btc_correlation <= 1.0
        assert analysis.btc_price == pytest.approx(float(base[-1]))

    def test_analyze_market_dimensions_warns_when_btc_correlation_unavailable(self, sample_ohlcv_data, sample_config, monkeypatch, caplog):
        config = dict(sample_config)
        config["btc_correlation"] = {"enabled": True, "lookback_bars": 5}

        from indicators import TechnicalIndicators

        if not hasattr(TechnicalIndicators, "calculate_ema"):
            monkeypatch.setattr(
                TechnicalIndicators,
                "calculate_ema",
                staticmethod(lambda series, period=50: series.ewm(span=period, adjust=False).mean()),
                raising=False,
            )

        api_client = Mock()
        api_client.get_candle.return_value = {"error": 0, "result": [[1, 100.0, 100.0, 100.0, 100.0, 10.0]]}

        router = AdaptiveStrategyRouter(config=config, api_client=api_client)
        with caplog.at_level(logging.WARNING, logger="crypto-bot.adaptive_router"):
            analysis = router.analyze_market_dimensions("THB_ETH", "15m", sample_ohlcv_data)

        assert analysis is not None
        assert analysis.btc_correlation == pytest.approx(0.0)
        assert "BTC correlation unavailable" in caplog.text
    
    def test_auto_switch_insufficient_data_returns_no_switch(self, router):
        """Test that insufficient data returns no switch."""
        # Create minimal data
        data = pd.DataFrame({
            "open": [1, 2, 3],
            "high": [2, 3, 4],
            "low": [1, 2, 3],
            "close": [1.5, 2.5, 3.5],
            "volume": [100, 100, 100],
        })
        
        decision = router.auto_switch_mode("THB_BTC", data)
        assert decision.should_switch is False
    
    def test_auto_switch_respects_check_interval(self, router):
        """Test that auto_switch respects check interval."""
        import time
        router.set_current_mode("scalping")
        router._last_check_time = time.time()  # Just checked
        
        decision = router.auto_switch_mode("THB_BTC")
        assert decision.should_switch is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
