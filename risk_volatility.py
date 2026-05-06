"""
Pair volatility classification and default SL/TP nets (shared risk / strategy surface).
Extracted from risk_management to clarify boundaries with signal_generator and protections.
"""

# Binance Thailand (*USDT) symbols.
VOLATILITY_CLASS = {
    "BTCUSDT": "low",
    "ETHUSDT": "high",
    "SOLUSDT": "high",
    "XRPUSDT": "high",
    "BNBUSDT": "high",
    "ADAUSDT": "high",
    "DOTUSDT": "high",
    "LINKUSDT": "high",
    "DOGEUSDT": "high",
}

# Default SL/TP percentages by volatility class — NET percentages (after fees)
DEFAULT_SL_TP = {
    "low": {"stop_loss_pct": -2.0, "take_profit_pct": 4.0},
    "high": {"stop_loss_pct": -4.0, "take_profit_pct": 7.0},
}
