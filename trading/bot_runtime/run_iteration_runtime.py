"""One trading loop iteration: gates, reconciliation, SL/TP, per-pair signal processing."""

from __future__ import annotations

import logging
from typing import Any

from signal_generator import ensure_signal_flow_record

logger = logging.getLogger(__name__)


def _resolve_active_pairs_for_iteration(bot: Any, *, allow_refresh: bool) -> list[str]:
    trading_pairs = bot._get_trading_pairs()
    logger.debug("Processing %d trading pair(s): %s", len(trading_pairs), trading_pairs)

    active_pairs = trading_pairs
    if bot._held_coins_only:
        active_pairs = [pair for pair in trading_pairs if bot.db.has_ever_held(pair)]

        if len(active_pairs) < len(trading_pairs):
            skipped = [p for p in trading_pairs if p not in active_pairs]
            skipped_key = tuple(skipped)
            if skipped_key != bot._last_portfolio_guard_skipped:
                logger.info("🛡️  [Portfolio Guard] Skipping never-held pairs: %s", skipped)
                bot._last_portfolio_guard_skipped = skipped_key
        else:
            bot._last_portfolio_guard_skipped = ()
    else:
        bot._last_portfolio_guard_skipped = ()

    return bot._filter_pairs_by_candle_readiness(active_pairs, allow_refresh=allow_refresh)


def _refresh_paused_signal_flow(bot: Any, reason: str) -> None:
    """Populate SigFlow diagnostics while trading remains paused and non-executing."""
    try:
        active_pairs = _resolve_active_pairs_for_iteration(bot, allow_refresh=False)
    except Exception as exc:
        logger.debug("[Paused Diagnostics] Could not resolve active pairs: %s", exc)
        return

    if not active_pairs:
        return

    strategy_mode = str(getattr(bot, "_active_strategy_mode", "standard") or "standard").strip().lower()
    try:
        active_strategies = bot._resolve_active_strategies_for_mode(strategy_mode)
    except Exception as exc:
        logger.debug("[Paused Diagnostics] Could not resolve strategies for mode=%s: %s", strategy_mode, exc)
        active_strategies = []

    refresh = getattr(getattr(bot, "signal_generator", None), "refresh_risk_config_for_mode", None)
    if callable(refresh):
        try:
            refresh(strategy_mode)
        except Exception as exc:
            logger.debug("[Paused Diagnostics] Risk config refresh skipped: %s", exc)

    try:
        if getattr(bot, "_state_machine_enabled", False):
            open_count = len(bot._state_manager.list_active_states())
        else:
            open_count = len(bot.executor.get_open_orders())
        risk_manager = getattr(bot, "risk_manager", None)
        daily_count = (
            risk_manager.trade_count_today if risk_manager is not None else len(getattr(bot, "_executed_today", []))
        )
        bot.signal_generator.sync_state(open_positions_count=open_count, daily_trades_count=daily_count)
    except Exception as exc:
        logger.debug("[Paused Diagnostics] Signal state sync skipped: %s", exc)

    for pair in active_pairs:
        try:
            data = bot._get_market_data_for_symbol(pair)
            if data is None or getattr(data, "empty", False):
                ensure_signal_flow_record(pair, f"Paused: no market data ({reason})")
                continue

            signals = bot.signal_generator.generate_signals(
                data=data,
                symbol=pair,
                use_strategies=active_strategies,
            )
            if not isinstance(signals, list):
                fallback = getattr(bot.signal_generator, "generate_sniper_signal", None)
                if callable(fallback):
                    fallback(data=data, symbol=pair)
        except Exception as exc:
            logger.debug("[Paused Diagnostics] %s signal diagnostics failed: %s", pair, exc)


def run_trading_iteration(bot: Any) -> None:
    """Auth / circuit / clock / pause / kill-switch → positions → each ready pair.

    Per pair: `SignalRuntimeHelper.process_pair_iteration` (see `trading/signal_runtime.py`).
    """
    if bot._auth_degraded:
        if not bot._auth_degraded_logged:
            logger.warning(
                "Trading loop running in degraded public-only mode — skipping reconciliation, balances, and order execution until exchange credentials are fixed"
            )
            bot._auth_degraded_logged = True
        return

    # Auth recovered — allow the warning to fire again on next degradation.
    if getattr(bot, "_auth_degraded_logged", False):
        bot._auth_degraded_logged = False

    if bot.api_client.is_circuit_open():
        logger.warning(
            "Circuit breaker is OPEN — skipping iteration. State: %s",
            bot.api_client.circuit_breaker.state,
        )
        return

    if not bot.api_client.check_clock_sync():
        logger.warning(
            "Clock offset %+.1fs > limit — skipping iteration",
            bot.api_client._clock.offset,
        )
        return

    bot._reconcile_tracked_positions_with_balance_state()

    trading_cfg = (getattr(bot, "config", {}) or {}).get("trading", {}) or {}
    if bool(trading_cfg.get("runtime_order_reconcile", True)):
        loop_count = int(getattr(bot, "_loop_count", 0) or 0)
        interval_seconds = max(1, int(getattr(bot, "interval_seconds", 60) or 60))
        reconcile_every_loops = max(1, int(300 / interval_seconds))
        if loop_count > 0 and loop_count % reconcile_every_loops == 0:
            try:
                bot._reconcile_open_orders_with_exchange(source="runtime")
            except Exception as exc:
                logger.warning("[OrderReconcile] runtime reconcile failed: %s", exc, exc_info=True)

    paused, reason = bot._is_paused()
    if paused:
        logger.warning("Trading PAUSED: %s", reason)
        bot._check_positions_for_sl_tp()
        _refresh_paused_signal_flow(bot, reason)
        return

    if bot._trading_disabled.is_set():
        logger.warning("Trading disabled via kill switch — skipping new trades")
        bot._check_positions_for_sl_tp()
        return

    if bot._state_machine_enabled:
        bot._state_manager.sync_in_position_states(bot.executor.get_open_orders())

    bot._check_positions_for_sl_tp()

    bot._advance_managed_trade_states()

    active_pairs = _resolve_active_pairs_for_iteration(bot, allow_refresh=True)
    logger.debug("Actual pairs to process: %s", active_pairs)

    for current_pair in active_pairs:
        bot._process_pair_iteration(current_pair)
