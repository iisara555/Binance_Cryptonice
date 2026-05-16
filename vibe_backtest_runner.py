#!/usr/bin/env python3
"""Save BTC/USDT 15m data from vibe-trading MCP to CSV, then run backtest."""
import sys, os, json, time
os.chdir('/root/Crypto_Sniper')
sys.path.insert(0, '/root/Crypto_Sniper')

import numpy as np
import pandas as pd
import warnings; warnings.filterwarnings('ignore')

from strategies.base import SignalType
from strategies.machete_v8b_lite import MacheteV8bLite
from strategies.simple_scalp_plus import SimpleScalpPlus

# ── Save fetched data to CSV ──────────────────────────────────────────────────
# (Data already saved by previous run, just reload)
CSV_PATH = '/root/Crypto_Sniper/btc_15m_vibe.csv'

def save_data(candles):
    """Convert vibe-trading candle list to CSV."""
    rows = []
    for c in candles:
        rows.append({
            'timestamp': c['trade_date'],
            'open': c['open'], 'high': c['high'],
            'low': c['low'], 'close': c['close'],
            'volume': c['volume']
        })
    df = pd.DataFrame(rows)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df = df.set_index('timestamp').sort_index()
    df.to_csv(CSV_PATH)
    print(f"[SAVE] {len(df)} candles → {CSV_PATH}")
    print(f"[RANGE] {df.index[0]} → {df.index[-1]}")
    return df

# ── Already have data from fetch, reload ─────────────────────────────────────
df = pd.read_csv(CSV_PATH, index_col='timestamp', parse_dates=True)
print(f"[LOADED] {len(df)} candles: {df.index[0]} → {df.index[-1]}")

close = df['close']; high = df['high']; low = df['low']; volume = df['volume']
hlc3 = (high + low + close) / 3

# ── Indicator helpers (same as bt_fast.py) ──────────────────────────────────
def ema(s, p): return s.ewm(span=p, adjust=False).mean()
def rsi_ta(s, p=14):
    d = s.diff(); g = d.clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    l = (-d).clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    rs = g / l.replace(0, np.nan); return (100 - 100/(1+rs)).fillna(50).clip(0,100)
def atr_ta(h, l, c, p=14):
    tr = pd.concat([h-l, (c-h.shift(1)).abs(), (c-l.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/p, adjust=False).mean()
def macd_ta(c, f=12, s=26, sig=9):
    ml = ema(c,f)-ema(c,s); sig_l = ema(ml,sig); return ml, sig_l, ml-sig_l
def stoch_ta(h, l, c, p=14):
    lo = l.rolling(p).min(); hi = h.rolling(p).max()
    k = 100*(c-lo)/(hi-lo).replace(0,np.nan); return k, k.rolling(3).mean()
def adx_ta(h, l, c, p=14):
    tr = pd.concat([h-l, (c-h.shift(1)).abs(), (c-l.shift(1)).abs()], axis=1).max(axis=1)
    pdm = h.diff().clip(lower=0); mdm = (-l.diff()).clip(lower=0)
    pdm[mdm > pdm] = 0; mdm[pdm > mdm] = 0
    atr_v = tr.ewm(alpha=1/p, adjust=False).mean()
    pdi = 100*pdm.ewm(alpha=1/p, adjust=False).mean()/atr_v.replace(0,np.nan)
    mdi = 100*mdm.ewm(alpha=1/p, adjust=False).mean()/atr_v.replace(0,np.nan)
    dx = 100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)
    return dx.ewm(alpha=1/p, adjust=False).mean()
def vol_ma(v, p=20, th=1.05):
    avg = v.rolling(p).mean(); return (v > avg*th).astype(int)
def fisher(h, l, p=10):
    med = (h+l)/2
    mn = med.expanding(p).min(); mx = med.expanding(p).max()
    norm = 2*(med-mn)/(mx-mn+1e-10) - 1
    norm = norm.clip(-0.999, 0.999)
    fisher = 0.5 * np.log((1+norm)/(1-norm))
    return fisher.diff().apply(np.sign)
def tema_sig(c, f=9, s=21):
    ef = ema(ema(ema(c,f),f),f); es = ema(ema(ema(c,s),s),s)
    return (ef - es).apply(np.sign)
def ao_sig(hlc3, f=5, s=34):
    sma_f = hlc3.rolling(f).mean(); sma_s = hlc3.rolling(s).mean()
    return (sma_f - sma_s).diff().apply(np.sign)
def rmi_ta(c, p=14, m=5):
    d = c - c.shift(m); up = d.clip(lower=0); dn = (-d).clip(lower=0)
    au = up.ewm(alpha=1/p, adjust=False).mean(); ad = dn.ewm(alpha=1/p, adjust=False).mean()
    return (100 - 100/(1+au/ad.replace(0,np.nan))).fillna(50)
def hull_ma(s, p=16):
    h = p//2; sp = int(np.sqrt(p))
    if len(s) < p: return pd.Series(0, index=s.index)
    wh = s.rolling(h).apply(lambda x: np.dot(x,range(1,len(x)+1))/sum(range(1,len(x)+1)), raw=True)
    wf = s.rolling(p).apply(lambda x: np.dot(x,range(1,len(x)+1))/sum(range(1,len(x)+1)), raw=True)
    hull = 2*wh - wf
    return hull.rolling(sp).apply(lambda x: np.dot(x,range(1,len(x)+1))/sum(range(1,len(x)+1)), raw=True) if len(hull)>=sp else pd.Series(0, index=s.index)
def sr_ch(h, l, lb=50):
    return l.rolling(lb).min(), h.rolling(lb).max()

print("⏳ Computing indicators on full dataset...", end=' ', flush=True)
atr_v = atr_ta(high, low, close, 14)
print("done")

def machete_bs(params):
    p = params
    fish = fisher(high, low, p.get('fisher_period',10))
    tema = tema_sig(close, p.get('tema_fast',9), p.get('tema_slow',21))
    ao = ao_sig(hlc3, p.get('ao_fast',5), p.get('ao_slow',34))
    adx = adx_ta(high, low, close, p.get('adx_period',14))
    v_ok = vol_ma(volume, p.get('vol_period',20), p.get('vol_threshold',1.1))
    rmi = rmi_ta(close, p.get('rmi_period',14), p.get('rmi_momentum',5))
    ssl_up, ssl_dn = sr_ch(high, low, p.get('ssl_period',10))
    atr_pct = atr_v / close * 100
    vc = atr_pct < p.get('atr_volatility_cap_pct', 4.0)
    sr_lo = ssl_up.shift(2) * (1 + p.get('sr_proximity_pct',2)/100)
    sr_hi = ssl_dn.shift(2) * (1 - 0.002)
    sr_ok = (close >= sr_lo.fillna(0)) & (close <= sr_hi.fillna(99999))
    c = pd.DataFrame({
        'f':(fish==1).fillna(False), 't':(tema>0).fillna(False),
        'a':(ao>0).fillna(False), 'ad':(adx>p.get('adx_threshold',25)).fillna(False),
        'v':(v_ok==1).fillna(False), 'sl':ssl_up.fillna(0)>ssl_dn.fillna(0),
        'rm':(rmi>=p.get('rmi_buy_min',52)).fillna(False),
    })
    cnt = c.sum(axis=1)
    buy = (cnt >= p.get('min_confirmations_buy',3)) & vc & sr_ok
    cb = pd.DataFrame({'f':(fish==-1).fillna(False),'t':(tema<0).fillna(False),'a':(ao<0).fillna(False)})
    sell = cb.sum(axis=1) >= p.get('min_confirmations_sell',2)
    return buy, sell

def scalp_bs(params):
    p = params
    h = hull_ma(close, p.get('hull_period',16))
    ef = ema(close, p.get('ema_fast',9)); es = ema(close, p.get('ema_slow',21))
    rs = rsi_ta(close, p.get('rsi_period',14))
    ad = adx_ta(high, low, close, p.get('adx_period',14))
    ml,ms,mh = macd_ta(close, p.get('macd_fast',12), p.get('macd_slow',26), p.get('macd_signal',9))
    sk,sd = stoch_ta(high, low, close, p.get('stoch_period',14))
    v_ok = vol_ma(volume, p.get('volume_period',20), p.get('volume_threshold',1.05))
    vwap = (hlc3*volume).rolling(p.get('volume_period',20)).sum()/volume.rolling(p.get('volume_period',20)).sum()
    c = pd.DataFrame({
        'h': h.notna() & (h>0), 'e': (ef>es).fillna(False),
        'r': ((rs>=p.get('rsi_buy_min',50))&(rs<=p.get('rsi_buy_max',70))).fillna(False),
        'a': (ad>=p.get('adx_threshold',18)).fillna(False),
        'm': ((ml>ms)&(mh>0)).fillna(False),
        's': ((sk>=p.get('stoch_buy_k_min',20))&(sk<=p.get('stoch_buy_k_max',80))&(sk>=sd)).fillna(False),
        'v': (close>=vwap.fillna(close)).fillna(False),
        'vol':(v_ok==1).fillna(False),
    })
    buy = c.sum(axis=1) >= p.get('min_confirmations_buy',5)
    cb = pd.DataFrame({
        'h':(h<0).fillna(False), 'e':(ef<es).fillna(False),
        'r':(rs<=p.get('rsi_sell_max',48)).fillna(True),
        'm':((ml<ms)&(mh<0)).fillna(False),
        's':((sk>=p.get('stoch_sell_k_max',80))&(sk<=sd)).fillna(False),
        'v':(close<=vwap.fillna(close)).fillna(True),
        'vol':(v_ok==1).fillna(False),
    })
    sell = cb.sum(axis=1) >= p.get('min_confirmations_sell',4)
    return buy, sell

def sim(buy, sell, px, cap=10000):
    px_arr = px.values if hasattr(px,'values') else np.array(px)
    buy_arr = buy.values if hasattr(buy,'values') else np.array(buy)
    sell_arr = sell.values if hasattr(sell,'values') else np.array(sell)
    port, pos, wins, losses, cons, maxc = cap, 0.0, 0, 0, 0, 0
    eq=[cap]; pnl=[]; entries=0
    for i in range(len(px_arr)):
        v = pos*px_arr[i] if pos>0 else port
        eq.append(v)
        if buy_arr[i] and pos==0:
            pos = port/px_arr[i]; pe = port; entries+=1
        elif sell_arr[i] and pos>0:
            ev = pos*px_arr[i]; p=(ev-pe)/pe; pnl.append(p)
            if p>=0: wins+=1; cons=0
            else: losses+=1; cons+=1; maxc=max(maxc,cons)
            port=ev; pos=0
    if pos>0: port = pos*px_arr[-1]
    ret=(port-cap)/cap; n=wins+losses; wr=wins/n if n else 0
    gp=sum(x for x in pnl if x>0); gl=sum(abs(x) for x in pnl if x<0)
    pf=gp/gl if gl>0 else (999 if gp>0 else 0)
    r=np.diff(eq)/np.clip(eq[:-1],1e-10,None); r=r[np.isfinite(r)]
    sh=(np.mean(r)/np.std(r)*np.sqrt(252*96)) if len(r)>1 and np.std(r)>0 else 0
    dd=max((p-e)/p for p,e in zip(np.maximum.accumulate(eq),eq)) if eq else 0
    return dict(ret=ret,sharpe=sh,maxdd=dd,winrate=wr,pf=pf,
                trades=n,wins=wins,losses=losses,max_cons=maxc,final_eq=port,entries=entries)

def show(t, rows):
    print(f"\n{'─'*80}\n  {t}\n{'─'*80}")
    print(f"{'#':<4}{'Ret':>8}{'Sharpe':>8}{'Win%':>7}{'PF':>7}{'Trades':>7}{'W':>5}{'L':>5}{'MaxDD':>8}")
    print(f"{'─'*80}")
    for i,r in enumerate(rows):
        print(f"{i+1:<4}{r['ret']:>7.2%}{r['sharpe']:>8.2f}{r['winrate']:>6.1%}{r['pf']:>7.2f}"
              f"{r['trades']:>7d}{r['wins']:>5d}{r['losses']:>5d}{r['maxdd']:>7.2%}  {r.get('params','')}")

t0=time.time()

# ══════════════════════════════════════════════════════════════════════════
# FULL DATA BACKTEST
# ══════════════════════════════════════════════════════════════════════════
print("\n" + "="*80)
print("  🗡️  MACHETE V8B-LITE — 6 เดือน (Nov 2025 – May 2026)")
print("="*80)

# Default Machete
buy0, sell0 = machete_bs({})
r0 = sim(buy0, sell0, close)
show("MACHETE [DEFAULT]", [r0])

# Optimized Machete
m_opt = dict(atr_multiplier=1.5, adx_threshold=10.0, min_confirmations_buy=3,
             rmi_buy_min=52.0, sr_proximity_pct=1.0, vol_threshold=0.8,
             risk_reward=1.5, min_buy_confidence=0.50,
             fisher_period=10, tema_fast=9, tema_slow=21,
             ao_fast=5, ao_slow=34, adx_period=14,
             vol_period=20, atr_period=14,
             atr_volatility_cap_pct=4.0, ssl_period=10,
             rmi_period=14, rmi_momentum=5,
             min_confirmations_sell=2, min_sell_confidence=0.50)
buy, sell = machete_bs(m_opt)
r_opt = sim(buy, sell, close)
show("MACHETE [OPTIMIZED]", [r_opt])

# Conservative Machete (very tight)
m_cons = {**m_opt, 'adx_threshold':8.0, 'min_confirmations_buy':2,
          'sr_proximity_pct':0.5, 'rmi_buy_min':45.0, 'vol_threshold':0.7}
buy, sell = machete_bs(m_cons)
r_cons = sim(buy, sell, close)
show("MACHETE [CONSERVATIVE]", [r_cons])

# Aggressive Machete (loose)
m_agr = {**m_opt, 'adx_threshold':20.0, 'min_confirmations_buy':2,
         'vol_threshold':1.0, 'sr_proximity_pct':1.5}
buy, sell = machete_bs(m_agr)
r_agr = sim(buy, sell, close)
show("MACHETE [AGGRESSIVE]", [r_agr])

# ══════════════════════════════════════════════════════════════════════════
print("\n\n" + "="*80)
print("  🔪 SIMPLE SCALP+ — 6 เดือน (Nov 2025 – May 2026)")
print("="*80)

# Default Scalp+
buy0, sell0 = scalp_bs({})
r0 = sim(buy0, sell0, close)
show("SCALP+ [DEFAULT]", [r0])

# Optimized Scalp+
s_opt = dict(atr_multiplier=0.8, min_confirmations_buy=3,
             adx_threshold=18.0, ema_fast=9, ema_slow=21,
             rsi_period=14, rsi_buy_min=50.0, rsi_buy_max=70.0,
             rsi_sell_max=48.0, macd_fast=12, macd_slow=26, macd_signal=9,
             stoch_period=14, stoch_buy_k_min=22.0, stoch_buy_k_max=80.0,
             stoch_sell_k_max=80.0, volume_period=20, volume_threshold=1.0,
             atr_period=14, min_buy_confidence=0.50,
             min_confirmations_sell=4, min_sell_confidence=0.55,
             hull_period=16)
buy, sell = scalp_bs(s_opt)
r_opt = sim(buy, sell, close)
show("SCALP+ [OPTIMIZED]", [r_opt])

# Aggressive Scalp+
s_agr = {**s_opt, 'min_confirmations_buy':2, 'adx_threshold':14.0,
         'rsi_buy_min':40.0, 'ema_fast':7, 'volume_threshold':0.9,
         'stoch_buy_k_min':15.0}
buy, sell = scalp_bs(s_agr)
r_agr = sim(buy, sell, close)
show("SCALP+ [AGGRESSIVE]", [r_agr])

# Conservative Scalp+
s_cons = {**s_opt, 'min_confirmations_buy':4, 'adx_threshold':20.0,
          'volume_threshold':1.05}
buy, sell = scalp_bs(s_cons)
r_cons = sim(buy, sell, close)
show("SCALP+ [CONSERVATIVE]", [r_cons])

# ══════════════════════════════════════════════════════════════════════════
print(f"\n\n{'='*80}")
print("  🏆 FINAL SUMMARY")
print(f"{'='*80}")
print(f"  Data: {df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')} ({len(df)} candles ~{len(df)*15/60/24:.0f} days)")
print(f"\n  Strategy              | Return   | Sharpe | Win% | PF   | Trades | MaxDD")
print(f"  --------------------- | -------- | ------ | ---- | ---- | ------ | -----")

configs = [
    ("MACHETE [DEFAULT]", r0),
    ("MACHETE [OPTIMIZED]", None),
    ("MACHETE [CONSERVATIVE]", None),
    ("SCALP+ [DEFAULT]", None),
    ("SCALP+ [OPTIMIZED]", None),
    ("SCALP+ [AGGRESSIVE]", None),
]

machete_configs = [
    ("MACHETE DEFAULT", m_opt, {}),
    ("MACHETE OPTIMIZED", m_opt, {}),
    ("MACHETE CONSERVATIVE", m_cons, {}),
    ("MACHETE AGGRESSIVE", m_agr, {}),
]

scalp_configs = [
    ("SCALP+ DEFAULT", {}, {}),
    ("SCALP+ OPTIMIZED", s_opt, {}),
    ("SCALP+ AGGRESSIVE", s_agr, {}),
    ("SCALP+ CONSERVATIVE", s_cons, {}),
]

# Recompute all for summary
all_m = []
for label, cfg, _ in machete_configs:
    is_def = (label == "MACHETE DEFAULT")
    is_cons = (label == "MACHETE CONSERVATIVE")
    is_agr = (label == "MACHETE AGGRESSIVE")
    if is_def:
        c = {}
    elif is_cons:
        c = m_cons
    elif is_agr:
        c = m_agr
    else:
        c = m_opt
    b, s = machete_bs(c)
    r = sim(b, s, close)
    all_m.append((label, r))

all_s = []
for label, cfg, _ in scalp_configs:
    is_def = (label == "SCALP+ DEFAULT")
    if is_def:
        c = {}
    elif "AGGRESSIVE" in label:
        c = s_agr
    elif "CONSERVATIVE" in label:
        c = s_cons
    else:
        c = s_opt
    b, s = scalp_bs(c)
    r = sim(b, s, close)
    all_s.append((label, r))

all_results = all_m + all_s
all_results.sort(key=lambda x: x[1]['ret'], reverse=True)

print(f"\n  🏅 RANKING (by Return):")
for i, (label, r) in enumerate(all_results):
    marker = "🥇" if i==0 else "🥈" if i==1 else "🥉" if i==2 else "  "
    print(f"  {marker} {label:<22} | {r['ret']:>7.2%} | {r['sharpe']:>6.2f} | {r['winrate']:>5.1%} | {r['pf']:>5.2f} | {r['trades']:>6d} | {r['maxdd']:>6.2%}")

# Head to head
m_best = max(all_m, key=lambda x: x[1]['ret'])
s_best = max(all_s, key=lambda x: x[1]['ret'])
print(f"\n  ⚖️  MACHETE vs SCALP+ (best of each):")
print(f"     Machete best:  {m_best[0]} → {m_best[1]['ret']:.2%} / Sharpe {m_best[1]['sharpe']:.2f} / Win {m_best[1]['winrate']:.1%} / PF {m_best[1]['pf']:.2f} / {m_best[1]['trades']} trades")
print(f"     Scalp+ best:   {s_best[0]} → {s_best[1]['ret']:.2%} / Sharpe {s_best[1]['sharpe']:.2f} / Win {s_best[1]['winrate']:.1%} / PF {s_best[1]['pf']:.2f} / {s_best[1]['trades']} trades")
print(f"\n  ⏱️  {time.strftime('%H:%M:%S')} | {time.time()-t0:.1f}s")