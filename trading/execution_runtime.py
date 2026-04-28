from __future__ import annotations

from dataclasses import dataclass
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, MutableSequence

from risk_management import check_pair_correlation
from trade_executor import OrderSide
from trading.orchestrator import BotMode, TradeDecision


logger = logging.getLogger(__name__)


class _NullLock:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


@dataclass(slots=True)
class ExecutionRuntimeDeps:
    read_only: bool
    send_alerts: bool
    format_skip_alert: Callable[[TradeDecision], str]
    send_alert: Callable[..., None]
    send_pending_alert: Callable[[TradeDecision, Dict[str, Any]], None]
    send_dry_run_alert: Callable[[TradeDecision, Dict[str, Any]], None]
    state_machine_enabled: bool
    allow_sell_entries_from_idle: bool
    state_manager: Any
    risk_manager: Any
    get_risk_portfolio_value: Callable[[Dict[str, Any]], float]
    config: Dict[str, Any]
    executor: Any
    database: Any
    timeframe: str
    submit_managed_entry: Callable[[TradeDecision, Dict[str, Any]], None]
    try_submit_managed_signal_sell: Callable[[TradeDecision], bool]
    send_trade_alert: Callable[[TradeDecision, Any], None]
    pending_decisions: MutableSequence[TradeDecision]
    pending_decisions_lock: Any
    get_portfolio_state: Callable[[], Dict[str, Any]]
    auth_degraded: bool
    mode: BotMode
    executed_today: MutableSequence[Dict[str, Any]]
    pre_trade_gate_check: Any = None
    register_sl_hold_entry: Any = None


class ExecutionRuntimeHelper:
    @staticmethod
    def process_full_auto(deps: ExecutionRuntimeDeps, decision: TradeDecision, portfolio: Dict[str, Any]) -> None:
        """Last mile after `SignalRuntimeHelper.process_pair_iteration` builds a `TradeDecision`."""
        if deps.read_only:
            logger.info("READ_ONLY mode — skipping trade execution")
            return

        if not decision.risk_check.passed:
            reason = getattr(decision.risk_check, "reason", getattr(decision.risk_check, "reasons", "Unknown reasons"))
            logger.info(f"🛡️ Risk Manager: ปฏิเสธสัญญาณเทรด ({reason})")
            if deps.send_alerts:
                deps.send_alert(deps.format_skip_alert(decision))
            return

        if callable(deps.pre_trade_gate_check) and not deps.pre_trade_gate_check(decision, portfolio):
            return

        rationale = getattr(decision.signal, "trade_rationale", "N/A")
        logger.info(f"[Trade Decision] {rationale}")
        side_thai = "ซื้อ" if decision.plan.side.value == "buy" else "ขาย"
        logger.info(f"🤖 [FULL_AUTO] กำลังส่งคำสั่งเทรด | {side_thai} ({decision.plan.side.value.upper()}) @ {decision.plan.entry_price:,.2f} quote")

        is_new_entry_buy = decision.plan.side.value == "buy"
        is_new_entry_idle_sell = (
            decision.plan.side.value == "sell"
            and not decision.plan.close_position
            and (not deps.state_machine_enabled or deps.allow_sell_entries_from_idle)
        )
        if (is_new_entry_buy or is_new_entry_idle_sell) and deps.risk_manager:
            if deps.state_machine_enabled:
                open_count = len(deps.state_manager.list_active_states())
            else:
                open_count = len(portfolio.get("positions", []))
            gate = deps.risk_manager.can_open_position(
                deps.get_risk_portfolio_value(portfolio), open_count,
            )
            if not gate.allowed:
                logger.warning("🚫 [RiskGate] Trade blocked for %s: %s", decision.plan.symbol, gate.reason)
                return

            corr_threshold = float(deps.config.get("risk", {}).get("correlation_threshold", 0.75))
            if corr_threshold < 1.0:
                open_symbols = [
                    str(pos.get("symbol", "")).upper()
                    for pos in deps.executor.get_open_orders()
                    if pos.get("symbol")
                ]
                if open_symbols:
                    corr_gate = check_pair_correlation(
                        candidate_symbol=decision.plan.symbol,
                        open_symbols=open_symbols,
                        db=deps.database,
                        threshold=corr_threshold,
                        timeframe=deps.timeframe,
                    )
                    if not corr_gate.allowed:
                        logger.warning("🔗 [CorrelationGate] Trade blocked for %s: %s", decision.plan.symbol, corr_gate.reason)
                        return

        if deps.state_machine_enabled:
            if decision.plan.side == OrderSide.BUY:
                deps.submit_managed_entry(decision, portfolio)
                return
            if deps.try_submit_managed_signal_sell(decision):
                return
            if not deps.allow_sell_entries_from_idle:
                logger.debug(
                    "[State] SELL signal skipped for %s: no in-position state and idle SELL is disabled",
                    decision.plan.symbol,
                )
                return

        result = deps.executor.execute_entry(decision.plan, deps.get_risk_portfolio_value(portfolio))
        if result.success:
            if callable(deps.register_sl_hold_entry) and result.order_id:
                deps.register_sl_hold_entry(result.order_id)
            decision.status = "executed"
            deps.executed_today.append({
                "decision": decision,
                "result": result,
                "timestamp": datetime.now(),
            })
            try:
                ts = datetime.now(timezone.utc)
                deps.database.insert_order(
                    pair=decision.plan.symbol,
                    side=decision.plan.side.value,
                    quantity=result.filled_amount,
                    price=result.filled_price or 0.0,
                    status="filled",
                    order_type="limit",
                    timestamp=ts,
                )
            except Exception as exc:
                logger.error(f"Failed to log order to database: {exc}", exc_info=True)
            deps.send_trade_alert(decision, result)
            return

        decision.status = "failed"
        if "Skipping order" in result.message:
            logger.info(result.message)
            return

        logger.error(f"Trade execution failed: {result.message}")
        if deps.send_alerts:
            symbol = decision.plan.symbol if decision.plan else "?"
            deps.send_alert(
                f"\u26a0\ufe0f Trade REJECTED [{symbol}]: {result.message}",
                to_telegram=True,
            )

    @staticmethod
    def process_semi_auto(deps: ExecutionRuntimeDeps, decision: TradeDecision, portfolio: Dict[str, Any]) -> None:
        if not decision.risk_check.passed:
            return
        if callable(deps.pre_trade_gate_check) and not deps.pre_trade_gate_check(decision, portfolio):
            return

        with (deps.pending_decisions_lock or _NullLock()):
            deps.pending_decisions.append(decision)

        deps.send_pending_alert(decision, portfolio)
        logger.info(f"SEMI_AUTO: Alert sent for {decision.plan.side.value} @ {decision.plan.entry_price}")

    @staticmethod
    def process_dry_run(
        deps: ExecutionRuntimeDeps,
        decision: TradeDecision,
        portfolio: Dict[str, Any],
    ) -> None:
        if not decision.risk_check.passed:
            return

        logger.info(f"DRY_RUN: Would execute {decision.plan.side.value} @ {decision.plan.entry_price}")
        logger.info(f"  Confidence: {decision.plan.confidence:.2%}")
        logger.info(f"  Strategy votes: {decision.plan.strategy_votes}")
        logger.info(f"  Risk-reward: {decision.plan.risk_reward_ratio:.2f}")
        deps.send_dry_run_alert(decision, portfolio)

    @staticmethod
    def approve_trade(deps: ExecutionRuntimeDeps, decision_id: int) -> bool:
        if deps.auth_degraded:
            logger.warning("approve_trade blocked: auth degraded mode is active")
            return False

        if deps.mode != BotMode.SEMI_AUTO:
            logger.warning("approve_trade called but mode is not semi_auto")
            return False

        with (deps.pending_decisions_lock or _NullLock()):
            if decision_id < 0 or decision_id >= len(deps.pending_decisions):
                logger.error(f"Invalid decision_id: {decision_id}")
                return False
            # Claim under lock to prevent duplicate concurrent approvals
            # from executing the same decision.
            decision = deps.pending_decisions.pop(decision_id)

        restored = False

        def _restore_pending_decision() -> None:
            nonlocal restored
            if restored:
                return
            with (deps.pending_decisions_lock or _NullLock()):
                insert_at = min(max(decision_id, 0), len(deps.pending_decisions))
                deps.pending_decisions.insert(insert_at, decision)
            restored = True

        portfolio = deps.get_portfolio_state()

        logger.info(f"Manual approval: executing trade #{decision_id}")

        if callable(deps.pre_trade_gate_check) and not deps.pre_trade_gate_check(decision, portfolio):
            _restore_pending_decision()
            return False

        if deps.state_machine_enabled:
            if decision.plan.side == OrderSide.BUY:
                deps.submit_managed_entry(decision, portfolio)
                return True
            if deps.try_submit_managed_signal_sell(decision):
                return True
            if not deps.allow_sell_entries_from_idle:
                logger.debug(
                    "approve_trade skipped SELL for %s: idle SELL is disabled",
                    decision.plan.symbol,
                )
                _restore_pending_decision()
                return False

        result = deps.executor.execute_entry(decision.plan, deps.get_risk_portfolio_value(portfolio))
        if not result.success:
            _restore_pending_decision()
            return False
        if callable(deps.register_sl_hold_entry) and result.order_id:
            deps.register_sl_hold_entry(result.order_id)

        decision.status = "executed"
        deps.executed_today.append({
            "decision": decision,
            "result": result,
            "timestamp": datetime.now(),
        })
        return True

    @staticmethod
    def reject_trade(deps: ExecutionRuntimeDeps, decision_id: int) -> bool:
        with (deps.pending_decisions_lock or _NullLock()):
            if decision_id < 0 or decision_id >= len(deps.pending_decisions):
                return False
            decision = deps.pending_decisions[decision_id]
            decision.status = "rejected"
            deps.pending_decisions.pop(decision_id)

        logger.info(f"Trade #{decision_id} rejected")
        return True
