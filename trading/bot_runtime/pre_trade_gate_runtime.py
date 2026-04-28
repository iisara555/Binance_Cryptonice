"""Pre-trade risk gate wired from ExecutionRuntimeDeps."""

from __future__ import annotations

import logging
from typing import Any, Dict

from helpers import parse_ticker_last

from trading.orchestrator import TradeDecision

logger = logging.getLogger(__name__)


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

    logger.warning("[PreTradeGate] %s blocked: %s", plan.symbol, result.summary())
    if bot.send_alerts:
        bot._send_alert(f"PreTradeGate blocked {plan.symbol}: {result.summary()}", to_telegram=False)
    return False
