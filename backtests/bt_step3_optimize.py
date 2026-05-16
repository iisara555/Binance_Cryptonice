#!/usr/bin/env python3
"""
Step 3: Aggressive parameter optimization for both strategies on 15m data.
Fixes: primary_timeframe forced to '15m', lowered barriers.
"""
import sys, os, itertools, time, json
os.chdir(_REPO)
sys.path.insert(0, str(_REPO))

import numpy as np
import pandas as pd
import warnings; warnings.filterwarnings('ignore')

from strategies.base import SignalType
from strategies.machete_v8b_lite import MacheteV8bLite
from strategies.simple_scalp_plus import SimpleScalpPlus

# ── Load full data ────────────────────────────────────────────────────────────
raw = pd.read_csv(str(_REPO / 'backtest_data_15m.csv'), index_col='timestamp', parse_dates=True)
print(f"[DATA] Total available: {len(raw)} candles ({raw.index[0]} → {raw.index[-1]})")

# Use all data this time — strategies need ~50-80 min bars warmup
N = min(600, len(raw))
data = raw.tail(N).copy()
print(f"[DATA] Using {len(data)} candles: {data.index[0]} → {data.index[-1]}\n")

# ── Backtest Engine ───────────────────────────────────────────────────────────
def backtest(cls, cfg, data, capital=10000.0):
    s = cls(config=cfg)
    port = capital; pos = 0.0; entry_px = 0.0
    wins = losses = cons = maxc = 0
    eq_curve = [capital]
    pnl_log = []; trades = 0

    for i in range(len(data)):
        sd = data.iloc[:i+1]
        sig = s.generate_signal(sd, "BTC/USDT")
        px = float(data.iloc[i]['close'])
        eq = pos * px if pos > 0 else port
        eq_curve.append(eq)

        if sig and sig.signal_type == SignalType.BUY and pos == 0:
            pos = port / px; entry_px = px; trades += 1
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
                trades=n, wins=wins, losses=losses, max_cons=maxc,
                final_eq=port, total_entries=trades)


def show(title, rows):
    print(f"\n{'─'*90}")
    print(f"  {title}")
    print(f"{'─'*90}")
    print(f"{'#':<4}{'Ret':>8}{'Sharpe':>8}{'Win%':>7}{'PF':>7}{'Trades':>7}{'Entries':>8}{'MaxDD':>8}{'Stk':>5}")
    print(f"{'─'*90}")
    for i, r in enumerate(rows):
        p = r.get('params','')
        print(f"{i+1:<4}{r['ret']:>7.2%}{r['sharpe']:>8.2f}{r['winrate']:>6.1%}"
              f"{r['pf']:>7.2f}{r['trades']:>7d}{r.get('total_entries',0):>8d}"
              f"{r['maxdd']:>7.2%}{r['max_cons']:>4d}  {p}")


def sweep(label, cls, base, grid):
    t0 = time.time()
    results = []
    for combo in itertools.product(*grid.values()):
        c = {**base, **dict(zip(grid.keys(), combo))}
        try:
            r = backtest(cls, c, data)
            r['params'] = '; '.join(f"{k}={v}" for k,v in zip(grid.keys(), combo))
            results.append(r)
        except: pass
    results.sort(key=lambda x: (x['ret'], x['sharpe']), reverse=True)
    print(f"  [{label}] {len(results)} combos in {time.time()-t0:.1f}s → best ret={results[0]['ret']:.2%}" if results else f"  [{label}] NO RESULTS")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# MACHETE OPTIMIZATION
# ═══════════════════════════════════════════════════════════════════════════════
print("="*70)
print("  🗡️  MACHETE V8B-LITE OPTIMIZATION")
print("="*70)

# Key fixes: primary_timeframe MUST be '15m' (matches our data),
# informative_timeframe '15m' (same TF since we only have 15m data)
# Lower barriers: reduce min_buy_confidence, min_confirmations

M_base = dict(
    name='m', enabled=True,
    fisher_period=10, tema_fast=9, tema_slow=21, ao_fast=5, ao_slow=34,
    adx_period=14, adx_threshold=25.0,
    vol_period=20, vol_threshold=1.1,
    atr_period=14, atr_multiplier=1.8, risk_reward=2.0,
    min_buy_confidence=0.65, min_sell_confidence=0.50,
    min_confirmations_buy=3, min_confirmations_sell=2,
    enable_relaxed_confirmation=True, relaxed_requires_adx_and_volume=True,
    relaxed_confirmation_delta=1,
    ssl_period=10, rmi_period=14, rmi_momentum=5, rmi_buy_min=52.0,
    atr_volatility_cap_pct=4.0, sr_lookback=50, sr_proximity_pct=2.0,
    primary_timeframe='15m', informative_timeframe='15m',
)

# ── Phase 1: Aggressive lowering ─────────────────────────────────────────────
print("\n▸ Phase 1: Lowering entry barriers")
m_r1 = sweep("MACH-aggressive", MacheteV8bLite, {**M_base,
    'adx_threshold': 15.0,       # was 25
    'min_buy_confidence': 0.50,  # was 0.65
    'min_confirmations_buy': 2,  # was 3
    'vol_threshold': 1.0,        # was 1.1
    'sr_proximity_pct': 3.0,     # wider SR zone
}, {
    'atr_multiplier': [1.5, 1.8, 2.0, 2.5],
    'rmi_buy_min': [40.0, 48.0, 52.0, 56.0, 60.0],
})
show("MACHETE — Aggressive (low adx/conf)", m_r1[:10])

# ── Phase 2: Fine-tune the best from aggressive ──────────────────────────────
if m_r1:
    best = m_r1[0]
    bp = {}
    for p in best['params'].split('; '):
        k,v = p.split('=')
        bp[k] = float(v) if '.' in v else int(v)
    
    print(f"\n▸ Phase 2: Fine-tuning around best machete config")
    m_r2 = sweep("MACH-fine", MacheteV8bLite, {**M_base, **bp}, {
        'adx_threshold': [12.0, 15.0, 18.0, 20.0],
        'min_buy_confidence': [0.40, 0.45, 0.50, 0.55],
        'min_confirmations_buy': [1, 2],
        'sr_proximity_pct': [2.0, 2.5, 3.0, 4.0],
    })
    show("MACHETE — Fine-tuned", m_r2[:10])
    m_r1 += m_r2

# ── Phase 3: Volatility filter tuning ────────────────────────────────────────
if m_r1:
    best = m_r1[0]
    bp = {}
    for p in best['params'].split('; '):
        k,v = p.split('=')
        bp[k] = float(v) if '.' in v else int(v)
    
    print(f"\n▸ Phase 3: Volatility & risk tuning")
    m_r3 = sweep("MACH-vol", MacheteV8bLite, {**M_base, **bp}, {
        'vol_threshold': [0.8, 0.9, 1.0, 1.1],
        'atr_volatility_cap_pct': [3.0, 3.5, 4.0, 5.0],
        'risk_reward': [1.5, 1.8, 2.0, 2.5],
    })
    show("MACHETE — Vol/Risk tuning", m_r3[:10])
    m_r1 += m_r3


# ═══════════════════════════════════════════════════════════════════════════════
# SIMPLE SCALP+ OPTIMIZATION  
# ═══════════════════════════════════════════════════════════════════════════════
print("\n\n" + "="*70)
print("  🔪 SIMPLE SCALP+ OPTIMIZATION")
print("="*70)

# FIX: primary_timeframe must be '15m' not '5m' since data is 15m!
S_base = dict(
    name='s', enabled=True,
    hull_period=16, ema_fast=9, ema_slow=21,
    rsi_period=14, rsi_buy_min=50.0, rsi_buy_max=70.0,
    rsi_sell_max=48.0,
    adx_period=14, adx_threshold=18.0,
    macd_fast=12, macd_slow=26, macd_signal=9,
    stoch_period=14, stoch_buy_k_min=20.0, stoch_buy_k_max=80.0,
    stoch_sell_k_max=80.0,
    volume_period=20, volume_threshold=1.05,
    atr_period=14, atr_multiplier=1.2, risk_reward=1.8,
    min_buy_confidence=0.70, min_sell_confidence=0.55,
    min_confirmations_buy=5, min_confirmations_sell=4,
    primary_timeframe='15m', informative_timeframe='15m',  # ← FIXED
)

# ── Phase 1: Aggressive lowering ─────────────────────────────────────────────
print("\n▸ Phase 1: Lowering entry barriers")
s_r1 = sweep("SCALP+-aggressive", SimpleScalpPlus, {**S_base,
    'min_buy_confidence': 0.50,   # was 0.70
    'min_confirmations_buy': 3,   # was 5
    'min_confirmations_sell': 2,  # was 4
    'adx_threshold': 12.0,        # was 18
    'rsi_buy_min': 40.0,          # was 50
}, {
    'atr_multiplier': [1.0, 1.2, 1.5, 1.8, 2.0],
    'stoch_buy_k_min': [15.0, 18.0, 20.0, 25.0],
    'macd_fast': [10, 12],
})
show("SCALP+ — Aggressive (low conf/adx)", s_r1[:10])

# ── Phase 2: Fine-tune ────────────────────────────────────────────────────────
if s_r1:
    best = s_r1[0]
    bp = {}
    for p in best['params'].split('; '):
        k,v = p.split('=')
        bp[k] = float(v) if '.' in v else int(v)

    print(f"\n▸ Phase 2: Fine-tuning around best scalp+ config")
    s_r2 = sweep("SCALP+-fine", SimpleScalpPlus, {**S_base, **bp}, {
        'ema_fast': [7, 8, 9, 10, 12],
        'ema_slow': [18, 20, 21, 24],
        'bollinger_std': [1.5, 2.0, 2.5],  # Note: not used directly but hull_period matters
        'volume_threshold': [0.95, 1.0, 1.05, 1.1],
        'rsi_buy_min': [35.0, 40.0, 45.0, 50.0],
    })
    show("SCALP+ — Fine-tuned", s_r2[:10])
    s_r1 += s_r2


# ═══════════════════════════════════════════════════════════════════════════════
# FINAL SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════
all_m = sorted(m_r1, key=lambda x: (x['ret'], x['sharpe']), reverse=True) if m_r1 else []
all_s = sorted(s_r1, key=lambda x: (x['ret'], x['sharpe']), reverse=True) if s_r1 else []

print("\n\n" + "="*70)
print("  🏆 FINAL — TOP 5 MACHETTE (all phases combined)")
print("="*70)
for i,r in enumerate(all_m[:5]):
    print(f"  #{i+1} Ret={r['ret']:.2%} | Sharpe={r['sharpe']:.2f} | Win={r['winrate']:.1%} | "
          f"PF={r['pf']:.2f} | Trades={r['trades']} | Entries={r.get('total_entries',0)} | "
          f"MaxDD={r['maxdd']:.2%} | Streak={r['max_cons']}")
    print(f"      {r['params']}")

print("\n" + "="*70)
print("  🏆 FINAL — TOP 5 SCALP+ (all phases combined)")
print("="*70)
for i,r in enumerate(all_s[:5]):
    print(f"  #{i+1} Ret={r['ret']:.2%} | Sharpe={r['sharpe']:.2f} | Win={r['winrate']:.1%} | "
          f"PF={r['pf']:.2f} | Trades={r['trades']} | Entries={r.get('total_entries',0)} | "
          f"MaxDD={r['maxdd']:.2%} | Streak={r['max_cons']}")
    print(f"      {r['params']}")

# Head-to-head
if all_m and all_s:
    bm, bs = all_m[0], all_s[0]
    print(f"\n{'='*70}")
    print("  ⚖️  HEAD-TO-HEAD COMPARISON")
    print(f"{'='*70}")
    print(f"     Machete:  Ret={bm['ret']:.2%}  Sharpe={bm['sharpe']:.2f}  "
          f"Win={bm['winrate']:.1%}  PF={bm['pf']:.2f}  Trades={bm['trades']}  MaxDD={bm['maxdd']:.2%}")
    print(f"     Scalp+:   Ret={bs['ret']:.2%}  Sharpe={bs['sharpe']:.2f}  "
          f"Win={bs['winrate']:.1%}  PF={bs['pf']:.2f}  Trades={bs['trades']}  MaxDD={bs['maxdd']:.2%}")
    ret_w = "Machete" if bm['ret']>bs['ret'] else "Scalp+"
    sh_w  = "Machete" if bm['sharpe']>bs['sharpe'] else "Scalp+"
    pf_w  = "Machete" if bm['pf']>bs['pf'] else "Scalp+"
    print(f"\n     🏅 Return winner:  {ret_w}")
    print(f"     🏅 Sharpe winner:  {sh_w}")
    print(f"     🏅 ProfitFactor winner: {pf_w}")
    print(f"\n  ⏱️  {time.strftime('%Y-%m-%d %H:%M UTC')} | "
          f"Data: {data.index[0].strftime('%m/%d %H:%M')} → {data.index[-1].strftime('%m/%d %H:%M')} ({len(data)} candles)")

# ── Recommendation Engine ────────────────────────────────────────────────────
print(f"\n\n{'='*70}")
print("  📋 RECOMMENDATIONS FOR BOT_CONFIG.YAML")
print(f"{'='*70}")

if all_m:
    bm = all_m[0]
    bp = {}
    for p in bm['params'].split('; '):
        k,v = p.split('=')
        bp[k] = float(v) if '.' in v else int(v)
    print(f"\n  MACHETE V8B-LITE recommended changes:")
    defaults = {'atr_multiplier':1.8, 'adx_threshold':25.0, 'min_confirmations_buy':3,
                'min_buy_confidence':0.65, 'vol_threshold':1.1, 'sr_proximity_pct':2.0,
                'rmi_buy_min':52.0}
    for k in ['atr_multiplier','adx_threshold','min_confirmations_buy',
              'min_buy_confidence','vol_threshold','sr_proximity_pct','rmi_buy_min']:
        old = defaults.get(k, '?')
        new = bp.get(k, old)
        if old != new:
            print(f"    ✏️  {k}: {old} → {new}")

if all_s:
    bs = all_s[0]
    bp = {}
    for p in bs['params'].split('; '):
        k,v = p.split('=')
        bp[k] = float(v) if '.' in v else int(v)
    print(f"\n  SIMPLE SCALP+ recommended changes:")
    defaults = {'atr_multiplier':1.2, 'adx_threshold':18.0, 'min_confirmations_buy':5,
                'min_buy_confidence':0.70, 'vol_threshold':1.05, 'ema_fast':9,
                'stoch_buy_k_min':20.0, 'volume_threshold':1.05}
    for k in ['atr_multiplier','adx_threshold','min_confirmations_buy',
              'min_buy_confidence','vol_threshold','ema_fast','stoch_buy_k_min']:
        old = defaults.get(k, '?')
        new = bp.get(k, old)
        if old != new:
            print(f"    ✏️  {k}: {old} → {new}")