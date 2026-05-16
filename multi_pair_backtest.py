#!/usr/bin/env python3
"""
Full backtest + optimization for all whitelist pairs.
Finds the BEST config per pair for both Machete and SimpleScalp+.
"""
import ccxt, pandas as pd, numpy as np, time, itertools, warnings, os
warnings.filterwarnings('ignore')
os.chdir('/root/Crypto_Sniper')

# ── Config ────────────────────────────────────────────────────────────────────
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'ADA/USDT', 'DOGE/USDT']
TIMEFRAME = '15m'
SINCE = '2025-11-01'
UNTIL  = '2026-05-13'
CAPITAL = 10000.0

# ── Fetch all pairs ───────────────────────────────────────────────────────────
DATA_DIR = '/root/Crypto_Sniper/multi_pair_data'
os.makedirs(DATA_DIR, exist_ok=True)

exchange = ccxt.binance()

def fetch_pair(symbol, timeframe, since, until):
    path = f"{DATA_DIR}/{symbol.replace('/','_')}_{timeframe}.csv"
    if os.path.exists(path):
        df = pd.read_csv(path, index_col='timestamp', parse_dates=True)
        print(f"  [CACHE] {symbol} → {len(df)} candles")
        return df
    since_ms = int(pd.Timestamp(since).timestamp() * 1000)
    until_ms = int(pd.Timestamp(until).timestamp() * 1000)
    candles = []
    cur = since_ms
    print(f"  Fetching {symbol} {timeframe}...", end=' ', flush=True)
    while cur < until_ms:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=cur, limit=1000)
            if not ohlcv: break
            candles.extend(ohlcv)
            cur = ohlcv[-1][0] + 1
            if len(ohlcv) < 1000: break
            time.sleep(exchange.rateLimit / 1000)
        except Exception as e:
            print(f"\n  ERROR {symbol}: {e}")
            break
    if candles:
        df = pd.DataFrame(candles, columns=['timestamp','open','high','low','close','volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.set_index('timestamp').sort_index()
        df.to_csv(path)
        print(f"→ {len(df)} candles ({df.index[0].strftime('%m/%d')}–{df.index[-1].strftime('%m/%d')})")
    return df

print("="*70)
print("  FETCHING PAIRS DATA")
print("="*70)
data = {}
for pair in PAIRS:
    df = fetch_pair(pair, TIMEFRAME, SINCE, UNTIL)
    if df is not None and len(df) > 100:
        data[pair] = df
    else:
        print(f"  SKIP {pair} (no data)")

print(f"\nLoaded {len(data)} pairs")

# ── Indicator library ────────────────────────────────────────────────────────
def ema(s, p): return s.ewm(span=p, adjust=False).mean()
def rsi_ta(s, p=14):
    d = s.diff(); g = d.clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    l = (-d).clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    return (100 - 100/(1+ g/l.replace(0,np.nan))).fillna(50).clip(0,100)
def atr_ta(h, l, c, p=14):
    tr = pd.concat([h-l, (c-h.shift(1)).abs(), (c-l.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/p, adjust=False).mean()
def macd_ta(c, f=12, s=26, sig=9):
    ml=ema(c,f)-ema(c,s); return ml, ema(ml,sig), ml-ema(ml,sig)
def stoch_ta(h, l, c, p=14):
    lo=l.rolling(p).min(); hi=h.rolling(p).max()
    k=100*(c-lo)/(hi-lo).replace(0,np.nan); return k, k.rolling(3).mean()
def adx_ta(h, l, c, p=14):
    tr=pd.concat([h-l,(c-h.shift(1)).abs(),(c-l.shift(1)).abs()],axis=1).max(axis=1)
    pdm=h.diff().clip(lower=0); mdm=(-l.diff()).clip(lower=0)
    pdm[mdm>pdm]=0; mdm[pdm>mdm]=0
    atr_v=tr.ewm(alpha=1/p,adjust=False).mean()
    pdi=100*pdm.ewm(alpha=1/p,adjust=False).mean()/atr_v.replace(0,np.nan)
    mdi=100*mdm.ewm(alpha=1/p,adjust=False).mean()/atr_v.replace(0,np.nan)
    dx=100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)
    return dx.ewm(alpha=1/p,adjust=False).mean()
def vol_ma(v, p=20, th=1.05):
    return (v > v.rolling(p).mean()*th).astype(int)
def fisher(h, l, p=10):
    med=(h+l)/2
    mn=med.expanding(p).min(); mx=med.expanding(p).max()
    norm=(2*(med-mn)/(mx-mn+1e-10)-1).clip(-0.999,0.999)
    fisher=0.5*np.log((1+norm)/(1-norm))
    return fisher.diff().apply(np.sign)
def tema_sig(c, f=9, s=21):
    ef=ema(ema(ema(c,f),f),f); es=ema(ema(ema(c,s),s),s)
    return (ef-es).apply(np.sign)
def ao_sig(hlc3, f=5, s=34):
    return (hlc3.rolling(f).mean()-hlc3.rolling(s).mean()).diff().apply(np.sign)
def rmi_ta(c, p=14, m=5):
    d=c-c.shift(m)
    up=d.clip(lower=0); dn=(-d).clip(lower=0)
    au=up.ewm(alpha=1/p,adjust=False).mean()
    ad=dn.ewm(alpha=1/p,adjust=False).mean()
    return (100-100/(1+au/ad.replace(0,np.nan))).fillna(50)
def hull_ma(s, p=16):
    h=p//2; sp=int(np.sqrt(p))
    if len(s)<p: return pd.Series(0, index=s.index)
    def wma(x):
        w=np.arange(1,len(x)+1); return np.dot(x,w)/w.sum()
    wh=s.rolling(h).apply(wma, raw=True); wf=s.rolling(p).apply(wma, raw=True)
    hull=2*wh-wf
    return hull.rolling(sp).apply(wma, raw=True) if len(hull)>=sp else pd.Series(0,index=s.index)
def sr_ch(h, l, lb=50):
    return l.rolling(lb).min(), h.rolling(lb).max()

# ── Strategy signal generators ─────────────────────────────────────────────────
def machete_signals(close, high, low, volume, params, precomputed):
    p = params
    c = precomputed
    atr_pct = c['atr'] / close * 100
    vc = atr_pct < p.get('atr_volatility_cap_pct', 4.0)
    sr_lo = c['ssl_up'].shift(2) * (1 + p.get('sr_proximity_pct',2)/100)
    sr_hi = c['ssl_dn'].shift(2) * (1 - 0.002)
    sr_ok = (close >= sr_lo.fillna(0)) & (close <= sr_hi.fillna(99999))
    conf = pd.DataFrame({
        'f':(c['fish']==1).fillna(False),
        't':(c['tema']>0).fillna(False),
        'a':(c['ao']>0).fillna(False),
        'ad':(c['adx']>p.get('adx_threshold',25)).fillna(False),
        'v':(c['vol']==1).fillna(False),
        'sl':c['ssl_up'].fillna(0)>c['ssl_dn'].fillna(0),
        'rm':(c['rmi']>=p.get('rmi_buy_min',52)).fillna(False),
    }).sum(axis=1)
    buy = (conf >= p.get('min_confirmations_buy',3)) & vc & sr_ok
    sell_conf = pd.DataFrame({
        'f':(c['fish']==-1).fillna(False),
        't':(c['tema']<0).fillna(False),
        'a':(c['ao']<0).fillna(False),
    }).sum(axis=1)
    sell = sell_conf >= p.get('min_confirmations_sell',2)
    return buy, sell

def scalp_signals(close, high, low, volume, params, precomputed):
    p = params
    c = precomputed
    c2 = pd.DataFrame({
        'h': c['hull'].notna() & (c['hull']>0),
        'e': (c['ema_f']>c['ema_s']).fillna(False),
        'r': ((c['rsi']>=p.get('rsi_buy_min',50))&(c['rsi']<=p.get('rsi_buy_max',70))).fillna(False),
        'a': (c['adx']>=p.get('adx_threshold',18)).fillna(False),
        'm': ((c['macd_l']>c['macd_s'])&(c['macd_h']>0)).fillna(False),
        's': ((c['stoch_k']>=p.get('stoch_buy_k_min',20))&(c['stoch_k']<=p.get('stoch_buy_k_max',80))&(c['stoch_k']>=c['stoch_d'])).fillna(False),
        'v': (close>=c['vwap'].fillna(close)).fillna(False),
        'vol':(c['vol']==1).fillna(False),
    }).sum(axis=1)
    buy = c2 >= p.get('min_confirmations_buy',5)
    cb = pd.DataFrame({
        'h':(c['hull']<0).fillna(False),
        'e':(c['ema_f']<c['ema_s']).fillna(False),
        'r':(c['rsi']<=p.get('rsi_sell_max',48)).fillna(True),
        'm':((c['macd_l']<c['macd_s'])&(c['macd_h']<0)).fillna(False),
        's':((c['stoch_k']>=p.get('stoch_sell_k_max',80))&(c['stoch_k']<=c['stoch_d'])).fillna(False),
        'v':(close<=c['vwap'].fillna(close)).fillna(True),
        'vol':(c['vol']==1).fillna(False),
    }).sum(axis=1)
    sell = cb >= p.get('min_confirmations_sell',4)
    return buy, sell

# ── Precompute indicators per pair ───────────────────────────────────────────
def precompute(df):
    close=df['close']; high=df['high']; low=df['low']; volume=df['volume']
    hlc3=(high+low+close)/3
    return {
        'atr': atr_ta(high,low,close,14),
        'fish': fisher(high,low,10),
        'tema': tema_sig(close,9,21),
        'ao': ao_sig(hlc3,5,34),
        'adx': adx_ta(high,low,close,14),
        'vol': vol_ma(volume,20,1.1),
        'rmi': rmi_ta(close,14,5),
        'ssl_up': sr_ch(high,low,10)[0],
        'ssl_dn': sr_ch(high,low,10)[1],
        'hull': hull_ma(close,16),
        'ema_f': ema(close,9),
        'ema_s': ema(close,21),
        'rsi': rsi_ta(close,14),
        'macd_l': macd_ta(close,12,26,9)[0],
        'macd_s': macd_ta(close,12,26,9)[1],
        'macd_h': macd_ta(close,12,26,9)[2],
        'stoch_k': stoch_ta(high,low,close,14)[0],
        'stoch_d': stoch_ta(high,low,close,14)[1],
        'vwap': (hlc3*volume).rolling(20).sum()/volume.rolling(20).sum(),
        'close': close, 'high': high, 'low': low, 'volume': volume,
    }

# ── Simulator ─────────────────────────────────────────────────────────────────
def sim(buy, sell, px_arr, cap=CAPITAL):
    port, pos, wins, losses, cons, maxc = cap, 0.0, 0, 0, 0, 0
    eq=[cap]; pnl=[]
    for i in range(len(px_arr)):
        v = pos*px_arr[i] if pos>0 else port
        eq.append(v)
        if buy[i] and pos==0:
            pos=port/px_arr[i]; pe=port
        elif sell[i] and pos>0:
            ev=pos*px_arr[i]; p=(ev-pe)/pe; pnl.append(p)
            if p>=0: wins+=1; cons=0
            else: losses+=1; cons+=1; maxc=max(maxc,cons)
            port=ev; pos=0
    if pos>0: port=pos*px_arr[-1]
    ret=(port-cap)/cap; n=wins+losses; wr=wins/n if n else 0
    gp=sum(x for x in pnl if x>0); gl=sum(abs(x) for x in pnl if x<0)
    pf=gp/gl if gl>0 else (999 if gp>0 else 0)
    r=np.diff(eq)/np.clip(eq[:-1],1e-10,None); r=r[np.isfinite(r)]
    sh=(np.mean(r)/np.std(r)*np.sqrt(252*96)) if len(r)>1 and np.std(r)>0 else 0
    dd=max((p-e)/p for p,e in zip(np.maximum.accumulate(eq),eq)) if eq else 0
    return dict(ret=ret,sharpe=sh,maxdd=dd,winrate=wr,pf=pf,
                trades=n,wins=wins,losses=losses,max_cons=maxc,final_eq=port)

# ── Parameter grids ───────────────────────────────────────────────────────────
MACHETE_GRID = {
    'adx_threshold': [8.0, 12.0, 15.0, 18.0, 20.0],
    'min_confirmations_buy': [2, 3],
    'rmi_buy_min': [40.0, 45.0, 48.0, 52.0],
    'sr_proximity_pct': [0.5, 1.0, 1.5, 2.0],
    'vol_threshold': [0.7, 0.8, 0.9, 1.0],
}

SCALP_GRID = {
    'min_confirmations_buy': [3, 4, 5],
    'adx_threshold': [14.0, 18.0, 20.0, 25.0],
    'rsi_buy_min': [38.0, 42.0, 45.0, 48.0],
    'ema_fast': [7, 8, 9, 10, 12],
    'atr_multiplier': [0.8, 1.0, 1.2, 1.5],
}

# ── Run backtest ──────────────────────────────────────────────────────────────
def run_bt(pair, df, strat_fn, grid, base_params, label):
    t0 = time.time()
    close=df['close']; high=df['high']; low=df['low']; volume=df['volume']
    px = close.values
    c = precompute(df)

    results = []
    keys = list(grid.keys())
    vals = list(grid.values())
    total = 1
    for v in vals: total *= len(v)

    for i, combo in enumerate(itertools.product(*vals)):
        cfg = {**base_params, **dict(zip(keys, combo))}
        try:
            buy, sell = strat_fn(close, high, low, volume, cfg, c)
            r = sim(buy.values, sell.values, px)
            r['params'] = '; '.join(f"{k}={v}" for k,v in zip(keys, combo))
            results.append(r)
        except Exception as e:
            pass

    results.sort(key=lambda x: (x['ret'], x['sharpe']), reverse=True)
    best = results[0] if results else None
    elapsed = time.time()-t0
    print(f"  [{label}] {len(results)} combos in {elapsed:.1f}s → best ret={best['ret']:.2%} win={best['winrate']:.1%} pf={best['pf']:.2f} tr={best['trades']}")
    return results, best

# ── MAIN ─────────────────────────────────────────────────────────────────────
print("\n" + "="*70)
print("  FULL BACKTEST — ALL WHITELIST PAIRS")
print("="*70)

all_pair_results = {}
pair_summary = []

for pair, df in data.items():
    print(f"\n{'='*70}")
    print(f"  📊 {pair}")
    print(f"{'='*70}")
    n = len(df)
    print(f"  {n} candles | {df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}")

    # Machete
    m_base = dict(
        name='m', enabled=True, fisher_period=10, tema_fast=9, tema_slow=21,
        ao_fast=5, ao_slow=34, adx_period=14,
        atr_period=14, atr_multiplier=1.5, risk_reward=2.0,
        min_buy_confidence=0.50, min_sell_confidence=0.50,
        min_confirmations_sell=2,
        enable_relaxed_confirmation=True, relaxed_requires_adx_and_volume=True,
        relaxed_confirmation_delta=1, ssl_period=10,
        rmi_period=14, rmi_momentum=5,
        atr_volatility_cap_pct=4.0,
        sr_lookback=50,
    )
    m_results, m_best = run_bt(pair, df, machete_signals, MACHETE_GRID, m_base, "MACHETE")

    # Scalp+
    s_base = dict(
        name='s', enabled=True, hull_period=16, ema_slow=21,
        rsi_period=14, rsi_buy_max=70.0, rsi_sell_max=48.0,
        macd_fast=12, macd_slow=26, macd_signal=9,
        stoch_period=14, stoch_buy_k_max=80.0, stoch_sell_k_max=80.0,
        volume_period=20, atr_period=14,
        min_sell_confidence=0.55, min_confirmations_sell=4,
        min_buy_confidence=0.50,
    )
    s_results, s_best = run_bt(pair, df, scalp_signals, SCALP_GRID, s_base, "SCALP+")

    all_pair_results[pair] = {'machete': m_results, 'scalp': s_results, 'm_best': m_best, 's_best': s_best}

    winner = m_best if m_best['ret'] > s_best['ret'] else s_best
    strat  = "MACHETE" if winner is m_best else "SCALP+"
    pair_summary.append({
        'pair': pair,
        'best_strat': strat,
        'ret': winner['ret'],
        'sharpe': winner['sharpe'],
        'winrate': winner['winrate'],
        'pf': winner['pf'],
        'trades': winner['trades'],
        'maxdd': winner['maxdd'],
        'm_ret': m_best['ret'],
        'm_win': m_best['winrate'],
        'm_pf': m_best['pf'],
        's_ret': s_best['ret'],
        's_win': s_best['winrate'],
        's_pf': s_best['pf'],
        'm_params': m_best['params'],
        's_params': s_best['params'],
    })

# ═══════════════════════════════════════════════════════════════════════════════
# FINAL REPORT
# ═══════════════════════════════════════════════════════════════════════════════
print("\n\n" + "="*70)
print("  🏆 PER-PAIR BEST CONFIG SUMMARY")
print("="*70)

for ps in pair_summary:
    m = ps['m_params']
    s = ps['s_params']
    print(f"\n{'─'*70}")
    print(f"  📊 {ps['pair']}")
    print(f"{'─'*70}")
    print(f"  MACHETE  | Ret={ps['m_ret']:.2%} | Win={ps['m_win']:.1%} | PF={ps['m_pf']:.2f} | {ps['trades']} trades")
    print(f"           | {m}")
    print(f"  SCALP+   | Ret={ps['s_ret']:.2%} | Win={ps['s_win']:.1%} | PF={ps['s_pf']:.2f}")
    print(f"           | {s}")
    print(f"  🏅 BEST: {ps['best_strat']} (Ret={ps['ret']:.2%}, Win={ps['winrate']:.1%}, PF={ps['pf']:.2f})")

print("\n\n" + "="*70)
print("  📋 BOT_CONFIG.YAML — RECOMMENDED PER-PAIR SETTINGS")
print("="*70)
for ps in pair_summary:
    pair = ps['pair'].replace('/USDT','').replace('/','_')
    if ps['best_strat'] == 'MACHETE':
        cfg = dict(x.split('=') for x in ps['m_params'].split('; '))
        cfg = {k: float(v) if '.' in v else int(v) for k,v in cfg.items()}
        print(f"\n  # {ps['pair']} → MACHETE")
        for k, v in sorted(cfg.items()):
            print(f"  {pair}_machete_{k}: {v}")
    else:
        cfg = dict(x.split('=') for x in ps['s_params'].split('; '))
        cfg = {k: float(v) if '.' in v else int(v) for k,v in cfg.items()}
        print(f"\n  # {ps['pair']} → SCALP+")
        for k, v in sorted(cfg.items()):
            print(f"  {pair}_scalp_{k}: {v}")

print(f"\n  ⏱️ {time.strftime('%H:%M:%S')}")