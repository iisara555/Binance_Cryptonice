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

    def __init__(self, config: Any = None) -> None:
        settings: Dict[str, Any] = config if isinstance(config, dict) else {}
        super().__init__(
            StrategyConfig(
                name=settings.get("name", self.strategy_name),
                enabled=bool(settings.get("enabled", True)),
            )
        )
        self.settings = settings

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
        self.min_buy_confidence = float(settings.get("min_buy_confidence", 0.50))
        self.min_sell_confidence = float(settings.get("min_sell_confidence", 0.50))

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
            return None

        if not all(col in data.columns for col in self.REQUIRED_COLUMNS):
            return None

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
            return None

        if fish_sig.empty or tema_sig.empty or ao_sig.empty or adx_series.empty:
            return None

        last_fisher = int(fish_sig.iloc[-1])
        last_tema = int(tema_sig.iloc[-1])
        last_ao = int(ao_sig.iloc[-1])
        last_adx = float(adx_series.iloc[-1]) if pd.notna(adx_series.iloc[-1]) else 0.0
        last_vol_ok = bool(vol_ok.iloc[-1]) if not vol_ok.empty and pd.notna(vol_ok.iloc[-1]) else False

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

        confirmations_bear: list[str] = []
        if last_fisher == -1:
            confirmations_bear.append("FISHER_BEAR")
        if last_tema == -1:
            confirmations_bear.append("TEMA_CROSS_DOWN")
        if last_ao == -1:
            confirmations_bear.append("AO_BEAR_MOM")

        current_price = float(close.iloc[-1])
        if current_price <= 0:
            return None

        if len(confirmations_bull) >= self.MIN_CONFIRMATIONS_BUY:
            return self._build_buy_signal(
                symbol=symbol,
                price=current_price,
                high=high,
                low=low,
                close=close,
                confirmations=confirmations_bull,
                last_adx=last_adx,
            )

        if len(confirmations_bear) >= self.MIN_CONFIRMATIONS_SELL:
            return self._build_sell_signal(
                symbol=symbol,
                price=current_price,
                confirmations=confirmations_bear,
                last_adx=last_adx,
            )

        return None

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
    ) -> Optional[TradingSignal]:
        """Build BUY signal with ATR-based SL/TP. Returns None if ATR is invalid."""
        atr_series = TechnicalIndicators.calculate_atr(high, low, close, self.atr_period)
        if atr_series.empty:
            return None

        atr_val = float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else 0.0
        if atr_val <= 0:
            logger.debug(
                "MacheteV8bLite: ATR invalid (%.6f) for %s -> skipping BUY",
                atr_val,
                symbol,
            )
            return None

        sl_distance = atr_val * self.atr_multiplier
        tp_distance = sl_distance * self.risk_reward
        stop_loss = round(price - sl_distance, 6)
        take_profit = round(price + tp_distance, 6)

        if not (stop_loss < price < take_profit):
            return None

        risk = price - stop_loss
        reward = take_profit - price
        actual_rr = reward / risk if risk > 0 else self.risk_reward

        confidence = min(0.90, len(confirmations) / 5.0)

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
                "atr_multiplier": self.atr_multiplier,
                "trade_rationale": rationale,
            },
        )

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
