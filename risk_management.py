"""
Risk Management Module for Crypto Trading Bot
===============================================
Handles position sizing, stop loss, take profit, and daily loss limits.
Includes dynamic SL/TP based on pair volatility (BTC vs ALT) and ATR-based stop loss.
"""

import json
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import MIN_RISK_REWARD_RATIO
from financial_precision import precise_add, precise_divide, precise_multiply, precise_round, precise_subtract
from risk_volatility import DEFAULT_SL_TP, VOLATILITY_CLASS

logger = logging.getLogger(__name__)
diag_logger = logging.getLogger("crypto-bot.signal_flow")


def _diag(pair: str, step: str, result: str, reason: str = ""):
    """Emit a standardised [SIGNAL_FLOW] diagnostic line."""
    from datetime import datetime as _dt

    ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    diag_logger.info(
        f"[SIGNAL_FLOW] {ts} | {pair} | Step: {step} | Result: {result}" + (f" | Reason: {reason}" if reason else "")
    )


# Binance Thailand trading fees: 0.1% per side, 0.2% round trip.
BINANCE_TH_FEE_PER_SIDE = 0.001
BINANCE_TH_FEE_ROUND_TRIP = 0.002

DEFAULT_MIN_ORDER_QUOTE = 10.0
MIN_ORDER_BUFFER = 1.10  # 10% safety buffer
# PreTradeGate compares pos_pct vs max_position_per_trade_pct; float jitter can show equal in logs but fail `<=`.
PRETRADE_GATE_POSITION_PCT_EPSILON = 1e-9


# --- NEW: SPEC_04 — Per-mode ATR profile -----------------------------------
# Each trading mode uses its own ATR multiplier so SL/TP distance scales with
# the mode's hold time and noise tolerance:
#   scalping   → 1.2 (tight, small TP)
#   trend_only → 1.8 (wider, room for swings)
#   standard   → 1.5 (middle of the road)
# Falls back to RiskConfig.atr_multiplier (2.5) when no mode is supplied so
# legacy callers stay backward compatible.
MODE_ATR_PROFILES: Dict[str, Dict[str, float]] = {
    "scalping": {"atr_multiplier": 1.2, "atr_period": 14},
    "trend_only": {"atr_multiplier": 1.8, "atr_period": 14},
    "standard": {"atr_multiplier": 1.5, "atr_period": 14},
}


def get_atr_profile(mode: Optional[str]) -> Dict[str, float]:
    """Return ATR profile (multiplier + period) for a trading mode.

    Unknown / missing modes fall back to the conservative defaults
    (multiplier=2.5, period=14) so callers can use this helper safely
    without pre-validating ``mode``.
    """
    key = str(mode or "").strip().lower()
    profile = MODE_ATR_PROFILES.get(key)
    if profile is None:
        return {"atr_multiplier": 2.5, "atr_period": 14}
    return dict(profile)


def classify_pair_volatility(symbol: str) -> str:
    """Classify a trading pair as 'low' or 'high' volatility."""
    symbol_upper = symbol.upper()
    for key, vol in VOLATILITY_CLASS.items():
        if key.upper() == symbol_upper:
            return vol
    # Default: treat as high volatility (altcoin)
    return "high"


def get_default_sl_tp(symbol: str) -> tuple[float, float]:
    """Return (stop_loss_pct, take_profit_pct) for a given symbol."""
    vol = classify_pair_volatility(symbol)
    d = DEFAULT_SL_TP[vol]
    return d["stop_loss_pct"], d["take_profit_pct"]


def resolve_effective_sl_tp_percentages(
    symbol: str,
    risk_config: Optional[Dict[str, Any]] = None,
) -> tuple[float, float]:
    """
    Resolve effective SL/TP percentages for a symbol.

    When ``use_dynamic_sl_tp`` is true (default), behavior is controlled by
    ``sl_tp_percent_source_when_dynamic``:

    - ``volatility`` (default): use pair volatility table (``DEFAULT_SL_TP`` /
      ``VOLATILITY_CLASS``) — ignores numeric ``stop_loss_pct`` / ``take_profit_pct``
      on the dict for this resolution path (backward compatible).
    - ``risk_config``: use ``stop_loss_pct`` / ``take_profit_pct`` from the
      supplied ``risk_config`` (e.g. values synced from ``strategy_mode`` in
      ``main._apply_strategy_mode_profile``) so bootstrap / manual SL/TP align
      with active strategy percentages.

    When dynamic SL/TP is disabled, configured global percentages are always used.
    """
    default_sl_pct, default_tp_pct = get_default_sl_tp(symbol)
    cfg = dict(risk_config or {})
    source = str(cfg.get("sl_tp_percent_source_when_dynamic") or "volatility").strip().lower()

    if bool(cfg.get("use_dynamic_sl_tp", True)):
        if source == "risk_config":
            try:
                stop_loss_pct = float(cfg.get("stop_loss_pct", default_sl_pct) or default_sl_pct)
            except (TypeError, ValueError):
                stop_loss_pct = float(default_sl_pct)
            try:
                take_profit_pct = float(cfg.get("take_profit_pct", default_tp_pct) or default_tp_pct)
            except (TypeError, ValueError):
                take_profit_pct = float(default_tp_pct)
            stop_loss_pct = -abs(stop_loss_pct) if stop_loss_pct else float(default_sl_pct)
            take_profit_pct = abs(take_profit_pct) if take_profit_pct else float(default_tp_pct)
            return float(stop_loss_pct), float(take_profit_pct)
        return float(default_sl_pct), float(default_tp_pct)

    try:
        stop_loss_pct = float(cfg.get("stop_loss_pct", default_sl_pct) or default_sl_pct)
    except (TypeError, ValueError):
        stop_loss_pct = float(default_sl_pct)
    try:
        take_profit_pct = float(cfg.get("take_profit_pct", default_tp_pct) or default_tp_pct)
    except (TypeError, ValueError):
        take_profit_pct = float(default_tp_pct)

    stop_loss_pct = -abs(stop_loss_pct) if stop_loss_pct else float(default_sl_pct)
    take_profit_pct = abs(take_profit_pct) if take_profit_pct else float(default_tp_pct)
    return stop_loss_pct, take_profit_pct


# ─────────────────────────────────────────────────────────────────────────────
# ATR calculation helpers
# ─────────────────────────────────────────────────────────────────────────────
def calculate_atr(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
) -> List[float]:
    """
    Calculate Average True Range (ATR) using Wilder's smoothing method.

    Args:
        highs: List of high prices
        lows: List of low prices
        closes: List of close prices
        period: ATR period (default 14)

    Returns:
        List of ATR values (same length as input, first `period-1` values are NaN-like 0)
    """
    if len(highs) < period or len(lows) < period or len(closes) < period:
        return [0.0] * len(highs)

    trs = []
    for i in range(len(closes)):
        if i == 0:
            tr = highs[i] - lows[i]
        else:
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i - 1])
            lc = abs(lows[i] - closes[i - 1])
            tr = max(hl, hc, lc)
        trs.append(tr)

    # Wilder's smoothing: first ATR = simple average of first `period` TRs
    atr = [0.0] * (period - 1)
    atr.append(sum(trs[:period]) / period)

    for i in range(period, len(trs)):
        atr_val = (atr[-1] * (period - 1) + trs[i]) / period
        atr.append(atr_val)

    return atr


@dataclass
class RiskConfig:
    max_risk_per_trade_pct: float = 1.0  # % of portfolio per trade
    max_daily_loss_pct: float = 5.0  # % of portfolio per day
    max_position_per_trade_pct: float = 10.0  # % of portfolio per single order
    max_drawdown_threshold_pct: float = 12.0  # hard block new BUY entries above this drawdown
    drawdown_soft_reduce_start_pct: float = 5.0  # start reducing risk above this drawdown
    min_drawdown_risk_multiplier: float = 0.35  # never reduce below this multiplier when soft-reducing
    drawdown_block_new_entries: bool = True
    stop_loss_pct: float = -5.0  # % from entry price (deprecated/ignored)
    take_profit_pct: float = 12.0  # % from entry price (deprecated/ignored)
    initial_balance: float = 1000.0
    min_balance_threshold: float = 100.0
    max_open_positions: int = 5
    max_daily_trades: int = 10
    cool_down_minutes: int = 5
    min_order_amount: float = DEFAULT_MIN_ORDER_QUOTE
    # ATR-based SL settings
    atr_multiplier: float = 2.5  # SL distance = ATR * this
    atr_period: int = 14  # ATR lookback period
    use_dynamic_sl_tp: bool = True  # Use pair-specific SL/TP
    # When False and Kelly edge is non-positive, keep fixed max_risk_per_trade_pct (no hard reject).
    use_fractional_kelly: bool = True

    def __post_init__(self):
        # Hard reject anything above 8.0% (obvious misconfiguration).
        # Warn (but allow) values above 5.0% as ultra-aggressive.
        if self.max_risk_per_trade_pct > 8.0:
            raise ValueError(
                f"max_risk_per_trade_pct={self.max_risk_per_trade_pct}% exceeds "
                f"the absolute safety ceiling of 8.0%. Fix your config."
            )
        if self.max_risk_per_trade_pct > 5.0:
            logger.warning(
                "max_risk_per_trade_pct=%.2f%% is ultra-aggressive (>5%%). "
                "Proceeding as configured — ensure this is intentional.",
                self.max_risk_per_trade_pct,
            )

    @classmethod
    def from_file(cls, path: str) -> "RiskConfig":
        with open(path, "r") as f:
            data = json.load(f)
        risk = data.get("risk", {})
        return cls(
            max_risk_per_trade_pct=risk.get("max_risk_per_trade_pct", 4.0),
            max_daily_loss_pct=risk.get("max_daily_loss_pct", 10.0),
            max_position_per_trade_pct=risk.get("max_position_per_trade_pct", 10.0),
            max_drawdown_threshold_pct=risk.get("max_drawdown_threshold_pct", 12.0),
            drawdown_soft_reduce_start_pct=risk.get("drawdown_soft_reduce_start_pct", 5.0),
            min_drawdown_risk_multiplier=risk.get("min_drawdown_risk_multiplier", 0.35),
            drawdown_block_new_entries=risk.get("drawdown_block_new_entries", True),
            stop_loss_pct=risk.get("stop_loss_pct", -5.0),
            take_profit_pct=risk.get("take_profit_pct", 12.0),
            atr_multiplier=risk.get("atr_multiplier", 3.0),
            atr_period=risk.get("atr_period", 14),
            use_dynamic_sl_tp=risk.get("use_dynamic_sl_tp", True),
            use_fractional_kelly=bool(risk.get("use_fractional_kelly", True)),
            initial_balance=data.get("portfolio", {}).get("initial_balance", 1000.0),
            min_balance_threshold=data.get("portfolio", {}).get("min_balance_threshold", 100.0),
            max_open_positions=data.get("trading", {}).get("max_open_positions", 5),
            max_daily_trades=data.get("trading", {}).get("max_daily_trades", 10),
            cool_down_minutes=data.get("trading", {}).get("cool_down_minutes", 5),
            min_order_amount=data.get("trading", {}).get("min_order_amount", DEFAULT_MIN_ORDER_QUOTE),
        )

    def to_file(self, path: str):
        data = {
            "risk": {
                "max_risk_per_trade_pct": self.max_risk_per_trade_pct,
                "max_daily_loss_pct": self.max_daily_loss_pct,
                "max_position_per_trade_pct": self.max_position_per_trade_pct,
                "max_drawdown_threshold_pct": self.max_drawdown_threshold_pct,
                "drawdown_soft_reduce_start_pct": self.drawdown_soft_reduce_start_pct,
                "min_drawdown_risk_multiplier": self.min_drawdown_risk_multiplier,
                "drawdown_block_new_entries": self.drawdown_block_new_entries,
                "stop_loss_pct": self.stop_loss_pct,
                "take_profit_pct": self.take_profit_pct,
                "atr_multiplier": self.atr_multiplier,
                "atr_period": self.atr_period,
                "use_dynamic_sl_tp": self.use_dynamic_sl_tp,
                "use_fractional_kelly": self.use_fractional_kelly,
            },
            "portfolio": {
                "initial_balance": self.initial_balance,
                "min_balance_threshold": self.min_balance_threshold,
            },
            "trading": {
                "max_open_positions": self.max_open_positions,
                "max_daily_trades": self.max_daily_trades,
                "cool_down_minutes": self.cool_down_minutes,
                "min_order_amount": self.min_order_amount,
            },
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: str = ""
    suggested_size: float = 0.0  # quote amount to trade


class RiskManager:
    """
    Central risk management for the trading bot.
    Enforces position sizing, stop loss, take profit, and daily loss limits.
    Supports dynamic SL/TP per pair volatility and ATR-based stop loss.
    """

    def __init__(self, config: RiskConfig):
        self.config = config
        self._daily_loss_start: Optional[float] = None
        self._daily_loss_date: Optional[date] = None
        self._trade_count_today: int = 0
        self._last_trade_time: Optional[datetime] = None
        self._cooling_down: bool = False
        self._peak_portfolio_value: Optional[float] = None
        self._state_file = Path("risk_state.json")
        # M4 fix: serialise all file I/O through a lock so concurrent calls
        # from the OMS thread and main loop cannot interleave partial JSON writes.
        self._state_lock = threading.Lock()
        # Load persisted state on startup
        self.load_state()

    # ── Persistence ─────────────────────────────────────────────────

    def save_state(self, path: Optional[str] = None):
        """Save risk manager state to JSON file.

        M4 fix: all file I/O is serialised through _state_lock to prevent
        two threads from interleaving partial writes that corrupt the JSON.
        """
        save_path = Path(path) if path else self._state_file
        data = {
            "daily_loss_start": self._daily_loss_start,
            "daily_loss_date": self._daily_loss_date.isoformat() if self._daily_loss_date else None,
            "trade_count_today": self._trade_count_today,
            "last_trade_time": self._last_trade_time.isoformat() if self._last_trade_time else None,
            "cooling_down": self._cooling_down,
            "peak_portfolio_value": self._peak_portfolio_value,
        }
        try:
            with self._state_lock:
                with open(save_path, "w") as f:
                    json.dump(data, f, indent=2)
            logger.debug(f"Risk state saved to {save_path}")
        except Exception as e:
            logger.warning(f"Failed to save risk state: {e}")

    def load_state(self, path: Optional[str] = None) -> bool:
        """Load risk manager state from JSON file. Returns True if loaded.

        M4 fix: file read and all state mutations are serialised through
        _state_lock so a concurrent save_state cannot produce a torn read.
        """
        load_path = Path(path) if path else self._state_file
        if not load_path.exists():
            logger.debug(f"No risk state file found at {load_path}")
            return False
        try:
            with self._state_lock:
                with open(load_path, "r") as f:
                    data = json.load(f)

                # Reset to safe defaults first (in case partial load happens)
                self._cooling_down = False
                self._trade_count_today = 0
                self._peak_portfolio_value = None

                # Load values
                self._daily_loss_start = data.get("daily_loss_start")
                date_str = data.get("daily_loss_date")
                self._daily_loss_date = date.fromisoformat(date_str) if date_str else None
                self._trade_count_today = data.get("trade_count_today", 0)
                last_time_str = data.get("last_trade_time")
                self._last_trade_time = datetime.fromisoformat(last_time_str) if last_time_str else None
                self._cooling_down = data.get("cooling_down", False)
                self._peak_portfolio_value = data.get("peak_portfolio_value")

                if self._daily_loss_date is None and self._last_trade_time is not None:
                    self._daily_loss_date = self._last_trade_time.date()

                # Reset daily counters if it's a new day
                if self._daily_loss_date and self._daily_loss_date != date.today():
                    self._daily_loss_date = date.today()
                    self._daily_loss_start = None
                    self._trade_count_today = 0
                    self._cooling_down = False

            logger.info(f"Risk state loaded from {load_path}")
            return True
        except Exception as e:
            # On ANY error, reset to safe defaults to prevent stuck state
            logger.warning(f"Failed to load risk state ({e}) - resetting to safe defaults")
            self._daily_loss_start = None
            self._daily_loss_date = None
            self._trade_count_today = 0
            self._last_trade_time = None
            self._cooling_down = False  # SAFE DEFAULT
            self._peak_portfolio_value = None
            return False

    def _update_peak_portfolio_value(self, portfolio_value: float) -> None:
        if portfolio_value <= 0:
            return
        if self._peak_portfolio_value is None or portfolio_value > self._peak_portfolio_value:
            self._peak_portfolio_value = portfolio_value
            self.save_state()

    def _get_current_drawdown_pct(self, portfolio_value: float) -> float:
        peak_value = float(self._peak_portfolio_value or 0.0)
        if portfolio_value <= 0 or peak_value <= 0 or portfolio_value >= peak_value:
            return 0.0
        return max(0.0, ((peak_value - portfolio_value) / peak_value) * 100.0)

    def _get_drawdown_risk_multiplier(self, portfolio_value: float) -> float:
        drawdown_pct = self._get_current_drawdown_pct(portfolio_value)
        soft_start = max(float(self.config.drawdown_soft_reduce_start_pct or 0.0), 0.0)
        hard_limit = max(float(self.config.max_drawdown_threshold_pct or 0.0), 0.0)
        min_multiplier = min(max(float(self.config.min_drawdown_risk_multiplier or 0.0), 0.0), 1.0)

        if drawdown_pct <= soft_start:
            return 1.0
        if hard_limit <= soft_start:
            return min_multiplier

        reduction_span = hard_limit - soft_start
        reduction_progress = min(max((drawdown_pct - soft_start) / reduction_span, 0.0), 1.0)
        return max(min_multiplier, 1.0 - ((1.0 - min_multiplier) * reduction_progress))

    # ── Position Sizing ────────────────────────────────────────────────

    def calculate_position_size(
        self,
        portfolio_value: float,
        entry_price: float,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        confidence: Optional[float] = None,
        symbol: Optional[str] = None,
    ) -> RiskCheckResult:
        """
        Calculate how much to invest in a single trade based on risk tolerance and Fractional Kelly.

        If stop_loss_price is provided, position size is capped at the amount
        that would lose `max_risk_per_trade_pct` if hit.
        Otherwise uses `max_position_per_trade_pct` as hard cap.

        God Mode Upgrade (Phase 3): Dynamic Half-Kelly Sizing based on confidence score.
        """
        if portfolio_value <= 0:
            _diag("GLOBAL", "RiskMgr:PositionSize", "REJECT", "Invalid portfolio value")
            return RiskCheckResult(False, "Invalid portfolio value")

        self._update_peak_portfolio_value(portfolio_value)

        # Use the configured max risk
        effective_risk_pct = self.config.max_risk_per_trade_pct

        # Apply Fractional Kelly if AI Confidence exists
        if confidence and confidence > 0 and stop_loss_price and take_profit_price:
            risk_dist = abs(entry_price - stop_loss_price)
            reward_dist = abs(take_profit_price - entry_price)

            if risk_dist > 0 and reward_dist > 0:
                p = confidence
                q = 1.0 - p
                b = reward_dist / risk_dist  # Risk-Reward Payout Ratio
                # Full Kelly Fraction
                kelly_pct = p - (q / b)
                sym = (symbol or "").strip() or "?"
                logger.debug(
                    "[Kelly] %s p=%.3f b=%.3f kelly=%.4f SL=%s TP=%s",
                    sym,
                    p,
                    b,
                    kelly_pct,
                    stop_loss_price,
                    take_profit_price,
                )
                if kelly_pct <= 0.0:
                    if self.config.use_fractional_kelly:
                        reason = f"Non-positive Kelly edge: kelly={kelly_pct:.4f} " f"(p={p:.2f}, b={b:.2f})"
                        _diag("GLOBAL", "RiskMgr:PositionSize", "REJECT", reason)
                        logger.info("[Kelly Sizing] Trade rejected due to non-positive edge: %s", reason)
                        return RiskCheckResult(False, reason)
                    logger.warning(
                        "[Kelly] %s edge non-positive (kelly=%.4f); use_fractional_kelly=false — "
                        "using fixed risk %% fallback (max_risk_per_trade_pct=%.2f%%)",
                        sym,
                        kelly_pct,
                        self.config.max_risk_per_trade_pct,
                    )
                    # Leave effective_risk_pct at max_risk_per_trade_pct from above.
                else:
                    # Apply Half-Kelly (Fractional) for safety in crypto
                    half_kelly = kelly_pct / 2.0

                    # Bound between min 0.1% and max_risk_per_trade_pct
                    # Max constraint: If Kelly wants to bet 5%, we still limit to 1.0% max
                    # A zero Kelly edge (break-even) falls back to the minimum floor.
                    dynamic_risk = max(0.1, min(half_kelly * 100, effective_risk_pct))

                    logger.debug(
                        f"\U0001f4d0 [Kelly Sizing] P={p:.2f}, b={b:.2f} -> Full=({kelly_pct*100:.1f}%), Half-Kelly=({half_kelly*100:.1f}%) -> Final Risk: {dynamic_risk:.2f}%"
                    )
                    effective_risk_pct = dynamic_risk

        drawdown_pct = self._get_current_drawdown_pct(portfolio_value)
        drawdown_multiplier = self._get_drawdown_risk_multiplier(portfolio_value)
        if drawdown_multiplier < 1.0:
            reduced_risk_pct = max(0.1, effective_risk_pct * drawdown_multiplier)
            logger.info(
                "[Drawdown Sizing] drawdown=%.2f%% peak=%.2f portfolio=%.2f multiplier=%.2f risk %.2f%% -> %.2f%%",
                drawdown_pct,
                float(self._peak_portfolio_value or 0.0),
                portfolio_value,
                drawdown_multiplier,
                effective_risk_pct,
                reduced_risk_pct,
            )
            effective_risk_pct = reduced_risk_pct

        # Hard cap: no single position > max_position_per_trade_pct
        hard_cap = portfolio_value * (self.config.max_position_per_trade_pct / 100)

        if stop_loss_price and stop_loss_price > 0 and entry_price > 0:
            # Dynamic Risk-based sizing: size to lose at most effective_risk_pct on this trade
            risk_amount = portfolio_value * (effective_risk_pct / 100)
            risk_per_unit = abs(entry_price - stop_loss_price)
            if risk_per_unit == 0:
                _diag(
                    "GLOBAL",
                    "RiskMgr:PositionSize",
                    "REJECT",
                    f"Stop loss too close to entry price (SL={stop_loss_price}, entry={entry_price})",
                )
                return RiskCheckResult(False, "Stop loss too close to entry price")

            # risk_based_size is in base asset units (e.g. BTC)
            risk_based_quantity = risk_amount / risk_per_unit
            # Convert back to quote asset for the position size
            suggested_investment = risk_based_quantity * entry_price
            suggested = min(suggested_investment, hard_cap)

            _diag(
                "GLOBAL",
                "RiskMgr:PositionSize",
                "INFO",
                f"portfolio={portfolio_value:.2f}, risk_pct={effective_risk_pct:.2f}%, "
                f"risk_amount={risk_amount:.2f}, SL_dist={risk_per_unit:.2f}, "
                f"suggested={suggested:.2f}, hard_cap={hard_cap:.2f}",
            )
            logger.debug(
                f"Risk Sizing: Portfolio={portfolio_value:.2f}, Risk={risk_amount:.2f} ({effective_risk_pct:.2f}%), SL_dist={risk_per_unit:.2f}, Inv={suggested:.2f}"
            )
        else:
            # Institutional rule: Reject trades without stop loss
            logger.warning(
                "\u26d4 Risk Manager: \u0e44\u0e21\u0e48\u0e2d\u0e19\u0e38\u0e21\u0e31\u0e15\u0e34\u0e01\u0e32\u0e23\u0e40\u0e17\u0e23\u0e14 (\u0e44\u0e21\u0e48\u0e1e\u0e1a\u0e02\u0e49\u0e2d\u0e21\u0e39\u0e25 ATR/Stop Loss)"
            )
            _diag(
                "GLOBAL",
                "RiskMgr:PositionSize",
                "REJECT",
                f"No ATR/Stop Loss data (SL={stop_loss_price}, entry={entry_price})",
            )
            return RiskCheckResult(
                False,
                "\u0e44\u0e21\u0e48\u0e2d\u0e19\u0e38\u0e21\u0e31\u0e15\u0e34\u0e01\u0e32\u0e23\u0e40\u0e17\u0e23\u0e14 (\u0e44\u0e21\u0e48\u0e1e\u0e1a\u0e02\u0e49\u0e2d\u0e21\u0e39\u0e25 ATR/Stop Loss)",
            )

        if suggested < self.config.min_order_amount:
            min_viable = self.config.min_order_amount * MIN_ORDER_BUFFER
            # Align with hard_cap / max_position_per_trade_pct (not a fixed 20%)
            if min_viable > hard_cap:
                reason = (
                    f"Portfolio too small for minimum order: min_viable={min_viable:.2f} quote "
                    f"> max position cap {hard_cap:.2f} quote "
                    f"({self.config.max_position_per_trade_pct:.1f}% of portfolio)"
                )
                _diag("GLOBAL", "RiskMgr:PositionSize", "REJECT", reason)
                return RiskCheckResult(False, reason)
            suggested = min_viable
            logger.info(
                "[RiskMgr] Size adjusted to minimum viable: %.2f quote",
                min_viable,
            )

        _diag("GLOBAL", "RiskMgr:PositionSize", "PASS", f"Position size approved: {suggested:.2f} quote")
        return RiskCheckResult(True, "Position size OK", round(suggested, 2))

    def validate_risk_reward(
        self,
        entry_price: float,
        stop_loss: Optional[float],
        take_profit: Optional[float],
    ) -> RiskCheckResult:
        """
        Enforce minimum Risk:Reward ratio before approving a trade.

        The potential reward (distance to TP) MUST be >= MIN_RISK_REWARD_RATIO
        times the risk (distance to SL). This ensures every trade has a
        mathematical edge aligned with SYSTEM_OBJECTIVE = MAXIMIZE_NET_PROFIT.

        Args:
            entry_price: Planned entry price
            stop_loss: Stop loss price
            take_profit: Take profit price

        Returns:
            RiskCheckResult with allowed=True if R:R >= MIN_RISK_REWARD_RATIO
        """
        if not stop_loss or not take_profit or entry_price <= 0:
            logger.warning(
                "\u26d4 R:R Enforcer: \u0e44\u0e21\u0e48\u0e2a\u0e32\u0e21\u0e32\u0e23\u0e16\u0e04\u0e33\u0e19\u0e27\u0e13 R:R (\u0e02\u0e32\u0e14\u0e02\u0e49\u0e2d\u0e21\u0e39\u0e25 SL/TP)"
            )
            _diag(
                "GLOBAL",
                "RiskMgr:RiskReward",
                "REJECT",
                f"Cannot calculate R:R — missing data (SL={stop_loss}, TP={take_profit}, entry={entry_price})",
            )
            return RiskCheckResult(
                False,
                "\u0e44\u0e21\u0e48\u0e2a\u0e32\u0e21\u0e32\u0e23\u0e16\u0e04\u0e33\u0e19\u0e27\u0e13 R:R (\u0e02\u0e32\u0e14\u0e02\u0e49\u0e2d\u0e21\u0e39\u0e25 SL/TP)",
            )

        risk_distance = abs(entry_price - stop_loss)
        reward_distance = abs(take_profit - entry_price)

        if risk_distance == 0:
            _diag(
                "GLOBAL",
                "RiskMgr:RiskReward",
                "REJECT",
                f"SL too close to entry (risk_distance=0, SL={stop_loss}, entry={entry_price})",
            )
            return RiskCheckResult(
                False,
                "SL \u0e15\u0e34\u0e14\u0e23\u0e32\u0e04\u0e32\u0e40\u0e02\u0e49\u0e32\u0e21\u0e32\u0e01\u0e40\u0e01\u0e34\u0e19\u0e44\u0e1b (risk_distance = 0)",
            )

        rr_ratio = reward_distance / risk_distance

        _diag(
            "GLOBAL",
            "RiskMgr:RiskReward",
            "INFO",
            f"entry={entry_price:.4f}, SL={stop_loss:.4f}, TP={take_profit:.4f} | "
            f"SL_dist={risk_distance:.4f}, TP_dist={reward_distance:.4f}, "
            f"R:R={rr_ratio:.2f} vs min={MIN_RISK_REWARD_RATIO}",
        )

        if rr_ratio < MIN_RISK_REWARD_RATIO:
            logger.debug(f"R:R Enforcer: reject R:R={rr_ratio:.2f} < {MIN_RISK_REWARD_RATIO}")
            _diag("GLOBAL", "RiskMgr:RiskReward", "REJECT", f"R:R {rr_ratio:.2f} < minimum {MIN_RISK_REWARD_RATIO}")
            return RiskCheckResult(
                False,
                f"R:R ratio {rr_ratio:.2f} < {MIN_RISK_REWARD_RATIO} \u2014 \u0e44\u0e21\u0e48\u0e1c\u0e48\u0e32\u0e19\u0e40\u0e01\u0e13\u0e11\u0e4c\u0e01\u0e33\u0e44\u0e23\u0e02\u0e31\u0e49\u0e19\u0e15\u0e48\u0e33",
            )

        _diag("GLOBAL", "RiskMgr:RiskReward", "PASS", f"R:R {rr_ratio:.2f} >= minimum {MIN_RISK_REWARD_RATIO}")
        logger.debug(f"R:R Enforcer: PASS | R:R = {rr_ratio:.2f} (min {MIN_RISK_REWARD_RATIO})")
        return RiskCheckResult(True, f"R:R = {rr_ratio:.2f}")

    def calc_sl_tp_from_atr(
        self,
        entry_price: float,
        atr_value: float,
        direction: str = "long",
        risk_reward_ratio: float = 2.0,
        mode: Optional[str] = None,  # --- NEW: SPEC_04 — per-mode ATR multiplier
    ) -> tuple[float, float]:
        """
        Calculate SL and TP prices purely from ATR.
        SL = entry - (ATR * atr_multiplier)
        TP = entry + (ATR * atr_multiplier * risk_reward_ratio)

        Args:
            entry_price: Position entry price
            atr_value: Current ATR value
            direction: 'long' only for spot mode
            risk_reward_ratio: TP distance = SL distance * this ratio
            mode: Optional trading mode ("scalping" | "trend_only" | "standard").
                  When provided and known, the multiplier is taken from
                  MODE_ATR_PROFILES[mode] instead of self.config.atr_multiplier.
                  Unknown/None ⇒ fall back to RiskConfig (backward compatible).

        Returns:
            (stop_loss_price, take_profit_price)
        """
        # Use precise calculations to avoid float accumulation errors
        if str(direction or "long").lower() != "long":
            return 0.0, 0.0

        # --- NEW: SPEC_04 — pick multiplier from MODE_ATR_PROFILES if mode set
        mode_key = str(mode or "").strip().lower()
        if mode_key and mode_key in MODE_ATR_PROFILES:
            multiplier = float(MODE_ATR_PROFILES[mode_key]["atr_multiplier"])
        else:
            multiplier = float(self.config.atr_multiplier)

        sl_distance = precise_multiply(atr_value, multiplier)
        tp_distance = precise_multiply(sl_distance, risk_reward_ratio)

        sl = precise_round(precise_subtract(entry_price, sl_distance), 6)
        tp = precise_round(precise_add(entry_price, tp_distance), 6)

        return sl, tp

    # ── Daily Loss Limit ───────────────────────────────────────────────

    def check_daily_loss_limit(self, current_portfolio_value: float) -> RiskCheckResult:
        """Block new trades if daily loss exceeds max_daily_loss_pct."""
        today = date.today()

        if self._daily_loss_date != today:
            # Reset for new day
            self._daily_loss_date = today
            self._daily_loss_start = current_portfolio_value
            self._trade_count_today = 0

        if self._daily_loss_start is None:
            self._daily_loss_start = current_portfolio_value

        max_loss = self._daily_loss_start * (self.config.max_daily_loss_pct / 100)
        current_loss = self._daily_loss_start - current_portfolio_value

        if current_loss >= max_loss:
            return RiskCheckResult(False, f"Daily loss limit reached: {current_loss:.2f} / {max_loss:.2f}")
        return RiskCheckResult(True, f"Daily loss OK: {current_loss:.2f} / {max_loss:.2f}")

    @property
    def trade_count_today(self) -> int:
        """Public read accessor for today's completed trade count."""
        return self._trade_count_today

    def record_trade(self):
        """Call after a completed trade to update counters."""
        today = date.today()
        if self._daily_loss_date != today:
            self._daily_loss_date = today
            self._daily_loss_start = None
            self._trade_count_today = 0
            self._cooling_down = False
        self._trade_count_today += 1
        self._last_trade_time = datetime.now()
        self.save_state()

    def record_trade_activity(self):
        """Refresh cooldown timestamp without incrementing the daily trade counter."""
        today = date.today()
        if self._daily_loss_date != today:
            self._daily_loss_date = today
            self._daily_loss_start = None
            self._trade_count_today = 0
            self._cooling_down = False
        self._last_trade_time = datetime.now()
        self.save_state()

    # ── Cooldown ───────────────────────────────────────────────────────

    def check_cooldown(self) -> bool:
        """Return True if bot should wait before next trade."""
        if self._last_trade_time is None:
            return False
        elapsed = (datetime.now() - self._last_trade_time).total_seconds() / 60
        return elapsed < self.config.cool_down_minutes

    # ── Global Risk Checks ──────────────────────────────────────────────

    def can_open_position(
        self,
        portfolio_value: float,
        open_positions_count: int,
        current_time: Optional[datetime] = None,
    ) -> RiskCheckResult:
        """
        Run all risk checks before opening a new position.
        """
        # 1. Portfolio value check
        if portfolio_value < self.config.min_balance_threshold:
            reason = f"Portfolio ({portfolio_value}) below min threshold ({self.config.min_balance_threshold})"
            _diag("GLOBAL", "RiskMgr:CanOpen", "REJECT", reason)
            return RiskCheckResult(False, reason)

        self._update_peak_portfolio_value(portfolio_value)

        # 2. Daily loss limit
        daily_check = self.check_daily_loss_limit(portfolio_value)
        if not daily_check.allowed:
            _diag("GLOBAL", "RiskMgr:CanOpen", "REJECT", f"Daily loss limit: {daily_check.reason}")
            return daily_check

        drawdown_pct = self._get_current_drawdown_pct(portfolio_value)
        if self.config.drawdown_block_new_entries and drawdown_pct >= self.config.max_drawdown_threshold_pct:
            reason = f"Drawdown limit reached: {drawdown_pct:.2f}% / " f"{self.config.max_drawdown_threshold_pct:.2f}%"
            _diag("GLOBAL", "RiskMgr:CanOpen", "REJECT", reason)
            return RiskCheckResult(False, reason)

        # 3. Max open positions
        if open_positions_count >= self.config.max_open_positions:
            reason = f"Max open positions reached ({self.config.max_open_positions})"
            _diag("GLOBAL", "RiskMgr:CanOpen", "REJECT", reason)
            return RiskCheckResult(False, reason)

        # 4. Max daily trades
        if self._trade_count_today >= self.config.max_daily_trades:
            reason = f"Max daily trades reached ({self.config.max_daily_trades})"
            _diag("GLOBAL", "RiskMgr:CanOpen", "REJECT", reason)
            return RiskCheckResult(False, reason)

        # 5. Cooldown
        if self.check_cooldown():
            _diag("GLOBAL", "RiskMgr:CanOpen", "REJECT", "Cooldown period active")
            return RiskCheckResult(False, "Cooldown period active")

        _diag(
            "GLOBAL",
            "RiskMgr:CanOpen",
            "PASS",
            f"portfolio={portfolio_value:.2f}, drawdown={drawdown_pct:.2f}%, positions={open_positions_count}/{self.config.max_open_positions}, "
            f"trades_today={self._trade_count_today}/{self.config.max_daily_trades}",
        )
        return RiskCheckResult(True, "All checks passed")

    def update_daily_start(self, portfolio_value: float):
        """Manually set the daily starting balance (e.g., on restart)."""
        self._daily_loss_start = portfolio_value
        self._daily_loss_date = date.today()
        self.save_state()

    def get_risk_summary(self, portfolio_value: float) -> dict:
        """Return a dict summarizing current risk state."""
        today = date.today()
        loss_start = self._daily_loss_start if self._daily_loss_date == today else portfolio_value
        current_loss = (loss_start - portfolio_value) if loss_start else 0
        max_loss = (loss_start or portfolio_value) * (self.config.max_daily_loss_pct / 100)

        return {
            "portfolio_value": portfolio_value,
            "peak_portfolio_value": round(float(self._peak_portfolio_value or portfolio_value), 2),
            "current_drawdown_pct": round(self._get_current_drawdown_pct(portfolio_value), 2),
            "daily_loss": round(current_loss, 2),
            "daily_loss_max": round(max_loss, 2),
            "daily_loss_pct": round((current_loss / portfolio_value * 100) if portfolio_value else 0, 2),
            "trades_today": self._trade_count_today,
            "max_daily_trades": self.config.max_daily_trades,
            "max_open_positions": self.config.max_open_positions,
            "cooling_down": self.check_cooldown(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Correlation-Aware Position Management
# ─────────────────────────────────────────────────────────────────────────────


def check_pair_correlation(
    candidate_symbol: str,
    open_symbols: List[str],
    db,
    threshold: float = 0.75,
    lookback_candles: int = 60,
    timeframe: str = "1h",
) -> RiskCheckResult:
    """Check if a candidate pair is highly correlated with any open position.

    Uses Pearson correlation of recent close prices.  If the candidate's
    returns are correlated above *threshold* with any already-held pair,
    the trade is blocked to avoid concentrated directional exposure.

    Args:
        candidate_symbol: trading pair to evaluate.
        open_symbols: List of trading pairs currently held.
        db: Database instance with ``get_candles(symbol, interval, limit)``.
        threshold: Correlation coefficient above which the trade is blocked (0.0-1.0).
        lookback_candles: Number of candles to compute correlation over.
        timeframe: Candle timeframe to use.

    Returns:
        RiskCheckResult — allowed=False if any pair exceeds the threshold.
    """
    if not open_symbols or not db:
        return RiskCheckResult(True, "No open positions to correlate against")

    try:
        import pandas as _pd

        cand_df = db.get_candles(candidate_symbol, interval=timeframe, limit=lookback_candles)
        if cand_df is None or (hasattr(cand_df, "empty") and cand_df.empty) or len(cand_df) < 20:
            return RiskCheckResult(True, "Insufficient data for correlation check")

        cand_returns = cand_df["close"].astype(float).pct_change().dropna()

        for held_symbol in open_symbols:
            if held_symbol.upper() == candidate_symbol.upper():
                continue

            try:
                held_df = db.get_candles(held_symbol, interval=timeframe, limit=lookback_candles)
                if held_df is None or (hasattr(held_df, "empty") and held_df.empty) or len(held_df) < 20:
                    continue

                held_returns = held_df["close"].astype(float).pct_change().dropna()

                # Align on the shorter series length
                min_len = min(len(cand_returns), len(held_returns))
                if min_len < 15:
                    continue

                corr = (
                    cand_returns.iloc[-min_len:]
                    .reset_index(drop=True)
                    .corr(held_returns.iloc[-min_len:].reset_index(drop=True))
                )

                if corr >= threshold:
                    reason = (
                        f"High correlation ({corr:.2f}) between {candidate_symbol} "
                        f"and open position {held_symbol} (threshold {threshold})"
                    )
                    _diag(candidate_symbol, "RiskMgr:Correlation", "REJECT", reason)
                    logger.info("🔗 [Correlation Guard] %s", reason)
                    return RiskCheckResult(False, reason)

            except Exception as exc:
                logger.debug("Correlation check skipped for %s vs %s: %s", candidate_symbol, held_symbol, exc)
                continue

        _diag(
            candidate_symbol,
            "RiskMgr:Correlation",
            "PASS",
            f"No high correlation with {len(open_symbols)} open position(s)",
        )
        return RiskCheckResult(True, "Correlation check passed")

    except Exception as exc:
        logger.debug("Correlation check failed for %s: %s", candidate_symbol, exc)
        return RiskCheckResult(True, f"Correlation check skipped: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# --- NEW: SPEC_04 — Anti-Whipsaw guards (SLHoldGuard / ConfirmationGate /
# slippage). These are stateless or lightly stateful helpers — they do NOT
# replace any logic in RiskManager / calc_sl_tp_from_atr; they sit alongside
# it and are invoked by trading_bot.py at the appropriate lifecycle hooks.
# ─────────────────────────────────────────────────────────────────────────────


class SLHoldGuard:
    """Anti-immediate-SL guard.

    After a position is opened, suppresses SL triggers for at least
    ``MIN_HOLD_SECONDS[mode]`` seconds so noise spikes around the entry
    fill cannot whipsaw the position out instantly.

    All public methods are thread-safe — concurrent calls from the OMS
    thread, monitor thread, and main loop are serialised through an
    internal RLock.
    """

    MIN_HOLD_SECONDS: Dict[str, int] = {
        "scalping": 30,  # 30 seconds
        "trend_only": 300,  # 5 minutes
        "standard": 60,  # 1 minute
    }

    def __init__(self) -> None:
        self._entry_times: Dict[str, Dict[str, Any]] = {}
        # RLock so get_status() can safely call is_sl_locked() if needed.
        self._lock = threading.RLock()

    # ── lifecycle hooks ────────────────────────────────────────────────

    def register_entry(self, position_id: str, mode: str = "standard") -> None:
        """Call immediately after a position is opened successfully."""
        if not position_id:
            return
        normalized_mode = str(mode or "standard").strip().lower() or "standard"
        with self._lock:
            self._entry_times[str(position_id)] = {
                "time": datetime.now(),
                "mode": normalized_mode,
            }
        logger.debug(
            "[SLHoldGuard] Registered position_id=%s mode=%s",
            position_id,
            normalized_mode,
        )

    def cleanup(self, position_id: str) -> None:
        """Call once the position is closed / no longer tracked."""
        if not position_id:
            return
        with self._lock:
            self._entry_times.pop(str(position_id), None)

    # ── queries ────────────────────────────────────────────────────────

    def is_sl_locked(self, position_id: str) -> bool:
        """Return True while the SL is still suppressed for this position.

        Returns False for unknown / cleaned-up positions so the caller's
        SL logic can run as usual.
        """
        if not position_id:
            return False
        with self._lock:
            entry = self._entry_times.get(str(position_id))
        if not entry:
            return False

        mode = str(entry.get("mode", "standard"))
        min_hold = self.MIN_HOLD_SECONDS.get(mode, 60)
        elapsed = (datetime.now() - entry["time"]).total_seconds()
        locked = elapsed < min_hold

        if locked:
            remaining = max(0.0, min_hold - elapsed)
            logger.debug(
                "[SLHoldGuard] %s locked for %.0fs more (mode=%s)",
                position_id,
                remaining,
                mode,
            )
        return locked

    def get_status(self) -> Dict[str, Dict[str, Any]]:
        """Return per-position lock status snapshot for CLI/dashboard display."""
        with self._lock:
            now = datetime.now()
            result: Dict[str, Dict[str, Any]] = {}
            for pid, info in self._entry_times.items():
                mode = str(info.get("mode", "standard"))
                min_hold = self.MIN_HOLD_SECONDS.get(mode, 60)
                age = (now - info["time"]).total_seconds()
                result[pid] = {
                    "mode": mode,
                    "age_seconds": round(age, 2),
                    "min_hold_seconds": min_hold,
                    "locked": age < min_hold,
                }
            return result


class ConfirmationGate:
    """Wait for N closed candles to confirm a signal direction before entry.

    Prevents entry on intra-bar noise spikes by requiring the next ``N``
    candles (per mode) to close in the signal direction relative to the
    candle on which the signal fired.
    """

    CONFIRMATION_CANDLES: Dict[str, int] = {
        "scalping": 1,  # need 1 candle close above signal
        "trend_only": 2,  # need 2 candle closes
        "standard": 1,
    }

    @staticmethod
    def _close(candle: Any) -> float:
        """Best-effort extraction of a close price from dict, object, or kline row."""
        if candle is None:
            return 0.0
        if isinstance(candle, dict):
            value = candle.get("close", candle.get("c", 0))
        elif isinstance(candle, (list, tuple)) and len(candle) > 4:
            value = candle[4]
        else:
            value = getattr(candle, "close", getattr(candle, "c", 0))
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def is_confirmed(
        candles: List[Any],
        signal_side: str,
        mode: str = "standard",
    ) -> bool:
        """Return True when the latest candles confirm ``signal_side``.

        Args:
            candles: chronological list of candles. Each entry can be a dict
                with a 'close' (or 'c') key, or any object exposing a
                ``.close``/``.c`` attribute. The first usable element from
                the tail of the slice is treated as the "signal candle"; the
                next ``N`` closes must all be in the signal direction.
            signal_side: "BUY" or "SELL".
            mode: trading mode key into CONFIRMATION_CANDLES.

        Returns:
            True if confirmed (entry allowed), False otherwise.
        """
        n = ConfirmationGate.CONFIRMATION_CANDLES.get(str(mode or "standard").strip().lower(), 1)
        if not candles or len(candles) < n + 1:
            return False

        recent = candles[-(n + 1) :]
        signal_close = ConfirmationGate._close(recent[0])
        confirm_closes = [ConfirmationGate._close(c) for c in recent[1:]]

        if signal_close <= 0 or any(c <= 0 for c in confirm_closes):
            # Treat malformed data as "not yet confirmed" — fail safe.
            logger.debug(
                "[ConfirmationGate] Skipping confirmation — non-positive close " "(signal=%.6f, confirm=%s)",
                signal_close,
                confirm_closes,
            )
            return False

        side_upper = str(signal_side or "").strip().upper()
        if side_upper == "BUY":
            confirmed = all(c > signal_close for c in confirm_closes)
        elif side_upper == "SELL":
            confirmed = all(c < signal_close for c in confirm_closes)
        else:
            return False

        if not confirmed:
            logger.debug(
                "[ConfirmationGate] %s not confirmed yet (need %d candle " "close(s) %s %.6f, got %s)",
                side_upper,
                n,
                "above" if side_upper == "BUY" else "below",
                signal_close,
                confirm_closes,
            )
        return confirmed


# --- NEW: SPEC_04 — Slippage guard ----------------------------------------
# Per-mode tolerance for the price drift between the signal candle's close
# and the live price seen at entry. If exceeded → skip the entry instead of
# chasing the move.
MAX_SLIPPAGE_PCT: Dict[str, float] = {
    # Scalping entries may be evaluated 30–60s after the signal bar; 0.15% was too
    # tight vs real drift. Override via risk/trading ``max_slippage_pct`` maps.
    "scalping": 2.0,
    "trend_only": 0.30,
    "standard": 0.20,
}


def _merge_max_slippage_overlays(config: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """Parse optional per-mode overrides from ``risk.max_slippage_pct`` and
    ``trading.max_slippage_pct`` (dicts). ``trading`` wins on duplicate keys.
    """
    out: Dict[str, float] = {}
    if not config:
        return out
    for section_key in ("risk", "trading"):
        branch = config.get(section_key)
        if not isinstance(branch, dict):
            continue
        raw = branch.get("max_slippage_pct")
        if not isinstance(raw, dict):
            continue
        for k, v in raw.items():
            key = str(k).strip().lower()
            if not key:
                continue
            try:
                out[key] = float(v)
            except (TypeError, ValueError):
                continue
    return out


def resolve_max_slippage_pct(mode_key: str, config: Optional[Dict[str, Any]]) -> float:
    """Effective max slippage % for :class:`PreTradeGate` (signal vs live ticker).

    Defaults: :data:`MAX_SLIPPAGE_PCT`. Optional YAML maps (see
    ``_merge_max_slippage_overlays``) override per mode.
    """
    rk = str(mode_key or "standard").strip().lower() or "standard"
    overlays = _merge_max_slippage_overlays(config)
    if rk in overlays:
        return overlays[rk]
    return float(MAX_SLIPPAGE_PCT.get(rk, 0.20))


def check_slippage(
    signal_price: float,
    current_price: float,
    mode: str = "standard",
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return True if slippage exceeds the per-mode tolerance (skip entry).

    Args:
        signal_price: price observed when the signal fired.
        current_price: price at which the entry would actually be placed.
        mode: trading mode key into MAX_SLIPPAGE_PCT / YAML overrides.
        config: bot config dict; optional ``risk`` / ``trading`` ``max_slippage_pct`` maps.

    Returns:
        True  → slippage too high, caller should skip entry.
        False → slippage acceptable, caller may proceed.
    """
    try:
        s = float(signal_price)
        c = float(current_price)
    except (TypeError, ValueError):
        return False
    if s <= 0:
        return False

    slippage_pct = abs(c - s) / s * 100.0
    max_slip = resolve_max_slippage_pct(mode, config)
    if slippage_pct > max_slip:
        logger.info(
            "[Slippage] Skip entry: slippage=%.3f%% > max=%.3f%% " "(mode=%s, signal=%.6f, current=%.6f)",
            slippage_pct,
            max_slip,
            mode,
            s,
            c,
        )
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# --- NEW: PreTradeGate ---
# Inspired by Freqtrade + Nate Herk's "buy-side gate": every order must pass
# an explicit checklist before it is sent to the exchange. Failures are logged
# with a precise reason — no silent skips, no "hope" overrides.
#
# PreTradeGate is intentionally stateless. It CALLS the existing RiskManager
# helpers (check_daily_loss_limit / check_cooldown / _get_current_drawdown_pct)
# and resolves max slippage via :func:`resolve_max_slippage_pct` (defaults in
# ``MAX_SLIPPAGE_PCT``, optional YAML ``risk``/``trading`` ``max_slippage_pct`` maps) so that
# the source of truth for risk policy stays in RiskManager plus config overrides.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class GateCheckResult:
    """Outcome of a :meth:`PreTradeGate.check_all` invocation.

    Attributes:
        passed: True only if every entry in ``checks`` passed.
        checks: full audit trail — one dict per check with the keys
            ``{"name": str, "passed": bool, "reason": str}``.
        failed_checks: names of any failed checks, in execution order.
    """

    passed: bool
    checks: List[Dict[str, Any]]
    failed_checks: List[str]

    def summary(self) -> str:
        if self.passed:
            return f"\u2705 All {len(self.checks)} gate checks passed"
        return f"\u274c BLOCKED \u2014 failed: {', '.join(self.failed_checks)}"


class PreTradeGate:
    """Explicit pre-trade checklist run before every order.

    Every check below MUST pass; if even one fails the trade is rejected and
    the failure reason is surfaced through :meth:`GateCheckResult.summary` so
    the caller can log it and emit a SIGNAL_FLOW diagnostic line.

    The gate is stateless: it relies on the supplied RiskManager and config
    for all policy decisions, and it never mutates RiskManager state on its
    own (any side effects come from the existing RiskManager methods it
    delegates to, e.g. the daily-counter rollover inside
    ``check_daily_loss_limit``).
    """

    def check_all(
        self,
        symbol: str,
        side: str,  # "BUY" | "SELL"
        proposed_amount_usdt: float,  # order size in quote (USDT)
        portfolio_value: float,
        open_positions_count: int,
        daily_trades_today: int,
        current_price: float,
        signal_price: float,  # price observed when signal fired
        signal_confidence: float,  # 0.0 – 1.0
        mode: str,
        config: Dict[str, Any],
        risk_manager: "RiskManager",
        pair_loss_guard: Optional[Any] = None,
    ) -> GateCheckResult:
        """Run all gate checks and return an aggregated :class:`GateCheckResult`.

        ``side`` is currently informational (the bot is spot-only / long-only)
        but is kept in the signature so future short logic can branch on it
        without breaking the public API.
        """
        checks: List[Dict[str, Any]] = []

        # Normalise mode once for any per-mode lookups (config keys + slippage).
        mode_key = str(mode or "standard").strip().lower() or "standard"

        # ── 1. Portfolio value ─────────────────────────────────────
        min_balance = float(config.get("portfolio", {}).get("min_balance_threshold", 100))
        checks.append(
            {
                "name": "Portfolio above minimum",
                "passed": portfolio_value >= min_balance,
                "reason": f"portfolio={portfolio_value:.2f} min={min_balance:.2f}",
            }
        )

        # ── 2. Max open positions ──────────────────────────────────
        max_pos = int(config.get("risk", {}).get("max_open_positions", 6))
        checks.append(
            {
                "name": "Max positions not reached",
                "passed": open_positions_count < max_pos,
                "reason": f"open={open_positions_count} max={max_pos}",
            }
        )

        # ── 3. Max daily trades ────────────────────────────────────
        max_daily = int(config.get("risk", {}).get("max_daily_trades", 50))
        checks.append(
            {
                "name": "Daily trade limit",
                "passed": daily_trades_today < max_daily,
                "reason": f"today={daily_trades_today} max={max_daily}",
            }
        )

        # ── 4. Daily loss limit (delegated to RiskManager) ─────────
        daily_check = risk_manager.check_daily_loss_limit(portfolio_value)
        checks.append(
            {
                "name": "Daily loss limit",
                "passed": bool(daily_check.allowed),
                "reason": daily_check.reason or ("OK" if daily_check.allowed else "blocked"),
            }
        )

        # ── 5. Position size ≤ max % of portfolio ──────────────────
        # ``calculate_position_size`` may lift quote to ``min_order_amount * MIN_ORDER_BUFFER``
        # under the hard cap; float noise can nudge pos_pct above ``max_pos_pct``.
        # Compare against an effective ceiling that matches that padding (see ``MIN_ORDER_BUFFER``).
        max_pos_pct = float(config.get("risk", {}).get("max_position_per_trade_pct", 15))
        pos_pct = (proposed_amount_usdt / portfolio_value * 100.0) if portfolio_value > 0 else 0.0
        gate_max_pct = max_pos_pct * MIN_ORDER_BUFFER
        checks.append(
            {
                "name": "Position size within limit",
                "passed": pos_pct <= gate_max_pct + PRETRADE_GATE_POSITION_PCT_EPSILON,
                "reason": f"size={pos_pct:.1f}% cfg_max={max_pos_pct:.1f}% gate≤{gate_max_pct:.1f}%",
            }
        )

        # ── 6. Minimum quote notional (internal trading.min_order_amount; spot ~$10) ─
        # Name states the *pass* condition so a failure means "not enough quote" (incl. 0 from failed sizing).
        min_order = float(config.get("trading", {}).get("min_order_amount", 10.0))
        checks.append(
            {
                "name": "Order quote >= min_order_amount",
                "passed": proposed_amount_usdt >= min_order,
                "reason": f"order={proposed_amount_usdt:.2f} min={min_order:.2f}",
            }
        )

        # ── 7. Cooldown between trades (delegated to RiskManager) ──
        cooling = bool(risk_manager.check_cooldown())
        checks.append(
            {
                "name": "Cooldown not active",
                "passed": not cooling,
                "reason": "Cooldown active" if cooling else "OK",
            }
        )

        # ── 8. Signal confidence above mode threshold ──────────────
        mode_profiles = config.get("mode_indicator_profiles", {}) or {}
        min_conf = float((mode_profiles.get(mode_key) or {}).get("min_confidence", 0.30))
        try:
            conf_val = float(signal_confidence)
        except (TypeError, ValueError):
            conf_val = 0.0
        checks.append(
            {
                "name": "Signal confidence threshold",
                "passed": conf_val >= min_conf,
                "reason": f"confidence={conf_val:.3f} min={min_conf:.3f}",
            }
        )

        # ── 9. Slippage guard (skipped if no valid signal price) ───
        try:
            sig_p = float(signal_price)
        except (TypeError, ValueError):
            sig_p = 0.0
        try:
            cur_p = float(current_price)
        except (TypeError, ValueError):
            cur_p = 0.0
        if sig_p > 0:
            slippage_pct = abs(cur_p - sig_p) / sig_p * 100.0
            max_slip = resolve_max_slippage_pct(mode_key, config)
            checks.append(
                {
                    "name": "Slippage within limit",
                    "passed": slippage_pct <= max_slip,
                    "reason": f"slippage={slippage_pct:.3f}% max={max_slip:.3f}%",
                }
            )

        # ── 10. Drawdown limit (delegated to RiskManager) ──────────
        drawdown_pct = float(risk_manager._get_current_drawdown_pct(portfolio_value))
        max_dd = float(config.get("risk", {}).get("max_drawdown_threshold_pct", 12.0))
        checks.append(
            {
                "name": "Drawdown within limit",
                "passed": drawdown_pct < max_dd,
                "reason": f"drawdown={drawdown_pct:.1f}% max={max_dd:.1f}%",
            }
        )

        # ── 11. Optional per-pair consecutive-loss cooldown ─────────
        if pair_loss_guard is not None:
            blocked = bool(pair_loss_guard.is_blocked(str(symbol or "")))
            reason = (
                pair_loss_guard.block_reason(str(symbol or ""))
                if blocked and callable(getattr(pair_loss_guard, "block_reason", None))
                else ("pair cooldown active" if blocked else "OK")
            )
            checks.append(
                {
                    "name": "Pair loss streak cooldown",
                    "passed": not blocked,
                    "reason": reason or ("OK" if not blocked else "blocked"),
                }
            )

        # ── Aggregate ──────────────────────────────────────────────
        failed = [c["name"] for c in checks if not c["passed"]]
        return GateCheckResult(
            passed=len(failed) == 0,
            checks=checks,
            failed_checks=failed,
        )
