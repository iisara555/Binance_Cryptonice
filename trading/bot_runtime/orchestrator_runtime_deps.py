"""Wire SignalRuntimeDeps / MultiTimeframeRuntimeDeps / ExecutionPlanDeps / ExecutionRuntimeDeps."""

from __future__ import annotations

import threading
from typing import Any

from trading.execution_runtime import ExecutionRuntimeDeps
from .pre_trade_gate_runtime import check_pre_trade_gate
from trading.signal_runtime import ExecutionPlanDeps, MultiTimeframeRuntimeDeps, SignalRuntimeDeps, SignalRuntimeHelper


def build_signal_runtime_deps(bot: Any) -> SignalRuntimeDeps:
    return SignalRuntimeDeps(
        get_portfolio_state=bot._get_portfolio_state,
        get_mtf_signal_for_symbol=bot._get_mtf_signal_for_symbol,
        apply_mtf_confirmation=lambda sym, sigs, mtf: SignalRuntimeHelper.apply_multi_timeframe_confirmation(
            build_multi_timeframe_runtime_deps(bot),
            sym,
            sigs,
            mtf,
        ),
        state_machine_enabled=bool(getattr(bot, "_state_machine_enabled", False)),
        state_manager=getattr(bot, "_state_manager", None),
        last_state_gate_logged=bot.__dict__.setdefault("_last_state_gate_logged", {}),
        get_market_data_for_symbol=bot._get_market_data_for_symbol,
        risk_manager=getattr(bot, "risk_manager", None),
        executed_today=bot.__dict__.setdefault("_executed_today", []),
        signal_generator=bot.signal_generator,
        database=bot.db,
        is_reused_signal_trigger=bot._is_reused_signal_trigger,
        get_signal_trigger_token=bot._get_signal_trigger_token,
        remember_consumed_signal_trigger=bot._remember_consumed_signal_trigger,
        allow_sell_entries_from_idle=bool(getattr(bot, "_allow_sell_entries_from_idle", False)),
        create_execution_plan_for_symbol=bot._create_execution_plan_for_symbol,
        signal_source=bot.signal_source,
        mode=bot.mode,
        active_strategy_mode=str(getattr(bot, "_active_strategy_mode", "standard") or "standard"),
        resolve_active_strategies=bot._resolve_active_strategies_for_mode,
        is_entry_signal_confirmed=bot._is_entry_signal_confirmed,
        process_full_auto=bot._process_full_auto,
        process_semi_auto=bot._process_semi_auto,
        process_dry_run=bot._process_dry_run,
    )


def build_multi_timeframe_runtime_deps(bot: Any) -> MultiTimeframeRuntimeDeps:
    return MultiTimeframeRuntimeDeps(
        mtf_enabled=bool(getattr(bot, "mtf_enabled", False)),
        signal_generator=bot.signal_generator,
        mtf_timeframes=list(getattr(bot, "mtf_timeframes", []) or []),
        database=bot.db,
        last_mtf_status=bot.__dict__.setdefault("_last_mtf_status", {}),
        serialize_mtf_signals_detail=SignalRuntimeHelper.serialize_mtf_signals_detail,
        merge_mtf_signals_detail=SignalRuntimeHelper.merge_mtf_signals_detail,
        mtf_confirmation_required=bool(getattr(bot, "_mtf_confirmation_required", False)),
    )


def build_execution_plan_deps(bot: Any) -> ExecutionPlanDeps:
    return ExecutionPlanDeps(
        state_machine_enabled=bool(getattr(bot, "_state_machine_enabled", False)),
        database=bot.db,
        held_coins_only=bool(getattr(bot, "_held_coins_only", False)),
        api_client=bot.api_client,
        min_trade_value_usdt=float(getattr(bot, "min_trade_value_usdt", 15.0) or 15.0),
        get_latest_atr=bot._get_latest_atr,
        risk_manager=bot.risk_manager,
        loop_count=int(getattr(bot, "_loop_count", 0) or 0),
    )


def build_execution_runtime_deps(bot: Any) -> ExecutionRuntimeDeps:
    pending_lock = getattr(bot, "_pending_decisions_lock", None)
    if pending_lock is None:
        pending_lock = threading.Lock()
        bot._pending_decisions_lock = pending_lock
    return ExecutionRuntimeDeps(
        read_only=bool(getattr(bot, "read_only", False)),
        send_alerts=bool(getattr(bot, "send_alerts", False)),
        format_skip_alert=getattr(bot, "_format_skip_alert", lambda *_args, **_kwargs: ""),
        send_alert=getattr(bot, "_send_alert", lambda *_args, **_kwargs: None),
        send_pending_alert=getattr(bot, "_send_pending_alert", lambda *_args, **_kwargs: None),
        send_dry_run_alert=getattr(bot, "_send_dry_run_alert", lambda *_args, **_kwargs: None),
        state_machine_enabled=bool(getattr(bot, "_state_machine_enabled", False)),
        allow_sell_entries_from_idle=bool(getattr(bot, "_allow_sell_entries_from_idle", False)),
        state_manager=getattr(bot, "_state_manager", None),
        risk_manager=getattr(bot, "risk_manager", None),
        get_risk_portfolio_value=bot._get_risk_portfolio_value,
        config=dict(getattr(bot, "config", {}) or {}),
        executor=getattr(bot, "executor", None),
        database=getattr(bot, "db", None),
        timeframe=str(getattr(bot, "timeframe", "1h") or "1h"),
        submit_managed_entry=getattr(bot, "_submit_managed_entry", lambda *_args, **_kwargs: None),
        try_submit_managed_signal_sell=getattr(
            bot, "_try_submit_managed_signal_sell", lambda *_args, **_kwargs: False
        ),
        send_trade_alert=getattr(bot, "_send_trade_alert", lambda *_args, **_kwargs: None),
        pending_decisions=bot.__dict__.setdefault("_pending_decisions", []),
        pending_decisions_lock=pending_lock,
        get_portfolio_state=bot._get_portfolio_state,
        auth_degraded=bool(getattr(bot, "_auth_degraded", False)),
        mode=bot.mode,
        executed_today=bot.__dict__.setdefault("_executed_today", []),
        pre_trade_gate_check=lambda d, p: check_pre_trade_gate(bot, d, p),
        register_sl_hold_entry=bot._register_sl_hold_entry,
    )
