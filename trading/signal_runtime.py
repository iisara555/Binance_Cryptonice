from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from helpers import extract_base_asset
from signal_generator import AggregatedSignal
from state_management import TradeLifecycleState
from strategy_base import detect_market_condition
from trade_executor import ExecutionPlan, OrderSide
from trading.orchestrator import BotMode, TradeDecision

logger = logging.getLogger(__name__)


def _reduce_opposing_signals_single_direction(signals: List[Any], symbol: str) -> List[Any]:
    """Keep at most one trade direction per symbol when BUY and SELL both appear in one tick."""
    aggs = [s for s in signals if isinstance(s, AggregatedSignal)]
    others = [s for s in signals if not isinstance(s, AggregatedSignal)]
    if len(aggs) < 2:
        return signals
    buckets: Dict[str, List[AggregatedSignal]] = {}
    for s in aggs:
        raw = s.signal_type.value if hasattr(s.signal_type, "value") else s.signal_type
        d = str(raw).upper()
        buckets.setdefault(d, []).append(s)
    if "BUY" not in buckets or "SELL" not in buckets:
        return signals
    best_buy = max(buckets["BUY"], key=lambda x: x.combined_confidence)
    best_sell = max(buckets["SELL"], key=lambda x: x.combined_confidence)
    if best_buy.combined_confidence >= best_sell.combined_confidence:
        kept = best_buy
        dropped = "SELL"
        other_best = best_sell.combined_confidence
    else:
        kept = best_sell
        dropped = "BUY"
        other_best = best_buy.combined_confidence
    logger.debug(
        "[Signal] %s opposing BUY/SELL same tick — keeping %s conf=%.3f (dropped %s conf=%.3f)",
        symbol,
        str(kept.signal_type.value if hasattr(kept.signal_type, "value") else kept.signal_type).upper(),
        kept.combined_confidence,
        dropped,
        other_best,
    )
    extra: List[AggregatedSignal] = []
    for direction, lst in buckets.items():
        if direction in ("BUY", "SELL"):
            continue
        extra.append(max(lst, key=lambda x: x.combined_confidence))
    return others + extra + [kept]


@dataclass(slots=True)
class SignalRuntimeDeps:
    """Deps for one pair iteration. End-to-end: orchestrator `_run_iteration` → here → `ExecutionRuntimeHelper`."""

    get_portfolio_state: Callable[[], Dict[str, Any]]
    get_mtf_signal_for_symbol: Callable[[str, Dict[str, Any]], Any]
    apply_mtf_confirmation: Callable[[str, List[AggregatedSignal], Any], List[AggregatedSignal]]
    state_machine_enabled: bool
    state_manager: Any
    last_state_gate_logged: Dict[str, str]
    get_market_data_for_symbol: Callable[[str], Any]
    risk_manager: Any
    executed_today: List[Dict[str, Any]]
    signal_generator: Any
    database: Any
    is_reused_signal_trigger: Callable[[Optional[AggregatedSignal]], bool]
    get_signal_trigger_token: Callable[[Optional[AggregatedSignal]], str]
    allow_sell_entries_from_idle: bool
    create_execution_plan_for_symbol: Callable[[AggregatedSignal, str], Optional[ExecutionPlan]]
    signal_source: Any
    mode: BotMode
    active_strategy_mode: str
    resolve_active_strategies: Callable[[str], List[str]]
    is_entry_signal_confirmed: Callable[[Any, str, str], bool]
    process_full_auto: Callable[[TradeDecision, Dict[str, Any]], None]
    process_semi_auto: Callable[[TradeDecision, Dict[str, Any]], None]
    process_dry_run: Callable[[TradeDecision, Dict[str, Any]], None]


@dataclass(slots=True)
class MultiTimeframeRuntimeDeps:
    mtf_enabled: bool
    signal_generator: Any
    mtf_timeframes: List[str]
    database: Any
    last_mtf_status: Dict[str, Dict[str, Any]]
    serialize_mtf_signals_detail: Callable[[Any], Dict[str, Dict[str, Any]]]
    merge_mtf_signals_detail: Callable[
        [Optional[Dict[str, Dict[str, Any]]], Optional[Dict[str, Dict[str, Any]]]], Dict[str, Dict[str, Any]]
    ]
    mtf_confirmation_required: bool


@dataclass(slots=True)
class ExecutionPlanDeps:
    state_machine_enabled: bool
    database: Any
    held_coins_only: bool
    api_client: Any
    min_trade_value_thb: float
    get_latest_atr: Callable[[str], Optional[float]]
    risk_manager: Any
    loop_count: int


class SignalRuntimeHelper:
    @staticmethod
    def process_pair_iteration(deps: SignalRuntimeDeps, symbol: str) -> None:
        """Per-pair path: MTF snapshot → primary OHLCV → sync_state → generate_signals →
        opposing-direction collapse → **MTF confirmation** → DB signal log → check_risk →
        execution plan → `process_full_auto` / semi / dry (see `trading/execution_runtime.py`).
        """
        portfolio = deps.get_portfolio_state()
        mtf_signal = deps.get_mtf_signal_for_symbol(symbol, portfolio)
        lifecycle_gated = False

        if deps.state_machine_enabled:
            snapshot = deps.state_manager.get_state(symbol)
            if snapshot.state != TradeLifecycleState.IDLE:
                last_logged = deps.last_state_gate_logged.get(symbol)
                if last_logged != snapshot.state.value:
                    logger.info("[State] %s gated by execution state: %s", symbol, snapshot.state.value)
                    deps.last_state_gate_logged[symbol] = snapshot.state.value
                lifecycle_gated = True
            deps.last_state_gate_logged.pop(symbol, None)

        data = deps.get_market_data_for_symbol(symbol)
        if data is None or data.empty:
            logger.debug(f"No market data for {symbol}, skipping")
            return

        if deps.state_machine_enabled:
            open_count = len(deps.state_manager.list_active_states())
        else:
            open_count = len(portfolio.get("positions", []))
        daily_count = deps.risk_manager.trade_count_today if deps.risk_manager is not None else len(deps.executed_today)
        deps.signal_generator.sync_state(
            open_positions_count=open_count,
            daily_trades_count=daily_count,
        )

        strategy_mode = str(deps.active_strategy_mode or "standard").strip().lower() or "standard"
        active_strategies = deps.resolve_active_strategies(strategy_mode)
        refresh = getattr(deps.signal_generator, "refresh_risk_config_for_mode", None)
        if callable(refresh):
            refresh(strategy_mode)
        signals = deps.signal_generator.generate_signals(
            data=data,
            symbol=symbol,
            use_strategies=active_strategies,
        )
        if not isinstance(signals, list):
            fallback = getattr(deps.signal_generator, "generate_sniper_signal", None)
            if callable(fallback):
                signals = fallback(data=data, symbol=symbol)
        if not isinstance(signals, list):
            signals = []
        signals = _reduce_opposing_signals_single_direction(signals, symbol)

        aggs = [s for s in signals if isinstance(s, AggregatedSignal)]
        aggs = deps.apply_mtf_confirmation(symbol, aggs, mtf_signal)
        signals = aggs

        current_market_condition = None
        for generated_signal in signals:
            if isinstance(generated_signal, AggregatedSignal):
                current_market_condition = generated_signal.market_condition
                break
        if current_market_condition is None:
            current_market_condition = detect_market_condition(data["close"].tolist())

        if not signals:
            logger.debug(f"No signals generated for {symbol}")
            return

        if lifecycle_gated:
            return

        for signal in signals:
            try:
                if isinstance(signal, AggregatedSignal):
                    sig_type = (
                        signal.signal_type.value.upper()
                        if hasattr(signal.signal_type, "value")
                        else str(signal.signal_type).upper()
                    )
                    strategy_names = ",".join(sorted(signal.strategy_votes.keys())) if signal.strategy_votes else ""
                    deps.database.insert_signal(
                        pair=signal.symbol,
                        signal_type=sig_type,
                        confidence=signal.combined_confidence,
                        result="pending",
                        strategy=strategy_names,
                    )
            except Exception as exc:
                logger.error(f"Failed to log signal to database: {exc}")

        for signal in signals:
            if not isinstance(signal, AggregatedSignal):
                continue

            portfolio = deps.get_portfolio_state()

            signal_type = (
                signal.signal_type.value.lower()
                if hasattr(signal.signal_type, "value")
                else str(signal.signal_type).lower()
            )
            if signal_type == "buy" and not deps.is_entry_signal_confirmed(data, signal_type, strategy_mode):
                logger.debug(
                    "[ConfirmationGate] %s BUY signal pending confirmation for mode=%s",
                    symbol,
                    strategy_mode,
                )
                continue
            if deps.is_reused_signal_trigger(signal):
                logger.info(
                    "[Signal] %s %s ignored: trigger already consumed (%s)",
                    symbol,
                    signal_type.upper(),
                    deps.get_signal_trigger_token(signal),
                )
                continue

            risk_check = deps.signal_generator.check_risk(signal, portfolio)

            if deps.state_machine_enabled:
                if signal_type == "buy":
                    approved, gate_reason = deps.state_manager.confirm_entry_signal(
                        symbol=symbol,
                        signal_type=signal_type,
                        confidence=signal.combined_confidence,
                        risk_passed=risk_check.passed,
                        signal_time=signal.timestamp,
                    )
                    if not approved:
                        if gate_reason.startswith("awaiting confirmation"):
                            logger.debug("[State] %s BUY signal queued: %s", symbol, gate_reason)
                        else:
                            logger.debug("[State] %s signal ignored: %s", symbol, gate_reason)
                        continue
                elif signal_type == "sell":
                    snapshot = deps.state_manager.get_state(symbol)
                    if snapshot.state == TradeLifecycleState.IN_POSITION:
                        pass
                    elif not deps.allow_sell_entries_from_idle:
                        logger.debug(
                            "[State] %s SELL signal ignored: lifecycle=%s and idle SELL is disabled",
                            symbol,
                            snapshot.state.value,
                        )
                        continue
                    else:
                        approved, gate_reason = deps.state_manager.confirm_idle_sell_signal(
                            symbol=symbol,
                            confidence=signal.combined_confidence,
                            risk_passed=risk_check.passed,
                            signal_time=signal.timestamp,
                        )
                        if not approved:
                            if gate_reason.startswith("awaiting confirmation"):
                                logger.debug("[State] %s SELL signal queued: %s", symbol, gate_reason)
                            else:
                                logger.debug("[State] %s SELL signal ignored: %s", symbol, gate_reason)
                            continue
                else:
                    logger.debug("[State] %s signal ignored: unsupported type '%s'", symbol, signal_type)
                    continue

            plan = deps.create_execution_plan_for_symbol(signal, symbol)
            if not plan:
                continue

            decision = TradeDecision(
                plan=plan,
                signal=signal,
                risk_check=risk_check,
                signal_source=deps.signal_source,
            )

            if deps.mode == BotMode.FULL_AUTO:
                deps.process_full_auto(decision, portfolio)
            elif deps.mode == BotMode.SEMI_AUTO:
                deps.process_semi_auto(decision, portfolio)
            else:
                deps.process_dry_run(decision, portfolio)

    @staticmethod
    def serialize_mtf_signals_detail(mtf_result: Any) -> Dict[str, Dict[str, Any]]:
        details: Dict[str, Dict[str, Any]] = {}
        for timeframe, tf_signal in (getattr(mtf_result, "signals", {}) or {}).items():
            signal_type = getattr(
                getattr(tf_signal, "signal_type", None), "value", getattr(tf_signal, "signal_type", None)
            )
            indicators = getattr(tf_signal, "indicators", {}) or {}
            details[str(timeframe)] = {
                "type": str(signal_type or "HOLD").upper(),
                "confidence": float(getattr(tf_signal, "confidence", 0.0) or 0.0),
                "trend_strength": float(getattr(tf_signal, "trend_strength", 0.0) or 0.0),
                "rsi": float(indicators.get("rsi", 0.0) or 0.0),
                "adx": float(indicators.get("adx", 0.0) or 0.0),
                "macd_hist": float(indicators.get("macd_hist", 0.0) or 0.0),
                "volume_ratio": float(indicators.get("volume_ratio", 0.0) or 0.0),
                "reason": str(getattr(tf_signal, "reason", "") or ""),
            }
        return details

    @staticmethod
    def merge_mtf_signals_detail(
        base_details: Optional[Dict[str, Dict[str, Any]]],
        override_details: Optional[Dict[str, Dict[str, Any]]],
    ) -> Dict[str, Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        ordered_timeframes: List[str] = []

        for source in (base_details or {}, override_details or {}):
            for timeframe in source.keys():
                normalized = str(timeframe)
                if normalized not in ordered_timeframes:
                    ordered_timeframes.append(normalized)

        for timeframe in ordered_timeframes:
            base_row = dict((base_details or {}).get(timeframe) or {})
            override_row = dict((override_details or {}).get(timeframe) or {})
            clean_override = {
                key: value
                for key, value in override_row.items()
                if value is not None and (not isinstance(value, str) or value.strip())
            }
            merged[timeframe] = {
                **base_row,
                **clean_override,
            }

        return merged

    @staticmethod
    def get_mtf_signal_for_symbol(
        deps: MultiTimeframeRuntimeDeps,
        symbol: str,
        portfolio: Dict[str, Any],
    ) -> Any:
        if not deps.mtf_enabled:
            return None

        recorded_at = datetime.now().isoformat()
        try:
            mtf_result = deps.signal_generator.generate_mtf_signals(
                pair=symbol,
                timeframes=deps.mtf_timeframes,
                db=deps.database,
            )
        except Exception as exc:
            logger.warning("MTF signal generation failed for %s: %s", symbol, exc)
            deps.last_mtf_status[symbol] = {
                "updated_at": recorded_at,
                "status": "error",
                "reason": str(exc),
                "timeframes": list(deps.mtf_timeframes),
            }
            return None

        if mtf_result is None:
            deps.last_mtf_status[symbol] = {
                "updated_at": recorded_at,
                "status": "waiting",
                "reason": "No multi-timeframe data available yet",
                "timeframes": list(deps.mtf_timeframes),
            }
            return None

        snapshot = {
            "updated_at": recorded_at,
            "timeframes": list(getattr(mtf_result, "timeframes", {}).keys() or deps.mtf_timeframes),
            "signals_detail": deps.serialize_mtf_signals_detail(mtf_result),
            "trend_alignment": float(getattr(mtf_result, "trend_alignment", 0.0) or 0.0),
            "consensus_strength": float(getattr(mtf_result, "consensus_strength", 0.0) or 0.0),
            "higher_timeframe_trend": getattr(
                getattr(mtf_result, "higher_timeframe_trend", None),
                "value",
                getattr(mtf_result, "higher_timeframe_trend", None),
            ),
            "higher_timeframe_confidence": float(getattr(mtf_result, "higher_timeframe_confidence", 0.0) or 0.0),
        }

        try:
            signal = deps.signal_generator.get_mtf_signal(
                pair=symbol,
                timeframes=deps.mtf_timeframes,
                portfolio=portfolio,
                db=deps.database,
                mtf_result=mtf_result,
            )
        except Exception as exc:
            logger.warning("MTF signal build failed for %s: %s", symbol, exc)
            deps.last_mtf_status[symbol] = {
                **snapshot,
                "status": "error",
                "reason": str(exc),
            }
            return None

        if signal is None:
            deps.last_mtf_status[symbol] = {
                **snapshot,
                "status": "waiting",
                "reason": "No aligned multi-timeframe signal yet",
            }
            return None

        metadata = getattr(signal, "metadata", {}) or {}
        deps.last_mtf_status[symbol] = {
            **snapshot,
            "status": "ready",
            "signal_type": signal.signal_type.value,
            "confidence": signal.confidence,
            "timeframes": list(metadata.get("timeframes_used") or snapshot["timeframes"]),
            "higher_timeframe_trend": metadata.get("higher_timeframe_trend") or snapshot["higher_timeframe_trend"],
            "signals_detail": deps.merge_mtf_signals_detail(
                snapshot["signals_detail"],
                metadata.get("signals_detail"),
            ),
        }
        return signal

    @staticmethod
    def apply_multi_timeframe_confirmation(
        deps: MultiTimeframeRuntimeDeps,
        symbol: str,
        signals: List[AggregatedSignal],
        mtf_signal: Any,
    ) -> List[AggregatedSignal]:
        if not deps.mtf_enabled or not signals:
            return signals

        if mtf_signal is None:
            if deps.mtf_confirmation_required:
                logger.debug("[MTF] %s skipped: higher-timeframe confirmation not ready", symbol)
                return []
            return signals

        mtf_type = str(getattr(mtf_signal.signal_type, "value", mtf_signal.signal_type)).upper()
        confirmed: List[AggregatedSignal] = []
        for signal in signals:
            signal_type = str(getattr(signal.signal_type, "value", signal.signal_type)).upper()
            if signal_type == mtf_type:
                signal.combined_confidence = min(
                    0.99,
                    max(signal.combined_confidence, (signal.combined_confidence + float(mtf_signal.confidence)) / 2),
                )
                rationale_suffix = f" | MTF confirmed {mtf_type} ({float(mtf_signal.confidence):.0%})"
                signal.trade_rationale = f"{signal.trade_rationale or '[Trade Triggered]'}{rationale_suffix}"
                confirmed.append(signal)
                continue

            if deps.mtf_confirmation_required:
                logger.debug("[MTF] %s filtered %s due to conflicting %s confirmation", symbol, signal_type, mtf_type)
                continue

            signal.combined_confidence = max(0.0, signal.combined_confidence * 0.75)
            rationale_suffix = f" | MTF conflict {mtf_type} ({float(mtf_signal.confidence):.0%})"
            signal.trade_rationale = f"{signal.trade_rationale or '[Trade Triggered]'}{rationale_suffix}"
            confirmed.append(signal)

        return confirmed

    @staticmethod
    def create_execution_plan_for_symbol(
        deps: ExecutionPlanDeps,
        signal: AggregatedSignal,
        symbol: str,
    ) -> Optional[ExecutionPlan]:
        signal_type_value = str(getattr(signal.signal_type, "value", signal.signal_type) or "").lower()
        side = OrderSide.BUY if signal_type_value == "buy" else OrderSide.SELL
        plan_close_flag = False

        try:
            if deps.state_machine_enabled:
                open_positions = []
            else:
                session = deps.database.get_session()
                try:
                    from sqlalchemy import text

                    open_positions = session.execute(
                        text("SELECT side, amount FROM positions WHERE symbol = :symbol AND remaining_amount > 0"),
                        {"symbol": symbol},
                    ).fetchall()
                finally:
                    session.close()

            if open_positions:
                current_side = open_positions[0][0]
                if current_side == "sell" and side == OrderSide.BUY:
                    plan_close_flag = True
                elif current_side == "buy" and side == OrderSide.SELL:
                    plan_close_flag = True
        except Exception as exc:
            logger.debug(f"[Position Check] Could not check positions: {exc}")

        if side == OrderSide.BUY and plan_close_flag is False and deps.held_coins_only:
            has_history = False
            try:
                if deps.database:
                    has_history = deps.database.has_ever_held(symbol)
            except Exception as exc:
                logger.debug(f"[Portfolio Guard] history lookup failed for {symbol}: {exc}")

            if not has_history:
                logger.debug(f"[Portfolio Guard] BUY rejected for {symbol}: never held")
                return None

        if side == OrderSide.SELL and plan_close_flag is False:
            try:
                base_asset = extract_base_asset(symbol)
                balances = deps.api_client.get_balances()
                base_data = balances.get(base_asset.upper()) or balances.get(base_asset.lower()) or {}
                available_base = float(base_data.get("available", 0))
                base_value_thb = available_base * signal.avg_price
                if base_value_thb < deps.min_trade_value_thb:
                    logger.debug(
                        f"[Strategy Bypass] {base_asset.upper()} value {base_value_thb:.2f} THB "
                        f"< MIN {deps.min_trade_value_thb:.2f} THB"
                    )
                    return None
            except Exception as exc:
                logger.debug(f"[Balance Check] ไม่สามารถตรวจสอบ balance: {exc}")

        entry_price = signal.avg_price
        atr_value = deps.get_latest_atr(symbol)
        if not atr_value or atr_value <= 0:
            logger.debug(f"Trade rejected for {symbol}: ATR unavailable")
            return None

        if signal.avg_stop_loss and signal.avg_take_profit and signal.avg_stop_loss > 0 and signal.avg_take_profit > 0:
            sl = signal.avg_stop_loss
            tp = signal.avg_take_profit
            logger.debug(f"Using signal SL/TP for {symbol}: SL={sl:.4f} TP={tp:.4f}")
        elif side == OrderSide.BUY:
            rr_ratio = signal.avg_risk_reward if signal.avg_risk_reward > 0 else 2.0
            sl, tp = deps.risk_manager.calc_sl_tp_from_atr(
                entry_price=entry_price,
                atr_value=atr_value,
                direction="long",
                risk_reward_ratio=rr_ratio,
            )
            logger.debug(f"ATR-based SL/TP for {symbol}: SL={sl:.4f} TP={tp:.4f} (ATR={atr_value:.4f})")
        else:
            # Spot mode: SELL is an exit action, not a new short position.
            sl, tp = None, None

        return ExecutionPlan(
            symbol=symbol,
            side=side,
            amount=0,
            entry_price=entry_price,
            stop_loss=sl,
            take_profit=tp,
            risk_reward_ratio=signal.avg_risk_reward,
            confidence=signal.combined_confidence,
            strategy_votes=signal.strategy_votes,
            notes=[
                f"Confidence: {signal.combined_confidence:.2%}",
                f"Strategies: {', '.join(signal.strategy_votes.keys())}",
                f"Risk Score: {signal.risk_score:.0f}/100",
                f"ATR: {atr_value:.4f}" if atr_value else "ATR: N/A",
                f"Dynamic SL/TP: pair={symbol}",
            ],
            signal_timestamp=signal.timestamp,
            signal_id=f"{signal.symbol}_{signal.signal_type.value}_{int(signal.timestamp.timestamp())}_{deps.loop_count}",
            max_price_drift_pct=1.5,
            close_position=plan_close_flag,
        )
