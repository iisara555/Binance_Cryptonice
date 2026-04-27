"""
Trend Following Strategy.

Edge: Bullish SMA(20)/SMA(50) crossover with ADX > 20 trend confirmation.
Fires ONLY on the bar where SMA20 actually crosses above SMA50 (no pyramiding
on persistent trends — that was the historical bug responsible for the 5.3%
win rate). Exits on the bearish cross-back.
"""
import logging
from typing import Optional

import pandas as pd

from cli_layout import log_signal_event

from .base import Signal, StrategyBase

logger = logging.getLogger("crypto_bot.strategy.trend_following")


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> Optional[float]:
    """Return latest ADX value (Wilder), or None if not enough data."""
    if len(close) < period * 2:
        return None
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)).astype(float) * up_move.clip(lower=0)
    minus_dm = ((down_move > up_move) & (down_move > 0)).astype(float) * down_move.clip(lower=0)

    tr1 = (high - low).abs()
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, pd.NA))
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, pd.NA))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, pd.NA)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    val = adx.iloc[-1]
    if pd.isna(val):
        return None
    return float(val)


class TrendFollowingStrategy(StrategyBase):
    """Trend following using SMA(20)/SMA(50) crossover + ADX confirmation."""

    MIN_ADX_FOR_TREND = 20.0

    def analyze(self, data: pd.DataFrame) -> Signal:
        if len(data) < 50:
            return Signal(action='HOLD', confidence=0.0)
        if not all(c in data.columns for c in ('high', 'low', 'close')):
            return Signal(action='HOLD', confidence=0.0)

        sma20 = data['close'].rolling(20).mean()
        sma50 = data['close'].rolling(50).mean()
        if pd.isna(sma20.iloc[-1]) or pd.isna(sma50.iloc[-1]) or pd.isna(sma20.iloc[-2]) or pd.isna(sma50.iloc[-2]):
            return Signal(action='HOLD', confidence=0.0)

        prev_above = sma20.iloc[-2] > sma50.iloc[-2]
        now_above = sma20.iloc[-1] > sma50.iloc[-1]

        bullish_cross = (not prev_above) and now_above
        bearish_cross = prev_above and (not now_above)

        if not (bullish_cross or bearish_cross):
            return Signal(action='HOLD', confidence=0.0)

        adx_val = _adx(data['high'], data['low'], data['close'], period=14)
        meta = {
            'sma20': float(sma20.iloc[-1]),
            'sma50': float(sma50.iloc[-1]),
            'adx': adx_val,
            'entry_reason': 'sma_crossover',
        }

        symbol = str(data.attrs.get("symbol", "?"))
        timeframe = str(data.attrs.get("timeframe", "?"))

        if bullish_cross:
            if adx_val is None or adx_val < self.MIN_ADX_FOR_TREND:
                # Reject weak/sideways markets. Log as a *state* line so
                # SuppressRepeatStateFilter collapses repeats on the console.
                logger.info(
                    "trend filter: ADX=%s below %s — bullish cross suppressed",
                    f"{adx_val:.1f}" if adx_val else "n/a",
                    self.MIN_ADX_FOR_TREND,
                    extra={"state_key": f"{symbol}:tf_adx_reject"},
                )
                return Signal(action='HOLD', confidence=0.0)

            confidence = min(0.85, 0.55 + (adx_val - self.MIN_ADX_FOR_TREND) / 50.0)
            entry_price = float(data['close'].iloc[-1])
            log_signal_event(
                logger,
                symbol=symbol,
                side='BUY',
                strategy='trend_following',
                confidence=confidence,
                trigger='sma20_cross_above_sma50',
                price=entry_price,
                timeframe=timeframe,
                extra={"adx": f"{adx_val:.1f}"},
            )
            return Signal(action='BUY', confidence=float(confidence), metadata=meta)

        # Bearish cross — used for exit context, not for spot SELL entries.
        log_signal_event(
            logger,
            symbol=symbol,
            side='SELL',
            strategy='trend_following',
            confidence=0.65,
            trigger='sma20_cross_below_sma50',
            price=float(data['close'].iloc[-1]),
            timeframe=timeframe,
        )
        return Signal(action='SELL', confidence=0.65, metadata=meta)
