"""
Breakout Strategy.

Edge: Donchian-style 20-bar breakout with volume confirmation. Fires ONLY on
the bar where price first pierces the prior 20-bar range — no longer keeps
emitting BUY for every subsequent bar that stays above the level (the legacy
bug that tanked the win rate).
"""
import pandas as pd

from .base import Signal, StrategyBase


class BreakoutStrategy(StrategyBase):
    """Breakout detection on the actual breakout bar only."""

    LOOKBACK = 20

    @staticmethod
    def _scale_confidence(distance_pct: float) -> float:
        # 0.55 base conviction at breakout threshold, ramps with breakout distance.
        return max(0.55, min(0.95, 0.55 + (max(distance_pct, 0.0) * 40.0)))

    def analyze(self, data: pd.DataFrame) -> Signal:
        if len(data) < self.LOOKBACK + 2:
            return Signal(action='HOLD', confidence=0.0)
        if not all(c in data.columns for c in ('high', 'low', 'close')):
            return Signal(action='HOLD', confidence=0.0)

        # Use shift(1) so the lookback excludes the current bar — we want the
        # range *up to but not including* now, then check whether now breaks it.
        prior_high = data['high'].shift(1).rolling(self.LOOKBACK).max()
        prior_low = data['low'].shift(1).rolling(self.LOOKBACK).min()

        prev_high = float(prior_high.iloc[-1]) if not pd.isna(prior_high.iloc[-1]) else None
        prev_low = float(prior_low.iloc[-1]) if not pd.isna(prior_low.iloc[-1]) else None
        if prev_high is None or prev_low is None:
            return Signal(action='HOLD', confidence=0.0)

        prev_close = float(data['close'].iloc[-2])
        cur_close = float(data['close'].iloc[-1])

        # Optional volume confirmation: current volume > 20-bar median.
        vol_ok = True
        if 'volume' in data.columns:
            recent_vol = data['volume'].iloc[-self.LOOKBACK:].median()
            if pd.notna(recent_vol) and recent_vol > 0:
                vol_ok = float(data['volume'].iloc[-1]) >= float(recent_vol) * 1.2

        # Bullish breakout: closed above prior high THIS bar, prior bar still inside range.
        if cur_close > prev_high and prev_close <= prev_high and vol_ok:
            distance_pct = (cur_close - prev_high) / max(prev_high, 1e-9)
            return Signal(
                action='BUY',
                confidence=self._scale_confidence(distance_pct),
                metadata={
                    'prior_high': prev_high,
                    'breakout_pct': float(distance_pct),
                    'entry_reason': 'donchian_breakout_long',
                },
            )

        # Bearish breakdown: spot exit context only, no short side on Binance.th spot.
        if cur_close < prev_low and prev_close >= prev_low:
            distance_pct = (prev_low - cur_close) / max(prev_low, 1e-9)
            return Signal(
                action='SELL',
                confidence=self._scale_confidence(distance_pct),
                metadata={
                    'prior_low': prev_low,
                    'breakdown_pct': float(distance_pct),
                    'entry_reason': 'donchian_breakdown',
                },
            )

        return Signal(action='HOLD', confidence=0.0)
