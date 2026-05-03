"""Balance monitor callback: portfolio invalidation, reconcile, deposit / quote-cash handling."""

from __future__ import annotations

import logging
from typing import Any, Dict

from balance_monitor import BalanceEvent
from trading.coercion import coerce_trade_float

logger = logging.getLogger(__name__)


def handle_balance_event(bot: Any, event: BalanceEvent, balance_state: Dict[str, Any]) -> None:
    """React to deposit and withdrawal events from the balance monitor."""
    bot._invalidate_portfolio_cache()
    bot._reconcile_tracked_positions_with_balance_state(balance_state)

    if event.source == "crypto" and event.event_type == "DEPOSIT":
        asset = str(event.coin or "").upper()
        pair = f"{asset}USDT" if asset else ""
        legacy_pair = f"THB_{asset}" if asset else ""
        pair_candidates = [candidate for candidate in (pair, legacy_pair) if candidate]
        tracked_position = next(
            (found for candidate in pair_candidates if (found := bot._find_tracked_position_by_symbol(candidate))),
            None,
        )
        display_pair = str((tracked_position or {}).get("symbol") or pair or legacy_pair)
        wallet_balance = bot._extract_total_balance(balance_state, asset)

        _executor = getattr(bot, "executor", None)
        _entry_in_flight = _executor is not None and any(
            _executor.is_entry_in_flight(candidate) for candidate in pair_candidates
        )

        if not tracked_position and pair:
            if _entry_in_flight:
                logger.debug(
                    "[BalanceMonitor] Skipping bootstrap for %s — bot BUY entry in-flight", asset
                )
            else:
                try:
                    active_pairs = {str(item).upper() for item in (bot._get_trading_pairs() or [])}
                except Exception:
                    active_pairs = set()
                bootstrap_pair = next((candidate for candidate in pair_candidates if candidate in active_pairs), pair)
                if bootstrap_pair in active_pairs and wallet_balance > 0:
                    try:
                        bot._bootstrap_missing_positions_from_balance_state(
                            balance_state, target_pairs=[bootstrap_pair]
                        )
                    except Exception as exc:
                        logger.error(
                            "Failed to bootstrap deposited position %s: %s", bootstrap_pair, exc, exc_info=True
                        )
                    tracked_position = next(
                        (found for candidate in pair_candidates if (found := bot._find_tracked_position_by_symbol(candidate))),
                        None,
                    )
                    display_pair = str((tracked_position or {}).get("symbol") or bootstrap_pair)

        if _entry_in_flight or (tracked_position and tracked_position.get("bot_executed")):
            logger.debug(
                "[BalanceMonitor] Skipping deposit alert for %s — position is bot-executed", asset
            )
        else:
            if tracked_position:
                entry_price = coerce_trade_float(tracked_position.get("entry_price"), 0.0)
                if str(tracked_position.get("bootstrap_source") or "").strip():
                    message = (
                        f"External crypto deposit detected: {asset} +{event.amount:.8f} "
                        f"(wallet {wallet_balance:.8f}). {display_pair} was registered into Position Book at "
                        f"entry {entry_price:,.2f} via bootstrap tracking."
                    )
                else:
                    message = (
                        f"External crypto deposit detected: {asset} +{event.amount:.8f} "
                        f"(wallet {wallet_balance:.8f}). Tracked {display_pair} entry remains {entry_price:,.2f}; "
                        f"bot will not average-in this deposit automatically."
                    )
            else:
                message = (
                    f"External crypto deposit detected: {asset} +{event.amount:.8f} "
                    f"(wallet {wallet_balance:.8f}). No tracked {display_pair} position exists, so the deposit "
                    f"was not auto-converted into a managed bot position."
                )

            logger.warning(message)
            if bot.send_alerts:
                bot._send_alert(message, to_telegram=True)

    quote_asset = str(getattr(getattr(bot, "_balance_monitor", None), "quote_asset", "USDT") or "USDT").upper()
    if str(event.coin or "").upper() == quote_asset:
        if event.event_type == "DEPOSIT":
            bot._clear_pause_reason("balance-monitor-quote-withdrawal")
            logger.info(
                "%s deposit detected | amount=%.2f | balance=%.2f", quote_asset, event.amount, event.balance
            )
        elif event.event_type.startswith("WITHDRAWAL"):
            bot._set_pause_reason(
                "balance-monitor-quote-withdrawal",
                f"{quote_asset} withdrawal detected ({event.amount:,.2f} {quote_asset})",
            )
