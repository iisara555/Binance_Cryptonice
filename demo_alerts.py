"""Demo: How alerts will look after quote_asset fix"""
from alerts import format_trade_alert, format_status_alert

print("=" * 60)
print("ALERT PREVIEW - After quote_asset Fix")
print("=" * 60)

# THB Mode - Binance TH
trade_thb = format_trade_alert(
    symbol="THB_BTC",
    side="BUY",
    price=5_000_000.0,
    amount=0.01,
    value_quote=50_000.0,
    pnl_amt=2500.0,
    pnl_pct=5.0,
    status="filled",
    quote_asset="THB",
)
# USDT Mode - Binance Global
trade_usdt = format_trade_alert(
    symbol="BTCUSDT",
    side="BUY",
    price=50_000.0,
    amount=0.01,
    value_quote=500.0,
    pnl_amt=25.0,
    pnl_pct=5.0,
    status="filled",
    quote_asset="USDT",
)

print("\n[THB Mode - Binance TH]:")
print("-" * 40)
# Replace emojis for terminal display
print(trade_thb.replace("\u2705", "[OK]").replace("\u2691", "[箭]"))

print("\n[USDT Mode - Binance Global]:")
print("-" * 40)
print(trade_usdt.replace("\u2705", "[OK]").replace("\u2691", "[箭]"))

print("\n" + "=" * 60)
print("KEY CHANGES:")
print("- Trade alerts now show correct currency")
print("- THB for Binance TH, USDT for Binance Global")
print("- Backward compatible with existing code")
print("=" * 60)
