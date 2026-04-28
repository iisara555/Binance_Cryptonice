"""
Scalping Strategy.
"""

from typing import Any, Dict

import numpy as np
import pandas as pd

from .base import Signal, SignalType, StrategyBase, StrategyConfig, TradingSignal


class ScalpingStrategy(StrategyBase):
    """Scalping strategy for short-term trades."""

    def __init__(self, config: Any = None):
        settings: Dict[str, Any] = config if isinstance(config, dict) else {}
        strategy_name = settings.get("name", self.__class__.__name__)
        super().__init__(StrategyConfig(name=strategy_name, enabled=settings.get("enabled", True)))
        self.settings = settings
        self.fast_ema = int(self.settings.get("fast_ema", 9))
        self.slow_ema = int(self.settings.get("slow_ema", 21))
        self.rsi_period = int(self.settings.get("rsi_period", 7))
        self.rsi_oversold = float(self.settings.get("rsi_oversold", 34.0))
        self.rsi_overbought = float(self.settings.get("rsi_overbought", 66.0))
        self.bollinger_period = int(self.settings.get("bollinger_period", 20))
        self.bollinger_std = float(self.settings.get("bollinger_std", 2.0))
        self.min_confidence = float(self.settings.get("min_entry_confidence", 0.30))
        self.stop_loss_pct = float(self.settings.get("stop_loss_pct", 0.75))
        self.take_profit_pct = float(self.settings.get("take_profit_pct", 1.75))

    @staticmethod
    def _calculate_rsi(closes: pd.Series, period: int) -> pd.Series:
        delta = closes.diff()
        gains = delta.clip(lower=0)
        losses = -delta.clip(upper=0)
        avg_gain = gains.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        avg_loss = losses.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(50.0)

    def _build_scalping_levels(self, price: float, action: str) -> tuple[float, float]:
        if action == "BUY":
            stop_loss = round(price * (1 - (self.stop_loss_pct / 100.0)), 6)
            take_profit = round(price * (1 + (self.take_profit_pct / 100.0)), 6)
        else:
            stop_loss = round(price * (1 + (self.stop_loss_pct / 100.0)), 6)
            take_profit = round(price * (1 - (self.take_profit_pct / 100.0)), 6)
        return stop_loss, take_profit

    def analyze(self, data: pd.DataFrame) -> Signal:
        min_rows = max(self.slow_ema + 2, self.bollinger_period + 2, self.rsi_period + 2)
        if len(data) < min_rows:
            return Signal(action="HOLD", confidence=0.0)

        closes = data["close"].astype(float)
        ema_fast = closes.ewm(span=self.fast_ema, adjust=False).mean()
        ema_slow = closes.ewm(span=self.slow_ema, adjust=False).mean()
        rsi = self._calculate_rsi(closes, self.rsi_period)

        rolling_mean = closes.rolling(window=self.bollinger_period).mean()
        rolling_std = closes.rolling(window=self.bollinger_period).std(ddof=0)
        lower_band = rolling_mean - (rolling_std * self.bollinger_std)
        upper_band = rolling_mean + (rolling_std * self.bollinger_std)

        current_close = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])
        current_ema_fast = float(ema_fast.iloc[-1])
        current_ema_slow = float(ema_slow.iloc[-1])
        prev_ema_fast = float(ema_fast.iloc[-2])
        prev_ema_slow = float(ema_slow.iloc[-2])
        current_rsi = float(rsi.iloc[-1])
        prev_lower_band = float(lower_band.iloc[-2]) if not pd.isna(lower_band.iloc[-2]) else 0.0
        current_lower_band = float(lower_band.iloc[-1]) if not pd.isna(lower_band.iloc[-1]) else 0.0
        prev_upper_band = float(upper_band.iloc[-2]) if not pd.isna(upper_band.iloc[-2]) else 0.0
        current_upper_band = float(upper_band.iloc[-1]) if not pd.isna(upper_band.iloc[-1]) else 0.0

        bullish_cross = prev_ema_fast <= prev_ema_slow and current_ema_fast > current_ema_slow
        bearish_cross = prev_ema_fast >= prev_ema_slow and current_ema_fast < current_ema_slow
        lower_band_bounce = prev_close <= prev_lower_band and current_close > current_lower_band
        upper_band_reject = prev_close >= prev_upper_band and current_close < current_upper_band

        if bullish_cross and current_rsi <= self.rsi_oversold and lower_band_bounce:
            confidence = max(self.min_confidence, 0.72)
            stop_loss, take_profit = self._build_scalping_levels(current_close, "BUY")
            return Signal(
                action="BUY",
                confidence=confidence,
                metadata={
                    "strategy_mode": "scalping",
                    "primary_timeframe": "5m",
                    "confirm_timeframe": "15m",
                    "trend_timeframe": "1h",
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "position_timeout_minutes": int(self.settings.get("position_timeout_minutes", 30)),
                    "entry_reason": "ema9_21_bullish_cross + rsi7_oversold + bollinger_lower_bounce",
                },
            )

        if bearish_cross and current_rsi >= self.rsi_overbought and upper_band_reject:
            confidence = max(self.min_confidence, 0.68)
            stop_loss, take_profit = self._build_scalping_levels(current_close, "SELL")
            return Signal(
                action="SELL",
                confidence=confidence,
                metadata={
                    "strategy_mode": "scalping",
                    "primary_timeframe": "5m",
                    "confirm_timeframe": "15m",
                    "trend_timeframe": "1h",
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "position_timeout_minutes": int(self.settings.get("position_timeout_minutes", 30)),
                    "entry_reason": "ema9_21_bearish_cross + rsi7_overbought + bollinger_upper_reject",
                },
            )

        return Signal(action="HOLD", confidence=0.0)

    def generate_signal(self, data: pd.DataFrame, symbol: str = "Unknown"):
        signal_action = self.analyze(data)
        sig_type = SignalType.HOLD
        if signal_action.action.upper() == "BUY":
            sig_type = SignalType.BUY
        elif signal_action.action.upper() == "SELL":
            sig_type = SignalType.SELL

        current_price = float(data["close"].iloc[-1]) if not data.empty and "close" in data else 0.0
        metadata = dict(signal_action.metadata or {})
        stop_loss = metadata.get("stop_loss")
        take_profit = metadata.get("take_profit")
        rr_ratio = 0.0
        if stop_loss and take_profit and current_price > 0:
            risk = abs(current_price - float(stop_loss))
            reward = abs(float(take_profit) - current_price)
            rr_ratio = reward / risk if risk > 0 else 0.0

        return TradingSignal(
            strategy_name=self.name,
            symbol=symbol,
            signal_type=sig_type,
            confidence=signal_action.confidence,
            price=current_price,
            risk_reward_ratio=rr_ratio,
            stop_loss=float(stop_loss) if stop_loss else None,
            take_profit=float(take_profit) if take_profit else None,
            metadata=metadata,
        )
