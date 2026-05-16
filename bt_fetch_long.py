#!/usr/bin/env python3
"""Fetch 6-month 15m data for SOL, ADA, DOGE via ccxt — batch forward from Nov 2025"""
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
limit = 1000  # Binance max per request

for sym, fname in symbol_map.items():
    out_path = os.path.join(data_dir, fname)
    all_ohlcv = []
    cursor = since_ts
    fetched = 0
    first_ts = None
    print(f"\nFetching {sym} 15m...")
    
    while True:
        ohlcv = exchange.fetch_ohlcv(sym, '15m', cursor, limit)
        if not ohlcv:
            break
        
        if first_ts is None:
            first_ts = ohlcv[0][0]
        
        all_ohlcv.extend(ohlcv)
        last_ts = ohlcv[-1][0]
        cursor = last_ts + 1  # next candle after last
        fetched += len(ohlcv)
        
        oldest = pd.Timestamp(ohlcv[0][0], unit='ms').strftime('%Y-%m-%d')
        newest = pd.Timestamp(ohlcv[-1][0], unit='ms').strftime('%Y-%m-%d')
        print(f"  batch {fetched//limit} → {fetched} candles ({oldest} → {newest})")
        
        if len(ohlcv) < limit:
            break
        if newest >= '2026-05-13':
            break
        
        time.sleep(0.3)  # rate limit
    
    df = pd.DataFrame(all_ohlcv, columns=['timestamp','open','high','low','close','volume'])
    df['trade_date'] = pd.to_datetime(df['timestamp'], unit='ms').dt.strftime('%Y-%m-%dT%H:%M:%S')
    df = df[['trade_date','open','high','low','close','volume']]
    df = df.drop_duplicates('trade_date').sort_values('trade_date').reset_index(drop=True)
    df.to_csv(out_path, index=False)
    print(f"  ✅ Saved {len(df)} candles → {out_path}")

print("\n=== ALL DONE ===")
