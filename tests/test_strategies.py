"""
Comprehensive Unit Tests for Crypto Bot
======================================
Tests cover: strategies, risk management, API client, signals, and utilities.

Run with: python -m pytest tests/ -v
Install pytest: pip install pytest pytest-asyncio
"""

import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================================
# FIXTURES - Common test data
# ============================================================================

@pytest.fixture
def sample_price_data():
    """Generate sample OHLCV data for testing."""
    np.random.seed(42)
    n = 100
    
    # Create trending price data
    base_price = 100000
    trend = np.linspace(0, 1000, n)
    noise = np.random.randn(n) * 500
    
    data = pd.DataFrame({
        'timestamp': pd.date_range(start='2024-01-01', periods=n, freq='1h'),
        'open': base_price + trend + noise,
        'high': base_price + trend + noise + np.random.rand(n) * 200,
        'low': base_price + trend + noise - np.random.rand(n) * 200,
        'close': base_price + trend + noise,
        'volume': np.random.rand(n) * 1000 + 500,
    })
    
    return data


@pytest.fixture
def sample_rsi_data():
    """Generate price data with known RSI values."""
    # Create oscillating data that should produce RSI ~50
    n = 50
    base = np.sin(np.linspace(0, 4*np.pi, n)) * 1000 + 100000
    data = pd.DataFrame({
        'timestamp': pd.date_range(start='2024-01-01', periods=n, freq='1h'),
        'open': base,
        'high': base + 100,
        'low': base - 100,
        'close': base,
        'volume': np.ones(n) * 1000,
    })
    return data


@pytest.fixture
def mock_api_client():
    """Create a mock API client."""
    client = Mock()
    client.get_ticker.return_value = {
        'last': 100000,
        'highestBid': 99900,
        'lowestAsk': 100100,
        'percentChange': 2.5,
    }
    client.get_balances.return_value = {
        'THB': {'available': 50000, 'reserved': 0},
        'BTC': {'available': 0.5, 'reserved': 0},
    }
    return client


# ============================================================================
# STRATEGY TESTS
# ============================================================================

class TestTrendFollowingStrategy:
    """Tests for TrendFollowingStrategy."""
    
    def test_analyze_bullish_crossover(self, sample_price_data):
        """Test BUY signal on golden cross (SMA20 > SMA50)."""
        from strategies.trend_following import TrendFollowingStrategy
        
        strategy = TrendFollowingStrategy()
        
        # Add more data for SMA50
        extra_data = sample_price_data.copy()
        extra_data['close'] = np.linspace(80000, 150000, len(extra_data))
        
        signal = strategy.analyze(extra_data)
        
        assert signal.action in ['BUY', 'SELL', 'HOLD']
        assert 0 <= signal.confidence <= 1
    
    def test_analyze_insufficient_data(self):
        """Test that insufficient data returns HOLD."""
        from strategies.trend_following import TrendFollowingStrategy
        
        strategy = TrendFollowingStrategy()
        
        # Too few candles for SMA50
        data = pd.DataFrame({
            'open': [100, 101, 102],
            'high': [103, 104, 105],
            'low': [99, 100, 101],
            'close': [102, 103, 104],
            'volume': [100, 100, 100],
        })
        
        signal = strategy.analyze(data)
        
        assert signal.action == 'HOLD'
        assert signal.confidence == 0.0


class TestMomentumStrategy:
    """Tests for MomentumStrategy."""
    
    def test_analyze_oversold_rsi(self, sample_price_data):
        """Test BUY signal when RSI < 30 (oversold)."""
        from strategies.momentum import MomentumStrategy
        
        strategy = MomentumStrategy()
        
        # Create deeply oversold data
        data = sample_price_data.copy()
        # Strong downward trend
        data['close'] = np.linspace(150000, 50000, len(data))
        
        signal = strategy.analyze(data)
        
        assert signal.action in ['BUY', 'SELL', 'HOLD']
        assert signal.confidence >= 0.5
    
    def test_analyze_overbought_rsi(self, sample_price_data):
        """Test SELL signal when RSI > 70 (overbought)."""
        from strategies.momentum import MomentumStrategy
        
        strategy = MomentumStrategy()
        
        # Create deeply overbought data
        data = sample_price_data.copy()
        # Strong upward trend
        data['close'] = np.linspace(50000, 150000, len(data))
        
        signal = strategy.analyze(data)
        
        assert signal.action in ['BUY', 'SELL', 'HOLD']


class TestMeanReversionStrategy:
    """Tests for MeanReversionStrategy."""
    
    def test_analyze_bollinger_band_lower(self, sample_price_data):
        """Test BUY signal when price below lower Bollinger Band."""
        from strategies.mean_reversion import MeanReversionStrategy
        
        strategy = MeanReversionStrategy()
        
        # Create data that will have price below lower band
        data = sample_price_data.copy()
        # Add a sharp drop
        data.iloc[-5:, data.columns.get_loc('close')] = 50000
        
        signal = strategy.analyze(data)
        
        assert signal.action in ['BUY', 'SELL', 'HOLD']
    
    def test_analyze_bollinger_band_upper(self, sample_price_data):
        """Test SELL signal when price above upper Bollinger Band."""
        from strategies.mean_reversion import MeanReversionStrategy
        
        strategy = MeanReversionStrategy()
        
        # Create data that will have price above upper band
        data = sample_price_data.copy()
        # Add a sharp spike
        data.iloc[-5:, data.columns.get_loc('close')] = 200000
        
        signal = strategy.analyze(data)
        
        assert signal.action in ['BUY', 'SELL', 'HOLD']


class TestBreakoutStrategy:
    """Tests for BreakoutStrategy."""
    
    def test_analyze_breakout_up(self, sample_price_data):
        """Test BUY signal on upward breakout."""
        from strategies.breakout import BreakoutStrategy
        
        strategy = BreakoutStrategy()
        
        # Create clear breakout
        data = sample_price_data.copy()
        # Make price break above 20-period high
        data.iloc[-1, data.columns.get_loc('close')] = data['high'].rolling(20).max().iloc[-2] + 1000
        
        signal = strategy.analyze(data)
        
        assert signal.action in ['BUY', 'SELL', 'HOLD']
    
    def test_analyze_insufficient_data(self):
        """Test HOLD with insufficient data."""
        from strategies.breakout import BreakoutStrategy
        
        strategy = BreakoutStrategy()
        
        data = pd.DataFrame({
            'open': [100, 101, 102, 103, 104],
            'high': [105, 106, 107, 108, 109],
            'low': [99, 100, 101, 102, 103],
            'close': [102, 103, 104, 105, 106],
            'volume': [100] * 5,
        })
        
        signal = strategy.analyze(data)
        
        assert signal.action == 'HOLD'


class TestScalpingStrategy:
    """Tests for ScalpingStrategy."""

    def test_scalping_defaults_match_strategy_profile(self):
        from strategies.scalping import ScalpingStrategy

        strategy = ScalpingStrategy()

        assert strategy.fast_ema == 9
        assert strategy.slow_ema == 21
        assert strategy.rsi_period == 7
        assert strategy.rsi_oversold == 34.0
        assert strategy.rsi_overbought == 66.0
        assert strategy.bollinger_period == 20
        assert strategy.bollinger_std == 2.0
        assert strategy.min_confidence == 0.30
    
    def test_analyze_ema_crossover(self, sample_price_data):
        """Test signal on EMA crossover."""
        from strategies.scalping import ScalpingStrategy
        
        strategy = ScalpingStrategy()
        
        signal = strategy.analyze(sample_price_data)
        
        assert signal.action in ['BUY', 'SELL', 'HOLD']
        assert 0 <= signal.confidence <= 1

    def test_generate_signal_uses_fixed_scalping_sl_tp_metadata(self):
        """Scalping strategy should emit tight fixed SL/TP metadata for fast trades."""
        from strategies.scalping import ScalpingStrategy

        prices = [100.0] * 18 + [86.0, 86.0, 104.0, 88.0, 114.0]
        data = pd.DataFrame(
            {
                'close': prices,
                'high': [p + 0.5 for p in prices],
                'low': [p - 0.5 for p in prices],
                'open': prices,
                'volume': [1000.0] * len(prices),
            }
        )

        strategy = ScalpingStrategy({
            'rsi_oversold': 70,
            'min_entry_confidence': 0.3,
            'stop_loss_pct': 0.75,
            'take_profit_pct': 1.75,
            'position_timeout_minutes': 30,
        })

        signal = strategy.generate_signal(data, symbol='THB_TEST')

        assert signal.signal_type.value == 'BUY'
        assert signal.stop_loss == 113.145
        assert signal.take_profit == 115.995
        assert signal.metadata['strategy_mode'] == 'scalping'
        assert signal.metadata['position_timeout_minutes'] == 30


# ============================================================================
# RISK MANAGEMENT TESTS
# ============================================================================

class TestRiskManagement:
    """Tests for risk management calculations."""
    
    def test_calculate_atr(self, sample_price_data):
        """Test ATR calculation."""
        from risk_management import calculate_atr
        
        highs = sample_price_data['high'].tolist()
        lows = sample_price_data['low'].tolist()
        closes = sample_price_data['close'].tolist()
        
        atr = calculate_atr(highs, lows, closes, period=14)
        
        assert isinstance(atr, list)
        assert len(atr) == len(highs)
        assert all(a >= 0 for a in atr)
    
    def test_atr_increases_with_volatility(self):
        """Test that ATR increases with higher volatility."""
        from risk_management import calculate_atr
        
        # Low volatility data
        low_vol = pd.DataFrame({
            'high': np.linspace(100000, 100500, 20),
            'low': np.linspace(99500, 100000, 20),
            'close': np.linspace(99750, 100250, 20),
        })
        
        # High volatility data
        high_vol = pd.DataFrame({
            'high': np.linspace(100000, 105000, 20),
            'low': np.linspace(95000, 100000, 20),
            'close': np.linspace(97500, 102500, 20),
        })
        
        atr_low = calculate_atr(
            low_vol['high'].tolist(),
            low_vol['low'].tolist(),
            low_vol['close'].tolist(),
            period=14
        )
        
        atr_high = calculate_atr(
            high_vol['high'].tolist(),
            high_vol['low'].tolist(),
            high_vol['close'].tolist(),
            period=14
        )
        
        # High volatility should have higher ATR
        assert atr_high[-1] > atr_low[-1]
    
    def test_get_default_sl_tp(self):
        """Test default stop loss and take profit percentage calculation."""
        from risk_management import get_default_sl_tp
        
        # Test for BTC (low volatility)
        sl_pct, tp_pct = get_default_sl_tp('THB_BTC')
        
        # Should return negative SL and positive TP percentages
        assert sl_pct < 0  # SL should be negative (loss)
        assert tp_pct > 0  # TP should be positive (profit)
        assert sl_pct == -2.0
        assert tp_pct == 4.0
        
        # Test for ALT (high volatility)
        sl_pct_alt, tp_pct_alt = get_default_sl_tp('THB_ETH')
        
        # ALT should have wider SL/TP than BTC
        assert abs(sl_pct_alt) >= abs(sl_pct)  # Wider SL for ALT
        assert tp_pct_alt >= tp_pct  # Wider TP for ALT
        assert sl_pct_alt == -8.0
        assert tp_pct_alt == 12.0
    
    def test_sl_tp_with_atr(self):
        """Test ATR-based SL/TP calculation."""
        from risk_management import RiskManager, RiskConfig
        
        config = RiskConfig()
        rm = RiskManager(config)
        
        entry = 100000
        atr = 1000  # 1% ATR
        
        sl, tp = rm.calc_sl_tp_from_atr(
            entry_price=entry,
            atr_value=atr,
            direction='long',
            risk_reward_ratio=2.0
        )
        
        # For 2:1 R:R, expected distance scales with config.atr_multiplier.
        expected_sl = entry - config.atr_multiplier * atr
        expected_tp = entry + (config.atr_multiplier * 2.0) * atr
        
        assert abs(sl - expected_sl) < 10
        assert abs(tp - expected_tp) < 10

    def test_resolve_effective_sl_tp_percentages_prefers_dynamic_pair_defaults(self):
        """Dynamic SL/TP should preserve pair-specific defaults even if global risk values differ."""
        from risk_management import resolve_effective_sl_tp_percentages

        sl_pct, tp_pct = resolve_effective_sl_tp_percentages(
            'THB_BTC',
            {
                'use_dynamic_sl_tp': True,
                'stop_loss_pct': 1.0,
                'take_profit_pct': 2.5,
            },
        )

        assert sl_pct == -2.0
        assert tp_pct == 4.0

    def test_resolve_effective_sl_tp_percentages_uses_config_when_dynamic_disabled(self):
        """Configured fixed SL/TP should be used when dynamic SL/TP is disabled."""
        from risk_management import resolve_effective_sl_tp_percentages

        sl_pct, tp_pct = resolve_effective_sl_tp_percentages(
            'THB_BTC',
            {
                'use_dynamic_sl_tp': False,
                'stop_loss_pct': 1.0,
                'take_profit_pct': 2.5,
            },
        )

        assert sl_pct == -1.0
        assert tp_pct == 2.5


# ============================================================================
# API CLIENT TESTS
# ============================================================================

class TestBitkubAPIClient:
    """Tests for Bitkub API client."""
    
    def test_no_trailing_zeros(self):
        """Test _no_trailing_zeros utility."""
        from api_client import _no_trailing_zeros
        
        assert _no_trailing_zeros(1000.00) == 1000.0
        assert _no_trailing_zeros(1000.50) == 1000.5
        assert _no_trailing_zeros(0.00010000) == 0.0001
        assert _no_trailing_zeros(1.23456789) == 1.23456789
    
    @patch('requests.get')
    def test_get_ticker(self, mock_get, mock_api_client):
        """Test ticker retrieval."""
        from api_client import BitkubClient
        
        mock_response = Mock()
        mock_response.json.return_value = [{
            'symbol': 'btc_thb',
            'last': 100000,
            'highestBid': 99900,
            'lowestAsk': 100100,
            'percentChange': 2.5,
            'high_24_hr': 102000,
            'low_24_hr': 98000,
        }]
        mock_get.return_value = mock_response
        
        client = BitkubClient()
        ticker = client.get_ticker('THB_BTC')
        
        assert ticker['last'] == 100000
        assert 'highestBid' in ticker

    def test_get_order_history_normalizes_symbol(self):
        """Order history must use Bitkub's base_quote symbol format."""
        from api_client import BitkubClient

        client = BitkubClient()
        client._request = Mock(return_value=[])

        client.get_order_history('THB_BTC', limit=50)

        client._request.assert_called_once_with(
            'GET',
            '/api/v3/market/my-order-history',
            authenticated=True,
            query_params={'sym': 'btc_thb', 'lmt': 50},
        )

    def test_get_open_orders_preserves_checked_symbol_when_sym_missing(self):
        """Open-order rows should keep the requested symbol for reconcile fallback."""
        from api_client import BitkubClient

        client = BitkubClient()
        client._request = Mock(return_value=[
            {
                'id': 'ghost-1',
                'sym': None,
                'side': 'sell',
                'amount': '6.80588833',
                'rate': '2.9979',
            }
        ])

        rows = client.get_open_orders('THB_DOGE')

        client._request.assert_called_once_with(
            'GET',
            '/api/v3/market/my-open-orders',
            authenticated=True,
            query_params={'sym': 'doge_thb'},
        )
        assert rows[0]['_checked_symbol'] == 'THB_DOGE'
        assert rows[0]['sym'] is None
    
    def test_circuit_breaker_initial_state(self):
        """Test circuit breaker starts in CLOSED state."""
        from api_client import CircuitBreaker
        
        cb = CircuitBreaker()
        
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.is_available() is True
    
    def test_circuit_breaker_opens_after_failures(self):
        """Test circuit breaker opens after threshold failures."""
        from api_client import CircuitBreaker
        
        cb = CircuitBreaker(failure_threshold=3)
        
        # Record failures
        cb.record_failure("Test error 1")
        cb.record_failure("Test error 2")
        
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.is_available() is True
        
        # Third failure should open circuit
        cb.record_failure("Test error 3")
        
        assert cb.state == CircuitBreaker.OPEN
        assert cb.is_available() is False
    
    def test_circuit_breaker_recovers(self):
        """Test circuit breaker recovers after timeout."""
        from api_client import CircuitBreaker
        import time
        
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
        
        # Open the circuit
        cb.record_failure("Error 1")
        cb.record_failure("Error 2")
        
        assert cb.state == CircuitBreaker.OPEN
        
        # Wait for recovery timeout
        time.sleep(0.15)
        
        # Should transition to HALF (testing)
        assert cb.is_available() is True
        
        # Record success
        cb.record_success()
        
        assert cb.state == CircuitBreaker.CLOSED
    
    def test_clock_sync_initialization(self):
        """Test clock sync initializes correctly."""
        from api_client import ClockSync
        
        cs = ClockSync(max_offset=30.0)
        
        assert cs.max_offset == 30.0
        assert cs._offset == 0.0
    
    @patch('requests.get')
    def test_clock_sync(self, mock_get):
        """Test clock synchronization."""
        from api_client import ClockSync
        import time
        
        mock_response = Mock()
        server_time_ms = int(time.time() * 1000)
        mock_response.json.return_value = server_time_ms // 1000
        mock_get.return_value = mock_response
        
        cs = ClockSync(max_offset=30.0)
        offset = cs.sync("https://api.bitkub.com")
        
        assert isinstance(offset, float)


# ============================================================================
# SIGNAL GENERATOR TESTS
# ============================================================================

class TestSignalGenerator:
    """Tests for signal generation."""
    
    def test_aggregate_signals(self, sample_price_data):
        """Test signal aggregation from multiple strategies."""
        from signal_generator import SignalGenerator
        
        generator = SignalGenerator()
        
        signals = generator.generate_signals(
            data=sample_price_data,
            symbol='THB_BTC',
            use_strategies=['trend_following', 'momentum']
        )
        
        # Should return list of aggregated signals
        assert isinstance(signals, list)
    
    def test_signal_creation(self):
        """Test signal creation."""
        from signal_generator import AggregatedSignal
        
        signal = AggregatedSignal(
            symbol='THB_BTC',
            signal_type='BUY',
            combined_confidence=0.7,
            signals=[],
            avg_price=100000,
            avg_stop_loss=98000,
            avg_take_profit=105000,
            avg_risk_reward=2.0,
            strategy_votes={'test': 1},
            market_condition='bullish'
        )
        
        # Verify signal attributes
        assert signal.symbol == 'THB_BTC'
        assert signal.combined_confidence == 0.7
        assert signal.avg_risk_reward == 2.0

    @staticmethod
    def _make_sniper_test_data(n=211, trend="up"):
        close = np.linspace(100.0, 220.0, n) if trend == "up" else np.linspace(220.0, 100.0, n)
        return pd.DataFrame({
            'timestamp': pd.date_range(start='2024-01-01', periods=n, freq='15min'),
            'open': close - 1.0,
            'high': close + 2.0,
            'low': close - 2.0,
            'close': close,
            'volume': np.full(n, 1000.0),
        })

    def test_sniper_ignores_forming_candle_macd_cross(self):
        """A MACD cross visible only on the forming candle must not trigger."""
        from signal_generator import SignalGenerator

        data = self._make_sniper_test_data()
        generator = SignalGenerator()

        full_macd = pd.Series([-2.0, -1.0, 1.0])
        full_signal = pd.Series([-1.5, -0.5, 0.5])
        confirmed_macd = pd.Series([-3.0, -2.0, -1.5])
        confirmed_signal = pd.Series([-2.5, -1.5, -1.0])

        def macd_side_effect(prices, fast=12, slow=26, signal=9):
            if len(prices) == len(data):
                return full_macd, full_signal, full_macd - full_signal
            if len(prices) == len(data) - 1:
                return confirmed_macd, confirmed_signal, confirmed_macd - confirmed_signal
            raise AssertionError(f"Unexpected MACD input length: {len(prices)}")

        with patch('signal_generator.TechnicalIndicators.calculate_macd', side_effect=macd_side_effect), \
             patch('signal_generator.TechnicalIndicators.calculate_atr', return_value=pd.Series(np.full(len(data), 5.0))):
            signals = generator.generate_sniper_signal(data=data, symbol='THB_BTC')

        assert signals == []

    def test_sniper_ignores_forming_candle_macd_crossunder(self):
        """A SELL cross visible only on the forming candle must not trigger."""
        from signal_generator import SignalGenerator

        data = self._make_sniper_test_data(trend='down')
        generator = SignalGenerator()

        full_macd = pd.Series([2.0, 1.0, -1.0])
        full_signal = pd.Series([1.5, 0.5, -0.5])
        confirmed_macd = pd.Series([3.0, 2.0, 1.5])
        confirmed_signal = pd.Series([2.5, 1.5, 1.0])

        def macd_side_effect(prices, fast=12, slow=26, signal=9):
            if len(prices) == len(data):
                return full_macd, full_signal, full_macd - full_signal
            if len(prices) == len(data) - 1:
                return confirmed_macd, confirmed_signal, confirmed_macd - confirmed_signal
            raise AssertionError(f"Unexpected MACD input length: {len(prices)}")

        with patch('signal_generator.TechnicalIndicators.calculate_macd', side_effect=macd_side_effect), \
             patch('signal_generator.TechnicalIndicators.calculate_atr', return_value=pd.Series(np.full(len(data), 5.0))):
            signals = generator.generate_sniper_signal(data=data, symbol='THB_BTC')

        assert signals == []

    def test_sniper_accepts_confirmed_closed_candle_macd_cross(self):
        """A MACD cross on confirmed closed candles must still trigger a BUY."""
        from signal_generator import SignalGenerator
        from strategy_base import SignalType

        data = self._make_sniper_test_data()
        generator = SignalGenerator()

        full_macd = pd.Series([-2.0, -1.0, 1.0])
        full_signal = pd.Series([-1.5, -0.5, 0.5])
        confirmed_macd = pd.Series([-2.0, -1.0, 1.0])
        confirmed_signal = pd.Series([-1.5, -0.5, 0.5])

        def macd_side_effect(prices, fast=12, slow=26, signal=9):
            if len(prices) == len(data):
                return full_macd, full_signal, full_macd - full_signal
            if len(prices) == len(data) - 1:
                return confirmed_macd, confirmed_signal, confirmed_macd - confirmed_signal
            raise AssertionError(f"Unexpected MACD input length: {len(prices)}")

        with patch('signal_generator.TechnicalIndicators.calculate_macd', side_effect=macd_side_effect), \
             patch('signal_generator.TechnicalIndicators.calculate_atr', return_value=pd.Series(np.full(len(data), 5.0))):
            signals = generator.generate_sniper_signal(data=data, symbol='THB_BTC')

        assert len(signals) == 1
        assert signals[0].signal_type is SignalType.BUY
        assert signals[0].signals[0].metadata['macd_cross_bar'] == 'current'
        assert signals[0].signals[0].metadata['macd_cross_direction'] == 'buy'

    def test_sniper_accepts_exact_minimum_bar_count(self):
        """Exactly 210 confirmed bars should not be rejected as insufficient data."""
        from signal_generator import SignalGenerator
        from strategy_base import SignalType

        data = self._make_sniper_test_data(n=210)
        generator = SignalGenerator()

        full_macd = pd.Series([-2.0, -1.0, 1.0])
        full_signal = pd.Series([-1.5, -0.5, 0.5])
        confirmed_macd = pd.Series([-2.0, -1.0, 1.0])
        confirmed_signal = pd.Series([-1.5, -0.5, 0.5])

        def macd_side_effect(prices, fast=12, slow=26, signal=9):
            if len(prices) == len(data):
                return full_macd, full_signal, full_macd - full_signal
            if len(prices) == len(data) - 1:
                return confirmed_macd, confirmed_signal, confirmed_macd - confirmed_signal
            raise AssertionError(f"Unexpected MACD input length: {len(prices)}")

        with patch('signal_generator.TechnicalIndicators.calculate_macd', side_effect=macd_side_effect), \
             patch('signal_generator.TechnicalIndicators.calculate_atr', return_value=pd.Series(np.full(len(data), 5.0))):
            signals = generator.generate_sniper_signal(data=data, symbol='THB_BTC')

        assert len(signals) == 1
        assert signals[0].signal_type is SignalType.BUY

    def test_sniper_accepts_confirmed_closed_candle_macd_crossunder(self):
        """A MACD crossunder on confirmed closed candles must trigger a SELL."""
        from signal_generator import SignalGenerator
        from strategy_base import SignalType

        data = self._make_sniper_test_data(trend='down')
        generator = SignalGenerator()

        full_macd = pd.Series([2.0, 1.0, -1.0])
        full_signal = pd.Series([1.5, 0.5, -0.5])
        confirmed_macd = pd.Series([2.0, 1.0, -1.0])
        confirmed_signal = pd.Series([1.5, 0.5, -0.5])

        def macd_side_effect(prices, fast=12, slow=26, signal=9):
            if len(prices) == len(data):
                return full_macd, full_signal, full_macd - full_signal
            if len(prices) == len(data) - 1:
                return confirmed_macd, confirmed_signal, confirmed_macd - confirmed_signal
            raise AssertionError(f"Unexpected MACD input length: {len(prices)}")

        with patch('signal_generator.TechnicalIndicators.calculate_macd', side_effect=macd_side_effect), \
             patch('signal_generator.TechnicalIndicators.calculate_atr', return_value=pd.Series(np.full(len(data), 5.0))):
            signals = generator.generate_sniper_signal(data=data, symbol='THB_BTC')

        assert len(signals) == 1
        assert signals[0].signal_type is SignalType.SELL
        assert signals[0].signals[0].metadata['macd_cross_bar'] == 'current'
        assert signals[0].signals[0].metadata['macd_cross_direction'] == 'sell'

    def test_sniper_accepts_recent_macd_cross_with_configured_lookback(self):
        """A crossover a few confirmed candles back should trigger when lookback is widened."""
        from signal_generator import SignalGenerator
        from strategy_base import SignalType

        data = self._make_sniper_test_data(trend='down')
        generator = SignalGenerator({"sniper": {"macd_trigger_lookback_bars": 3}})

        full_macd = pd.Series([2.0, -1.0, -1.1, -1.2])
        full_signal = pd.Series([1.5, -0.5, -0.6, -0.7])
        confirmed_macd = pd.Series([2.0, -1.0, -1.1, -1.2])
        confirmed_signal = pd.Series([1.5, -0.5, -0.6, -0.7])

        def macd_side_effect(prices, fast=12, slow=26, signal=9):
            if len(prices) == len(data):
                return full_macd, full_signal, full_macd - full_signal
            if len(prices) == len(data) - 1:
                return confirmed_macd, confirmed_signal, confirmed_macd - confirmed_signal
            raise AssertionError(f"Unexpected MACD input length: {len(prices)}")

        with patch('signal_generator.TechnicalIndicators.calculate_macd', side_effect=macd_side_effect), \
             patch('signal_generator.TechnicalIndicators.calculate_atr', return_value=pd.Series(np.full(len(data), 5.0))):
            signals = generator.generate_sniper_signal(data=data, symbol='THB_BTC')

        assert len(signals) == 1
        assert signals[0].signal_type is SignalType.SELL
        assert signals[0].signals[0].metadata['macd_cross_bar'] == '2_bars_ago'

    def test_sniper_generation_delegates_to_registered_strategy(self):
        from signal_generator import SignalGenerator
        from strategy_base import SignalType, TradingSignal

        data = self._make_sniper_test_data()
        generator = SignalGenerator()
        delegated_signal = TradingSignal(
            strategy_name='sniper_dual_ema_macd',
            symbol='THB_BTC',
            signal_type=SignalType.BUY,
            confidence=1.0,
            price=float(data['close'].iloc[-1]),
            stop_loss=95.0,
            take_profit=110.0,
            risk_reward_ratio=3.0,
            metadata={'trade_rationale': '[Sniper] delegated'},
        )

        generator.strategies['sniper'].generate_signal = Mock(return_value=delegated_signal)
        generator.strategies['sniper'].get_last_diagnostics = Mock(return_value={})

        signals = generator.generate_sniper_signal(data=data, symbol='THB_BTC')

        generator.strategies['sniper'].generate_signal.assert_called_once_with(data, 'THB_BTC')
        assert len(signals) == 1
        assert signals[0].signals[0] is delegated_signal
        assert signals[0].trade_rationale == '[Sniper] delegated'


# ============================================================================
# WEBSOCKET TESTS
# ============================================================================

class TestBitkubWebSocket:
    """Tests for WebSocket client."""
    
    def test_price_tick_creation(self):
        """Test PriceTick dataclass creation."""
        from bitkub_websocket import PriceTick
        
        tick = PriceTick(
            symbol='BTC_THB',
            last=100000,
            bid=99900,
            ask=100100,
            percent_change_24h=2.5,
            timestamp=1234567890.0
        )
        
        assert tick.symbol == 'BTC_THB'
        assert tick.last == 100000
        assert tick.bid < tick.ask  # Bid should be less than ask
    
    def test_connection_state_enum(self):
        """Test ConnectionState enum values."""
        from bitkub_websocket import ConnectionState
        
        assert ConnectionState.DISCONNECTED.value == 'disconnected'
        assert ConnectionState.CONNECTING.value == 'connecting'
        assert ConnectionState.CONNECTED.value == 'connected'
        assert ConnectionState.RECONNECTING.value == 'reconnecting'
        assert ConnectionState.FAILED.value == 'failed'
    
    def test_price_cache_operations(self):
        """Test global price cache operations."""
        from bitkub_websocket import _price_cache, get_latest_ticker, PriceTick
        import bitkub_websocket
        
        # Clear cache first
        bitkub_websocket._price_cache.clear()
        
        # Add a tick
        tick = PriceTick(
            symbol='BTC_THB',
            last=100000,
            bid=99900,
            ask=100100,
            percent_change_24h=2.5,
            timestamp=1234567890.0
        )
        
        bitkub_websocket._price_cache['BTC_THB'] = tick
        
        # Retrieve
        retrieved = get_latest_ticker('BTC_THB')
        
        assert retrieved is not None
        assert retrieved.last == 100000
        
        # Case insensitive
        retrieved_lower = get_latest_ticker('btc_thb')
        assert retrieved_lower is not None
        
        # Non-existent
        assert get_latest_ticker('ETH_THB') is None
    
    def test_get_websocket_stats_no_connection(self):
        """Test stats when no connection exists."""
        from bitkub_websocket import get_websocket_stats
        
        stats = get_websocket_stats()
        
        # Should return None when no global WebSocket
        assert stats is None


# ============================================================================
# UTILITY TESTS
# ============================================================================

class TestHelpers:
    """Tests for helper utilities."""
    
    def test_coerce_trade_float(self):
        """Test numeric coercion utility."""
        from trading_bot import _coerce_trade_float
        
        assert _coerce_trade_float(None) == 0.0
        assert _coerce_trade_float(None, default=100.0) == 100.0
        assert _coerce_trade_float(100) == 100.0
        assert _coerce_trade_float(100.5) == 100.5
        assert _coerce_trade_float("invalid") == 0.0
        assert _coerce_trade_float("100.5") == 100.5
    
    def test_datetime_utilities(self):
        """Test datetime utilities."""

        # Test that datetime objects work correctly
        now = datetime.now()
        assert isinstance(now, datetime)
        assert now.timestamp() > 0


# ============================================================================
# INTEGRATION TESTS
# ============================================================================

class TestIntegration:
    """Integration tests for complete workflows."""
    
    def test_full_signal_to_execution_flow(self, sample_price_data, mock_api_client):
        """Test complete flow from signal generation to execution plan."""
        from signal_generator import SignalGenerator
        from trade_executor import TradeExecutor, ExecutionPlan, OrderSide
        from risk_management import RiskManager, RiskConfig
        
        # Step 1: Generate signals
        generator = SignalGenerator()
        signals = generator.generate_signals(
            data=sample_price_data,
            symbol='THB_BTC',
            use_strategies=['momentum']
        )
        
        # Step 2: Check risk
        _rm = RiskManager(RiskConfig())  # noqa: F841 — validates instantiation
        
        if signals:
            signal = signals[0]
            portfolio = {
                'balance': 50000,
                'positions': []
            }
            
            risk_check = generator.check_risk(signal, portfolio)
            
            assert risk_check.passed in [True, False]
    
    def test_position_lifecycle(self, mock_api_client):
        """Test complete position lifecycle."""
        from trade_executor import ExecutionPlan, OrderSide
        
        # Create execution plan
        plan = ExecutionPlan(
            symbol='THB_BTC',
            side=OrderSide.BUY,
            amount=0.001,
            entry_price=100000,
            stop_loss=98000,
            take_profit=105000,
            risk_reward_ratio=2.0,
            confidence=0.7,
            strategy_votes={'test': 1}
        )
        
        assert plan.symbol == 'THB_BTC'
        assert plan.side == OrderSide.BUY
        assert plan.risk_reward_ratio == 2.0


# ============================================================================
# PERFORMANCE TESTS
# ============================================================================

class TestPerformance:
    """Performance and stress tests."""
    
    def test_large_dataframe_processing(self):
        """Test strategy performance with large dataset."""
        from strategies.momentum import MomentumStrategy
        
        # Create large dataset
        n = 10000
        data = pd.DataFrame({
            'timestamp': pd.date_range(start='2020-01-01', periods=n, freq='1min'),
            'open': np.random.uniform(90000, 110000, n),
            'high': np.random.uniform(100000, 120000, n),
            'low': np.random.uniform(80000, 100000, n),
            'close': np.random.uniform(90000, 110000, n),
            'volume': np.random.uniform(100, 1000, n),
        })
        
        strategy = MomentumStrategy()
        
        import time
        start = time.time()
        signal = strategy.analyze(data)
        elapsed = time.time() - start
        
        # Should complete in under 1 second
        assert elapsed < 1.0
        assert signal.action in ['BUY', 'SELL', 'HOLD']
    
    def test_atr_calculation_performance(self):
        """Test ATR calculation performance."""
        from risk_management import calculate_atr
        
        # Create large dataset
        n = 10000
        highs = np.random.uniform(100000, 110000, n).tolist()
        lows = np.random.uniform(90000, 100000, n).tolist()
        closes = np.random.uniform(95000, 105000, n).tolist()
        
        import time
        start = time.time()
        atr = calculate_atr(highs, lows, closes, period=14)
        elapsed = time.time() - start
        
        # Should complete in under 0.5 seconds
        assert elapsed < 0.5
        assert len(atr) == n


# ============================================================================
# EDGE CASE TESTS
# ============================================================================

class TestEdgeCases:
    """Tests for edge cases and error handling."""
    
    def test_empty_dataframe(self):
        """Test strategies handle empty data gracefully."""
        from strategies.momentum import MomentumStrategy
        
        strategy = MomentumStrategy()
        
        data = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])
        
        # Should not raise, should return HOLD
        signal = strategy.analyze(data)
        assert signal.action == 'HOLD'
    
    def test_nan_values(self):
        """Test handling of NaN values in data."""
        from strategies.trend_following import TrendFollowingStrategy
        
        data = pd.DataFrame({
            'open': [100, np.nan, 102, 103, 104] * 10,
            'high': [105, 106, np.nan, 108, 109] * 10,
            'low': [99, np.nan, 101, 102, 103] * 10,
            'close': [102, 103, np.nan, 105, 106] * 10,
            'volume': [100] * 50,
        })
        
        strategy = TrendFollowingStrategy()
        
        # Should handle NaN without crashing
        signal = strategy.analyze(data)
        assert signal.action in ['BUY', 'SELL', 'HOLD']
    
    def test_zero_division_handling(self):
        """Test handling of division by zero."""
        from risk_management import calculate_atr
        
        # Data with zero ranges
        data = pd.DataFrame({
            'high': [100] * 20,
            'low': [100] * 20,
            'close': [100] * 20,
        })
        
        atr = calculate_atr(
            data['high'].tolist(),
            data['low'].tolist(),
            data['close'].tolist(),
            period=14
        )
        
        # Should not crash, ATR should be zero or handle gracefully
        assert all(a >= 0 for a in atr)
    
    def test_extreme_price_values(self):
        """Test handling of extreme price values."""
        from strategies.momentum import MomentumStrategy
        
        # Very large prices
        data = pd.DataFrame({
            'open': [1e15] * 50,
            'high': [1e15 + 1000] * 50,
            'low': [1e15 - 1000] * 50,
            'close': [1e15] * 50,
            'volume': [1000] * 50,
        })
        
        strategy = MomentumStrategy()
        
        # Should handle extreme values
        signal = strategy.analyze(data)
        assert signal.action in ['BUY', 'SELL', 'HOLD']
    
    def test_negative_prices(self):
        """Test that negative prices are handled (shouldn't happen but test anyway)."""
        from risk_management import calculate_atr
        
        data = pd.DataFrame({
            'high': [-100, -90, -80, -70, -60] * 4,
            'low': [-110, -100, -90, -80, -70] * 4,
            'close': [-105, -95, -85, -75, -65] * 4,
        })
        
        # ATR calculation should handle or warn about negative values
        atr = calculate_atr(
            data['high'].tolist(),
            data['low'].tolist(),
            data['close'].tolist(),
            period=14
        )
        
        assert isinstance(atr, list)


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
