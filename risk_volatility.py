"""
Pair volatility classification and default SL/TP nets (shared risk / strategy surface).
Extracted from risk_management to clarify boundaries with signal_generator and protections.
"""

# BTC pairs are considered "low volatility", ALT pairs are "high volatility".
# Project migrated from Bitkub (THB_*) to Binance Thailand (*USDT) — both
# formats are accepted so legacy state files / tests keep working.
VOLATILITY_CLASS = {
    # --- NEW: SPEC_04 — Binance Thailand symbol format -----------------
    "BTCUSDT": "low",
    "ETHUSDT": "high",
    "SOLUSDT": "high",
    "XRPUSDT": "high",
    "BNBUSDT": "high",
    "ADAUSDT": "high",
    "DOTUSDT": "high",
    "LINKUSDT": "high",
    "DOGEUSDT": "high",
    # --- Legacy (Bitkub) — kept for backward compatibility -------------
    "THB_BTC": "low",
    "BTC_THB": "low",
    "THB_ETH": "high",
    "THB_SOL": "high",
    "THB_XRP": "high",
    "THB_BNB": "high",
    "THB_ADA": "high",
    "THB_DOT": "high",
    "THB_LINK": "high",
    "THB_DOGE": "high",
}

# Default SL/TP percentages by volatility class — NET percentages (after fees)
DEFAULT_SL_TP = {
    "low": {"stop_loss_pct": -2.0, "take_profit_pct": 4.0},
    "high": {"stop_loss_pct": -4.0, "take_profit_pct": 7.0},
}
