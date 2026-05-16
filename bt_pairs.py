#!/usr/bin/env python3
"""Backtest SOL/ADA/DOGE — vectorized, same TA as vibe_backtest_runner.py"""
import pandas as pd
import numpy as np
from itertools import product

# ── TA helpers ──────────────────────────────────────────────────────────────
def ema(s, n):
    return s.ewm(alpha=1/n, adjust=False).mean()

def atr_ta(h, l, c, n=14):
    hl = h - l; hc = (h - c.shift()).abs(); lc = (l - c.shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def fisher(h, l, p=10):
    hl = (h + l) / 2
    highest = hl.rolling(p).max(); lowest = hl.rolling(p).min()
    hl = hl.where(highest != lowest, other=lowest + 1e-9)
    return np.log((hl - lowest + 1e-9) / (highest - hl + 1e-9)) / 2

def fisher_sig(h, l, p=10):
    f = fisher(h, l, p)
    median = f.rolling(9).median()
    return f.diff().apply(np.sign)

def tema_sig(c, f=9, s=21):
    ef = ema(c, f); es = ema(c, s)
    return (ef - es).diff().apply(np.sign)

def ao_sig(h, l, c, f=5, s=34):
    hlc3 = (h + l + c) / 3
    sma_f = hlc3.rolling(f).mean(); sma_s = hlc3.rolling(s).mean()
    return (sma_f - sma_s).diff().apply(np.sign)

def adx_ta(h, l, c, n=14):
    pl = c.diff()
    nh = (h - h.shift()).clip(lower=0)
    nl = (l.shift() - l).clip(lower=0)
    dmp = pl.where((pl > nh) & (pl > nl), other=0.0)
    dmn = (-pl).where((-pl > nh) & (-pl > nl), other=0.0)
    adx = (100 - 100 / (1 + dmp.ewm(alpha=1/n, adjust=False).mean()
                       / (dmn.ewm(alpha=1/n, adjust=False).mean() + 1e-9))
           ).ewm(alpha=1/n, adjust=False).mean()
    return adx

def vol_ma(v, p=20, t=1.1):
    return (v / v.rolling(p).mean() > t).astype(int)

def rmi_ta(c, p=14, m=5):
    d = c - c.shift(m); up = d.clip(lower=0); dn = (-d).clip(lower=0)
    au = up.ewm(alpha=1/p, adjust=False).mean()
    ad = dn.ewm(alpha=1/p, adjust=False).mean()
    return (100 - 100 / (1 + au / (ad + 1e-9))).fillna(50)

def rsi_ta(c, p=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    dn = (-d).clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    return (100 - 100 / (1 + up / (dn + 1e-9))).fillna(50)

def macd_ta(c, f=12, s=26, sig=9):
    ml = ema(c, f) - ema(c, s)
    sig_l = ema(ml, sig)
    return ml, sig_l, ml - sig_l

def stoch_ta(h, l, c, p=14):
    lo = l.rolling(p).min(); hi = h.rolling(p).max()
    k = 100 * (c - lo) / (hi - lo + 1e-9)
    d = k.ewm(alpha=1/3, adjust=False).mean()
    return k, d

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

# ── Strategy signal generators ──────────────────────────────────────────────
def machete_bs(h, l, c, v, params):
    p = params
    fish = fisher_sig(h, l, p.get('fisher_period', 10))
    tema = tema_sig(c, p.get('tema_fast', 9), p.get('tema_slow', 21))
    ao = ao_sig(h, l, c, p.get('ao_fast', 5), p.get('ao_slow', 34))
    adx = adx_ta(h, l, c, p.get('adx_period', 14))
    v_ok = vol_ma(v, p.get('vol_period', 20), p.get('vol_threshold', 1.1))
    rmi = rmi_ta(c, p.get('rmi_period', 14), p.get('rmi_momentum', 5))
    atr = atr_ta(h, l, c, 14)
    atr_pct = atr / c * 100
    vc = atr_pct < p.get('atr_volatility_cap_pct', 4.0)
    ssl_up = h.rolling(10).max().shift(2)
    ssl_dn = l.rolling(10).min().shift(2)
    sr_lo = ssl_up * (1 + p.get('sr_proximity_pct', 2) / 100)
    sr_hi = ssl_dn * (1 - 0.002)
    sr_ok = (c >= sr_lo.fillna(0)) & (c <= sr_hi.fillna(99999))
    cb = pd.DataFrame({
        'f':  (fish == 1).fillna(False),
        't':  (tema > 0).fillna(False),
        'a':  (ao > 0).fillna(False),
        'ad': (adx > p.get('adx_threshold', 25)).fillna(False),
        'v':  (v_ok == 1).fillna(False),
        'sl': ssl_up.fillna(0) > ssl_dn.fillna(0),
        'rm': (rmi >= p.get('rmi_buy_min', 52)).fillna(False),
    })
    buy = (cb.sum(axis=1) >= p.get('min_confirmations_buy', 3)) & vc & sr_ok
    cb_sell = pd.DataFrame({
        'f': (fish == -1).fillna(False),
        't': (tema < 0).fillna(False),
        'a': (ao < 0).fillna(False),
    })
    sell = cb_sell.sum(axis=1) >= p.get('min_confirmations_sell', 2)
    return buy, sell

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

def simulate(buy, sell, px, cap=10000):
    px_arr = px.values if hasattr(px, 'values') else np.array(px)
    buy_arr = buy.values if hasattr(buy, 'values') else np.array(buy)
    sell_arr = sell.values if hasattr(sell, 'values') else np.array(sell)
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
    return {
        'ret': ret, 'win': wins, 'loss': losses, 'trades': trades,
        'win_rate': wins / trades * 100 if trades > 0 else 0,
        'max_dd': max_dd * 100
    }

# ── Main ─────────────────────────────────────────────────────────────────────
PAIRS = ['SOL_USDT', 'ADA_USDT', 'DOGE_USDT']
DATA_DIR = '/root/Crypto_Sniper/multi_pair_data'

M_GRID = {
    'adx_threshold': [20.0, 25.0],
    'min_confirmations_buy': [2, 3],
    'rmi_buy_min': [48.0, 52.0, 58.0],
    'sr_proximity_pct': [0.5, 1.0, 2.0],
    'vol_threshold': [0.7, 1.0, 1.3],
}
S_GRID = {
    'min_confirmations_buy': [3, 4, 5],
    'adx_threshold': [14.0, 18.0, 22.0],
    'rsi_buy_min': [38.0, 46.0, 50.0],
    'ema_fast': [7, 9],
}

def grid_combos(grid):
    keys = list(grid.keys()); values = list(grid.values())
    return [dict(zip(keys, combo)) for combo in product(*values)]

print("=== SOL/ADA/DOGE BACKTEST (10-day data) ===\n")
for pair in PAIRS:
    df = pd.read_csv(f'{DATA_DIR}/{pair}_15m.csv')
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    h, l, c, v = df['high'], df['low'], df['close'], df['volume']
    print(f"📊 {pair} | {len(df)} candles {df['trade_date'].iloc[0].date()} → {df['trade_date'].iloc[-1].date()}")

    m_combos = grid_combos(M_GRID)
    s_combos = grid_combos(S_GRID)

    best_m = {'ret': -999}
    for params in m_combos:
        buy, sell = machete_bs(h, l, c, v, params)
        r = simulate(buy, sell, c)
        if r['ret'] > best_m['ret']:
            best_m = {**r, 'params': params}

    best_s = {'ret': -999}
    for params in s_combos:
        buy, sell = scalp_bs(h, l, c, v, params)
        r = simulate(buy, sell, c)
        if r['ret'] > best_s['ret']:
            best_s = {**r, 'params': params}

    m_win = 'MACHETE' if best_m['ret'] > best_s['ret'] else 'SCALP+'
    print(f"  MACHETE | Ret={best_m['ret']:.2f}% | Win={best_m['win_rate']:.1f}% | {best_m['trades']}tr")
    print(f"  SCALP+  | Ret={best_s['ret']:.2f}% | Win={best_s['win_rate']:.1f}% | {best_s['trades']}tr")
    print(f"  🏅 Winner: {m_win}\n")

print("=== DONE ===")
