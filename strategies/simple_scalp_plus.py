"""
SimpleScalp+ Strategy
=====================

Fast confluence strategy for scalping mode.
Requires 4/5 confirmations from Hull MA, EMA trend, RSI regime,
VWAP location, and volume participation.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

from indicators import TechnicalIndicators, hull_signal, volume_confirmation, vwap
from .base import SignalType, StrategyBase, StrategyConfig, TradingSignal


class SimpleScalpPlus(StrategyBase):
    """SimpleScalp+ - Hull + EMA + RSI + VWAP + Volume (4/5 confirmation)."""

    strategy_name: str = "simple_scalp_plus"
    MIN_CONFIRMATIONS_BUY = 4
    MIN_CONFIRMATIONS_SELL = 4

    def __init__(self, config: Any = None) -> None:
        settings: Dict[str, Any] = config if isinstance(config, dict) else {}
        super().__init__(
            StrategyConfig(
                name=settings.get("name", self.strategy_name),
                enabled=bool(settings.get("enabled", True)),
            )
        )
        self.settings = settings
        self.hull_period = max(4, int(settings.get("hull_period", 16)))
        self.ema_fast = max(2, int(settings.get("ema_fast", 9)))
        self.ema_slow = max(self.ema_fast + 1, int(settings.get("ema_slow", 21)))
        self.rsi_period = max(2, int(settings.get("rsi_period", 7)))
        self.rsi_buy_min = float(settings.get("rsi_buy_min", 48.0))
        self.rsi_sell_max = float(settings.get("rsi_sell_max", 52.0))
        self.volume_period = max(2, int(settings.get("volume_period", 20)))
        self.volume_threshold = float(settings.get("volume_threshold", 1.05))
        self.atr_period = max(2, int(settings.get("atr_period", 14)))
        self.atr_multiplier = float(settings.get("atr_multiplier", 1.2))
        self.risk_reward = float(settings.get("risk_reward", 1.8))
        self.min_buy_confidence = float(settings.get("min_buy_confidence", 0.55))
        self.min_sell_confidence = float(settings.get("min_sell_confidence", 0.55))
        self.min_bars = max(
            self.ema_slow + 5,
            self.hull_period + 5,
            self.volume_period + 5,
            self.atr_period * 2,
            40,
        )

    def generate_signal(self, data: pd.DataFrame, symbol: str = "Unknown") -> Optional[TradingSignal]:
        if data is None or len(data) < self.min_bars:
            return None
        required = ("close", "high", "low", "volume")
        if not all(column in data.columns for column in required):
            return None

        close = data["close"].astype(float)
        high = data["high"].astype(float)
        low = data["low"].astype(float)
        volume = data["volume"].astype(float)

        ema_fast_series = close.ewm(span=self.ema_fast, adjust=False).mean()
        ema_slow_series = close.ewm(span=self.ema_slow, adjust=False).mean()
        rsi_series = TechnicalIndicators.calculate_rsi(close, self.rsi_period)
        vwap_series = vwap(high, low, close, volume, period=self.volume_period)
        hull_sig = hull_signal(close, period=self.hull_period)
        vol_ok = volume_confirmation(volume, period=self.volume_period, threshold=self.volume_threshold)
        atr_series = TechnicalIndicators.calculate_atr(high, low, close, self.atr_period)

        current_price = float(close.iloc[-1])
        if current_price <= 0:
            return None

        ema_fast_now = float(ema_fast_series.iloc[-1])
        ema_slow_now = float(ema_slow_series.iloc[-1])
        rsi_now = float(rsi_series.iloc[-1]) if pd.notna(rsi_series.iloc[-1]) else 50.0
        vwap_now = float(vwap_series.iloc[-1]) if pd.notna(vwap_series.iloc[-1]) else current_price
        hull_now = int(hull_sig.iloc[-1]) if not hull_sig.empty and pd.notna(hull_sig.iloc[-1]) else 0
        volume_ok_now = bool(vol_ok.iloc[-1]) if not vol_ok.empty and pd.notna(vol_ok.iloc[-1]) else False
        atr_now = float(atr_series.iloc[-1]) if not atr_series.empty and pd.notna(atr_series.iloc[-1]) else 0.0
        if atr_now <= 0:
            return None

        buy_confirmations: list[str] = []
        if hull_now == 1:
            buy_confirmations.append("HULL_UP")
        if ema_fast_now > ema_slow_now:
            buy_confirmations.append("EMA_TREND_UP")
        if rsi_now >= self.rsi_buy_min:
            buy_confirmations.append("RSI_BULL")
        if current_price >= vwap_now:
            buy_confirmations.append("ABOVE_VWAP")
        if volume_ok_now:
            buy_confirmations.append("VOLUME_OK")

        sell_confirmations: list[str] = []
        if hull_now == -1:
            sell_confirmations.append("HULL_DOWN")
        if ema_fast_now < ema_slow_now:
            sell_confirmations.append("EMA_TREND_DOWN")
        if rsi_now <= self.rsi_sell_max:
            sell_confirmations.append("RSI_BEAR")
        if current_price <= vwap_now:
            sell_confirmations.append("BELOW_VWAP")
        if volume_ok_now:
            sell_confirmations.append("VOLUME_OK")

        if len(buy_confirmations) >= self.MIN_CONFIRMATIONS_BUY:
            sl_distance = atr_now * self.atr_multiplier
            stop_loss = round(current_price - sl_distance, 6)
            take_profit = round(current_price + (sl_distance * self.risk_reward), 6)
            risk = current_price - stop_loss
            reward = take_profit - current_price
            rr = reward / risk if risk > 0 else self.risk_reward
            confidence = min(0.95, len(buy_confirmations) / 5.0 + 0.10)
            return TradingSignal(
                strategy_name=self.name,
                symbol=symbol,
                signal_type=SignalType.BUY,
                confidence=confidence,
                price=current_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                risk_reward_ratio=rr,
                metadata={
                    "confirmations": buy_confirmations,
                    "confirmations_count": len(buy_confirmations),
                    "rsi": round(rsi_now, 2),
                    "atr": round(atr_now, 6),
                    "trade_rationale": (
                        f"[SimpleScalp+] BUY {len(buy_confirmations)}/5 confirmations "
                        f"[{', '.join(buy_confirmations)}]"
                    ),
                },
            )

        if len(sell_confirmations) >= self.MIN_CONFIRMATIONS_SELL:
            confidence = min(0.90, len(sell_confirmations) / 5.0 + 0.10)
            return TradingSignal(
                strategy_name=self.name,
                symbol=symbol,
                signal_type=SignalType.SELL,
                confidence=confidence,
                price=current_price,
                metadata={
                    "confirmations": sell_confirmations,
                    "confirmations_count": len(sell_confirmations),
                    "rsi": round(rsi_now, 2),
                    "trade_rationale": (
                        f"[SimpleScalp+] SELL {len(sell_confirmations)}/5 confirmations "
                        f"[{', '.join(sell_confirmations)}]"
                    ),
                },
            )

        return None

    def validate_signal(self, signal: Any, data: pd.DataFrame) -> bool:
        if not super().validate_signal(signal, data):
            return False
        if signal.signal_type == SignalType.BUY:
            return float(signal.confidence) >= self.min_buy_confidence
        if signal.signal_type == SignalType.SELL:
            return float(signal.confidence) >= self.min_sell_confidence
        return False
