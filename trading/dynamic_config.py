"""NAV-adaptive risk parameter computation for CryptoBot V1.

All risk parameters scale automatically when portfolio NAV changes.
No manual config edits needed when balance grows or shrinks.

Expected behaviour (min_order_amount=10):
  NAV=42  → pos=28%  slots=3  floor=33.60
  NAV=100 → pos=12%  slots=6  floor=80.00
  NAV=500 → pos=10%  slots=6  floor=400.00
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict

logger = logging.getLogger(__name__)

# ── Tuning constants ──────────────────────────────────────────────────────────

_NAV_RECOMPUTE_THRESHOLD_PCT = 10.0  # retrigger if NAV drifts by more than this %
_CASH_RESERVE_PCT = 15.0             # keep this % of NAV as undeployed cash
_MIN_ORDER_BUFFER = 1.15             # position_usdt must be >= min_order_amount * this
_NAV_FLOOR_PCT = 0.10               # natural position floor: 10% of NAV
_POSITION_CAP_PCT = 40.0            # hard ceiling on position_pct
_BALANCE_FLOOR_RATIO = 0.80         # min_balance_threshold = NAV * this
_DAILY_LOSS_RATIO = 0.03            # max_daily_loss_usdt = NAV * this
_TRAILING_ACTIVATION_RATIO = 0.50   # trailing_activation_pct = take_profit_pct * this


# ── Core computation ──────────────────────────────────────────────────────────


def compute_dynamic_risk(nav: float, config: Dict[str, Any]) -> Dict[str, Any]:
    """Compute NAV-scaled risk parameters.

    Args:
        nav:    Current portfolio NAV in quote currency (must be > 0).
        config: Flat dict; reads 'min_order_amount' and 'take_profit_pct'.

    Returns:
        Dict of computed values ready to be applied to bot config sections.

    Raises:
        ValueError: if nav <= 0.
    """
    if nav <= 0:
        raise ValueError(f"NAV must be positive, got {nav!r}")

    min_order = float(config.get("min_order_amount", 10.0))
    take_profit_pct = float(config.get("take_profit_pct", 10.0))

    # ── Position percentage ───────────────────────────────────────────────────
    # Must be high enough that position_usdt >= min_order * buffer,
    # or at least 10 % of NAV — whichever gives more capital.
    min_pos_usdt = min_order * _MIN_ORDER_BUFFER       # absolute floor
    natural_pos_usdt = nav * _NAV_FLOOR_PCT            # 10% of NAV
    pos_usdt = max(min_pos_usdt, natural_pos_usdt)
    position_pct = float(min(math.ceil(pos_usdt / nav * 100), _POSITION_CAP_PCT))

    # ── Slot count ────────────────────────────────────────────────────────────
    available_pct = 100.0 - _CASH_RESERVE_PCT          # 85 % deployable
    max_open_positions = max(1, min(6, int(available_pct / position_pct)))

    # ── Other parameters ──────────────────────────────────────────────────────
    min_balance_threshold = round(nav * _BALANCE_FLOOR_RATIO, 2)
    max_daily_loss_usdt = round(nav * _DAILY_LOSS_RATIO, 2)
    trailing_activation_pct = round(take_profit_pct * _TRAILING_ACTIVATION_RATIO, 2)

    return {
        "min_balance_threshold": min_balance_threshold,
        "max_position_per_trade_pct": position_pct,
        "position_size_cap_pct": position_pct,
        "max_open_positions": max_open_positions,
        "max_daily_loss_usdt": max_daily_loss_usdt,
        "trailing_activation_pct": trailing_activation_pct,
    }


# ── NAV fetching ──────────────────────────────────────────────────────────────


def fetch_startup_nav(api_client: Any, config: Dict[str, Any]) -> float:
    """Fetch current portfolio NAV from the exchange at startup.

    Returns the sum of quote-asset holdings plus mark-to-quote of other
    assets.  Falls back to config['portfolio']['initial_balance'] on any
    error so the bot can still start.
    """
    fallback = float((config.get("portfolio") or {}).get("initial_balance", 42.0))
    try:
        balances = api_client.get_balances()
        if not balances:
            logger.warning("[DynamicConfig] Empty balance response — using initial_balance %.2f", fallback)
            return fallback

        quote = str(
            (config.get("data") or {})
            .get("hybrid_dynamic_coin_config", {})
            .get("quote_asset", "THB")
        ).upper()

        total = 0.0
        for asset, payload in balances.items():
            asset_u = str(asset or "").upper()
            if isinstance(payload, dict):
                qty = float(payload.get("available", 0.0) or 0.0) + float(payload.get("reserved", 0.0) or 0.0)
            else:
                qty = float(payload or 0.0)
            if qty <= 0:
                continue
            if asset_u == quote:
                total += qty
            else:
                try:
                    ticker = api_client.get_ticker(f"{asset_u}{quote}")
                    price = float(ticker.get("last") or ticker.get("close") or 0.0)
                    if price > 0:
                        total += qty * price
                except Exception:
                    pass

        if total <= 0:
            logger.warning("[DynamicConfig] NAV sum is zero — using initial_balance %.2f", fallback)
            return fallback

        logger.info("[DynamicConfig] Startup NAV = %.2f %s", total, quote)
        return total

    except Exception as exc:
        logger.warning("[DynamicConfig] NAV fetch failed (%s) — using initial_balance %.2f", exc, fallback)
        return fallback


# ── Config / RiskManager patching ────────────────────────────────────────────


def apply_dynamic_risk_to_config(config: Dict[str, Any], dynamic: Dict[str, Any]) -> None:
    """Patch a config dict in-place with computed dynamic risk values."""
    risk = config.setdefault("risk", {})
    portfolio = config.setdefault("portfolio", {})
    execution = config.setdefault("execution", {})
    pos_sizing = config.setdefault("auto_trader", {}).setdefault("position_sizing", {})

    risk["max_position_per_trade_pct"] = dynamic["max_position_per_trade_pct"]
    risk["max_open_positions"] = dynamic["max_open_positions"]
    portfolio["min_balance_threshold"] = dynamic["min_balance_threshold"]
    execution["trailing_activation_pct"] = dynamic["trailing_activation_pct"]
    pos_sizing["max_position_pct"] = dynamic["position_size_cap_pct"]

    # Keep scalping mode profile in sync so apply_strategy_mode_profile reads
    # the updated value when it rebuilds the merged config.
    scalping = (config.get("strategy_mode") or {}).get("scalping")
    if isinstance(scalping, dict):
        scalping["max_position_per_trade_pct"] = dynamic["max_position_per_trade_pct"]
        scalping["position_size_cap_pct"] = dynamic["position_size_cap_pct"]


def apply_dynamic_risk_to_manager(risk_manager: Any, dynamic: Dict[str, Any]) -> None:
    """Apply computed values directly to a live RiskManager's config dataclass."""
    cfg = getattr(risk_manager, "config", None)
    if cfg is None:
        return
    cfg.max_position_per_trade_pct = dynamic["max_position_per_trade_pct"]
    cfg.max_open_positions = int(dynamic["max_open_positions"])
    cfg.min_balance_threshold = dynamic["min_balance_threshold"]


# ── Runtime tracker ───────────────────────────────────────────────────────────


class DynamicRiskConfig:
    """Tracks NAV and triggers recomputation when it drifts beyond threshold.

    Attach as ``bot._dynamic_risk_config`` to enable the loop-based trigger
    in ``run_iteration_runtime.py``.
    """

    def __init__(self, nav: float, config: Dict[str, Any]) -> None:
        self._last_nav = float(nav)
        self._config = config
        self._last_dynamic: Dict[str, Any] = compute_dynamic_risk(nav, config)

    @property
    def last_nav(self) -> float:
        return self._last_nav

    @property
    def last_dynamic(self) -> Dict[str, Any]:
        return dict(self._last_dynamic)

    def should_recompute(self, current_nav: float) -> bool:
        """True if NAV changed more than _NAV_RECOMPUTE_THRESHOLD_PCT since last compute."""
        if self._last_nav <= 0:
            return True
        delta_pct = abs(current_nav - self._last_nav) / self._last_nav * 100
        return delta_pct > _NAV_RECOMPUTE_THRESHOLD_PCT

    def recompute(self, current_nav: float) -> Dict[str, Any]:
        """Recompute, update state, and return new dynamic values."""
        self._last_dynamic = compute_dynamic_risk(current_nav, self._config)
        self._last_nav = current_nav
        return dict(self._last_dynamic)
