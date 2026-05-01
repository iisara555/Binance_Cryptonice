"""Generate Telegram Alert Samples"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

from alerts import format_trade_alert, format_error_alert, format_status_alert

print("="*60)
print("TELEGRAM ALERT EXAMPLES")
print("="*60)

print("\n" + "="*60)
print("1. BUY ORDER FILLED")
print("="*60)
print(format_trade_alert("THB_BTC", "BUY", 1522500.0, 0.06666667, 101500.0, None, None, "filled"))

print("\n" + "="*60)
print("2. SELL - TAKE PROFIT (Profit)")
print("="*60)
print(format_trade_alert("THB_ETH", "SELL", 55000.0, 0.90909091, 50000.0, 2600.0, 5.2, "filled", "Take Profit"))

print("\n" + "="*60)
print("3. SELL - STOP LOSS (Loss)")
print("="*60)
print(format_trade_alert("THB_DOGE", "SELL", 2.45, 20000.0, 49000.0, -400.0, -0.8, "filled", "Stop Loss Triggered"))

print("\n" + "="*60)
print("4. ERROR / CRITICAL ALERT")
print("="*60)
print(format_error_alert("API Connection Failed", "Timeout connecting to Bitkub API after 10 seconds", "error"))

print("\n" + "="*60)
print("5. PORTFOLIO SUMMARY")
print("="*60)
print(format_status_alert(50000.0, 75000.0, 2500.0, 3.45, "5d 12h", ["BTC", "ETH", "SOL"]))
