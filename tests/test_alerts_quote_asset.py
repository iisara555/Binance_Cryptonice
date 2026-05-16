"""Quick test for quote_asset parameter changes in alerts.py"""
from alerts import format_trade_alert, format_status_alert

# Test 1: format_trade_alert with THB
result_thb = format_trade_alert('BTC', 'BUY', 5000000, 0.01, 50000, quote_asset='THB')
assert 'THB' in result_thb, "Should contain THB"
assert '5,000,000.00' in result_thb, "Should show formatted price"
print("PASS: format_trade_alert with THB")

# Test 2: format_trade_alert with USDT
result_usdt = format_trade_alert('BTC', 'BUY', 50000, 0.01, 500, quote_asset='USDT')
assert 'USDT' in result_usdt, "Should contain USDT"
assert '50,000.00' in result_usdt, "Should show formatted price"
print("PASS: format_trade_alert with USDT")

# Test 3: format_status_alert with THB
status_thb = format_status_alert(50000, 100000, 5000, 5.0, quote_asset='THB')
assert 'THB' in status_thb, "Should contain THB"
print("PASS: format_status_alert with THB")

# Test 4: format_status_alert with USDT
status_usdt = format_status_alert(500, 1000, 50, 5.0, quote_asset='USDT')
assert 'USDT' in status_usdt, "Should contain USDT"
print("PASS: format_status_alert with USDT")

print("\nAll tests passed!")
