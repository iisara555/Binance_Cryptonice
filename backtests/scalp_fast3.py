#!/usr/bin/env python3
"""Fast Scalp+ win-rate optimization for BTC & ETH 15m"""
import pandas as pd
import numpy as np
from itertools import product
import time

# ── Precompute base indicators ────────────────────────────────────────────────
def load(path):
    df = pd.read_csv(path)
    C = df['close'].values; V = df['volume'].values
    H = df['high'].values; L = df['low'].values
    hlc3 = pd.Series((H + L + C) / 3)

    t0 = time.time()
    # EMA
    ema7  = hlc3.ewm(alpha=1/7,  adjust=False).mean().values
    ema9  = hlc3.ewm(alpha=1/9,  adjust=False).mean().values
    ema21 = pd.Series(C).ewm(alpha=1/21, adjust=False).mean().values
    ema12 = pd.Series(C).ewm(alpha=1/12, adjust=False).mean()
    ema26 = pd.Series(C).ewm(alpha=1/26, adjust=False).mean()
    ema_sig9 = (ema12 - ema26).ewm(alpha=1/9, adjust=False).mean()
    macd_h = (ema12 - ema26 - ema_sig9).values

    # RSI
    delta = pd.Series(C).diff()
    up = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi14 = (100 - 100 / (1 + up / (dn + 1e-9))).fillna(50).values

    # ADX
    plus_dm  = (H - np.roll(H, 1)); plus_dm[0] = 0
    minus_dm = (np.roll(L, 1) - L); minus_dm[0] = 0
    tr = np.maximum(H - L, np.maximum(np.abs(H - np.roll(C, 1)), np.abs(L - np.roll(C, 1))))
    tr[0] = H[0] - L[0]
    atr14 = pd.Series(tr).ewm(alpha=1/14, adjust=False).mean().values
    plus_di  = 100 * pd.Series(np.maximum(plus_dm, 0)).ewm(alpha=1/14, adjust=False).mean().values / (atr14 + 1e-9)
    minus_di = 100 * pd.Series(np.maximum(minus_dm, 0)).ewm(alpha=1/14, adjust=False).mean().values / (atr14 + 1e-9)
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-9)
    adx14 = pd.Series(dx).ewm(alpha=1/14, adjust=False).mean().values

    # Stochastic K
    stoch_k = np.full(len(C), np.nan)
    for i in range(14, len(C)):
        lo = L[i-14:i+1].min()
        hi = H[i-14:i+1].max()
        stoch_k[i] = 100 * (C[i] - lo) / (hi - lo + 1e-9)

    # Volume MA
    vol_ma20 = pd.Series(V).rolling(20).mean().values

    # VWAP
    vwap = (pd.Series(hlc3) * pd.Series(V)).rolling(20).sum().values / pd.Series(V).rolling(20).sum().values

    print(f"  TA precompute: {time.time()-t0:.1f}s")
    return {
        'C': C, 'V': V, 'H': H, 'L': L,
        'ema7': ema7, 'ema9': ema9, 'ema21': ema21,
        'macd_hist': macd_h, 'rsi14': rsi14, 'adx14': adx14,
        'stoch_k': stoch_k, 'vol_ma20': vol_ma20, 'vwap': vwap,
    }

def scalp_sim(d, p):
    C = d['C']; V = d['V']
    ema7 = d['ema7']; ema9 = d['ema9']; ema21 = d['ema21']
    macd_h = d['macd_hist']; rsi = d['rsi14']; adx = d['adx14']
    sk = d['stoch_k']; vol_ma20 = d['vol_ma20']; vwap = d['vwap']

    ef = ema7 if p['ema_fast'] == 7 else ema9
    vol_ok = (V / vol_ma20 > p['volume_threshold']) & ~np.isnan(vol_ma20)

    n = len(C)
    buy = np.zeros(n, dtype=bool)
    sell = np.zeros(n, dtype=bool)

    for i in range(20, n):
        cb = 0
        if ef[i] > ema21[i]: cb += 1
        if rsi[i] >= p['rsi_buy_min'] and rsi[i] <= p['rsi_buy_max']: cb += 1
        if adx[i] >= p['adx_threshold']: cb += 1
        if macd_h[i] > 0: cb += 1
        if 20 <= sk[i] <= 80: cb += 1
        if C[i] >= vwap[i]: cb += 1
        if vol_ok[i]: cb += 1
        if ef[i] > ema21[i]: cb += 1  # hull proxy (ema cross)
        buy[i] = cb >= p['min_confirmations_buy']

        cs = 0
        if ef[i] < ema21[i]: cs += 1
        if rsi[i] <= 48: cs += 1
        if macd_h[i] < 0: cs += 1
        if sk[i] >= 80: cs += 1
        if C[i] <= vwap[i]: cs += 1
        if vol_ok[i]: cs += 1
        sell[i] = cs >= p['min_confirmations_sell']

    cash=10000.; assets=0.; entry=0.; tr=wi=lo=0
    peak=10000.; max_dd=0.
    for i in range(1, n):
        if buy[i] and assets==0:
            entry=C[i]; assets=cash/entry; cash=0.; tr+=1
        elif sell[i] and assets>0:
            pnl=(C[i]-entry)/entry
            if pnl>0: wi+=1
            else: lo+=1
            cash=assets*C[i]
            peak=max(peak,cash); max_dd=max(max_dd,(peak-cash)/peak)
            assets=0.
    final=cash if assets==0 else assets*C[-1]
    ret=(final-10000)/10000*100
    pf=wi/lo if lo>0 else wi+0.001
    wr=wi/tr*100 if tr>0 else 0
    return {'ret':ret,'win_rate':wr,'pf':pf,'trades':tr,'win':wi,'loss':lo,'max_dd':max_dd*100}

GRID = {
    'min_confirmations_buy': [3, 4, 5],
    'adx_threshold':         [10.0, 14.0, 18.0],
    'rsi_buy_min':           [30.0, 38.0, 46.0],
    'rsi_buy_max':           [60.0, 70.0, 80.0],
    'ema_fast':              [7, 9],
    'volume_threshold':        [0.8, 1.0, 1.3],
    'min_confirmations_sell': [3, 4],
}

def combos(g):
    keys=list(g.keys()); vals=list(g.values())
    return [dict(zip(keys,c)) for c in product(*vals)]

DATA={'BTC':str(_REPO / 'btc_15m_vibe.csv'),
      'ETH':str(_REPO / 'multi_pair_data/ETH_USDT_15m.csv')}

print("=== SCALP+ WIN-RATE OPTIMIZE ===\n")
t0=time.time()
results={}
for pair,path in DATA.items():
    print(f"  {pair}")
    d=load(path)
    cs=combos(GRID)
    print(f"  {len(cs)} combos...")
    best={'win_rate':0,'ret':-999,'trades':0,'pf':0}
    t1=time.time()
    for p in cs:
        r=scalp_sim(d,p)
        r['params']=p
        if r['trades']>=50:
            if r['win_rate']>best['win_rate']:
                best=r
    print(f"  {pair}: {time.time()-t1:.0f}s")
    print(f"  BEST WR={best['win_rate']:.1f}% Ret={best['ret']:.2f}% PF={best['pf']:.2f} {best['trades']}tr")
    print(f"  {best['params']}\n")
    results[pair]=best

print(f"TOTAL: {time.time()-t0:.0f}s")
for pair,r in results.items():
    print(f"{pair}: WR={r['win_rate']:.1f}% Ret={r['ret']:.2f}% PF={r['pf']:.2f} {r['trades']}tr | {r['params']}")
