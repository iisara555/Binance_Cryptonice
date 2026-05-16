#!/usr/bin/env python3
"""
Backtest machete_v8b_lite vs simple_scalp_plus on 15m timeframe.
Optimized for speed: reduced param grid, smaller data fetch.
"""

import sys, os, itertools, warnings
warnings.filterwarnings('ignore')
os.chdir('/root/Crypto_Sniper')
sys.path.insert(0, '/root/Crypto_Sniper')

import numpy as np
import pandas as pd
import ccxt
from datetime import datetime, timedelta

from strategies.machete_v8b_lite import MacheteV8bLite
from strategies.simple_scalp_plus import SimpleScalpPlus
from strategies.base import SignalType


# ── Data Fetching ──────────────────────────────────────────────────────────────
def fetch_ohlcv(symbol="BTC/USDT", timeframe="15m", lookback_candles=2000):
    exchange = ccxt.binance({'enableRateLimit': True})
    print(f"[DATA] Fetching {symbol} {timeframe} ({lookback_candles} candles max)...")
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=lookback_candles)
    df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    df.drop_duplicates(inplace=True)
    df.sort_index(inplace=True)
    print(f"[DATA] Loaded {len(df)} candles: {df.index[0]} → {df.index[-1]}")
    return df


# ── Backtest Engine ───────────────────────────────────────────────────────────
def run_backtest(strategy, data, capital=10000.0):
    portfolio = capital
    position = 0.0
    entry_price = 0.0
    wins = losses = 0
    cons_loss = max_cons = 0
    equity = [capital]
    pnl_list = []
    hold_bars_list = []
    trades_log = []

    for i in range(len(data)):
        sdata = data.iloc[:i+1]
        sig = strategy.analyze(sdata)
        price = float(data.iloc[i]['close'])

        # equity tracking
        eq = position * price if position > 0 else portfolio
        equity.append(eq)

        if sig == 'buy' and position == 0:
            position = portfolio / price
            entry_price = price
            trades_log.append(('BUY', i, price))

        elif sig == 'sell' and position > 0:
            exit_val = position * price
            pnl = (exit_val - portfolio) / portfolio
            pnl_list.append(pnl)

            if pnl >= 0:
                wins += 1; cons_loss = 0
            else:
                losses += 1; cons_loss += 1
                max_cons = max(max_cons, cons_loss)

            hold = i - trades_log[-1][1] if trades_log and trades_log[-1][0]=='BUY' else 0
            hold_bars_list.append(hold)
            trades_log.append(('SELL', i, price, pnl))
            portfolio = exit_val
            position = 0

    # close remaining
    if position > 0:
        portfolio = position * float(data.iloc[-1]['close'])

    final_eq = portfolio
    total_ret = (final_eq - capital) / capital
    n_trades = wins + losses
    win_rate = wins / n_trades if n_trades else 0
    gp = sum(p for p in pnl_list if p > 0)
    gl = sum(abs(p) for p in pnl_list if p < 0)
    pf = gp / gl if gl > 0 else (999 if gp > 0 else 0)

    # approximate Sharpe (annualized for 15m bars)
    rets = np.diff(equity) / np.array(equity[:-1])
    rets = rets[np.isfinite(rets)]
    sharpe = (np.mean(rets) / np.std(rets) * np.sqrt(252 * 96)) if len(rets) > 1 and np.std(rets) > 0 else 0

    max_dd = max((peak - eq) / peak for peak, eq in zip(
        np.maximum.accumulate(equity), equity)) if equity else 0

    avg_hold = np.mean(hold_bars_list) if hold_bars_list else 0

    return {
        'ret': total_ret, 'sharpe': sharpe, 'maxdd': max_dd,
        'winrate': win_rate, 'pf': pf, 'trades': n_trades,
        'wins': wins, 'losses': losses, 'avg_hold': avg_hold,
        'max_cons_loss': max_cons, 'final_eq': final_eq,
    }


class Wrap:
    def __init__(self, cls, cfg):
        self.s = cls(config=cfg)
    def analyze(self, data):
        sig = self.s.generate_signal(data, "BTC/USDT")
        if sig is None: return None
        if sig.signal_type == SignalType.BUY: return 'buy'
        if sig.signal_type == SignalType.SELL: return 'sell'
        return None


def print_tbl(rows, title):
    print(f"\n{'─'*95}")
    print(f"  {title}")
    print(f"{'─'*95}")
    print(f"{'#':<4}{'Return':>8}{'Sharpe':>9}{'WinR%':>8}{'PF':>7}{'Trades':>7}{'AvgH':>6}{'MaxDD':>8}{'Streak':>8}")
    print(f"{'─'*95}")
    for i, r in enumerate(rows):
        p = r.get('params', {})
        extra = ' '.join(f"{k}={v}" for k,v in p.items()) if p else ''
        print(f"{i+1:<4}{r['ret']:>7.2%}{r['sharpe']:>9.2f}{r['winrate']:>7.1%}"
              f"{r['pf']:>7.2f}{r['trades']:>7d}{r['avg_hold']:>5.0f}"
              f"{r['maxdd']:>7.2%}{r['max_cons_loss']:>7d}  {extra}")


# ── Base configs ──────────────────────────────────────────────────────────────
MACH_BASE = {
    'name':'machete_v8b_lite','enabled':True,
    'fisher_period':10,'tema_fast':9,'tema_slow':21,'ao_fast':5,'ao_slow':34,
    'adx_period':14,'adx_threshold':25.0,
    'vol_period':20,'vol_threshold':1.1,
    'atr_period':14,'atr_multiplier':1.8,'risk_reward':2.0,
    'min_buy_confidence':0.65,'min_sell_confidence':0.50,
    'min_confirmations_buy':3,'min_confirmations_sell':2,
    'enable_relaxed_confirmation':True,'relaxed_requires_adx_and_volume':True,
    'relaxed_confirmation_delta':1,
    'ssl_period':10,'rmi_period':14,'rmi_momentum':5,'rmi_buy_min':52.0,
    'atr_volatility_cap_pct':4.0,'sr_lookback':50,'sr_proximity_pct':2.0,
    'primary_timeframe':'15m','informative_timeframe':'15m',
}

SCALP_BASE = {
    'name':'simple_scalp_plus','enabled':True,
    'hull_period':16,'ema_fast':9,'ema_slow':21,
    'rsi_period':14,'rsi_buy_min':50.0,'rsi_buy_max':70.0,'rsi_sell_max':48.0,
    'adx_period':14,'adx_threshold':18.0,
    'macd_fast':12,'macd_slow':26,'macd_signal':9,
    'stoch_period':14,'stoch_buy_k_min':20.0,'stoch_buy_k_max':80.0,'stoch_sell_k_max':80.0,
    'volume_period':20,'volume_threshold':1.05,
    'atr_period':14,'atr_multiplier':1.2,'risk_reward':1.8,
    'min_buy_confidence':0.70,'min_sell_confidence':0.55,
    'min_confirmations_buy':5,'min_confirmations_sell':4,
    'primary_timeframe':'5m','informative_timeframe':'15m',
}


def main():
    data = fetch_ohlcv("BTC/USDT", "15m", 1500)

    # ── Phase 1: Base ───────────────────────────────────────────────────
    for name, cls, base in [("MACHETE", MacheteV8bLite, MACH_BASE),
                            ("SCALP+", SimpleScalpPlus, SCALP_BASE)]:
        w = Wrap(cls, base)
        r = run_backtest(w, data)
        print_tbl([r], f"{name} — DEFAULT CONFIG")

    # ── Phase 2: Quick coarse sweep ─────────────────────────────────────
    print("\n\n ▓▓▓ PARAMETER SWEEP ▓▓▓")

    # Machete coarse
    m_grid = {
        'atr_multiplier': [1.5, 1.8, 2.0],
        'adx_threshold': [20.0, 25.0, 30.0],
        'min_confirmations_buy': [2, 3, 4],
    }
    machete_all = []
    for combo in itertools.product(*m_grid.values()):
        c = {**MACH_BASE, **dict(zip(m_grid.keys(), combo))}
        try:
            r = run_backtest(Wrap(MacheteV8bLite, c), data)
            r['params'] = dict(zip(m_grid.keys(), combo))
            machete_all.append(r)
        except:
            pass
    machete_all.sort(key=lambda x: x['ret'], reverse=True)
    print_tbl(machete_all[:8], "MACHETE — COARSE SWEEP (atr × adx × minconf)")

    # Scalp+ coarse
    s_grid = {
        'atr_multiplier': [1.0, 1.2, 1.5],
        'min_confirmations_buy': [4, 5, 6],
        'adx_threshold': [14.0, 18.0, 22.0],
    }
    scalp_all = []
    for combo in itertools.product(*s_grid.values()):
        c = {**SCALP_BASE, **dict(zip(s_grid.keys(), combo))}
        try:
            r = run_backtest(Wrap(SimpleScalpPlus, c), data)
            r['params'] = dict(zip(s_grid.keys(), combo))
            scalp_all.append(r)
        except:
            pass
    scalp_all.sort(key=lambda x: x['ret'], reverse=True)
    print_tbl(scalp_all[:8], "SCALP+ — COARSE SWEEP (atr × minconf × adx)")

    # ── Phase 3: Fine-tune best ─────────────────────────────────────────
    def fine_tune(name, cls, base, best_params, fine_grid):
        results = []
        for combo in itertools.product(*fine_grid.values()):
            c = {**base, **best_params, **dict(zip(fine_grid.keys(), combo))}
            try:
                r = run_backtest(Wrap(cls, c), data)
                r['params'] = {**best_params, **dict(zip(fine_grid.keys(), combo))}
                results.append(r)
            except:
                pass
        results.sort(key=lambda x: x['ret'], reverse=True)
        return results

    if machete_all:
        bm = machete_all[0]
        fm = fine_tune("machete", MacheteV8bLite, MACH_BASE, bm['params'], {
            'rmi_buy_min': [48.0, 50.0, 52.0, 56.0],
            'sr_proximity_pct': [1.0, 1.5, 2.0],
        })
        print_tbl(fm[:8], "MACHETE — FINE TUNE (RMI + SR)")
        machete_all += fm

    if scalp_all:
        bs = scalp_all[0]
        fs = fine_tune("scalp", SimpleScalpPlus, SCALP_BASE, bs['params'], {
            'ema_fast': [8, 9, 10, 12],
            'bollinger_std': [1.5, 2.0, 2.5],
            'stoch_buy_k_min': [15.0, 20.0, 25.0],
        })
        print_tbl(fs[:8], "SCALP+ — FINE TUNE (EMA + Bollinger + Stoch)")
        scalp_all += fs

    # ── Final ───────────────────────────────────────────────────────────
    machete_all.sort(key=lambda x: x['ret'], reverse=True)
    scalp_all.sort(key=lambda x: x['ret'], reverse=True)

    print("\n\n" + "="*70)
    print("  🏆 FINAL TOP 3 — MACHETTE")
    print("="*70)
    for i, r in enumerate(machete_all[:3]):
        print(f"  #{i+1} Ret={r['ret']:.2%} Sharpe={r['sharpe']:.2f} "
              f"Win={r['winrate']:.1%} PF={r['pf']:.2f} Trades={r['trades']} "
              f"MaxDD={r['maxdd']:.2%} Params={r['params']}")

    print("\n" + "="*70)
    print("  🏆 FINAL TOP 3 — SCALP+")
    print("="*70)
    for i, r in enumerate(scalp_all[:3]):
        print(f"  #{i+1} Ret={r['ret']:.2%} Sharpe={r['sharpe']:.2f} "
              f"Win={r['winrate']:.1%} PF={r['pf']:.2f} Trades={r['trades']} "
              f"MaxDD={r['maxdd']:.2%} Params={r['params']}")

    if machete_all and scalp_all:
        bm, bs = machete_all[0], scalp_all[0]
        print(f"\n  ⚖️  COMPARISON:")
        print(f"     Return: {'MACHETE' if bm['ret']>bs['ret'] else 'SCALP+'} wins "
              f"({max(bm['ret'],bs['ret']):.2%} vs {min(bm['ret'],bs['ret']):.2%})")
        print(f"     Sharpe: {'MACHETE' if bm['sharpe']>bs['sharpe'] else 'SCALP+'} wins "
              f"({max(bm['sharpe'],bs['sharpe']):.2f} vs {min(bm['sharpe'],bs['sharpe']):.2f})")
        print(f"     Trades: Machete={bm['trades']}  Scalp+={bs['trades']}")
        print(f"\n  📊 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} | "
              f"Data: {data.index[0].strftime('%m/%d')} → {data.index[-1].strftime('%m/%d')} ({len(data)} candles)")


if __name__ == "__main__":
    main()