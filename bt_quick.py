#!/usr/bin/env python3
"""Quick backtest on SOL/ADA/DOGE (1000 candles ~10 days) to compare Machete vs Scalp+"""
import pandas as pd
import numpy as np
import sys, os
sys.path.insert(0, '/root/Crypto_Sniper')

from strategies.machete_v8b_lite import MacheteV8bLite
from strategies.simple_scalp_plus import SimpleScalpPlus

PAIRS = ['SOL_USDT', 'ADA_USDT', 'DOGE_USDT']
DATA_DIR = '/root/Crypto_Sniper/multi_pair_data'

def load_csv(pair):
    df = pd.read_csv(f'{DATA_DIR}/{pair}_15m.csv')
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    return df

def run_machete(df, params):
    strat = MacheteV8bLite()
    # default + param overrides
    cfg = {
        'timeframe': '15m', 'primary_timeframe': '15m',
        'adx_threshold': params.get('adx_threshold', 20.0),
        'min_confirmations_buy': int(params.get('min_confirmations_buy', 3)),
        'rmi_buy_min': params.get('rmi_buy_min', 50.0),
        'sr_proximity_pct': params.get('sr_proximity_pct', 0.5),
        'vol_threshold': params.get('vol_threshold', 0.7),
    }
    sigs = strat.generate_signal(df, cfg)
    return sigs

def run_scalp(df, params):
    strat = SimpleScalpPlus()
    cfg = {
        'timeframe': '15m', 'primary_timeframe': '15m',
        'min_confirmations_buy': int(params.get('min_confirmations_buy', 3)),
        'adx_threshold': params.get('adx_threshold', 20.0),
        'rsi_buy_min': params.get('rsi_buy_min', 50.0),
        'ema_fast': int(params.get('ema_fast', 9)),
    }
    sigs = strat.generate_signal(df, cfg)
    return sigs

def simulate(sigs, df, pair_name):
    close = df['close'].values
    cash, assets, entry_val = 10000.0, 0, 0.0
    entry_px = 0.0
    trades = 0; wins = 0; loss = 0
    max_dd = 0.0; peak = 10000.0
    longs = sigs['long'].values
    
    for i in range(1, len(longs)):
        if longs[i] == 1 and assets == 0:
            entry_px = close[i]
            assets = cash / entry_px
            entry_val = cash
            cash = 0.0
            trades += 1
        elif longs[i] == -1 and assets > 0:
            exit_px = close[i]
            pnl = (exit_px - entry_px) / entry_px
            if pnl > 0: wins += 1
            else: loss += 1
            val = assets * exit_px
            max_dd = max(max_dd, max(0, (peak - val) / peak))
            peak = max(peak, val)
            cash = val; assets = 0.0
    
    final_val = cash if assets == 0 else assets * close[-1]
    ret = (final_val - 10000) / 10000 * 100
    win_rate = wins / trades * 100 if trades > 0 else 0
    
    print(f"  {pair_name}: {trades} trades | Win={wins}/{loss} ({win_rate:.1f}%) | Ret={ret:.2f}% | MaxDD={max_dd*100:.1f}%")
    return {'pair': pair_name, 'trades': trades, 'win': wins, 'loss': loss, 
            'win_rate': win_rate, 'ret': ret, 'max_dd': max_dd}

print("=== QUICK BACKTEST — 10-DAY DATA ===\n")
results = []
for pair in PAIRS:
    df = load_csv(pair)
    print(f"📊 {pair} | {len(df)} candles {df['trade_date'].iloc[0].date()} → {df['trade_date'].iloc[-1].date()}")
    
    # Machete defaults
    m_sigs = run_machete(df, {})
    m_result = simulate(m_sigs, df, 'MACHETE')
    
    # Scalp+ defaults
    s_sigs = run_scalp(df, {})
    s_result = simulate(s_sigs, df, 'SCALP+')
    
    winner = 'MACHETE' if m_result['ret'] > s_result['ret'] else 'SCALP+'
    print(f"  🏅 Winner: {winner}\n")
    results.append({'pair': pair, 'winner': winner, 'm': m_result, 's': s_result})

print("\n=== SUMMARY ===")
for r in results:
    print(f"{r['pair']}: 🏅 {r['winner']}")
