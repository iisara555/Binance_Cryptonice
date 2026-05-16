#!/usr/bin/env python3
"""Fetch BTC/USDT 15m via ccxt (Binance) and save to CSV for backtest."""
import ccxt, pandas as pd, time

# Fetch from Binance (free, no API key needed for public endpoints)
exchange = ccxt.binance()
symbol = 'BTC/USDT'
timeframe = '15m'

# Nov 2025 → May 2026
since = int(pd.Timestamp('2025-11-01').timestamp() * 1000)
now = int(pd.Timestamp('2026-05-13').timestamp() * 1000)

print(f"Fetching {symbol} {timeframe} from {pd.Timestamp('2025-11-01')} → {pd.Timestamp('2026-05-13')}...")
print(f"Since timestamp: {since} → Now: {now}")

all_candles = []
# Fetch in batches (max 1000 candles per request for Binance)
current_since = since
batch = 0
while current_since < now:
    batch += 1
    print(f"  Batch {batch}: fetching from {pd.Timestamp(current_since/1000, unit='ms')}...", end=' ')
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=current_since, limit=1000)
        if not ohlcv:
            print("no data, breaking")
            break
        all_candles.extend(ohlcv)
        current_since = ohlcv[-1][0] + 1
        print(f"got {len(ohlcv)} candles, total {len(all_candles)}")
        if len(ohlcv) < 1000:
            break
        time.sleep(exchange.rateLimit / 1000)  # be nice to the API
    except Exception as e:
        print(f"ERROR: {e}")
        break

print(f"\nTotal: {len(all_candles)} candles fetched")

# Convert to DataFrame
df = pd.DataFrame(all_candles, columns=['timestamp','open','high','low','close','volume'])
df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
df = df.set_index('timestamp').sort_index()

csv_path = '/root/Crypto_Sniper/btc_15m_vibe.csv'
df.to_csv(csv_path)
print(f"Saved to {csv_path}")
print(f"Range: {df.index[0]} → {df.index[-1]}")
print(f"Shape: {df.shape}")