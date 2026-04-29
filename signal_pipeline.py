"""
Explicit strategy pipeline steps: collect raw ``TradingSignal`` list then aggregate.
Kept separate from ``signal_generator`` to avoid circular imports and clarify stages.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Tuple

import pandas as pd

logger = logging.getLogger("crypto-bot.signal")


def collect_raw_trading_signals(
    *,
    strategies: Dict[str, Any],
    strategy_names: List[str],
    data: pd.DataFrame,
    symbol: str,
    diag_fn: Callable[..., None],
    emit_sniper_diagnostics: Callable[[str, Any], None],
) -> Tuple[List[Any], Dict[str, str]]:
    """
    Run each strategy's ``generate_signal`` + ``validate_signal``; return raw signals and reject map.
    """
    all_signals: List[Any] = []
    strategy_reject_reasons: Dict[str, str] = {}

    for name in strategy_names:
        if name not in strategies:
            continue

        strategy = strategies[name]

        try:
            signal = strategy.generate_signal(data, symbol)
            if str(name).lower() == "sniper":
                emit_sniper_diagnostics(symbol, strategy)
            if signal:
                if strategy.validate_signal(signal, data):
                    all_signals.append(signal)
                    diag_fn(
                        symbol,
                        f"Strategy:{name}",
                        "PASS",
                        f"type={signal.signal_type.value}, conf={signal.confidence:.3f}, "
                        f"price={signal.price:.2f}, RR={signal.risk_reward_ratio or 'N/A'}",
                    )
                else:
                    strategy_reason = ""
                    if hasattr(strategy, "get_last_reject_reason"):
                        strategy_reason = str(getattr(strategy, "get_last_reject_reason")() or "").strip()
                    if strategy_reason:
                        strategy_reject_reasons[str(name)] = strategy_reason
                    diag_fn(
                        symbol,
                        f"Strategy:{name}",
                        "REJECT",
                        f"validate_signal() returned False (type={signal.signal_type.value}, "
                        f"conf={signal.confidence:.3f})"
                        + (f", reason_code={strategy_reason}" if strategy_reason else ""),
                    )
            else:
                strategy_reason = ""
                if hasattr(strategy, "get_last_reject_reason"):
                    strategy_reason = str(getattr(strategy, "get_last_reject_reason")() or "").strip()
                if strategy_reason:
                    strategy_reject_reasons[str(name)] = strategy_reason
                diag_fn(
                    symbol,
                    f"Strategy:{name}",
                    "REJECT",
                    "generate_signal() returned None (no setup detected)"
                    + (f", reason_code={strategy_reason}" if strategy_reason else ""),
                )
        except Exception as e:
            logger.warning(
                "Signal strategy error | pair=%s strategy=%s %s: %s",
                symbol,
                name,
                type(e).__name__,
                e,
            )
            diag_fn(symbol, f"Strategy:{name}", "REJECT", f"{type(e).__name__}: {e}")

    if strategy_reject_reasons:
        # grep StrategyRejectSnapshot; enable DEBUG log level on `crypto-bot.signal` to correlate per-strategy reject codes (SR_GUARD_BLOCKED, INSUFFICIENT_CONFIRMATIONS, ...) without noisy INFO loops.
        logger.debug(
            "[StrategyRejectSnapshot] pair=%s %s",
            symbol,
            " ".join(f"{nm}={rs}" for nm, rs in sorted(strategy_reject_reasons.items())),
        )

    return all_signals, strategy_reject_reasons
