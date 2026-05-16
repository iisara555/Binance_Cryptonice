#!/usr/bin/env python3
"""Fetch 6-month 15m data for SOL, ADA, DOGE via ccxt — forward pagination"""
import ccxt
import pandas as pd
import time, os

exchange = ccxt.binance()
symbol_map = {
    'SOL/USDT': 'SOL_USDT_15m.csv',
    'ADA/USDT': 'ADA_USDT_15m.csv',
    'DOGE/USDT': 'DOGE_USDT_15m.csv',
}
data_dir = '/root/Crypto_Sniper/multi_pair_data'
os.makedirs(data_dir, exist_ok=True)

since_ts = int(pd.Timestamp('2025-11-01').timestamp() * 1000)
limit = 1500  # Binance max

for sym, fname in symbol_map.items():
    out_path = os.path.join(data_dir, fname)
    all_ohlcv = []
    cursor = since_ts
    fetched = 0
    print(f"\nFetching {sym} 15m...")
    
    while True:
        ohlcv = exchange.fetch_ohlcv(sym, '15m', cursor, limit)
        if not ohlcv:
            break
        all_ohlcv.extend(ohlcv)
        cursor = ohlcv[-1][0] + 1  # next candle after last
        fetched += len(ohlcv)
        oldest = pd.Timestamp(ohlcv[0][0], unit='ms').strftime('%Y-%m-%d')
        newest = pd.Timestamp(ohlcv[-1][0], unit='ms').strftime('%Y-%m-%d')
        print(f"  → {fetched} candles ({oldest} → {newest})")
        if len(ohlcv) < limit:
            break
        time.sleep(0.25)
    
    df = pd.DataFrame(all_ohlcv, columns=['timestamp','open','high','low','close','volume'])
    df['trade_date'] = pd.to_datetime(df['timestamp'], unit='ms').dt.strftime('%Y-%m-%dT%H:%M:%S')
    df = df[['trade_date','open','high','low','close','volume']]
    df = df.drop_duplicates('trade_date').sort_values('trade_date').reset_index(drop=True)
    df.to_csv(out_path, index=False)
    print(f"  ✅ Saved {len(df)} candles → {out_path}")

print("\n=== ALL DONE ===")
