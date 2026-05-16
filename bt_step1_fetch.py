#!/usr/bin/env python3
"""Step 1: Fetch BTC/USDT 15m data and save to CSV."""
import ccxt, pandas as pd, sys, os
os.chdir('/root/Crypto_Sniper')
print("[1/2] Fetching 15m BTC/USDT from Binance...")
ex = ccxt.binance({'enableRateLimit': True})
ohlcv = ex.fetch_ohlcv('BTC/USDT', '15m', limit=600)
df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
df.set_index('timestamp', inplace=True)
df.to_csv('/root/Crypto_Sniper/backtest_data_15m.csv')
print(f"  Saved {len(df)} candles to backtest_data_15m.csv")
print(f"  Range: {df.index[0]} → {df.index[-1]}")