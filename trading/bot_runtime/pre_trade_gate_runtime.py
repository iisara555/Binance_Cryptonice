"""Pre-trade risk gate wired from ExecutionRuntimeDeps."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from helpers import parse_ticker_last

from trading.orchestrator import TradeDecision

logger = logging.getLogger(__name__)


def _buy_quote_and_risk_sizing(
    bot: Any, plan: Any, portfolio: Dict[str, Any]
) -> Tuple[float, Optional[Any]]:
    """Return (quote_usdt, RiskCheckResult|None) for BUY — same path as ``execute_entry`` sizing.

    ``ExecutionPlan.amount`` for new BUY entries is intentionally 0; actual size is
    computed inside ``TradeExecutor.execute_entry`` via ``RiskManager.calculate_position_size``.
    """
    preset = float(getattr(plan, "amount", 0.0) or 0.0)
    if preset > 0:
        return preset, None
    rm = getattr(bot, "risk_manager", None)
    if rm is None:
        return 0.0, None
    pv = float(bot._get_risk_portfolio_value(portfolio))
    entry = float(getattr(plan, "entry_price", 0.0) or 0.0)
    sl = getattr(plan, "stop_loss", None)
    tp = getattr(plan, "take_profit", None)
    conf = float(getattr(plan, "confidence", 0.0) or 0.0)
    sizing = rm.calculate_position_size(
        portfolio_value=pv,
        entry_price=entry,
        stop_loss_price=sl,
        take_profit_price=tp,
        confidence=conf,
        symbol=str(getattr(plan, "symbol", "") or "") or None,
    )
    if sizing.allowed:
        return float(getattr(sizing, "suggested_size", None) or 0.0), sizing
    return 0.0, sizing


def _estimate_buy_quote_for_gate(bot: Any, plan: Any, portfolio: Dict[str, Any]) -> float:
    """Quote-only sizing probe — used by tests; internal code uses :func:`_buy_quote_and_risk_sizing`."""
    q, _ = _buy_quote_and_risk_sizing(bot, plan, portfolio)
    return q


def check_pre_trade_gate(bot: Any, decision: TradeDecision, portfolio: Dict[str, Any]) -> bool:
    """Return True when the trade may proceed past PreTradeGate."""
    if not bool(getattr(bot, "_pre_trade_gate_enabled", True)):
        return True
    plan = getattr(decision, "plan", None)
    if plan is None or getattr(plan, "close_position", False):
        return True
    if getattr(bot, "risk_manager", None) is None:
        return True

    side_value = str(getattr(getattr(plan, "side", ""), "value", getattr(plan, "side", "")) or "").upper()
    if side_value not in {"BUY", "SELL"}:
        return True

    current_price = float(getattr(plan, "entry_price", 0.0) or 0.0)
    try:
        ticker = bot.api_client.get_ticker(plan.symbol)
        current_price = float(parse_ticker_last(ticker) or current_price or 0.0)
    except Exception as exc:
        logger.warning(
            "[PreTradeGate] Ticker fetch failed for %s — using plan entry price for gate: %s",
            getattr(plan, "symbol", "?"),
            exc,
        )

    if side_value == "BUY":
        proposed_quote, sizing_res = _buy_quote_and_risk_sizing(bot, plan, portfolio)
        cfg_root = getattr(bot, "config", None) or {}
        min_order = float((cfg_root.get("trading") or {}).get("min_order_amount", 10.0))
        sl_raw = getattr(plan, "stop_loss", None)
        tp_raw = getattr(plan, "take_profit", None)
        pv_gate = float(bot._get_risk_portfolio_value(portfolio))
        if sizing_res is not None:
            logger.info(
                "[PreTradeGate] BUY sizing_preview symbol=%s quote_est=%.4f sizing_allowed=%s "
                "risk_reason=%s pv=%.2f entry=%.8f sl=%s tp=%s conf=%.4f min_order=%.2f",
                getattr(plan, "symbol", "?"),
                proposed_quote,
                sizing_res.allowed,
                (sizing_res.reason or "")[:280].replace("\n", " "),
                pv_gate,
                float(getattr(plan, "entry_price", 0.0) or 0.0),
                repr(sl_raw),
                repr(tp_raw),
                float(getattr(plan, "confidence", 0.0) or 0.0),
                min_order,
            )
        else:
            logger.info(
                "[PreTradeGate] BUY sizing_preview symbol=%s quote_est=%.4f preset_amount (no RiskCheckResult) pv=%.2f min_order=%.2f",
                getattr(plan, "symbol", "?"),
                proposed_quote,
                pv_gate,
                min_order,
            )
    else:
        proposed_quote = float(getattr(plan, "amount", 0.0) or 0.0)
        if side_value == "SELL":
            proposed_quote *= max(float(getattr(plan, "entry_price", 0.0) or current_price or 0.0), 0.0)

    if getattr(bot, "_state_machine_enabled", False) and getattr(bot, "_state_manager", None):
        open_positions_count = len(bot._state_manager.list_active_states())
    else:
        open_positions_count = len(bot.executor.get_open_orders())

    result = bot._pre_trade_gate.check_all(
        symbol=plan.symbol,
        side=side_value,
        proposed_amount_usdt=proposed_quote,
        portfolio_value=bot._get_risk_portfolio_value(portfolio),
        open_positions_count=open_positions_count,
        daily_trades_today=len(getattr(bot, "_executed_today", []) or []),
        current_price=current_price,
        signal_price=float(getattr(plan, "entry_price", 0.0) or current_price or 0.0),
        signal_confidence=float(getattr(plan, "confidence", 0.0) or 0.0),
        mode=str(getattr(bot, "_active_strategy_mode", "standard") or "standard"),
        config=bot.config,
        risk_manager=bot.risk_manager,
        pair_loss_guard=getattr(bot, "_pair_loss_guard", None),
    )
    if result.passed:
        return True

    failed_csv = ",".join(result.failed_checks) if getattr(result, "failed_checks", None) else ""
    fail_audit = "; ".join(
        f'{c.get("name", "?")}={c.get("reason", "")}' for c in getattr(result, "checks", []) or [] if not c.get("passed", True)
    )
    if fail_audit:
        logger.info("[PreTradeGate] gate_fail_audit symbol=%s %s", getattr(plan, "symbol", "?"), fail_audit[:2400])

    # grep: `failed_checks=` exposes PreTradeGate failure names without parsing emoji summary text
    logger.warning(
        "[PreTradeGate] %s blocked: %s | failed_checks=%s",
        plan.symbol,
        result.summary(),
        failed_csv,
    )
    if bot.send_alerts:
        bot._send_alert(f"PreTradeGate blocked {plan.symbol}: {result.summary()}", to_telegram=False)
    return False
