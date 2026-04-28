"""One trading loop iteration: gates, reconciliation, SL/TP, per-pair signal processing."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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

    paused, reason = bot._is_paused()
    if paused:
        logger.warning("Trading PAUSED: %s", reason)
        bot._check_positions_for_sl_tp()
        return

    if bot._trading_disabled.is_set():
        logger.warning("Trading disabled via kill switch — skipping new trades")
        bot._check_positions_for_sl_tp()
        return

    if bot._state_machine_enabled:
        bot._state_manager.sync_in_position_states(bot.executor.get_open_orders())

    bot._check_positions_for_sl_tp()

    bot._advance_managed_trade_states()

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

    active_pairs = bot._filter_pairs_by_candle_readiness(active_pairs, allow_refresh=True)

    logger.debug("Actual pairs to process: %s", active_pairs)

    for current_pair in active_pairs:
        bot._process_pair_iteration(current_pair)
