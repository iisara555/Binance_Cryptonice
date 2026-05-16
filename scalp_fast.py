#!/usr/bin/env python3
"""Fast Scalp+ optimization — precompute indicators then sweep params"""
import pandas as pd
import numpy as np
from itertools import product
import time

# ── TA helpers ──────────────────────────────────────────────────────────────
def ema(s, n):
    return s.ewm(alpha=1/n, adjust=False).mean()

def atr_ta(h, l, c, n=14):
    hl = h - l; hc = (h - c.shift()).abs(); lc = (l - c.shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def adx_ta(h, l, c, n=14):
    pl = c.diff()
    nh = (h - h.shift()).clip(lower=0)
    nl = (l.shift() - l).clip(lower=0)
    dmp = pl.where((pl > nh) & (pl > nl), other=0.0)
    dmn = (-pl).where((-pl > nh) & (-pl > nl), other=0.0)
    inner = dmp.ewm(alpha=1/n, adjust=False).mean() / (dmn.ewm(alpha=1/n, adjust=False).mean() + 1e-9)
    return (100 - 100 / (1 + inner)).ewm(alpha=1/n, adjust=False).mean()

def vol_ma(v, p=20, t=1.1):
    return (v / v.rolling(p).mean() > t).astype(int)

def rsi_ta(c, p=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    dn = (-d).clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    return (100 - 100 / (1 + up / (dn + 1e-9))).fillna(50)

def macd_ta(c, f=12, s=26, sig=9):
    ml = ema(c, f) - ema(c, s)
    return ml, ema(ml, sig), ml - ema(ml, sig)

def stoch_ta(h, l, c, p=14):
    lo = l.rolling(p).min(); hi = h.rolling(p).max()
    k = 100 * (c - lo) / (hi - lo + 1e-9)
    return k, k.ewm(alpha=1/3, adjust=False).mean()

def hull_ma(s, p=16):
    h = p // 2; sp = int(np.sqrt(p))
    if len(s) < p:
        return pd.Series(0, index=s.index)
    def wmean(x):
        w = np.arange(1, len(x) + 1)
        return np.dot(x, w) / w.sum()
    wh = s.rolling(h).apply(wmean, raw=True)
    wf = s.rolling(p).apply(wmean, raw=True)
    hull = 2 * wh - wf
    if len(hull) < sp:
        return pd.Series(0, index=s.index)
    return hull.rolling(sp).apply(wmean, raw=True)

def simulate(buy, sell, px_arr, cap=10000):
    buy_arr = buy.values if hasattr(buy, 'values') else np.array(buy)
    sell_arr = sell.values if hasattr(sell, 'values') else np.array(sell)
    px_arr = px_arr.values if hasattr(px_arr, 'values') else np.array(px_arr)
    cash, assets = cap, 0.0
    entry_px = 0.0
    trades = wins = losses = 0
    peak = cap; max_dd = 0.0
    for i in range(1, len(px_arr)):
        if buy_arr[i] and assets == 0:
            entry_px = px_arr[i]; assets = cash / entry_px; cash = 0.0; trades += 1
        elif sell_arr[i] and assets > 0:
            pnl = (px_arr[i] - entry_px) / entry_px
            if pnl > 0: wins += 1
            else: losses += 1
            cash = assets * px_arr[i]
            peak = max(peak, cash); max_dd = max(max_dd, (peak - cash) / peak)
            assets = 0.0
    final = cash if assets == 0 else assets * px_arr[-1]
    ret = (final - cap) / cap * 100
    pf = wins / losses if losses > 0 else (wins + 0.001)
    win_rate = wins / trades * 100 if trades > 0 else 0
    return {'ret': ret, 'win': wins, 'loss': losses, 'trades': trades,
            'win_rate': win_rate, 'max_dd': max_dd * 100, 'pf': pf}

# ── Precompute base indicators ───────────────────────────────────────────────
def precompute(path):
    df = pd.read_csv(path)
    h = df['high'].values; l = df['low'].values
    c = pd.Series(df['close'].values); v = df['volume'].values
    hlc3 = (pd.Series(h) + pd.Series(l) + c) / 3
    close = c

    print("  Computing indicators...")
    t0 = time.time()

    # Fixed indicators
    adx = adx_ta(pd.Series(h), pd.Series(l), close, 14)
    vol_20 = vol_ma(pd.Series(v), 20, 1.05)
    vwap = (hlc3 * pd.Series(v)).rolling(20).sum() / pd.Series(v).rolling(20).sum()
    rsi14 = rsi_ta(close, 14)

    # Varied indicators
    ema_fast = {7: ema(close, 7), 9: ema(close, 9)}
    ema_slow = {21: ema(close, 21)}
    macd = {7: macd_ta(close, 7, 26, 9), 12: macd_ta(close, 12, 26, 9)}
    stoch = {14: stoch_ta(pd.Series(h), pd.Series(l), close, 14)}
    hull = {16: hull_ma(close, 16), 20: hull_ma(close, 20)}
    vol_thresh = {0.8: vol_ma(pd.Series(v), 20, 0.8),
                  1.0: vol_ma(pd.Series(v), 20, 1.0),
                  1.3: vol_ma(pd.Series(v), 20, 1.3)}

    print(f"  Indicators done in {time.time()-t0:.1f}s")
    return {
        'h': h, 'l': l, 'c': c.values, 'v': v,
        'hlc3': hlc3.values, 'close': close.values,
        'adx': adx.values, 'vol_20': vol_20.values,
        'vwap': vwap.values, 'rsi14': rsi14.values,
        'ema_fast': {k: v.values for k, v in ema_fast.items()},
        'ema_slow': {k: v.values for k, v in ema_slow.items()},
        'macd': {k: (ml.values, ms.values, mh.values) for k, (ml, ms, mh) in macd.items()},
        'stoch': {k: (sk.values, sd.values) for k, (sk, sd) in stoch.items()},
        'hull': {k: v.values for k, v in hull.items()},
        'vol_thresh': {k: v.values for k, v in vol_thresh.items()},
    }

def scalp_bs_fast(d, params):
    c = d['c']; v = d['v']
    hlc3 = d['hlc3']; close = d['close']
    adx = d['adx']; vol_20 = d['vol_20']; vwap = d['vwap']; rsi14 = d['rsi14']

    ema_f = d['ema_fast'][params['ema_fast']]
    ema_s = d['ema_slow'][21]
    ml, ms, mh = d['macd'][12]
    sk, sd = d['stoch'][14]
    hma = d['hull'][params.get('hull_period', 16)]
    v_ok = d['vol_thresh'][params['volume_threshold']]

    adx_th = params['adx_threshold']
    rsi_min = params['rsi_buy_min']
    rsi_max = params['rsi_buy_max']
    min_cb = params['min_confirmations_buy']
    min_cs = params['min_confirmations_sell']

    n = len(c)
    buy = np.zeros(n, dtype=bool)
    sell = np.zeros(n, dtype=bool)

    ef = ema_f; es = ema_s
    rs = rsi14; ad = adx; skk = sk; sdk = sd
    vwap_arr = vwap; v_ok_arr = v_ok
    hma_arr = hma; ml_arr = ml; ms_arr = ms; mh_arr = mh

    for i in range(20, n):
        # Confirmation counts
        cnt_buy = 0
        if hma_arr[i] > 0: cnt_buy += 1
        if ef[i] > es[i]: cnt_buy += 1
        if rs[i] >= rsi_min and rs[i] <= rsi_max: cnt_buy += 1
        if ad[i] >= adx_th: cnt_buy += 1
        if ml_arr[i] > ms_arr[i] and mh_arr[i] > 0: cnt_buy += 1
        if skk[i] >= 20 and skk[i] <= 80 and skk[i] >= sdk[i]: cnt_buy += 1
        if close[i] >= vwap_arr[i]: cnt_buy += 1
        if v_ok_arr[i] == 1: cnt_buy += 1
        buy[i] = cnt_buy >= min_cb

        cnt_sell = 0
        if hma_arr[i] < 0: cnt_sell += 1
        if ef[i] < es[i]: cnt_sell += 1
        if rs[i] <= 48: cnt_sell += 1
        if ml_arr[i] < ms_arr[i] and mh_arr[i] < 0: cnt_sell += 1
        if skk[i] >= 80 and skk[i] <= sdk[i]: cnt_sell += 1
        if close[i] <= vwap_arr[i]: cnt_sell += 1
        if v_ok_arr[i] == 1: cnt_sell += 1
        sell[i] = cnt_sell >= min_cs

    return buy, sell

# ── Grid ─────────────────────────────────────────────────────────────────────
GRID = {
    'min_confirmations_buy': [3, 4, 5],
    'adx_threshold':         [10.0, 14.0, 18.0],
    'rsi_buy_min':           [30.0, 38.0, 46.0],
    'rsi_buy_max':           [60.0, 70.0, 80.0],
    'ema_fast':              [7, 9],
    'volume_threshold':       [0.8, 1.0, 1.3],
    'min_confirmations_sell': [3, 4],
    'hull_period':            [16, 20],
}

def grid_combos(grid):
    keys = list(grid.keys()); values = list(grid.values())
    return [dict(zip(keys, combo)) for combo in product(*values)]

# ── Main ─────────────────────────────────────────────────────────────────────
DATA = {
    'BTC': '/root/Crypto_Sniper/btc_15m_vibe.csv',
    'ETH': '/root/Crypto_Sniper/multi_pair_data/ETH_USDT_15m.csv',
}

print("=== SCALP+ WIN-RATE OPTIMIZE — 15m BTC & ETH ===\n")
t0 = time.time()

all_results = {}
for pair, path in DATA.items():
    print(f"📊 {pair}")
    d = precompute(path)
    combos = grid_combos(GRID)
    print(f"  Testing {len(combos)} combos...")

    best = {'win_rate': 0, 'trades': 0, 'ret': -999, 'pf': 0}

    for params in combos:
        buy, sell = scalp_bs_fast(d, params)
        r = simulate(pd.Series(buy), pd.Series(sell), pd.Series(d['c']))
        r['params'] = params

        if r['trades'] >= 50:
            # Score = win_rate (primary), with ret as tiebreaker
            score = r['win_rate'] * 1000 + r['ret']
            best_score = best['win_rate'] * 1000 + best['ret']
            if score > best_score:
                best = r

    print(f"\n  🏅 BEST WIN RATE (min 50 tr):")
    print(f"     WR={best['win_rate']:.1f}% | Ret={best['ret']:.2f}% | PF={best['pf']:.2f} | {best['trades']}tr")
    print(f"     {best['params']}\n")
    all_results[pair] = best

print(f"=== DONE in {time.time()-t0:.1f}s ===\n")
for pair, r in all_results.items():
    print(f"{pair}: WR={r['win_rate']:.1f}% Ret={r['ret']:.2f}% PF={r['pf']:.2f} {r['trades']}tr")
    print(f"  {r['params']}")
