#!/usr/bin/env python3
"""Ultra-fast Scalp+ optimizer — pandas for TA precompute, numpy for param sweep"""
import pandas as pd
import numpy as np
from itertools import product
import time

# ── TA precompute via pandas (fast C-accelerated rolling) ────────────────────
def precompute(path):
    df = pd.read_csv(path)
    n = len(df)
    print(f"  {n} candles, computing indicators...")

    H = df['high'].astype(float)
    L = df['low'].astype(float)
    C = df['close'].astype(float)
    V = df['volume'].astype(float)
    HLC3 = (H + L + C) / 3

    # EMA
    ema7  = HLC3.ewm(alpha=1/7,  adjust=False).mean()
    ema9  = HLC3.ewm(alpha=1/9,  adjust=False).mean()
    ema12 = C.ewm(alpha=1/12, adjust=False).mean()
    ema21 = C.ewm(alpha=1/21, adjust=False).mean()
    ema26 = C.ewm(alpha=1/26, adjust=False).mean()
    ema_sig9 = (ema12 - ema26).ewm(alpha=1/9, adjust=False).mean()
    macd_hist = (ema12 - ema26) - ema_sig9

    # RSI
    delta = C.diff()
    up = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi14 = 100 - 100 / (1 + up / (dn + 1e-9))

    # ADX
    plus_dm  = (H - H.shift(1)).clip(lower=0)
    minus_dm = (L.shift(1) - L).clip(lower=0)
    tr = pd.concat([H - L, (H - C.shift(1)).abs(), (L - C.shift(1)).abs()], axis=1).max(axis=1)
    atr14   = tr.ewm(alpha=1/14, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(alpha=1/14, adjust=False).mean() / (atr14 + 1e-9)
    minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False).mean() / (atr14 + 1e-9)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9)
    adx14 = dx.ewm(alpha=1/14, adjust=False).mean()

    # Stochastic K
    stoch_k = 100 * (C - L.rolling(14).min()) / (H.rolling(14).max() - L.rolling(14).min() + 1e-9)

    # Volume MA
    vol_ma20 = V.rolling(20).mean()

    # VWAP
    vwap = (HLC3 * V).rolling(20).sum() / V.rolling(20).sum()

    # Hull MA — use pre-computed WMA helper
    def hull(s, p=16):
        h = p // 2; sp = int(np.sqrt(p))
        w_h = np.arange(1, h+1); w_p = np.arange(1, p+1); w_sp = np.arange(1, sp+1)
        wh = s.rolling(h).apply(lambda x: np.dot(x, w_h[:len(x)]) / w_h[:len(x)].sum(), raw=True)
        wf = s.rolling(p).apply(lambda x: np.dot(x, w_p[:len(x)]) / w_p[:len(x)].sum(), raw=True)
        hull_s = 2 * wh - wf
        return hull_s.rolling(sp).apply(lambda x: np.dot(x, w_sp[:len(x)]) / w_sp[:len(x)].sum(), raw=True)

    hull16 = hull(C, 16)
    hull20 = hull(C, 20)

    print(f"  Indicators computed")
    return {
        'n': n, 'C': C.values, 'V': V.values,
        'ema7': ema7.values, 'ema9': ema9.values, 'ema12': ema12.values,
        'ema21': ema21.values, 'macd_hist': macd_hist.values,
        'rsi14': rsi14.values, 'adx14': adx14.values, 'stoch_k': stoch_k.values,
        'vol_ma20': vol_ma20.values, 'vwap': vwap.values,
        'hull16': hull16.values, 'hull20': hull20.values,
    }

# ── Vectorized simulation ─────────────────────────────────────────────────────
def scalp_sim(d, params):
    n = d['n']; C = d['C']; V = d['V']
    ema7 = d['ema7']; ema9 = d['ema9']; ema21 = d['ema21']
    macd_h = d['macd_hist']; rsi = d['rsi14']; adx = d['adx14']
    sk = d['stoch_k']; vol_ma20 = d['vol_ma20']; vwap = d['vwap']
    hull16 = d['hull16']; hull20 = d['hull20']

    ef = ema7 if params.get('ema_fast') == 7 else ema9
    hma = hull16 if params.get('hull_period', 16) == 16 else hull20
    v_thresh = params['volume_threshold']
    vol_ok = (V / vol_ma20 > v_thresh) & ~np.isnan(vol_ma20)

    rsi_min = params['rsi_buy_min']
    rsi_max = params['rsi_buy_max']
    adx_th  = params['adx_threshold']
    min_cb  = params['min_confirmations_buy']
    min_cs  = params['min_confirmations_sell']
    warm    = 25

    # Boolean confirmations
    c_hull  = ~np.isnan(hma) & (hma > 0)
    c_ema   = ef > ema21
    c_rsi   = (rsi >= rsi_min) & (rsi <= rsi_max)
    c_adx   = adx >= adx_th
    c_macd  = macd_h > 0
    c_stoch = (sk >= 20) & (sk <= 80)
    c_vwap  = C >= vwap
    c_vol   = vol_ok

    buy_cnt = (c_hull.astype(int) + c_ema.astype(int) + c_rsi.astype(int) +
               c_adx.astype(int) + c_macd.astype(int) + c_stoch.astype(int) +
               c_vwap.astype(int) + c_vol.astype(int))
    buy = (buy_cnt >= min_cb) & (np.arange(n) >= warm)

    s_hull  = ~np.isnan(hma) & (hma < 0)
    s_ema   = ef < ema21
    s_rsi   = rsi <= 48
    s_macd  = macd_h < 0
    s_stoch = sk >= 80
    s_vwap  = C <= vwap
    s_vol   = vol_ok

    sell_cnt = (s_hull.astype(int) + s_ema.astype(int) + s_rsi.astype(int) +
                s_macd.astype(int) + s_stoch.astype(int) + s_vwap.astype(int) +
                s_vol.astype(int))
    sell = (sell_cnt >= min_cs) & (np.arange(n) >= warm)

    cash = 10000.0; assets = 0.0; entry_px = 0.0
    trades = wins = losses = 0
    peak = 10000.0; max_dd = 0.0

    for i in range(1, n):
        if buy[i] and assets == 0:
            entry_px = C[i]; assets = cash / entry_px; cash = 0.0; trades += 1
        elif sell[i] and assets > 0:
            pnl = (C[i] - entry_px) / entry_px
            if pnl > 0: wins += 1
            else: losses += 1
            cash = assets * C[i]
            peak = max(peak, cash); max_dd = max(max_dd, (peak - cash) / peak)
            assets = 0.0

    final = cash if assets == 0 else assets * C[-1]
    ret = (final - 10000) / 10000 * 100
    pf = wins / losses if losses > 0 else wins + 0.001
    wr = wins / trades * 100 if trades > 0 else 0
    return {'ret': ret, 'win_rate': wr, 'pf': pf, 'trades': trades,
            'win': wins, 'loss': losses, 'max_dd': max_dd * 100}

# ── Grid ─────────────────────────────────────────────────────────────────────
GRID = {
    'min_confirmations_buy': [3, 4, 5],
    'adx_threshold':          [10.0, 14.0, 18.0],
    'rsi_buy_min':           [30.0, 38.0, 46.0],
    'rsi_buy_max':           [60.0, 70.0, 80.0],
    'ema_fast':               [7, 9],
    'volume_threshold':        [0.8, 1.0, 1.3],
    'min_confirmations_sell': [3, 4],
    'hull_period':            [16, 20],
}

def combos(grid):
    keys = list(grid.keys()); vals = list(grid.values())
    return [dict(zip(keys, c)) for c in product(*vals)]

# ── Main ────────────────────────────────────────────────────────────────────
DATA = {
    'BTC': '/root/Crypto_Sniper/btc_15m_vibe.csv',
    'ETH': '/root/Crypto_Sniper/multi_pair_data/ETH_USDT_15m.csv',
}

print("=== SCALP+ WIN-RATE OPTIMIZE — BTC & ETH 15m ===\n")
t0 = time.time()

results = {}
for pair, path in DATA.items():
    print(f"📊 {pair}")
    d = precompute(path)
    cs = combos(GRID)
    print(f"  {len(cs)} combos...")

    best = {'win_rate': 0, 'ret': -999, 'trades': 0, 'pf': 0}
    for p in cs:
        r = scalp_sim(d, p)
        r['params'] = p
        if r['trades'] >= 50:
            score = r['win_rate'] * 1000 + r['ret']
            best_score = best['win_rate'] * 1000 + best['ret']
            if score > best_score:
                best = r

    print(f"\n  🏅 BEST WIN RATE (min 50 tr):")
    print(f"     WR={best['win_rate']:.1f}% | Ret={best['ret']:.2f}% | PF={best['pf']:.2f} | {best['trades']}tr ({best['win']}W/{best['loss']}L)")
    print(f"     {best['params']}\n")
    results[pair] = best

print(f"=== DONE in {time.time()-t0:.0f}s ===")
for pair, r in results.items():
    print(f"{pair}: WR={r['win_rate']:.1f}% Ret={r['ret']:.2f}% PF={r['pf']:.2f} {r['trades']}tr | {r['params']}")
