"""
Pure helpers for CLI snapshot DTO fields (no Rich). Built from ``main.TradingBotApp.get_cli_snapshot``.
"""

from typing import Any, Callable, Dict, List


def build_balance_breakdown_lines(
    *,
    quote_asset: str,
    breakdown: List[Dict[str, Any]],
    total_balance_quote: float,
    usdt_thb_suffix: Callable[[float], str],
) -> List[str]:
    """One human-readable line per balance row; USDT rows may append THB suffix via ``usdt_thb_suffix``."""
    quote_upper = str(quote_asset).upper()
    lines: List[str] = []

    for item in breakdown:
        asset = str(item.get("asset") or "").upper()
        amount = float(item.get("amount", 0.0) or 0.0)
        value_quote = float(item.get("value_thb", 0.0) or 0.0)
        if asset and value_quote > 0:
            allocation_pct = (
                (value_quote / total_balance_quote * 100.0) if total_balance_quote > 0 else 0.0
            )
            if asset in {quote_upper, "THB"}:
                amount_text = f"{amount:,.2f}"
            else:
                amount_text = f"{amount:,.8f}"
            line = f"{asset} {amount_text} = {value_quote:,.2f} {quote_asset} ({allocation_pct:.2f}%)"
            if asset == "USDT":
                line += usdt_thb_suffix(amount)
            lines.append(line)
    return lines


def quote_cash_totals_strings(
    quote_asset: str,
    cash_avail_quote: float,
    total_balance_quote: float,
    usdt_thb_suffix: Callable[[float], str],
) -> tuple[str, str]:
    """``(available_balance_str, total_balance_str)`` with optional USDT→THB suffix when quote is USDT."""
    available_balance_str = f"{cash_avail_quote:,.2f} {quote_asset}"
    total_balance_str = f"{total_balance_quote:,.2f} {quote_asset}"
    if str(quote_asset).upper() == "USDT":
        available_balance_str += usdt_thb_suffix(cash_avail_quote)
        total_balance_str += usdt_thb_suffix(total_balance_quote)
    return available_balance_str, total_balance_str
