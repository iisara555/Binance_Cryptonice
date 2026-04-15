"""
Tests for SRG-3 (Negative Kelly clamp) and SRG-4 (Enum mismatch on market condition).
"""
import pytest
from unittest.mock import Mock

from strategy_base import MarketCondition, SignalType, TradingSignal
from signal_generator import SignalGenerator
from risk_management import RiskManager, RiskConfig, RiskCheckResult


# ══════════════════════════════════════════════════════════════════════════════
# SRG-4 — Enum mismatch on ensemble market condition
# ══════════════════════════════════════════════════════════════════════════════


class TestSRG4MarketConditionCoercion:
    """_coerce_market_condition must accept strings, Enums, and junk gracefully."""

    def test_string_bull_coerced_to_enum(self):
        """A bare 'BULL' string must be normalised to MarketCondition.BULL."""
        result = SignalGenerator._coerce_market_condition("BULL")
        assert result is MarketCondition.BULL

    def test_lowercase_string_coerced(self):
        """Case-insensitive: 'bear' → MarketCondition.BEAR."""
        result = SignalGenerator._coerce_market_condition("bear")
        assert result is MarketCondition.BEAR

    def test_enum_passes_through(self):
        """An already-correct MarketCondition is returned unchanged."""
        result = SignalGenerator._coerce_market_condition(MarketCondition.VOLATILE)
        assert result is MarketCondition.VOLATILE

    def test_unknown_string_defaults_to_sideway(self):
        """Garbage input falls back to SIDEWAY rather than raising."""
        result = SignalGenerator._coerce_market_condition("CHAOS")
        assert result is MarketCondition.SIDEWAY

    def test_none_defaults_to_sideway(self):
        """None input falls back to SIDEWAY."""
        result = SignalGenerator._coerce_market_condition(None)
        assert result is MarketCondition.SIDEWAY


class TestSRG4ConditionSuitabilityLookup:
    """_adjust_for_market_condition must boost/penalise correctly for BULL/BEAR
    (the values that detect_market_condition actually returns) even when the
    condition arrives as a string."""

    @staticmethod
    def _make_signal(strategy_name: str) -> TradingSignal:
        return TradingSignal(
            strategy_name=strategy_name,
            symbol="THB_BTC",
            signal_type=SignalType.BUY,
            confidence=0.7,
            price=1_500_000.0,
        )

    def test_bull_string_boosts_trend_following(self):
        """'BULL' (string) should match the suitability map and boost
        trend_following confidence instead of silently missing."""
        sg = SignalGenerator()
        signals = [self._make_signal("trend_following")]
        result = sg._adjust_for_market_condition(0.80, signals, "BULL")
        # trend_following is suitable for BULL → boost (confidence * 1.1)
        assert result == pytest.approx(min(0.95, 0.80 * 1.1))

    def test_bear_enum_boosts_scalping(self):
        """MarketCondition.BEAR should match the suitability map for
        scalping."""
        sg = SignalGenerator()
        signals = [self._make_signal("scalping")]
        result = sg._adjust_for_market_condition(0.70, signals, MarketCondition.BEAR)
        assert result == pytest.approx(min(0.95, 0.70 * 1.1))

    def test_unrecognised_string_penalises_confidence(self):
        """An unknown condition (e.g. 'CHAOS') coerces to SIDEWAY.
        A trend_following-only signal list is not suitable for SIDEWAY
        → confidence penalised to 0.7×."""
        sg = SignalGenerator()
        signals = [self._make_signal("trend_following")]
        result = sg._adjust_for_market_condition(0.80, signals, "CHAOS")
        # 'CHAOS' → SIDEWAY; trend_following not in SIDEWAY suitable list
        # → matching == 0 → confidence * 0.7
        assert result == pytest.approx(0.80 * 0.7)

    def test_risk_score_volatile_string(self):
        """_calculate_risk_score must add 25 to risk score when market_condition
        is passed as the string 'VOLATILE' (not the enum)."""
        sg = SignalGenerator()
        signals = [self._make_signal("breakout")]
        score = sg._calculate_risk_score(signals, "VOLATILE")
        # base 30 + volatile 25 = 55 minimum
        assert score >= 55


# ══════════════════════════════════════════════════════════════════════════════
# SRG-3 — Negative Kelly fraction must be clamped to zero
# ══════════════════════════════════════════════════════════════════════════════


class TestSRG3NegativeKelly:
    """calculate_position_size must never let a negative Kelly fraction
    shrink or invert the position size."""

    @staticmethod
    def _make_rm() -> RiskManager:
        return RiskManager(RiskConfig(
            max_risk_per_trade_pct=1.0,
            max_position_per_trade_pct=10.0,
        ))

    def test_negative_edge_trade_is_rejected(self):
        """With win-rate=30% and payoff=1:1 the full Kelly is −0.40.
        Negative Kelly implies negative expectancy and must be rejected."""
        rm = self._make_rm()
        entry = 100_000.0
        sl = 99_000.0   # risk distance 1 000
        tp = 101_000.0   # reward distance 1 000 → b = 1.0
        confidence = 0.30  # win rate 30%  →  Kelly = 0.30 - 0.70/1.0 = −0.40

        result = rm.calculate_position_size(
            portfolio_value=1_000_000.0,
            entry_price=entry,
            stop_loss_price=sl,
            take_profit_price=tp,
            confidence=confidence,
        )
        assert not result.allowed
        assert "Non-positive Kelly edge" in result.reason

    def test_zero_edge_trade_is_rejected(self):
        """With win-rate=50% and payoff=1:1, full Kelly = 0.0.
        Zero edge implies no statistical advantage and must be rejected."""
        rm = self._make_rm()
        entry = 100_000.0
        sl = 99_000.0
        tp = 101_000.0
        confidence = 0.50  # Kelly = 0.50 - 0.50/1.0 = 0.0

        result = rm.calculate_position_size(
            portfolio_value=1_000_000.0,
            entry_price=entry,
            stop_loss_price=sl,
            take_profit_price=tp,
            confidence=confidence,
        )
        assert not result.allowed
        assert "Non-positive Kelly edge" in result.reason

    def test_positive_edge_uses_kelly_sizing(self):
        """With win-rate=70% and payoff=2:1 the Kelly is strongly positive.
        Position size should be smaller than the hard cap because the
        Half-Kelly risk% is less than the default 1.0%."""
        rm = self._make_rm()
        entry = 100_000.0
        sl = 99_000.0    # risk = 1 000
        tp = 102_000.0    # reward = 2 000 → b = 2.0
        confidence = 0.70  # Kelly = 0.70 - 0.30/2.0 = 0.55
                           # Half-Kelly = 0.275 → 27.5% → capped at 1.0%

        result = rm.calculate_position_size(
            portfolio_value=1_000_000.0,
            entry_price=entry,
            stop_loss_price=sl,
            take_profit_price=tp,
            confidence=confidence,
        )
        assert result.allowed
        # Kelly is capped at effective_risk_pct (1.0%), same as default
        assert result.suggested_size == pytest.approx(100_000.0, rel=0.01)

    def test_severely_negative_kelly_does_not_crash(self):
        """Extreme case: win_rate=5%, RR=0.5 → Kelly deeply negative.
        Must be rejected."""
        rm = self._make_rm()
        entry = 100_000.0
        sl = 98_000.0     # risk = 2 000
        tp = 101_000.0     # reward = 1 000 → b = 0.5
        confidence = 0.05  # Kelly = 0.05 - 0.95/0.5 = −1.85

        result = rm.calculate_position_size(
            portfolio_value=1_000_000.0,
            entry_price=entry,
            stop_loss_price=sl,
            take_profit_price=tp,
            confidence=confidence,
        )
        assert not result.allowed
        assert "Non-positive Kelly edge" in result.reason
