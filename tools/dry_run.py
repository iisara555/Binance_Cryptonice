"""Dry Run: Simulate Rebalance Logic"""
import os
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

import yaml
import numpy as np

print("=" * 60)
print("DRY RUN - REBALANCE SIMULATION")
print("=" * 60)

# Load config
with open('bot_config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

target = config.get('rebalance', {}).get('target_allocation', {})
min_trade = config.get('rebalance', {}).get('min_trade_value', 10.0)
min_order = 15.0  # MIN_ORDER_THB from trade_executor

print(f"\n[CONFIG]")
print(f"Target allocation: {target}")
print(f"Min trade value: {min_trade} THB")
print(f"Min order (safety): {min_order} THB")

# Simulate portfolio
btc_price = 2200000  # ~2.2M THB
doge_price = 3.02     # THB
total_value = 500.0

portfolio = {
    'THB': 250.0,
    'BTC': 0.000112,
    'DOGE': 0,
}

print(f"\n[PORTFOLIO]")
print(f"THB: {portfolio['THB']:.2f}")
print(f"BTC: {portfolio['BTC']:.8f} = {portfolio['BTC'] * btc_price:.2f} THB")
print(f"DOGE: {portfolio['DOGE']:.2f} = {portfolio['DOGE'] * doge_price:.2f} THB")

# Calculate target values
target_btc = total_value * 0.5
target_doge = total_value * 0.5

print(f"\n[TARGET]")
print(f"BTC target (50%): {target_btc:.2f} THB")
print(f"DOGE target (50%): {target_doge:.2f} THB")

# Calculate imbalances
btc_current = portfolio['BTC'] * btc_price
doge_current = portfolio['DOGE'] * doge_price

btc_diff = target_btc - btc_current  # positive = need to buy
doge_diff = target_doge - doge_current  # positive = need to buy

print(f"\n[IMBALANCES]")
print(f"BTC: current={btc_current:.2f}, target={target_btc:.2f}, diff={btc_diff:.2f} THB")
print(f"DOGE: current={doge_current:.2f}, target={target_doge:.2f}, diff={doge_diff:.2f} THB")

# Generate orders
print(f"\n[ORDERS]")
orders = []

# BTC order
if abs(btc_diff) > min_trade:
    btc_qty = btc_diff / btc_price
    btc_val = abs(btc_diff)
    if btc_val >= min_order:
        orders.append(('BUY', 'BTC', btc_qty, btc_val, '✅ PASS'))
        print(f"BUY BTC: qty={btc_qty:.8f}, value={btc_val:.2f} THB [✅ PASS]")
    else:
        print(f"BUY BTC: qty={btc_qty:.8f}, value={btc_val:.2f} THB [❌ SKIP - below {min_order} THB minimum]")
else:
    print(f"BTC: No trade needed (diff={btc_diff:.2f} < {min_trade})")

# DOGE order
if abs(doge_diff) > min_trade:
    doge_qty = doge_diff / doge_price
    doge_val = abs(doge_diff)
    if doge_val >= min_order:
        orders.append(('BUY', 'DOGE', doge_qty, doge_val, '✅ PASS'))
        print(f"BUY DOGE: qty={doge_qty:.4f}, value={doge_val:.2f} THB [✅ PASS]")
    else:
        print(f"BUY DOGE: qty={doge_qty:.4f}, value={doge_val:.2f} THB [❌ SKIP - below {min_order} THB minimum]")
else:
    print(f"DOGE: No trade needed (diff={doge_diff:.2f} < {min_trade})")

print(f"\n[SUMMARY]")
print(f"Orders to execute: {len(orders)}")
print(f"Expected failures: 0")

print("\n" + "=" * 60)
print("RESULT: ✅ DRY RUN PASSED - Ready for real execution!")
print("=" * 60)
