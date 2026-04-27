"""
Momentum Strategy.

Edge: RSI exit-from-oversold (cross back above 30) and exit-from-overbought
(cross back below 70). Fires only on the actual crossover bar — staying inside
the threshold no longer continuously emits BUY/SELL like the legacy version
did (root cause of the over-trading + 5.3% win rate).
"""
import pandas as pd

from .base import Signal, StrategyBase


class MomentumStrategy(StrategyBase):
    """Momentum strategy using RSI threshold crossovers."""

    OVERSOLD = 30.0
    OVERBOUGHT = 70.0

    @staticmethod
    def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / loss.replace(0, pd.NA)
        return 100 - (100 / (1 + rs))

    def analyze(self, data: pd.DataFrame) -> Signal:
        if len(data) < 20 or 'close' not in data.columns:
            return Signal(action='HOLD', confidence=0.0)

        rsi = self._rsi(data['close'], period=14)
        if pd.isna(rsi.iloc[-1]) or pd.isna(rsi.iloc[-2]):
            return Signal(action='HOLD', confidence=0.0)

        prev_rsi = float(rsi.iloc[-2])
        cur_rsi = float(rsi.iloc[-1])

        # Bullish: RSI was oversold, now bouncing back above 30.
        if prev_rsi <= self.OVERSOLD < cur_rsi:
            confidence = min(0.85, 0.6 + (cur_rsi - self.OVERSOLD) / 100.0)
            return Signal(
                action='BUY',
                confidence=float(confidence),
                metadata={'rsi': cur_rsi, 'rsi_prev': prev_rsi, 'entry_reason': 'rsi_oversold_recovery'},
            )

        # Bearish: RSI was overbought, now rolling back below 70.
        if prev_rsi >= self.OVERBOUGHT > cur_rsi:
            confidence = min(0.85, 0.6 + (self.OVERBOUGHT - cur_rsi) / 100.0)
            return Signal(
                action='SELL',
                confidence=float(confidence),
                metadata={'rsi': cur_rsi, 'rsi_prev': prev_rsi, 'entry_reason': 'rsi_overbought_rollover'},
            )

        return Signal(action='HOLD', confidence=0.0)
