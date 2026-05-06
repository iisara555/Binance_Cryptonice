"""
SimpleScalp Strategy
====================

Fast mean-reversion scalp: enters when price dips below its 21-period EMA
while StochRSI confirms oversold momentum and ADX confirms the move has
directional strength.

Entry  : price < EMA(21)  AND  StochRSI_K < 30  AND  ADX(14) > 20
Exit   : StochRSI_K > 70  OR   price > EMA(21)
SL     : 4% below entry (fixed pct)
TP     : 1% above entry  (fixed pct)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import pandas as pd
import pandas_ta as ta

from indicators import TechnicalIndicators
from .base import SignalType, StrategyBase, StrategyConfig, TradingSignal

logger = logging.getLogger(__name__)


class SimpleScalp(StrategyBase):
    """EMA21 + StochRSI + ADX scalp strategy.

    Best for : 5m scalping with 15m confirmation timeframe.
    Min bars : 50.
    """

    strategy_name: str = "simple_scalp"
    _NO_SETUP_REASON = "NO_SETUP"

    REQUIRED_COLUMNS = ("close", "high", "low")

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

        self.ema_period = max(2, int(settings.get("ema_period", 21)))
        self.stoch_rsi_period = max(2, int(settings.get("stoch_rsi_period", 14)))
        self.stoch_rsi_smooth = max(1, int(settings.get("stoch_rsi_smooth", 3)))
        self.adx_period = max(2, int(settings.get("adx_period", 14)))
        self.adx_threshold = float(settings.get("adx_threshold", 20.0))
        self.stoch_oversold = float(settings.get("stoch_oversold", 30.0))
        self.stoch_overbought = float(settings.get("stoch_overbought", 70.0))
        self.stop_loss_pct = float(settings.get("stop_loss_pct", 4.0))
        self.take_profit_pct = float(settings.get("take_profit_pct", 1.0))
        self.risk_per_trade_pct = float(settings.get("risk_per_trade_pct", 2.0))
        self.primary_timeframe = str(settings.get("timeframe", "5m"))
        self.confirm_timeframe = str(settings.get("confirm_timeframe", "15m"))

        # Warmup: RSI needs ~stoch_rsi_period*2 bars, Stoch adds stoch_rsi_smooth,
        # ADX needs ~adx_period*3 bars for Wilder smoothing to settle.
        self.min_bars = max(
            self.ema_period + 5,
            self.stoch_rsi_period * 2 + self.stoch_rsi_smooth * 2,
            self.adx_period * 3,
            50,
        )

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def get_min_candles_required(self) -> int:
        """Minimum OHLCV rows needed before the strategy can produce a signal."""
        return self.min_bars

    def get_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return a copy of *df* with EMA, StochRSI and ADX columns appended.

        Column names added:
          ``ema{period}``, ``stochrsi_k``, ``stochrsi_d``, ``adx{period}``
        """
        if df is None or df.empty or not all(c in df.columns for c in self.REQUIRED_COLUMNS):
            return df
        out = df.copy()
        close = out["close"].astype(float)
        high = out["high"].astype(float)
        low = out["low"].astype(float)

        out[f"ema{self.ema_period}"] = close.ewm(span=self.ema_period, adjust=False).mean()

        srsi = ta.stochrsi(
            close,
            length=self.stoch_rsi_period,
            rsi_length=self.stoch_rsi_period,
            k=self.stoch_rsi_smooth,
            d=self.stoch_rsi_smooth,
        )
        if srsi is not None and not srsi.empty:
            k_cols = srsi.filter(like="STOCHRSIk").columns
            d_cols = srsi.filter(like="STOCHRSId").columns
            out["stochrsi_k"] = srsi[k_cols[0]].fillna(50.0) if len(k_cols) else 50.0
            out["stochrsi_d"] = srsi[d_cols[0]].fillna(50.0) if len(d_cols) else 50.0

        adx = TechnicalIndicators.calculate_adx(high, low, close, self.adx_period)
        out[f"adx{self.adx_period}"] = adx.fillna(0.0)
        return out

    def generate_signal(
        self,
        data: pd.DataFrame,
        symbol: str = "Unknown",
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[TradingSignal]:
        """Return BUY / SELL / None based on EMA + StochRSI + ADX state."""
        if data is None or len(data) < self.min_bars:
            return None

        if not all(c in data.columns for c in self.REQUIRED_COLUMNS):
            return self._reject("MISSING_REQUIRED_COLUMNS")

        ind = self._compute_indicators(data)
        price = ind["current_price"]
        ema21 = ind["ema21"]
        srsi_k = ind["stochrsi_k"]
        adx = ind["adx"]

        if price <= 0:
            return self._reject("INVALID_PRICE")

        # ── Exit conditions (checked before entry) ──────────────────────
        exit_srsi = srsi_k > self.stoch_overbought
        exit_ema = price > ema21

        if exit_srsi or exit_ema:
            parts: list[str] = []
            if exit_srsi:
                parts.append(f"stochrsi_k={srsi_k:.1f}>{self.stoch_overbought}")
            if exit_ema:
                parts.append(f"price={price:.4f}>ema{self.ema_period}={ema21:.4f}")
            rationale = "[SimpleScalp] SELL: " + " | ".join(parts)
            return TradingSignal(
                strategy_name=self.name,
                symbol=symbol,
                signal_type=SignalType.SELL,
                confidence=0.70,
                price=price,
                metadata={
                    "exit_reason": " | ".join(parts),
                    f"ema{self.ema_period}": round(ema21, 6),
                    "stochrsi_k": round(srsi_k, 2),
                    "adx": round(adx, 2),
                    "primary_timeframe": self.primary_timeframe,
                    "trade_rationale": rationale,
                },
            )

        # ── Entry conditions ─────────────────────────────────────────────
        entry_ema = price < ema21
        entry_srsi = srsi_k < self.stoch_oversold
        entry_adx = adx > self.adx_threshold

        if entry_ema and entry_srsi and entry_adx:
            # Confidence: deeper oversold → stronger signal
            distance = self.stoch_oversold - srsi_k          # 0 .. stoch_oversold
            confidence = min(0.95, 0.50 + (distance / self.stoch_oversold) * 0.45)

            sl = round(price * (1.0 - self.stop_loss_pct / 100.0), 6)
            tp = round(price * (1.0 + self.take_profit_pct / 100.0), 6)
            rr = round(self.take_profit_pct / self.stop_loss_pct, 4)

            rationale = (
                f"[SimpleScalp] BUY | price={price:.4f}<EMA{self.ema_period}={ema21:.4f} | "
                f"StochRSI_K={srsi_k:.1f}<{self.stoch_oversold} | ADX={adx:.1f}>{self.adx_threshold}"
            )
            return TradingSignal(
                strategy_name=self.name,
                symbol=symbol,
                signal_type=SignalType.BUY,
                confidence=float(confidence),
                price=price,
                stop_loss=sl,
                take_profit=tp,
                risk_reward_ratio=rr,
                metadata={
                    f"ema{self.ema_period}": round(ema21, 6),
                    "stochrsi_k": round(srsi_k, 2),
                    "adx": round(adx, 2),
                    "stop_loss_pct": self.stop_loss_pct,
                    "take_profit_pct": self.take_profit_pct,
                    "primary_timeframe": self.primary_timeframe,
                    "confirm_timeframe": self.confirm_timeframe,
                    "trade_rationale": rationale,
                },
            )

        return self._reject("NO_ENTRY_CONDITIONS")

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _compute_indicators(self, data: pd.DataFrame) -> Dict[str, Any]:
        """Compute scalar indicator values for the last row.

        Extracted into a separate method so unit tests can monkeypatch it
        to inject exact indicator values without constructing synthetic data.
        """
        close = data["close"].astype(float)
        high = data["high"].astype(float)
        low = data["low"].astype(float)

        ema = close.ewm(span=self.ema_period, adjust=False).mean()
        ema_now = float(ema.iloc[-1])

        srsi_k = 50.0
        srsi_d = 50.0
        try:
            srsi = ta.stochrsi(
                close,
                length=self.stoch_rsi_period,
                rsi_length=self.stoch_rsi_period,
                k=self.stoch_rsi_smooth,
                d=self.stoch_rsi_smooth,
            )
            if srsi is not None and not srsi.empty:
                k_cols = srsi.filter(like="STOCHRSIk").columns
                d_cols = srsi.filter(like="STOCHRSId").columns
                if len(k_cols):
                    v = srsi[k_cols[0]].iloc[-1]
                    srsi_k = float(v) if pd.notna(v) else 50.0
                if len(d_cols):
                    v = srsi[d_cols[0]].iloc[-1]
                    srsi_d = float(v) if pd.notna(v) else 50.0
        except Exception as exc:
            logger.debug("SimpleScalp: StochRSI failed on %s: %s", "unknown", exc)

        adx_series = TechnicalIndicators.calculate_adx(high, low, close, self.adx_period)
        adx_now = (
            float(adx_series.iloc[-1])
            if not adx_series.empty and pd.notna(adx_series.iloc[-1])
            else 0.0
        )

        return {
            "current_price": float(close.iloc[-1]),
            "ema21": ema_now,
            "stochrsi_k": srsi_k,
            "stochrsi_d": srsi_d,
            "adx": adx_now,
        }

    def _reject(self, reason_code: str) -> None:
        self._last_reject_reason = str(reason_code or self._NO_SETUP_REASON).strip()
        return None

    def get_last_reject_reason(self) -> str:
        return str(self._last_reject_reason or self._NO_SETUP_REASON)
