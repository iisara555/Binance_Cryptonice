"""
MacheteV8b-Lite Strategy
========================

Multi-indicator confirmation strategy adapted from the Freqtrade community
``MacheteV8b`` strategy, simplified to a rule-based engine (no FreqAI).

Concept
-------
Combine five independent indicators and require an "agreement" between at
least three of them before generating a BUY entry. This dramatically cuts
false signals during ranging or noisy markets and is well suited to the
``trend_only`` mode on 15m / 1h timeframes.

Indicators
----------
1. Fisher Transform - bullish/bearish reversal trigger.
2. TEMA crossover  - low-lag trend direction confirmation.
3. Awesome Oscillator (zero cross) - momentum regime change.
4. ADX             - trend strength filter.
5. Volume confirmation - price moves backed by real activity.

Entry / Exit logic
------------------
BUY  : >= 3 of 5 bullish confirmations -> TradingSignal(BUY) with ATR SL/TP.
SELL : >= 2 of 3 bearish reversal signals -> TradingSignal(SELL) (exit only).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import pandas as pd

from indicators import (
    TechnicalIndicators,
    ao_signal,
    fisher_signal,
    tema_signal,
    volume_confirmation,
)

from .base import (
    SignalType,
    StrategyBase,
    StrategyConfig,
    TradingSignal,
)


logger = logging.getLogger(__name__)


class MacheteV8bLite(StrategyBase):
    """MacheteV8b-Lite - Fisher + TEMA + AO + ADX + Volume confluence.

    Best for : TREND_FOLLOW mode on 15m/1h timeframes.
    Min bars : 50.
    """

    strategy_name: str = "machete_v8b_lite"
    MIN_CONFIRMATIONS_BUY: int = 3
    MIN_CONFIRMATIONS_SELL: int = 2

    REQUIRED_COLUMNS = ("close", "high", "low", "volume")
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

        # Indicator periods / thresholds (all overridable via config)
        self.fisher_period = max(2, int(settings.get("fisher_period", 10)))
        self.tema_fast = max(2, int(settings.get("tema_fast", 9)))
        self.tema_slow = max(self.tema_fast + 1, int(settings.get("tema_slow", 21)))
        self.ao_fast = max(2, int(settings.get("ao_fast", 5)))
        self.ao_slow = max(self.ao_fast + 1, int(settings.get("ao_slow", 34)))
        self.adx_period = max(2, int(settings.get("adx_period", 14)))
        self.adx_threshold = float(settings.get("adx_threshold", 25.0))

        # Volume filter
        self.vol_period = max(2, int(settings.get("vol_period", 20)))
        self.vol_threshold = float(settings.get("vol_threshold", 1.1))

        # Risk levels (ATR-based, aligned with risk_management conventions)
        self.atr_period = max(2, int(settings.get("atr_period", 14)))
        self.atr_multiplier = float(settings.get("atr_multiplier", 1.8))
        self.risk_reward = float(settings.get("risk_reward", 2.0))

        # Per-strategy minimum confidence floors used in validate_signal()
        self.min_buy_confidence = float(settings.get("min_buy_confidence", 0.65))
        self.min_sell_confidence = float(settings.get("min_sell_confidence", 0.50))
        self.min_confirmations_buy = max(
            1,
            min(5, int(settings.get("min_confirmations_buy", self.MIN_CONFIRMATIONS_BUY))),
        )
        self.min_confirmations_sell = max(
            1,
            min(3, int(settings.get("min_confirmations_sell", self.MIN_CONFIRMATIONS_SELL))),
        )
        self.enable_relaxed_confirmation = bool(
            settings.get("enable_relaxed_confirmation", False)
        )
        self.relaxed_requires_adx_and_volume = bool(
            settings.get("relaxed_requires_adx_and_volume", True)
        )
        self.relaxed_confirmation_delta = max(
            0, min(3, int(settings.get("relaxed_confirmation_delta", 1)))
        )
        self.ssl_period = max(4, int(settings.get("ssl_period", 10)))
        self.rmi_period = max(4, int(settings.get("rmi_period", 14)))
        self.rmi_momentum = max(1, int(settings.get("rmi_momentum", 5)))
        self.rmi_buy_min = float(settings.get("rmi_buy_min", 52.0))
        self.atr_volatility_cap_pct = float(settings.get("atr_volatility_cap_pct", 4.0))
        self.sr_lookback = max(10, int(settings.get("sr_lookback", 50)))
        self.sr_proximity_pct = float(settings.get("sr_proximity_pct", 2.0))
        self.primary_timeframe = str(settings.get("primary_timeframe", "15m"))
        self.informative_timeframe = str(settings.get("informative_timeframe", "15m"))

        # Minimum data length the strategy needs to evaluate cleanly
        self.min_bars = max(
            50,
            self.tema_slow + 5,
            self.ao_slow + 5,
            self.fisher_period * 3,
            self.adx_period * 3,
            self.atr_period * 2,
            self.vol_period + 5,
        )

    def generate_signal(
        self,
        data: pd.DataFrame,
        symbol: str = "Unknown",
    ) -> Optional[TradingSignal]:
        """Return a TradingSignal when >=3/5 BUY (or >=2/3 SELL) confirmations fire."""
        if data is None or len(data) < self.min_bars:
            return self._reject("INSUFFICIENT_BARS")

        if not all(col in data.columns for col in self.REQUIRED_COLUMNS):
            return self._reject("MISSING_REQUIRED_COLUMNS")

        close = data["close"].astype(float)
        high = data["high"].astype(float)
        low = data["low"].astype(float)
        volume = data["volume"].astype(float)

        try:
            fish_sig = fisher_signal(high, low, self.fisher_period)
            tema_sig = tema_signal(close, self.tema_fast, self.tema_slow)
            ao_sig = ao_signal(high, low, self.ao_fast, self.ao_slow)
            adx_series = TechnicalIndicators.calculate_adx(
                high, low, close, self.adx_period
            )
            vol_ok = volume_confirmation(volume, self.vol_period, self.vol_threshold)
        except Exception as exc:
            logger.debug("MacheteV8bLite: indicator failure on %s: %s", symbol, exc)
            return self._reject("INDICATOR_CALCULATION_FAILED")

        if fish_sig.empty or tema_sig.empty or ao_sig.empty or adx_series.empty:
            return self._reject("EMPTY_INDICATOR_SERIES")

        last_fisher = int(fish_sig.iloc[-1])
        last_tema = int(tema_sig.iloc[-1])
        last_ao = int(ao_sig.iloc[-1])
        last_adx = float(adx_series.iloc[-1]) if pd.notna(adx_series.iloc[-1]) else 0.0
        last_vol_ok = bool(vol_ok.iloc[-1]) if not vol_ok.empty and pd.notna(vol_ok.iloc[-1]) else False
        rmi_series = self._calculate_rmi(close, period=self.rmi_period, momentum=self.rmi_momentum)
        rmi_now = float(rmi_series.iloc[-1]) if not rmi_series.empty and pd.notna(rmi_series.iloc[-1]) else 50.0
        ssl_up, ssl_down = self._calculate_ssl_channel(high, low, period=self.ssl_period)
        ssl_bull = bool(
            not ssl_up.empty
            and not ssl_down.empty
            and pd.notna(ssl_up.iloc[-1])
            and pd.notna(ssl_down.iloc[-1])
            and float(ssl_up.iloc[-1]) > float(ssl_down.iloc[-1])
        )
        ichi_bull = self._is_ichimoku_bullish(high, low, close)

        confirmations_bull: list[str] = []
        if last_fisher == 1:
            confirmations_bull.append("FISHER_BULL")
        if last_tema == 1:
            confirmations_bull.append("TEMA_CROSS_UP")
        if last_ao == 1:
            confirmations_bull.append("AO_MOMENTUM")
        if last_adx > self.adx_threshold:
            confirmations_bull.append("ADX_TRENDING")
        if last_vol_ok:
            confirmations_bull.append("VOLUME_OK")
        if ssl_bull:
            confirmations_bull.append("SSL_BULL")
        if ichi_bull:
            confirmations_bull.append("ICHIMOKU_BULL")
        if rmi_now >= self.rmi_buy_min:
            confirmations_bull.append("RMI_MOMENTUM")

        confirmations_bear: list[str] = []
        if last_fisher == -1:
            confirmations_bear.append("FISHER_BEAR")
        if last_tema == -1:
            confirmations_bear.append("TEMA_CROSS_DOWN")
        if last_ao == -1:
            confirmations_bear.append("AO_BEAR_MOM")

        current_price = float(close.iloc[-1])
        if current_price <= 0:
            return self._reject("INVALID_CURRENT_PRICE")
        atr_pct = 0.0
        atr_series = TechnicalIndicators.calculate_atr(high, low, close, self.atr_period)
        if atr_series.empty:
            return self._reject("ATR_SERIES_EMPTY")
        atr_now = float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else 0.0
        if atr_now <= 0:
            return self._reject("ATR_INVALID")
        atr_pct = (atr_now / current_price) * 100.0 if current_price > 0 else 0.0
        if atr_pct > self.atr_volatility_cap_pct:
            return self._reject("ATR_VOLATILITY_TOO_HIGH")
        support, resistance = self._dynamic_sr(low, high, lookback=self.sr_lookback)
        if support <= 0 or resistance <= 0 or support >= resistance:
            return self._reject("SR_INVALID")
        sr_ok = (current_price >= support * (1.0 + self.sr_proximity_pct / 100.0)) and (
            current_price <= resistance * (1.0 - 0.002)
        )
        if not sr_ok:
            return self._reject("SR_GUARD_BLOCKED")

        buy_gate = self._resolve_buy_confirmation_gate(
            confirmations_bull=confirmations_bull,
            adx_trending=("ADX_TRENDING" in confirmations_bull),
            volume_ok=("VOLUME_OK" in confirmations_bull),
        )
        if len(confirmations_bull) >= int(buy_gate.get("required", self.min_confirmations_buy)):
            return self._build_buy_signal(
                symbol=symbol,
                price=current_price,
                high=high,
                low=low,
                close=close,
                confirmations=confirmations_bull,
                last_adx=last_adx,
                atr_val=atr_now,
                rmi_now=rmi_now,
                support=support,
                resistance=resistance,
                gate_mode=str(buy_gate.get("mode", "standard")),
                required_confirmations_effective=int(
                    buy_gate.get("required", self.min_confirmations_buy)
                ),
            )

        if len(confirmations_bear) >= self.min_confirmations_sell:
            self._last_reject_reason = self._NO_SETUP_REASON
            return self._build_sell_signal(
                symbol=symbol,
                price=current_price,
                confirmations=confirmations_bear,
                last_adx=last_adx,
            )

        return self._reject("INSUFFICIENT_CONFIRMATIONS")

    def _build_buy_signal(
        self,
        *,
        symbol: str,
        price: float,
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        confirmations: list[str],
        last_adx: float,
        atr_val: float,
        rmi_now: float,
        support: float,
        resistance: float,
        gate_mode: str,
        required_confirmations_effective: int,
    ) -> Optional[TradingSignal]:
        """Build BUY signal with ATR-based SL/TP. Returns None if ATR is invalid."""
        sl_distance = atr_val * self.atr_multiplier
        tp_distance = sl_distance * self.risk_reward
        stop_loss = round(price - sl_distance, 6)
        take_profit = round(price + tp_distance, 6)

        if not (stop_loss < price < take_profit):
            return self._reject("INVALID_SLTP_GEOMETRY")

        risk = price - stop_loss
        reward = take_profit - price
        actual_rr = reward / risk if risk > 0 else self.risk_reward

        confidence = min(0.90, len(confirmations) / 5.0)
        self._last_reject_reason = self._NO_SETUP_REASON

        rationale = (
            f"[MacheteV8b] BUY | {len(confirmations)}/5 confirmations "
            f"[{', '.join(confirmations)}] | "
            f"ADX={last_adx:.1f}, ATR={atr_val:.4f}, "
            f"SL={stop_loss:.4f}, TP={take_profit:.4f}, RR={actual_rr:.2f}"
        )

        return TradingSignal(
            strategy_name=self.name,
            symbol=symbol,
            signal_type=SignalType.BUY,
            confidence=confidence,
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_reward_ratio=actual_rr,
            metadata={
                "confirmations": list(confirmations),
                "confirmations_count": len(confirmations),
                "adx": round(last_adx, 2),
                "atr": round(atr_val, 6),
                "rmi": round(rmi_now, 2),
                "support": round(support, 6),
                "resistance": round(resistance, 6),
                "primary_timeframe": self.primary_timeframe,
                "informative_timeframe": self.informative_timeframe,
                "atr_multiplier": self.atr_multiplier,
                "gate_mode": str(gate_mode or "standard"),
                "required_confirmations_effective": int(
                    max(1, required_confirmations_effective)
                ),
                "trade_rationale": rationale,
            },
        )

    def _resolve_buy_confirmation_gate(
        self,
        *,
        confirmations_bull: list[str],
        adx_trending: bool,
        volume_ok: bool,
    ) -> Dict[str, Any]:
        required = int(self.min_confirmations_buy)
        mode = "standard"
        if not self.enable_relaxed_confirmation:
            return {"required": required, "mode": mode}

        allow_relaxed = bool(adx_trending and volume_ok)
        if not self.relaxed_requires_adx_and_volume:
            # Optional fallback: allow relaxed gate when at least one structural filter passes.
            allow_relaxed = bool(adx_trending or volume_ok)

        if allow_relaxed and self.relaxed_confirmation_delta > 0:
            required = max(1, required - int(self.relaxed_confirmation_delta))
            mode = "relaxed"
        return {"required": required, "mode": mode}

    @staticmethod
    def _calculate_rmi(close: pd.Series, period: int = 14, momentum: int = 5) -> pd.Series:
        momentum_delta = close - close.shift(momentum)
        up = momentum_delta.clip(lower=0.0)
        down = (-momentum_delta).clip(lower=0.0)
        avg_up = up.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        avg_down = down.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
        rs = avg_up / avg_down.replace(0, pd.NA)
        rmi = 100 - (100 / (1 + rs))
        return rmi.fillna(50.0).clip(0.0, 100.0)

    @staticmethod
    def _calculate_ssl_channel(high: pd.Series, low: pd.Series, period: int = 10) -> tuple[pd.Series, pd.Series]:
        sma_high = high.rolling(window=period).mean()
        sma_low = low.rolling(window=period).mean()
        return sma_high, sma_low

    @staticmethod
    def _is_ichimoku_bullish(high: pd.Series, low: pd.Series, close: pd.Series) -> bool:
        if len(close) < 52:
            return False
        tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
        kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2
        span_a = ((tenkan + kijun) / 2).shift(26)
        span_b = ((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26)
        if span_a.empty or span_b.empty or tenkan.empty or kijun.empty:
            return False
        if pd.isna(span_a.iloc[-1]) or pd.isna(span_b.iloc[-1]) or pd.isna(tenkan.iloc[-1]) or pd.isna(kijun.iloc[-1]):
            return False
        cloud_top = max(float(span_a.iloc[-1]), float(span_b.iloc[-1]))
        return bool(float(close.iloc[-1]) > cloud_top and float(tenkan.iloc[-1]) > float(kijun.iloc[-1]))

    @staticmethod
    def _dynamic_sr(low: pd.Series, high: pd.Series, lookback: int = 50) -> tuple[float, float]:
        support_s = low.rolling(window=lookback).min()
        resistance_s = high.rolling(window=lookback).max()
        support = float(support_s.iloc[-1]) if not support_s.empty and pd.notna(support_s.iloc[-1]) else 0.0
        resistance = float(resistance_s.iloc[-1]) if not resistance_s.empty and pd.notna(resistance_s.iloc[-1]) else 0.0
        return support, resistance

    def _reject(self, reason_code: str) -> Optional[TradingSignal]:
        self._last_reject_reason = str(reason_code or self._NO_SETUP_REASON).strip() or self._NO_SETUP_REASON
        return None

    def get_last_reject_reason(self) -> str:
        return str(self._last_reject_reason or self._NO_SETUP_REASON)

    def _build_sell_signal(
        self,
        *,
        symbol: str,
        price: float,
        confirmations: list[str],
        last_adx: float,
    ) -> TradingSignal:
        """Build SELL exit signal. SL/TP intentionally omitted (exit-only)."""
        confidence = min(0.85, len(confirmations) / 3.0)
        rationale = (
            f"[MacheteV8b] SELL | {len(confirmations)}/3 reversal signals "
            f"[{', '.join(confirmations)}] | ADX={last_adx:.1f}"
        )

        return TradingSignal(
            strategy_name=self.name,
            symbol=symbol,
            signal_type=SignalType.SELL,
            confidence=confidence,
            price=price,
            metadata={
                "confirmations": list(confirmations),
                "confirmations_count": len(confirmations),
                "adx": round(last_adx, 2),
                "trade_rationale": rationale,
            },
        )

    def validate_signal(self, signal: Any, data: pd.DataFrame) -> bool:
        """Add strategy-specific confidence floor on top of the base structural checks."""
        if not super().validate_signal(signal, data):
            return False

        if signal.signal_type == SignalType.BUY:
            return float(signal.confidence) >= self.min_buy_confidence
        if signal.signal_type == SignalType.SELL:
            return float(signal.confidence) >= self.min_sell_confidence
        return False
