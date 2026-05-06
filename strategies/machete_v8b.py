"""
MacheteV8b Strategy (Full)
==========================

Community strategy ported from Freqtrade's MacheteV8b.  Six independent
signal groups use OR logic: any single group firing is enough to enter.
Exit is driven entirely by the dynamic ROI table and the 10% stop-loss —
the strategy does NOT emit SELL signals.

Signal groups
-------------
1. quickie       : TEMA(9) crossover + above SMA(200) + lower-BB touch
2. scalp         : price above EMA(5) + Stochastic(%K) < 20
3. adx_smas      : ADX(14) > 20 + SMA(3) crosses above SMA(6)
4. awesome_macd  : Awesome Oscillator positive + MACD bullish cross
5. gettin_moist  : RSI(7) < 40 + MACD crosses up + ROC(6) > 0
6. hlhb          : EMA(5) crosses above EMA(10) + RSI(10) > 50 + ADX > 20

Entry      : ANY enabled group fires  → BUY
Exit       : dynamic ROI table OR -10% stop-loss (handled by bot)
Confidence : groups_fired / total_enabled_groups
Min bars   : 205  (SMA-200 dominates warmup)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pandas as pd

from indicators import (
    TechnicalIndicators,
    awesome_oscillator,
    tema,
)
from .base import SignalType, StrategyBase, StrategyConfig, TradingSignal

logger = logging.getLogger(__name__)


class MacheteV8b(StrategyBase):
    """MacheteV8b — six OR-logic signal groups with dynamic ROI exit."""

    strategy_name: str = "machete_v8b"
    _NO_SETUP_REASON = "NO_SETUP"

    REQUIRED_COLUMNS = ("open", "close", "high", "low", "volume")

    # Default dynamic ROI table (minutes → minimum profit fraction)
    MINIMAL_ROI: Dict[str, float] = {
        "0": 0.279,
        "92": 0.109,
        "245": 0.059,
        "561": 0.0,
    }

    # All group names in declaration order (used for confidence denominator)
    _ALL_GROUPS: List[str] = [
        "quickie",
        "scalp",
        "adx_smas",
        "awesome_macd",
        "gettin_moist",
        "hlhb",
    ]

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

        self.primary_timeframe = str(settings.get("timeframe", "15m"))
        self.informative_timeframe = str(settings.get("informative_timeframe", "1h"))
        self.stoploss_pct = abs(float(settings.get("stoploss", -0.10))) * 100.0  # store as positive %

        # Which groups are enabled (default: all on)
        signals_cfg: Dict[str, bool] = dict(settings.get("signals", {}) or {})
        self._enabled_groups: List[str] = [
            g for g in self._ALL_GROUPS if bool(signals_cfg.get(g, True))
        ]

        # Per-group indicator periods (overridable)
        self.tema_fast = max(2, int(settings.get("tema_fast", 9)))
        self.sma_slow = max(10, int(settings.get("sma_slow", 200)))
        self.bb_period = max(5, int(settings.get("bb_period", 20)))
        self.bb_std = float(settings.get("bb_std", 2.0))
        self.ema_fast = max(2, int(settings.get("ema_fast", 5)))
        self.ema_med = max(self.ema_fast + 1, int(settings.get("ema_med", 10)))
        self.stoch_period = max(3, int(settings.get("stoch_period", 5)))
        self.stoch_oversold = float(settings.get("stoch_oversold", 20.0))
        self.adx_period = max(2, int(settings.get("adx_period", 14)))
        self.adx_threshold = float(settings.get("adx_threshold", 20.0))
        self.sma_fast = max(2, int(settings.get("sma_fast", 3)))
        self.sma_mid = max(self.sma_fast + 1, int(settings.get("sma_mid", 6)))
        self.ao_fast = max(2, int(settings.get("ao_fast", 5)))
        self.ao_slow = max(self.ao_fast + 1, int(settings.get("ao_slow", 34)))
        self.macd_fast = max(2, int(settings.get("macd_fast", 12)))
        self.macd_slow = max(self.macd_fast + 1, int(settings.get("macd_slow", 26)))
        self.macd_signal = max(2, int(settings.get("macd_signal", 9)))
        self.rsi_fast = max(2, int(settings.get("rsi_fast", 7)))
        self.rsi_med = max(2, int(settings.get("rsi_med", 10)))
        self.roc_period = max(2, int(settings.get("roc_period", 6)))
        self.rsi_gettin_moist_threshold = float(settings.get("rsi_gettin_moist_threshold", 40.0))
        self.rsi_hlhb_threshold = float(settings.get("rsi_hlhb_threshold", 50.0))
        self.bb_touch_buffer = float(settings.get("bb_touch_buffer", 0.01))  # 1% above lower band

        # SMA-200 dominates warmup
        self.min_bars = max(
            self.sma_slow + 5,
            self.ao_slow + 5,
            self.macd_slow + self.macd_signal + 5,
            self.adx_period * 3,
            205,
        )

        # Override ROI table from config if provided
        roi_cfg = settings.get("minimal_roi")
        if isinstance(roi_cfg, dict):
            self._minimal_roi = {str(k): float(v) for k, v in roi_cfg.items()}
        else:
            self._minimal_roi = dict(self.MINIMAL_ROI)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def get_min_candles_required(self) -> int:
        return self.min_bars

    def get_minimal_roi(self) -> Dict[str, float]:
        """Return the dynamic ROI table (minutes → min profit fraction)."""
        return dict(self._minimal_roi)

    def generate_signal(
        self,
        data: pd.DataFrame,
        symbol: str = "Unknown",
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[TradingSignal]:
        """Return a BUY TradingSignal when any enabled group fires, else None."""
        if data is None or len(data) < self.min_bars:
            return self._reject("INSUFFICIENT_BARS")

        if not all(c in data.columns for c in self.REQUIRED_COLUMNS):
            return self._reject("MISSING_REQUIRED_COLUMNS")

        price = float(data["close"].astype(float).iloc[-1])
        if price <= 0:
            return self._reject("INVALID_PRICE")

        fired: List[str] = []
        for group in self._enabled_groups:
            checker = getattr(self, f"_check_{group}", None)
            if checker is None:
                continue
            try:
                if checker(data):
                    fired.append(group)
            except Exception as exc:
                logger.debug("MacheteV8b: group %s failed on %s: %s", group, symbol, exc)

        if not fired:
            return self._reject("NO_GROUPS_FIRED")

        total_enabled = max(1, len(self._enabled_groups))
        confidence = min(1.0, len(fired) / total_enabled)

        sl = round(price * (1.0 - self.stoploss_pct / 100.0), 6)

        rationale = (
            f"[MacheteV8b] BUY | {len(fired)}/{total_enabled} groups fired "
            f"[{', '.join(fired)}] | price={price:.4f} SL={sl:.4f}"
        )
        self._last_reject_reason = self._NO_SETUP_REASON
        return TradingSignal(
            strategy_name=self.name,
            symbol=symbol,
            signal_type=SignalType.BUY,
            confidence=confidence,
            price=price,
            stop_loss=sl,
            take_profit=None,           # dynamic ROI handles exit
            risk_reward_ratio=None,
            metadata={
                "groups_fired": fired,
                "groups_enabled": list(self._enabled_groups),
                "groups_fired_count": len(fired),
                "groups_total": total_enabled,
                "stoploss_pct": self.stoploss_pct,
                "minimal_roi": dict(self._minimal_roi),
                "primary_timeframe": self.primary_timeframe,
                "informative_timeframe": self.informative_timeframe,
                "trade_rationale": rationale,
            },
        )

    # ------------------------------------------------------------------ #
    # Signal group checks — each returns True/False for the last bar      #
    # Extracted so tests can monkeypatch individual checks.               #
    # ------------------------------------------------------------------ #

    def _check_quickie(self, data: pd.DataFrame) -> bool:
        """TEMA(9) crossover above + price > SMA(200) + touches lower BB."""
        close = data["close"].astype(float)
        high = data["high"].astype(float)
        low = data["low"].astype(float)

        tema9 = tema(close, self.tema_fast)
        sma200 = close.rolling(self.sma_slow).mean()
        _, _, bb_lower = TechnicalIndicators.calculate_bollinger_bands(
            close, self.bb_period, self.bb_std
        )

        if (
            tema9.empty
            or sma200.empty
            or bb_lower.empty
            or pd.isna(tema9.iloc[-1])
            or pd.isna(tema9.iloc[-2])
            or pd.isna(sma200.iloc[-1])
            or pd.isna(bb_lower.iloc[-1])
        ):
            return False

        price_now = float(close.iloc[-1])
        price_prev = float(close.iloc[-2])
        tema_now = float(tema9.iloc[-1])
        tema_prev = float(tema9.iloc[-2])
        sma200_now = float(sma200.iloc[-1])
        lower_bb = float(bb_lower.iloc[-1])

        crosses_above_tema = (price_now > tema_now) and (price_prev <= tema_prev)
        above_sma200 = price_now > sma200_now
        touches_lower_bb = price_now <= lower_bb * (1.0 + self.bb_touch_buffer)

        return crosses_above_tema and above_sma200 and touches_lower_bb

    def _check_scalp(self, data: pd.DataFrame) -> bool:
        """Price above EMA(5) AND Stochastic %K < 20 (oversold bounce)."""
        close = data["close"].astype(float)
        high = data["high"].astype(float)
        low = data["low"].astype(float)

        ema5 = close.ewm(span=self.ema_fast, adjust=False).mean()
        stoch_k, _ = TechnicalIndicators.calculate_stochastic(high, low, close, self.stoch_period)

        if stoch_k.empty or pd.isna(stoch_k.iloc[-1]) or pd.isna(ema5.iloc[-1]):
            return False

        price_now = float(close.iloc[-1])
        return (price_now > float(ema5.iloc[-1])) and (float(stoch_k.iloc[-1]) < self.stoch_oversold)

    def _check_adx_smas(self, data: pd.DataFrame) -> bool:
        """ADX(14) > 20 AND SMA(3) crosses above SMA(6)."""
        close = data["close"].astype(float)
        high = data["high"].astype(float)
        low = data["low"].astype(float)

        adx = TechnicalIndicators.calculate_adx(high, low, close, self.adx_period)
        sma3 = close.rolling(self.sma_fast).mean()
        sma6 = close.rolling(self.sma_mid).mean()

        if (
            adx.empty
            or pd.isna(adx.iloc[-1])
            or pd.isna(sma3.iloc[-1])
            or pd.isna(sma3.iloc[-2])
            or pd.isna(sma6.iloc[-1])
            or pd.isna(sma6.iloc[-2])
        ):
            return False

        adx_trending = float(adx.iloc[-1]) > self.adx_threshold
        sma_cross_up = (float(sma3.iloc[-1]) > float(sma6.iloc[-1])) and (
            float(sma3.iloc[-2]) <= float(sma6.iloc[-2])
        )
        return adx_trending and sma_cross_up

    def _check_awesome_macd(self, data: pd.DataFrame) -> bool:
        """Awesome Oscillator > 0 AND MACD bullish cross."""
        close = data["close"].astype(float)
        high = data["high"].astype(float)
        low = data["low"].astype(float)

        ao = awesome_oscillator(high, low, self.ao_fast, self.ao_slow)
        macd_line, macd_sig, _ = TechnicalIndicators.calculate_macd(
            close, self.macd_fast, self.macd_slow, self.macd_signal
        )

        if (
            ao.empty
            or macd_line.empty
            or macd_sig.empty
            or pd.isna(ao.iloc[-1])
            or pd.isna(macd_line.iloc[-1])
            or pd.isna(macd_line.iloc[-2])
            or pd.isna(macd_sig.iloc[-1])
            or pd.isna(macd_sig.iloc[-2])
        ):
            return False

        ao_positive = float(ao.iloc[-1]) > 0
        macd_cross_up = (float(macd_line.iloc[-1]) > float(macd_sig.iloc[-1])) and (
            float(macd_line.iloc[-2]) <= float(macd_sig.iloc[-2])
        )
        return ao_positive and macd_cross_up

    def _check_gettin_moist(self, data: pd.DataFrame) -> bool:
        """RSI(7) < 40 AND MACD crosses up AND ROC(6) > 0."""
        close = data["close"].astype(float)
        high = data["high"].astype(float)

        rsi = TechnicalIndicators.calculate_rsi(close, self.rsi_fast)
        macd_line, macd_sig, _ = TechnicalIndicators.calculate_macd(
            close, self.macd_fast, self.macd_slow, self.macd_signal
        )
        roc = (close - close.shift(self.roc_period)) / close.shift(self.roc_period).replace(0, float("nan")) * 100

        if (
            rsi.empty
            or macd_line.empty
            or pd.isna(rsi.iloc[-1])
            or pd.isna(macd_line.iloc[-1])
            or pd.isna(macd_line.iloc[-2])
            or pd.isna(macd_sig.iloc[-1])
            or pd.isna(macd_sig.iloc[-2])
            or pd.isna(roc.iloc[-1])
        ):
            return False

        rsi_oversold = float(rsi.iloc[-1]) < self.rsi_gettin_moist_threshold
        macd_cross_up = (float(macd_line.iloc[-1]) > float(macd_sig.iloc[-1])) and (
            float(macd_line.iloc[-2]) <= float(macd_sig.iloc[-2])
        )
        roc_positive = float(roc.iloc[-1]) > 0
        return rsi_oversold and macd_cross_up and roc_positive

    def _check_hlhb(self, data: pd.DataFrame) -> bool:
        """EMA(5) crosses above EMA(10) AND RSI(10) > 50 AND ADX > 20."""
        close = data["close"].astype(float)
        high = data["high"].astype(float)
        low = data["low"].astype(float)

        ema5 = close.ewm(span=self.ema_fast, adjust=False).mean()
        ema10 = close.ewm(span=self.ema_med, adjust=False).mean()
        rsi = TechnicalIndicators.calculate_rsi(close, self.rsi_med)
        adx = TechnicalIndicators.calculate_adx(high, low, close, self.adx_period)

        if (
            pd.isna(ema5.iloc[-1])
            or pd.isna(ema5.iloc[-2])
            or pd.isna(ema10.iloc[-1])
            or pd.isna(ema10.iloc[-2])
            or rsi.empty
            or pd.isna(rsi.iloc[-1])
            or adx.empty
            or pd.isna(adx.iloc[-1])
        ):
            return False

        ema_cross_up = (float(ema5.iloc[-1]) > float(ema10.iloc[-1])) and (
            float(ema5.iloc[-2]) <= float(ema10.iloc[-2])
        )
        rsi_bullish = float(rsi.iloc[-1]) > self.rsi_hlhb_threshold
        adx_trending = float(adx.iloc[-1]) > self.adx_threshold
        return ema_cross_up and rsi_bullish and adx_trending

    # ------------------------------------------------------------------ #
    # Utilities                                                            #
    # ------------------------------------------------------------------ #

    def _reject(self, reason_code: str) -> None:
        self._last_reject_reason = str(reason_code or self._NO_SETUP_REASON).strip()
        return None

    def get_last_reject_reason(self) -> str:
        return str(self._last_reject_reason or self._NO_SETUP_REASON)
