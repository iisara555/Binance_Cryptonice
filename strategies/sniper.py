"""Sniper strategy using Dual EMA + MACD alignment."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd

from .base import SignalType, StrategyBase, StrategyConfig, TradingSignal


class SniperStrategy(StrategyBase):
    """Dual EMA + MACD strategy extracted from SignalGenerator."""

    def __init__(self, config: Optional[Dict[str, Any]] = None, *, indicators: Any) -> None:
        super().__init__(StrategyConfig(name="sniper_dual_ema_macd"))
        self._config = dict(config or {})
        self._indicators = indicators
        self._micro_trend_tolerance_pct = max(
            0.0,
            float(self._config.get("micro_trend_tolerance_pct", 0.15) or 0.15),
        )
        self._trigger_lookback_bars = max(
            1,
            int(self._config.get("macd_trigger_lookback_bars", 3) or 3),
        )
        self._last_diagnostics: Dict[str, Dict[str, str]] = {}

    @staticmethod
    def _find_recent_macd_cross(
        macd_line: pd.Series,
        signal_line: pd.Series,
        *,
        direction: str,
        lookback_bars: int,
    ) -> Optional[int]:
        if len(macd_line) < 2 or len(signal_line) < 2:
            return None

        max_offset = min(max(1, int(lookback_bars or 1)), len(macd_line) - 1)
        for offset in range(max_offset):
            idx = len(macd_line) - 1 - offset
            prev_idx = idx - 1
            if prev_idx < 0:
                break

            current_macd = float(macd_line.iloc[idx])
            previous_macd = float(macd_line.iloc[prev_idx])
            current_signal = float(signal_line.iloc[idx])
            previous_signal = float(signal_line.iloc[prev_idx])

            if direction == "buy":
                if current_macd > current_signal and previous_macd <= previous_signal:
                    return offset
            elif current_macd < current_signal and previous_macd >= previous_signal:
                return offset
        return None

    def _record_diag(self, step: str, result: str, reason: str) -> None:
        self._last_diagnostics[str(step)] = {
            "result": str(result),
            "reason": str(reason),
        }

    def get_last_diagnostics(self) -> Dict[str, Dict[str, str]]:
        return dict(self._last_diagnostics)

    def generate_signal(self, data: pd.DataFrame, symbol: str = "Unknown") -> Optional[TradingSignal]:
        self._last_diagnostics = {}

        min_bars = 210
        if data is None or len(data) < min_bars:
            self._record_diag(
                "Sniper:DataCheck",
                "REJECT",
                f"Insufficient data ({len(data) if data is not None else 0}/{min_bars} bars)",
            )
            return None

        close = data["close"].astype(float)
        high = data["high"].astype(float)
        low = data["low"].astype(float)

        ema_50 = close.ewm(span=50, adjust=False).mean()
        ema_200 = close.ewm(span=200, adjust=False).mean()
        macd_line, signal_line, _ = self._indicators.calculate_macd(close)
        atr = self._indicators.calculate_atr(high, low, close, period=14)

        current_close = float(close.iloc[-1])
        current_ema50 = float(ema_50.iloc[-1])
        current_ema200 = float(ema_200.iloc[-1])
        current_atr = float(atr.iloc[-1])

        bullish_macro = current_ema50 > current_ema200
        bearish_macro = current_ema50 < current_ema200
        self._record_diag(
            "Sniper:MacroTrend",
            "PASS" if bullish_macro or bearish_macro else "REJECT",
            (
                f"buy_ok={bullish_macro}, sell_ok={bearish_macro}, "
                f"EMA50={current_ema50:,.2f} vs EMA200={current_ema200:,.2f}"
            ),
        )
        if not bullish_macro and not bearish_macro:
            return None

        micro_tolerance = self._micro_trend_tolerance_pct / 100.0
        bullish_micro = current_close >= (current_ema50 * (1.0 - micro_tolerance))
        bearish_micro = current_close <= (current_ema50 * (1.0 + micro_tolerance))
        self._record_diag(
            "Sniper:MicroTrend",
            "PASS" if bullish_micro or bearish_micro else "REJECT",
            (
                f"buy_ok={bullish_micro}, sell_ok={bearish_micro}, "
                f"Close={current_close:,.2f} vs EMA50={current_ema50:,.2f}, "
                f"tolerance={self._micro_trend_tolerance_pct:.2f}%"
            ),
        )
        if not bullish_micro and not bearish_micro:
            return None

        confirmed_close = close.iloc[:-1]
        confirmed_timestamps = None
        if "timestamp" in data.columns:
            confirmed_timestamps = pd.to_datetime(data["timestamp"], errors="coerce").iloc[:-1]

        macd_line_conf, signal_line_conf, _ = self._indicators.calculate_macd(confirmed_close)
        buy_cross_offset = self._find_recent_macd_cross(
            macd_line_conf,
            signal_line_conf,
            direction="buy",
            lookback_bars=self._trigger_lookback_bars,
        )
        sell_cross_offset = self._find_recent_macd_cross(
            macd_line_conf,
            signal_line_conf,
            direction="sell",
            lookback_bars=self._trigger_lookback_bars,
        )

        macd_cross_up_now = buy_cross_offset == 0
        macd_cross_up_prev = buy_cross_offset == 1
        macd_cross_down_now = sell_cross_offset == 0
        macd_cross_down_prev = sell_cross_offset == 1

        buy_trigger_ok = buy_cross_offset is not None
        sell_trigger_ok = sell_cross_offset is not None

        trigger_bar = ""
        trigger_timestamp = ""
        if buy_trigger_ok:
            trigger_bar = (
                "current"
                if buy_cross_offset == 0
                else ("previous" if buy_cross_offset == 1 else f"{buy_cross_offset}_bars_ago")
            )
            if confirmed_timestamps is not None and len(confirmed_timestamps) >= (buy_cross_offset or 0) + 1:
                trigger_ts = confirmed_timestamps.iloc[-1 - int(buy_cross_offset or 0)]
                if pd.notna(trigger_ts):
                    trigger_timestamp = str(trigger_ts)
        elif sell_trigger_ok:
            trigger_bar = (
                "current"
                if sell_cross_offset == 0
                else ("previous" if sell_cross_offset == 1 else f"{sell_cross_offset}_bars_ago")
            )
            if confirmed_timestamps is not None and len(confirmed_timestamps) >= (sell_cross_offset or 0) + 1:
                trigger_ts = confirmed_timestamps.iloc[-1 - int(sell_cross_offset or 0)]
                if pd.notna(trigger_ts):
                    trigger_timestamp = str(trigger_ts)

        self._record_diag(
            "Sniper:MACDTrigger",
            "PASS" if buy_trigger_ok or sell_trigger_ok else "REJECT",
            (
                f"buy_now={macd_cross_up_now}, buy_prev={macd_cross_up_prev}, "
                f"sell_now={macd_cross_down_now}, sell_prev={macd_cross_down_prev}, "
                f"lookback={self._trigger_lookback_bars}, "
                f"trigger_bar={trigger_bar or 'none'}, trigger_timestamp={trigger_timestamp or 'n/a'}"
            ),
        )

        signal_type: Optional[SignalType] = None
        if bullish_macro and bullish_micro and buy_trigger_ok:
            signal_type = SignalType.BUY
        elif bearish_macro and bearish_micro and sell_trigger_ok:
            signal_type = SignalType.SELL
        if signal_type is None:
            return None

        if current_atr <= 0:
            self._record_diag("Sniper:ATR", "REJECT", f"ATR={current_atr} (invalid)")
            return None

        # Calculate ADX for trend strength filter and dynamic SL/TP adjustment
        adx = self._indicators.calculate_adx(high, low, close, period=14)
        current_adx = float(adx.iloc[-1]) if not adx.empty else 25.0

        # Inverse volatility scaling: wider stops in high ADX (momentum/volatility expansion)
        # to avoid whipsaws from market noise during momentum spikes.
        # ADX > 50: very strong trend, high volatility → wider SL (2.0x) to absorb noise.
        # ADX 30-50: strong trend, moderate volatility → balanced SL/TP (1.5x).
        # ADX < 30: weak trend → still 1.5x ATR floor (the prior 1.0x floor was the
        #           dominant cause of premature stop-outs and the 5.3% win rate).
        if current_adx > 50:
            sl_mult = 2.0
            tp_mult = 3.0
            adx_context = "Very Strong Trend (Wide SL)"
        elif current_adx > 30:
            sl_mult = 1.5
            tp_mult = 3.0
            adx_context = "Strong Trend"
        else:
            sl_mult = 1.5
            tp_mult = 2.5
            adx_context = "Weak Trend (1.5x ATR Floor)"

        self._record_diag(
            "Sniper:ADX",
            "PASS",
            f"ADX={current_adx:.1f} ({adx_context}) → SL_mult={sl_mult}, TP_mult={tp_mult}",
        )

        if signal_type is SignalType.BUY:
            stop_loss = current_close - (sl_mult * current_atr)
            take_profit = current_close + (tp_mult * current_atr)
            risk = current_close - stop_loss
            reward = take_profit - current_close
            macro_text = f"EMA50({current_ema50:,.0f})>EMA200({current_ema200:,.0f})"
            micro_text = f"Close({current_close:,.0f})>EMA50"
            cross_text = "BUY MACD cross"
            trigger_side = "buy"
        else:
            stop_loss = current_close + (sl_mult * current_atr)
            take_profit = current_close - (tp_mult * current_atr)
            risk = stop_loss - current_close
            reward = current_close - take_profit
            macro_text = f"EMA50({current_ema50:,.0f})<EMA200({current_ema200:,.0f})"
            micro_text = f"Close({current_close:,.0f})<EMA50"
            cross_text = "SELL MACD cross"
            trigger_side = "sell"
        rr_ratio = reward / risk if risk > 0 else 0.0

        trade_rationale = (
            f"[Sniper] {signal_type.value} | Alignment: "
            f"{macro_text}, {micro_text}, "
            f"{cross_text} ({trigger_bar or 'n/a'}) | "
            f"SL={stop_loss:,.0f} TP={take_profit:,.0f} RR={rr_ratio:.2f} | "
            f"ATR={current_atr:,.0f}"
        )
        self._record_diag(
            "Sniper:Result",
            "PASS",
            (
                f"{signal_type.value} conf=1.0, trigger_bar={trigger_bar or 'n/a'}, "
                f"trigger_timestamp={trigger_timestamp or 'n/a'}, SL={stop_loss:,.2f}, "
                f"TP={take_profit:,.2f}, RR={rr_ratio:.2f}"
            ),
        )

        return TradingSignal(
            strategy_name=self.name,
            symbol=symbol,
            signal_type=signal_type,
            confidence=1.0,
            price=current_close,
            timestamp=datetime.now(),
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_reward_ratio=rr_ratio,
            metadata={
                "ema50": current_ema50,
                "ema200": current_ema200,
                "atr": current_atr,
                "adx": current_adx,
                "adx_context": adx_context,
                "sl_multiplier": sl_mult,
                "tp_multiplier": tp_mult,
                "macd": float(macd_line_conf.iloc[-1]),
                "macd_signal": float(signal_line_conf.iloc[-1]),
                "macd_cross_bar": trigger_bar,
                "macd_cross_timestamp": trigger_timestamp,
                "macd_cross_direction": trigger_side,
                "trade_rationale": trade_rationale,
            },
        )
