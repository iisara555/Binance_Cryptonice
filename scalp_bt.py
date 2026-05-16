#!/usr/bin/env python3
"""Find best Simple Scalp+ params on 15m — BTC & ETH (6-month data)"""
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

def scalp_bs(h, l, c, v, params):
    p = params
    hlc3 = (h + l + c) / 3
    hma = hull_ma(c, p.get('hull_period', 16))
    ef = ema(c, p.get('ema_fast', 9)); es = ema(c, p.get('ema_slow', 21))
    rs = rsi_ta(c, p.get('rsi_period', 14))
    ad = adx_ta(h, l, c, p.get('adx_period', 14))
    ml, ms, mh = macd_ta(c, p.get('macd_fast', 12), p.get('macd_slow', 26), p.get('macd_signal', 9))
    sk, sd = stoch_ta(h, l, c, p.get('stoch_period', 14))
    v_ok = vol_ma(v, p.get('volume_period', 20), p.get('volume_threshold', 1.05))
    vwap = (hlc3 * v).rolling(p.get('volume_period', 20)).sum() / v.rolling(p.get('volume_period', 20)).sum()
    cb = pd.DataFrame({
        'h':   hma.notna() & (hma > 0),
        'e':   (ef > es).fillna(False),
        'r':   ((rs >= p.get('rsi_buy_min', 50)) & (rs <= p.get('rsi_buy_max', 70))).fillna(False),
        'a':   (ad >= p.get('adx_threshold', 18)).fillna(False),
        'm':   ((ml > ms) & (mh > 0)).fillna(False),
        's':   ((sk >= p.get('stoch_buy_k_min', 20)) & (sk <= p.get('stoch_buy_k_max', 80)) & (sk >= sd)).fillna(False),
        'v':   (c >= vwap.fillna(c)).fillna(False),
        'vol': (v_ok == 1).fillna(False),
    })
    buy = cb.sum(axis=1) >= p.get('min_confirmations_buy', 5)
    cb_sell = pd.DataFrame({
        'h':   (hma < 0).fillna(False),
        'e':   (ef < es).fillna(False),
        'r':   (rs <= p.get('rsi_sell_max', 48)).fillna(True),
        'm':   ((ml < ms) & (mh < 0)).fillna(False),
        's':   ((sk >= p.get('stoch_sell_k_max', 80)) & (sk <= sd)).fillna(False),
        'v':   (c <= vwap.fillna(c)).fillna(True),
        'vol': (v_ok == 1).fillna(False),
    })
    sell = cb_sell.sum(axis=1) >= p.get('min_confirmations_sell', 4)
    return buy, sell

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

# ── Scalp+ param grid ─────────────────────────────────────────────────────────
# Lean grid — 3×3×3×2×3×2×2 = 648 combos
GRID = {
    'min_confirmations_buy': [3, 4, 5],
    'adx_threshold':         [10.0, 14.0, 18.0],
    'rsi_buy_min':           [30.0, 38.0, 46.0],
    'ema_fast':              [7, 9],
    'rsi_buy_max':           [60.0, 70.0, 80.0],
    'volume_threshold':       [0.8, 1.0],
    'min_confirmations_sell': [3, 4],
}

def grid_combos(grid):
    keys = list(grid.keys()); values = list(grid.values())
    return [dict(zip(keys, combo)) for combo in product(*values)]

# ── Main ─────────────────────────────────────────────────────────────────────
DATA = {
    'BTC': '/root/Crypto_Sniper/btc_15m_vibe.csv',
    # 'ETH': '/root/Crypto_Sniper/multi_pair_data/ETH_USDT_15m.csv',
}

print("=== SCALP+ OPTIMIZE — 15m BTC & ETH (6-month) ===\n")
t0 = time.time()

results = {}
for pair, path in DATA.items():
    df = pd.read_csv(path)
    df['trade_date'] = pd.to_datetime(df['timestamp'])
    h, l, c, v = df['high'], df['low'], df['close'], df['volume']
    print(f"📊 {pair} | {len(df)} candles {df['trade_date'].iloc[0].date()} → {df['trade_date'].iloc[-1].date()}")
    
    combos = grid_combos(GRID)
    print(f"  {len(combos)} combos...\n")
    
    # Find best by win_rate (min 50 trades for statistical significance)
    best_by_wr = {'win_rate': 0, 'trades': 0}
    best_by_ret = {'ret': -999}
    best_by_pf = {'pf': 0}
    
    for params in combos:
        buy, sell = scalp_bs(h, l, c, v, params)
        r = simulate(buy, sell, c)
        r['params'] = params
        
        if r['trades'] >= 50:
            if r['win_rate'] > best_by_wr['win_rate']:
                best_by_wr = r
            if r['pf'] > best_by_pf['pf']:
                best_by_pf = r
            if r['ret'] > best_by_ret['ret']:
                best_by_ret = r
    
    print(f"  🏅 Best Win Rate (min 50 tr):")
    print(f"     WR={best_by_wr['win_rate']:.1f}% | Ret={best_by_wr['ret']:.2f}% | PF={best_by_wr['pf']:.2f} | {best_by_wr['trades']}tr")
    print(f"     {best_by_wr['params']}\n")
    
    print(f"  🏅 Best Profit Factor (min 50 tr):")
    print(f"     PF={best_by_pf['pf']:.2f} | WR={best_by_pf['win_rate']:.1f}% | Ret={best_by_pf['ret']:.2f}% | {best_by_pf['trades']}tr")
    print(f"     {best_by_pf['params']}\n")
    
    print(f"  🏅 Best Return (min 50 tr):")
    print(f"     Ret={best_by_ret['ret']:.2f}% | WR={best_by_ret['win_rate']:.1f}% | PF={best_by_ret['pf']:.2f} | {best_by_ret['trades']}tr")
    print(f"     {best_by_ret['params']}\n")
    
    results[pair] = {
        'by_wr': best_by_wr,
        'by_pf': best_by_pf,
        'by_ret': best_by_ret,
    }

print(f"=== DONE in {time.time()-t0:.1f}s ===")
