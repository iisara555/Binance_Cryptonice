#!/usr/bin/env python3
"""
Machete V8b-Lite targeted param sweep — BTC/ETH 15m.
Compares Best WR vs Best Score configs.
"""
import sys, time
import numpy as np
import pandas as pd
sys.path.insert(0, str(_REPO))

# Load data
btc = pd.read_csv(str(_REPO / 'btc_15m_vibe.csv'), parse_dates=['timestamp'])
eth = pd.read_csv(str(_REPO / 'multi_pair_data/ETH_USDT_15m.csv'), parse_dates=['timestamp'])
for df in [btc, eth]:
    df.sort_values('timestamp', inplace=True)
    df.reset_index(drop=True, inplace=True)
print(f"BTC: {len(btc)} | ETH: {len(eth)}")

# ── Indicator precompute (fast pandas) ────────────────────────────────────
def compute_indicators(df):
    close = df['close'].values.astype(float)
    high  = df['high'].values.astype(float)
    low   = df['low'].values.astype(float)
    vol   = df['volume'].values.astype(float)
    n = len(close)
    period = 14

    # ADX
    tr = np.zeros(n); pdm = np.zeros(n); mdm = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i]-low[i], max(abs(high[i]-close[i-1]), abs(low[i]-close[i-1])))
        pdm[i] = max(high[i]-high[i-1], 0)
        mdm[i] = max(low[i-1]-low[i], 0)

    atr  = pd.Series(tr).rolling(period).mean().fillna(0).values
    di_p = 100 * pd.Series(pdm).rolling(period).mean().fillna(0).values / (atr + 1e-9)
    di_m = 100 * pd.Series(mdm).rolling(period).mean().fillna(0).values / (atr + 1e-9)
    dx   = 100 * np.abs(di_p - di_m) / (di_p + di_m + 1e-9)
    adx  = pd.Series(dx).rolling(period).mean().fillna(0).values

    vol_ma = pd.Series(vol).rolling(10).mean().fillna(0).values

    # RMI
    change = np.zeros(n); change[1:] = close[1:] - close[:-1]
    up = np.maximum(change, 0); down = np.maximum(-change, 0)
    up_ma = pd.Series(up).ewm(span=14, adjust=False).mean().values
    dn_ma = pd.Series(down).ewm(span=14, adjust=False).mean().values
    rmi = 100 * up_ma / (up_ma + dn_ma + 1e-9)

    # Fisher
    hl2 = (high + low) / 2
    m = pd.Series(hl2)
    vmax = m.rolling(10).max().shift(1).fillna(0.001)
    vmin = m.rolling(10).min().shift(1).fillna(0.001)
    xr = 2 * (m - vmin) / (vmax - vmin + 1e-9) - 1
    fish = 0.5 * np.log((1 + xr.clip(-0.999, 0.999)) / (1 - xr.clip(-0.999, 0.999))).fillna(0).values

    # TEMA cross
    def tema(s, p):
        e1 = pd.Series(s).ewm(span=p, adjust=False).mean().values
        e2 = pd.Series(e1).ewm(span=p, adjust=False).mean().values
        e3 = pd.Series(e2).ewm(span=p, adjust=False).mean().values
        return 3*e1 - 3*e2 + e3
    tf = tema(close, 9); ts = tema(close, 21)
    tema_sig = np.where(tf > ts, 1, -1)
    tema_cross_up = np.zeros(n, dtype=bool)
    tema_cross_up[1:] = (tema_sig[1:] == 1) & (tema_sig[:-1] != 1)

    # AO
    sma5  = pd.Series(hl2).rolling(5).mean().values
    sma34 = pd.Series(hl2).rolling(34).mean().values
    ao = sma5 - sma34

    # Ichimoku (tenkan > kijun)
    tenkan = (pd.Series(high).rolling(9).max().values + pd.Series(low).rolling(9).min().values) / 2
    kijun  = (pd.Series(high).rolling(26).max().values + pd.Series(low).rolling(26).min().values) / 2
    ichi_bull = (tenkan > kijun)

    # SSL
    ssl_period = 10
    sma_h = pd.Series(high).rolling(ssl_period).mean().values
    sma_l = pd.Series(low).rolling(ssl_period).mean().values
    ssl_up = (close > sma_h)

    # SR rolling min
    sr_level = pd.Series(close).rolling(50).min().fillna(close[0]).values

    return dict(close=close, adx=adx, vol=vol, vol_ma=vol_ma,
                rmi=rmi, fish=fish, tema_cross_up=tema_cross_up,
                ssl_up=ssl_up, sr_level=sr_level,
                ichi_bull=ichi_bull, ao=ao)

print("Computing indicators...")
t0 = time.time()
ta_btc = compute_indicators(btc)
ta_eth = compute_indicators(eth)
print(f"TA done in {time.time()-t0:.2f}s")

# ── Sim ─────────────────────────────────────────────────────────────────
def sim(df, ta, params):
    close = ta['close']; adx = ta['adx']; vol = ta['vol']
    vol_ma = ta['vol_ma']; rmi = ta['rmi']; fish = ta['fish']
    tema_cross_up = ta['tema_cross_up']; ssl_up = ta['ssl_up']
    ichi_bull = ta['ichi_bull']; ao = ta['ao']

    adx_th = params['adx_threshold']
    vol_th = params['vol_threshold']
    rmi_min = params['rmi_buy_min']
    mcb = params['min_confirmations_buy']
    n = len(close)

    in_pos=False; entry=0.0; trades=0; wins=0; win_amounts=[]

    for i in range(55, n):
        vol_ok = vol[i] >= vol_ma[i] * vol_th
        adx_ok = adx[i] > adx_th
        rmi_ok = rmi[i] >= rmi_min
        fish_bull = fish[i] > fish[i-1] and fish[i] > 0
        tema_ok = tema_cross_up[i]
        ssl_bull = ssl_up[i]
        ichi_ok = ichi_bull[i]

        bull = sum([vol_ok, adx_ok, rmi_ok, fish_bull, tema_ok, ssl_bull, ichi_ok])

        if not in_pos:
            if bull >= mcb and adx_ok and vol_ok:
                in_pos = True; entry = close[i]
        else:
            if not ssl_up[i] or ao[i] < 0:
                pnl = (close[i] - entry) / entry
                trades += 1
                if pnl > 0: wins += 1
                win_amounts.append(pnl)
                in_pos = False

    return {
        'trades': trades, 'wins': wins,
        'total_return': sum(win_amounts)*100 if win_amounts else 0.0,
        'win_rate': wins/trades if trades else 0.0,
        'avg_win': np.mean([w for w in win_amounts if w>0])*100 if [w for w in win_amounts if w>0] else 0.0,
        'avg_loss': np.mean([w for w in win_amounts if w<0])*100 if [w for w in win_amounts if w<0] else 0.0,
    }

def score_result(r):
    return r['total_return'] * 0.7 + r['win_rate'] * 100 * 0.3

# ── Targeted configs ──────────────────────────────────────────────────────
CONFIGS = [
    # Config A: current deployed (adx=20, vol=0.7, mcb=3)
    {'name': 'A (current)',   'adx_threshold': 20.0, 'vol_threshold': 0.7, 'rmi_buy_min': 52.0, 'min_confirmations_buy': 3},
    # Config B: higher ADX filter (more selective)
    {'name': 'B (hi-ADX)',    'adx_threshold': 25.0, 'vol_threshold': 0.7, 'rmi_buy_min': 52.0, 'min_confirmations_buy': 4},
    # Config C: lower ADX + lower vol (more sensitive)
    {'name': 'C (lo-ADX)',    'adx_threshold': 15.0, 'vol_threshold': 0.6, 'rmi_buy_min': 48.0, 'min_confirmations_buy': 3},
    # Config D: balanced medium
    {'name': 'D (balanced)',  'adx_threshold': 18.0, 'vol_threshold': 0.75,'rmi_buy_min': 50.0, 'min_confirmations_buy': 3},
    # Config E: aggressive (lower thresholds = more trades)
    {'name': 'E (aggressive)','adx_threshold': 12.0, 'vol_threshold': 0.5, 'rmi_buy_min': 45.0, 'min_confirmations_buy': 2},
]

def run_pair(name, df, ta):
    print(f"\n=== {name} ===")
    results = []
    for cfg in CONFIGS:
        t0 = time.time()
        r = sim(df, ta, cfg)
        r['name'] = cfg['name']
        r['config'] = cfg
        elapsed = time.time() - t0
        r['elapsed'] = elapsed
        results.append(r)
        print(f"  {cfg['name']:15s}: WR={r['win_rate']:.1%} Ret={r['total_return']:.2f}% Trades={r['trades']} | {elapsed:.2f}s")

    df_res = pd.DataFrame(results)
    df_res['score'] = df_res.apply(lambda x: score_result(x), axis=1)
    best_wr = df_res.loc[df_res['win_rate'].idxmax()]
    best_sc = df_res.loc[df_res['score'].idxmax()]
    return best_wr, best_sc, df_res

bw_btc, bs_btc, dr_btc = run_pair('BTC', btc, ta_btc)
bw_eth, bs_eth, dr_eth = run_pair('ETH', eth, ta_eth)

print("\n========== FINAL SUMMARY ==========")
for pair, bw, bs in [('BTC', bw_btc, bs_btc), ('ETH', bw_eth, bs_eth)]:
    print(f"\n{pair}:")
    print(f"  Best WR   : WR={bw['win_rate']:.1%} Ret={bw['total_return']:.2f}% Trades={bw['trades']} | {bw['name']}")
    print(f"  Best Score: WR={bs['win_rate']:.1%} Ret={bs['total_return']:.2f}% Trades={bs['trades']} | {bs['name']}")

    bw_ret = bw['total_return']
    bs_ret = bs['total_return']
    print(f"  → {'Best Score' if bs_ret >= bw_ret else 'Best WR'} กำไรดีกว่า (Ret: {max(bw_ret,bs_ret):.2f}% vs {min(bw_ret,bs_ret):.2f}%)")