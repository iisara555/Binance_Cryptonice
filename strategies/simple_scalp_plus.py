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
    _NO_SETUP_REASON = "NO_SETUP"

    def __init__(self, config: Any = None) -> None:
        settings: Dict[str, Any] = config if isinstance(config, dict) else {}
        super().__init__(
            StrategyConfig(
                name=settings.get("name", self.strategy_name),
                enabled=bool(settings.get("enabled", True)),
            )
        )
        self.settings = settings
        self._last_reject_reason = self._NO_SETUP_REASON
        self.hull_period = max(4, int(settings.get("hull_period", 16)))
        self.ema_fast = max(2, int(settings.get("ema_fast", 9)))
        self.ema_slow = max(self.ema_fast + 1, int(settings.get("ema_slow", 21)))
        self.rsi_period = max(2, int(settings.get("rsi_period", 14)))
        self.rsi_buy_min = float(settings.get("rsi_buy_min", 50.0))
        self.rsi_buy_max = float(settings.get("rsi_buy_max", 70.0))
        self.rsi_sell_max = float(settings.get("rsi_sell_max", 48.0))
        self.volume_period = max(2, int(settings.get("volume_period", 20)))
        self.volume_threshold = float(settings.get("volume_threshold", 1.05))
        self.adx_period = max(2, int(settings.get("adx_period", 14)))
        self.adx_threshold = float(settings.get("adx_threshold", 18.0))
        self.macd_fast = max(2, int(settings.get("macd_fast", 12)))
        self.macd_slow = max(self.macd_fast + 1, int(settings.get("macd_slow", 26)))
        self.macd_signal = max(2, int(settings.get("macd_signal", 9)))
        self.stoch_period = max(3, int(settings.get("stoch_period", 14)))
        self.stoch_buy_k_min = float(settings.get("stoch_buy_k_min", 20.0))
        self.stoch_buy_k_max = float(settings.get("stoch_buy_k_max", 80.0))
        self.stoch_sell_k_max = float(settings.get("stoch_sell_k_max", 80.0))
        self.atr_period = max(2, int(settings.get("atr_period", 14)))
        self.atr_multiplier = float(settings.get("atr_multiplier", 1.2))
        self.risk_reward = float(settings.get("risk_reward", 1.8))
        self.min_buy_confidence = float(settings.get("min_buy_confidence", 0.70))
        self.min_sell_confidence = float(settings.get("min_sell_confidence", 0.55))
        self.min_confirmations_buy = max(3, min(7, int(settings.get("min_confirmations_buy", 5))))
        self.min_confirmations_sell = max(3, min(7, int(settings.get("min_confirmations_sell", 4))))
        self.primary_timeframe = str(settings.get("primary_timeframe", "5m"))
        self.informative_timeframe = str(settings.get("informative_timeframe", "1h"))
        self.min_bars = max(
            self.ema_slow + 5,
            self.hull_period + 5,
            self.volume_period + 5,
            self.atr_period * 2,
            self.stoch_period + 5,
            40,
        )

    def generate_signal(self, data: pd.DataFrame, symbol: str = "Unknown") -> Optional[TradingSignal]:
        if data is None or len(data) < self.min_bars:
            return self._reject("INSUFFICIENT_BARS")
        required = ("close", "high", "low", "volume")
        if not all(column in data.columns for column in required):
            return self._reject("MISSING_REQUIRED_COLUMNS")

        close = data["close"].astype(float)
        high = data["high"].astype(float)
        low = data["low"].astype(float)
        volume = data["volume"].astype(float)

        ema_fast_series = close.ewm(span=self.ema_fast, adjust=False).mean()
        ema_slow_series = close.ewm(span=self.ema_slow, adjust=False).mean()
        rsi_series = TechnicalIndicators.calculate_rsi(close, self.rsi_period)
        adx_series = TechnicalIndicators.calculate_adx(high, low, close, self.adx_period)
        macd_line, macd_signal, macd_hist = TechnicalIndicators.calculate_macd(
            close,
            fast=self.macd_fast,
            slow=self.macd_slow,
            signal=self.macd_signal,
        )
        stoch_k, stoch_d = TechnicalIndicators.calculate_stochastic(high, low, close, period=self.stoch_period)
        vwap_series = vwap(high, low, close, volume, period=self.volume_period)
        hull_sig = hull_signal(close, period=self.hull_period)
        vol_ok = volume_confirmation(volume, period=self.volume_period, threshold=self.volume_threshold)
        atr_series = TechnicalIndicators.calculate_atr(high, low, close, self.atr_period)

        current_price = float(close.iloc[-1])
        if current_price <= 0:
            return self._reject("INVALID_PRICE")

        ema_fast_now = float(ema_fast_series.iloc[-1])
        ema_slow_now = float(ema_slow_series.iloc[-1])
        rsi_now = float(rsi_series.iloc[-1]) if pd.notna(rsi_series.iloc[-1]) else 50.0
        adx_now = float(adx_series.iloc[-1]) if not adx_series.empty and pd.notna(adx_series.iloc[-1]) else 0.0
        macd_now = float(macd_line.iloc[-1]) if not macd_line.empty and pd.notna(macd_line.iloc[-1]) else 0.0
        macd_sig_now = float(macd_signal.iloc[-1]) if not macd_signal.empty and pd.notna(macd_signal.iloc[-1]) else 0.0
        macd_hist_now = float(macd_hist.iloc[-1]) if not macd_hist.empty and pd.notna(macd_hist.iloc[-1]) else 0.0
        stoch_k_now = float(stoch_k.iloc[-1]) if not stoch_k.empty and pd.notna(stoch_k.iloc[-1]) else 50.0
        stoch_d_now = float(stoch_d.iloc[-1]) if not stoch_d.empty and pd.notna(stoch_d.iloc[-1]) else 50.0
        vwap_now = float(vwap_series.iloc[-1]) if pd.notna(vwap_series.iloc[-1]) else current_price
        hull_now = int(hull_sig.iloc[-1]) if not hull_sig.empty and pd.notna(hull_sig.iloc[-1]) else 0
        volume_ok_now = bool(vol_ok.iloc[-1]) if not vol_ok.empty and pd.notna(vol_ok.iloc[-1]) else False
        atr_now = float(atr_series.iloc[-1]) if not atr_series.empty and pd.notna(atr_series.iloc[-1]) else 0.0
        if atr_now <= 0:
            return self._reject("ATR_INVALID")

        buy_confirmations: list[str] = []
        if hull_now == 1:
            buy_confirmations.append("HULL_UP")
        if ema_fast_now > ema_slow_now:
            buy_confirmations.append("EMA_TREND_UP")
        if self.rsi_buy_min <= rsi_now <= self.rsi_buy_max:
            buy_confirmations.append("RSI_BULL")
        if adx_now >= self.adx_threshold:
            buy_confirmations.append("ADX_TRENDING")
        if macd_now > macd_sig_now and macd_hist_now > 0:
            buy_confirmations.append("MACD_BULL")
        if self.stoch_buy_k_min <= stoch_k_now <= self.stoch_buy_k_max and stoch_k_now >= stoch_d_now:
            buy_confirmations.append("STOCH_BULL")
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
        if adx_now >= self.adx_threshold:
            sell_confirmations.append("ADX_TRENDING")
        if macd_now < macd_sig_now and macd_hist_now < 0:
            sell_confirmations.append("MACD_BEAR")
        if stoch_k_now >= self.stoch_sell_k_max and stoch_k_now <= stoch_d_now:
            sell_confirmations.append("STOCH_BEAR")
        if current_price <= vwap_now:
            sell_confirmations.append("BELOW_VWAP")
        if volume_ok_now:
            sell_confirmations.append("VOLUME_OK")

        if len(buy_confirmations) >= self.min_confirmations_buy:
            sl_distance = atr_now * self.atr_multiplier
            stop_loss = round(current_price - sl_distance, 6)
            take_profit = round(current_price + (sl_distance * self.risk_reward), 6)
            risk = current_price - stop_loss
            reward = take_profit - current_price
            rr = reward / risk if risk > 0 else self.risk_reward
            confidence = min(0.97, len(buy_confirmations) / 7.0 + 0.22)
            self._last_reject_reason = self._NO_SETUP_REASON
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
                    "adx": round(adx_now, 2),
                    "macd_hist": round(macd_hist_now, 6),
                    "stoch_k": round(stoch_k_now, 2),
                    "stoch_d": round(stoch_d_now, 2),
                    "primary_timeframe": self.primary_timeframe,
                    "informative_timeframe": self.informative_timeframe,
                    "atr": round(atr_now, 6),
                    "trade_rationale": (
                        f"[SimpleScalp+] BUY {len(buy_confirmations)}/7 confirmations "
                        f"[{', '.join(buy_confirmations)}]"
                    ),
                },
            )

        if len(sell_confirmations) >= self.min_confirmations_sell:
            confidence = min(0.93, len(sell_confirmations) / 7.0 + 0.18)
            self._last_reject_reason = self._NO_SETUP_REASON
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
                    "adx": round(adx_now, 2),
                    "macd_hist": round(macd_hist_now, 6),
                    "stoch_k": round(stoch_k_now, 2),
                    "stoch_d": round(stoch_d_now, 2),
                    "primary_timeframe": self.primary_timeframe,
                    "informative_timeframe": self.informative_timeframe,
                    "trade_rationale": (
                        f"[SimpleScalp+] SELL {len(sell_confirmations)}/7 confirmations "
                        f"[{', '.join(sell_confirmations)}]"
                    ),
                },
            )

        return self._reject("INSUFFICIENT_CONFIRMATIONS")

    def validate_signal(self, signal: Any, data: pd.DataFrame) -> bool:
        if not super().validate_signal(signal, data):
            return False
        if signal.signal_type == SignalType.BUY:
            return float(signal.confidence) >= self.min_buy_confidence
        if signal.signal_type == SignalType.SELL:
            return float(signal.confidence) >= self.min_sell_confidence
        return False

    def _reject(self, reason_code: str) -> Optional[TradingSignal]:
        self._last_reject_reason = str(reason_code or self._NO_SETUP_REASON).strip() or self._NO_SETUP_REASON
        return None

    def get_last_reject_reason(self) -> str:
        return str(self._last_reject_reason or self._NO_SETUP_REASON)
