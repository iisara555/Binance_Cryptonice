#!/usr/bin/env python3
"""Step 2: Fast backtest with reduced data and lean param sweep."""
import sys, os, itertools, time
os.chdir(_REPO)
sys.path.insert(0, str(_REPO))

import numpy as np
import pandas as pd
import warnings; warnings.filterwarnings('ignore')

from strategies.base import SignalType
from strategies.machete_v8b_lite import MacheteV8bLite
from strategies.simple_scalp_plus import SimpleScalpPlus

# ── Load data, use only last N candles ────────────────────────────────────────
raw = pd.read_csv(str(_REPO / 'backtest_data_15m.csv'), index_col='timestamp', parse_dates=True)
# Use last 180 candles (~45 hours of 15m data) to keep backtest fast
N = min(180, len(raw))
data = raw.tail(N).copy()
print(f"[DATA] Using last {len(data)} candles: {data.index[0]} → {data.index[-1]}\n")

# ── Backtest (still O(n²) but n is small now) ────────────────────────────────
def backtest(cls, cfg, data, capital=10000.0):
    s = cls(config=cfg)
    port = capital; pos = 0.0; entry = 0.0
    wins = losses = cons = maxc = 0
    eq_curve = [capital]
    pnl_log = []

    for i in range(len(data)):
        sd = data.iloc[:i+1]
        sig = s.generate_signal(sd, "BTC/USDT")
        px = float(data.iloc[i]['close'])
        eq = pos * px if pos > 0 else port
        eq_curve.append(eq)

        if sig and sig.signal_type == SignalType.BUY and pos == 0:
            pos = port / px; entry = px
        elif sig and sig.signal_type == SignalType.SELL and pos > 0:
            exit_val = pos * px
            pnl = (exit_val - port) / port
            pnl_log.append(pnl)
            if pnl >= 0: wins += 1; cons = 0
            else: losses += 1; cons += 1; maxc = max(maxc, cons)
            port = exit_val; pos = 0

    if pos > 0: port = pos * float(data.iloc[-1]['close'])
    ret = (port - capital) / capital
    n = wins + losses
    wr = wins / n if n else 0
    gp = sum(p for p in pnl_log if p > 0)
    gl = sum(abs(p) for p in pnl_log if p < 0)
    pf = gp / gl if gl > 0 else (999 if gp > 0 else 0)
    rets = np.diff(eq_curve) / np.clip(eq_curve[:-1], 1e-10, None)
    rets = rets[np.isfinite(rets)]
    sh = (np.mean(rets) / np.std(rets) * np.sqrt(252*96)) if len(rets)>1 and np.std(rets)>0 else 0
    dd = max((p - e) / p for p, e in zip(np.maximum.accumulate(eq_curve), eq_curve)) if eq_curve else 0
    return dict(ret=ret, sharpe=sh, maxdd=dd, winrate=wr, pf=pf,
                trades=n, wins=wins, losses=losses, max_cons=maxc, final_eq=port)


def show(title, rows):
    print(f"\n{'─'*85}")
    print(f"  {title}")
    print(f"{'─'*85}")
    print(f"{'#':<4}{'Ret':>8}{'Sharpe':>8}{'Win%':>7}{'PF':>7}{'Tr':>5}{'W':>4}{'L':>4}{'MaxDD':>8}{'Stk':>5}")
    print(f"{'─'*85}")
    for i, r in enumerate(rows):
        p = r.get('params','')
        print(f"{i+1:<4}{r['ret']:>7.2%}{r['sharpe']:>8.2f}{r['winrate']:>6.1%}"
              f"{r['pf']:>7.2f}{r['trades']:>5d}{r['wins']:>4d}{r['losses']:>4d}"
              f"{r['maxdd']:>7.2%}{r['max_cons']:>4d}  {p}")


# ── Base configs ─────────────────────────────────────────────────────────────
M0 = dict(name='m', enabled=True, fisher_period=10, tema_fast=9, tema_slow=21,
    ao_fast=5, ao_slow=34, adx_period=14, adx_threshold=25.0, vol_period=20,
    vol_threshold=1.1, atr_period=14, atr_multiplier=1.8, risk_reward=2.0,
    min_buy_confidence=0.65, min_sell_confidence=0.50,
    min_confirmations_buy=3, min_confirmations_sell=2,
    enable_relaxed_confirmation=True, relaxed_requires_adx_and_volume=True,
    relaxed_confirmation_delta=1, ssl_period=10,
    rmi_period=14, rmi_momentum=5, rmi_buy_min=52.0,
    atr_volatility_cap_pct=4.0, sr_lookback=50, sr_proximity_pct=2.0,
    primary_timeframe='15m', informative_timeframe='15m')

S0 = dict(name='s', enabled=True, hull_period=16, ema_fast=9, ema_slow=21,
    rsi_period=14, rsi_buy_min=50.0, rsi_buy_max=70.0, rsi_sell_max=48.0,
    adx_period=14, adx_threshold=18.0,
    macd_fast=12, macd_slow=26, macd_signal=9,
    stoch_period=14, stoch_buy_k_min=20.0, stoch_buy_k_max=80.0,
    stoch_sell_k_max=80.0, volume_period=20, volume_threshold=1.05,
    atr_period=14, atr_multiplier=1.2, risk_reward=1.8,
    min_buy_confidence=0.70, min_sell_confidence=0.55,
    min_confirmations_buy=5, min_confirmations_sell=4,
    primary_timeframe='5m', informative_timeframe='15m')

t0_total = time.time()

# ── PHASE 1: Default ─────────────────────────────────────────────────────────
print("="*70)
print("  PHASE 1: DEFAULT CONFIG (from bot_config.yaml)")
print("="*70)
r1 = backtest(MacheteV8bLite, M0, data)
show("MACHETE V8B-LITE [DEFAULT]", [r1])

r2 = backtest(SimpleScalpPlus, S0, data)
show("SIMPLE SCALP+ [DEFAULT]", [r2])

# ── PHASE 2: Sweep ───────────────────────────────────────────────────────────
print("\n ▓▓▓ PHASE 2: PARAMETER SWEEP ▓▓▓")

m_grid = {'atr_multiplier':[1.5, 1.8, 2.0],
          'adx_threshold':[20.0, 25.0, 30.0],
          'min_confirmations_buy':[2, 3]}

t0 = time.time()
m_all = []
for combo in itertools.product(*m_grid.values()):
    c = {**M0, **dict(zip(m_grid.keys(), combo))}
    try:
        r = backtest(MacheteV8bLite, c, data)
        r['params'] = '; '.join(f"{k}={v}" for k,v in zip(m_grid.keys(), combo))
        m_all.append(r)
    except Exception: pass
m_all.sort(key=lambda x: x['ret'], reverse=True)
print(f"  [MACHETE] {len(m_all)} combos in {time.time()-t0:.1f}s")
show("MACHETE — COARSE (atr×adx×minconf)", m_all[:10])

s_grid = {'atr_multiplier':[1.0, 1.2, 1.5, 1.8],
          'min_confirmations_buy':[4, 5],
          'adx_threshold':[14.0, 18.0]}

t0 = time.time()
s_all = []
for combo in itertools.product(*s_grid.values()):
    c = {**S0, **dict(zip(s_grid.keys(), combo))}
    try:
        r = backtest(SimpleScalpPlus, c, data)
        r['params'] = '; '.join(f"{k}={v}" for k,v in zip(s_grid.keys(), combo))
        s_all.append(r)
    except Exception: pass
s_all.sort(key=lambda x: x['ret'], reverse=True)
print(f"  [SCALP+] {len(s_all)} combos in {time.time()-t0:.1f}s")
show("SCALP+ — COARSE (atr×minconf×adx)", s_all[:10])

# ── PHASE 3: Fine-tune best ──────────────────────────────────────────────────
print("\n ▓▓▓ PHASE 3: FINE-TUNE BEST ▓▓▓")

if m_all:
    bm = m_all[0]
    bp = {}
    for p in bm['params'].split('; '):
        k,v = p.split('=')
        bp[k] = float(v) if '.' in v else int(v)
    t0 = time.time()
    fm = []
    fg = {'rmi_buy_min':[48.0,50.0,52.0,56.0], 'sr_proximity_pct':[1.0,1.5,2.0]}
    for combo in itertools.product(*fg.values()):
        c = {**M0, **bp, **dict(zip(fg.keys(), combo))}
        try:
            r = backtest(MacheteV8bLite, c, data)
            r['params'] = '; '.join(f"{k}={v}" for k,v in {**bp, **dict(zip(fg.keys(), combo))}.items())
            fm.append(r)
        except: pass
    fm.sort(key=lambda x: x['ret'], reverse=True)
    print(f"  [MACHETE-FINE] {len(fm)} combos in {time.time()-t0:.1f}s")
    show("MACHETE — FINE (RMI + SR)", fm[:8])
    m_all += fm

if s_all:
    bs = s_all[0]
    bp = {}
    for p in bs['params'].split('; '):
        k,v = p.split('=')
        bp[k] = float(v) if '.' in v else int(v)
    t0 = time.time()
    fs = []
    fg = {'ema_fast':[8,9,10,12], 'bollinger_std':[1.5,2.0,2.5]}
    for combo in itertools.product(*fg.values()):
        c = {**S0, **bp, **dict(zip(fg.keys(), combo))}
        try:
            r = backtest(SimpleScalpPlus, c, data)
            r['params'] = '; '.join(f"{k}={v}" for k,v in {**bp, **dict(zip(fg.keys(), combo))}.items())
            fs.append(r)
        except: pass
    fs.sort(key=lambda x: x['ret'], reverse=True)
    print(f"  [SCALP+-FINE] {len(fs)} combos in {time.time()-t0:.1f}s")
    show("SCALP+ — FINE (EMA + Bol)", fs[:8])
    s_all += fs

# ── FINAL SUMMARY ────────────────────────────────────────────────────────────
m_all.sort(key=lambda x: x['ret'], reverse=True)
s_all.sort(key=lambda x: x['ret'], reverse=True)

print("\n\n" + "="*70)
print("  🏆 FINAL — TOP 3 MACHETTE")
print("="*70)
for i,r in enumerate(m_all[:3]):
    print(f"  #{i+1} Ret={r['ret']:.2%} | Sharpe={r['sharpe']:.2f} | Win={r['winrate']:.1%} | PF={r['pf']:.2f} | Trades={r['trades']} | MaxDD={r['maxdd']:.2%} | StreakLoss={r['max_cons']}")
    print(f"      Params: {r['params']}")

print("\n" + "="*70)
print("  🏆 FINAL — TOP 3 SCALP+")
print("="*70)
for i,r in enumerate(s_all[:3]):
    print(f"  #{i+1} Ret={r['ret']:.2%} | Sharpe={r['sharpe']:.2f} | Win={r['winrate']:.1%} | PF={r['pf']:.2f} | Trades={r['trades']} | MaxDD={r['maxdd']:.2%} | StreakLoss={r['max_cons']}")
    print(f"      Params: {r['params']}")

if m_all and s_all:
    bm, bs = m_all[0], s_all[0]
    print(f"\n  ⚖️  HEAD-TO-HEAD:")
    print(f"     Return: {'MACHETE' if bm['ret']>bs['ret'] else 'SCALP+'} ({max(bm['ret'],bs['ret']):.2%} vs {min(bm['ret'],bs['ret']):.2%})")
    print(f"     Sharpe: {'MACHETE' if bm['sharpe']>bs['sharpe'] else 'SCALP+'} ({max(bm['sharpe'],bs['sharpe']):.2f} vs {min(bm['sharpe'],bs['sharpe']):.2f})")
    print(f"     Trades: Machete={bm['trades']}  Scalp+={bs['trades']}")
    print(f"\n  Total time: {time.time()-t0_total:.1f}s")
    print(f"  Data: {data.index[0].strftime('%m/%d %H:%M')} → {data.index[-1].strftime('%m/%d %H:%M')} ({len(data)} candles)")