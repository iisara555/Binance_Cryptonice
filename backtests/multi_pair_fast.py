#!/usr/bin/env python3
"""
Ultra-fast multi-pair backtest: vectorized sim + small grid.
Last 90 candles (~3 months) per pair, lean grid.
"""
import ccxt, pandas as pd, numpy as np, time, itertools, warnings, os
warnings.filterwarnings('ignore')
os.chdir(_REPO)

PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'ADA/USDT', 'DOGE/USDT']
TIMEFRAME = '15m'
CANDLES_LIMIT = 2000  # last 2000 candles ≈ 20 days (enough for 15m)
CAPITAL = 10000.0

DATA_DIR = str(_REPO / 'multi_pair_data')
os.makedirs(DATA_DIR, exist_ok=True)

exchange = ccxt.binance()

def fetch_pair(symbol):
    path = f"{DATA_DIR}/{symbol.replace('/','_')}_{TIMEFRAME}.csv"
    if os.path.exists(path):
        df = pd.read_csv(path, index_col='timestamp', parse_dates=True)
        return df.tail(CANDLES_LIMIT)
    since = int(pd.Timestamp('2026-02-01').timestamp() * 1000)
    candles = []
    cur = since
    print(f"  Fetching {symbol}...", end=' ', flush=True)
    while cur < time.time()*1000:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, since=cur, limit=1000)
            if not ohlcv: break
            candles.extend(ohlcv)
            cur = ohlcv[-1][0] + 1
            if len(ohlcv) < 1000: break
            time.sleep(0.2)
        except Exception as e:
            print(f"ERR {e}"); break
    if candles:
        df = pd.DataFrame(candles, columns=['timestamp','open','high','low','close','volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.set_index('timestamp').sort_index()
        df.to_csv(path)
        print(f"→ {len(df)}")
        return df.tail(CANDLES_LIMIT)
    return None

print("Fetching pairs...")
data = {}
for pair in PAIRS:
    df = fetch_pair(pair)
    if df is not None: data[pair] = df
print(f"Loaded {len(data)} pairs\n")

# ── Indicator library ─────────────────────────────────────────────────────────
def ema(s, p): return s.ewm(span=p, adjust=False).mean()
def rsi_ta(s, p=14):
    d=s.diff(); g=d.clip(lower=0).ewm(alpha=1/p,adjust=False).mean()
    l=(-d).clip(lower=0).ewm(alpha=1/p,adjust=False).mean()
    return (100-100/(1+g/l.replace(0,np.nan))).fillna(50).clip(0,100)
def atr_ta(h,l,c,p=14):
    tr=pd.concat([h-l,(c-h.shift(1)).abs(),(c-l.shift(1)).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/p,adjust=False).mean()
def macd_ta(c,f=12,s=26,sig=9):
    ml=ema(c,f)-ema(c,s); return ml, ema(ml,sig), ml-ema(ml,sig)
def stoch_ta(h,l,c,p=14):
    lo=l.rolling(p).min(); hi=h.rolling(p).max()
    k=100*(c-lo)/(hi-lo).replace(0,np.nan); return k, k.rolling(3).mean()
def adx_ta(h,l,c,p=14):
    tr=pd.concat([h-l,(c-h.shift(1)).abs(),(c-l.shift(1)).abs()],axis=1).max(axis=1)
    pdm=h.diff().clip(lower=0); mdm=(-l.diff()).clip(lower=0)
    pdm[mdm>pdm]=0; mdm[pdm>mdm]=0
    av=tr.ewm(alpha=1/p,adjust=False).mean()
    pdi=100*pdm.ewm(alpha=1/p,adjust=False).mean()/av.replace(0,np.nan)
    mdi=100*mdm.ewm(alpha=1/p,adjust=False).mean()/av.replace(0,np.nan)
    dx=100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)
    return dx.ewm(alpha=1/p,adjust=False).mean()
def vol_ma(v,p=20,th=1.05): return (v>v.rolling(p).mean()*th).astype(int)
def fisher(h,l,p=10):
    med=(h+l)/2; mn=med.expanding(p).min(); mx=med.expanding(p).max()
    norm=(2*(med-mn)/(mx-mn+1e-10)-1).clip(-0.999,0.999)
    return (0.5*np.log((1+norm)/(1-norm))).diff().apply(np.sign)
def tema_sig(c,f=9,s=21):
    ef=ema(ema(ema(c,f),f),f); es=ema(ema(ema(c,s),s),s)
    return (ef-es).apply(np.sign)
def ao_sig(hlc3,f=5,s=34):
    return (hlc3.rolling(f).mean()-hlc3.rolling(s).mean()).diff().apply(np.sign)
def rmi_ta(c,p=14,m=5):
    d=c-c.shift(m); up=d.clip(lower=0); dn=(-d).clip(lower=0)
    au=up.ewm(alpha=1/p,adjust=False).mean(); ad=dn.ewm(alpha=1/p,adjust=False).mean()
    return (100-100/(1+au/ad.replace(0,np.nan))).fillna(50)
def hull_ma(s,p=16):
    h=p//2; sp=int(np.sqrt(p))
    if len(s)<p: return pd.Series(0,index=s.index)
    def wma(x): w=np.arange(1,len(x)+1); return np.dot(x,w)/w.sum()
    wh=s.rolling(h).apply(wma,raw=True); wf=s.rolling(p).apply(wma,raw=True)
    hull=2*wh-wf
    return hull.rolling(sp).apply(wma,raw=True) if len(hull)>=sp else pd.Series(0,index=s.index)
def sr_ch(h,l,lb=50): return l.rolling(lb).min(), h.rolling(lb).max()

def precompute(df):
    close=df['close']; high=df['high']; low=df['low']; volume=df['volume']
    hlc3=(high+low+close)/3
    macd_l, macd_s, macd_h = macd_ta(close,12,26,9)
    stoch_k, stoch_d = stoch_ta(high,low,close,14)
    ssl_up, ssl_dn = sr_ch(high,low,10)
    vwap = (hlc3*volume).rolling(20).sum()/volume.rolling(20).sum()
    return dict(
        atr=atr_ta(high,low,close,14),
        fish=fisher(high,low,10), tema=tema_sig(close,9,21),
        ao=ao_sig(hlc3,5,34), adx=adx_ta(high,low,close,14),
        vol=vol_ma(volume,20,1.1), rmi=rmi_ta(close,14,5),
        ssl_up=ssl_up, ssl_dn=ssl_dn,
        hull=hull_ma(close,16), ema_f=ema(close,9), ema_s=ema(close,21),
        rsi=rsi_ta(close,14),
        macd_l=macd_l, macd_s=macd_s, macd_h=macd_h,
        stoch_k=stoch_k, stoch_d=stoch_d,
        vwap=vwap, close=close, high=high, low=low, volume=volume,
    )

# ── Fast numba-style simulator (pure Python but loop over bool arrays) ─────────
def sim_fast(buy, sell, px):
    """Fast scalar sim - buy/sell are bool arrays, px is float array."""
    n = len(px)
    cash = CAPITAL; assets = 0.0
    wins=losses=cons=maxc=0; pnl=[]
    for i in range(n):
        if buy[i] and assets == 0:
            assets = cash / px[i]   # buy with full cash
            entry_val = cash        # portfolio value at entry
            cash = 0.0
        elif sell[i] and assets > 0:
            exit_val = assets * px[i]
            p = (exit_val - entry_val) / entry_val
            pnl.append(p)
            if p >= 0: wins += 1; cons = 0
            else: losses += 1; cons += 1; maxc = max(maxc, cons)
            cash = exit_val; assets = 0.0
    final = cash + assets * px[-1] if assets > 0 else cash
    ret = (final - CAPITAL) / CAPITAL
    n2 = wins + losses
    wr = wins / n2 if n2 else 0
    gp = sum(x for x in pnl if x > 0); gl = sum(abs(x) for x in pnl if x < 0)
    pf = gp / gl if gl > 0 else (999 if gp > 0 else 0)
    return ret, wr, pf, n2, wins, losses, maxc

# ── Machete ─────────────────────────────────────────────────────────────────
def machete_signals(c, p):
    close=c['close']
    atr_pct=c['atr']/close*100
    vc=(atr_pct<p.get('atr_volatility_cap_pct',4.0)).values
    sr_lo=c['ssl_up'].shift(2).fillna(0)*(1+p.get('sr_proximity_pct',2)/100)
    sr_hi=c['ssl_dn'].shift(2).fillna(99999)*(1-0.002)
    sr_ok=((close>=sr_lo)&(close<=sr_hi)).values
    fish=(c['fish']==1).fillna(False).values
    tema=(c['tema']>0).fillna(False).values
    ao=(c['ao']>0).fillna(False).values
    adx=(c['adx']>p.get('adx_threshold',25)).fillna(False).values
    vol=(c['vol']==1).fillna(False).values
    ssl=(c['ssl_up'].fillna(0)>c['ssl_dn'].fillna(0)).values
    rmi=(c['rmi']>=p.get('rmi_buy_min',52)).fillna(False).values
    conf = fish.astype(int)+tema.astype(int)+ao.astype(int)+adx.astype(int)+vol.astype(int)+ssl.astype(int)+rmi.astype(int)
    min_conf = p.get('min_confirmations_buy',3)
    buy = (conf>=min_conf) & vc & sr_ok
    sell = (c['fish']==-1).fillna(False).astype(int)+(c['tema']<0).fillna(False).astype(int)+(c['ao']<0).fillna(False).astype(int)
    sell = sell.values >= p.get('min_confirmations_sell',2)
    return buy, sell

# ── Scalp+ ─────────────────────────────────────────────────────────────────
def scalp_signals(c, p):
    close=c['close'].values
    hull_ok=c['hull'].notna().values
    h=c['hull'].values>0
    e=c['ema_f'].values>c['ema_s'].values
    r=(c['rsi'].values>=p.get('rsi_buy_min',50))&(c['rsi'].values<=p.get('rsi_buy_max',70))
    a=c['adx'].values>=p.get('adx_threshold',18)
    m=(c['macd_l'].values>c['macd_s'].values)&(c['macd_h'].values>0)
    sk=c['stoch_k'].values; sd=c['stoch_d'].values
    s=(sk>=p.get('stoch_buy_k_min',20))&(sk<=p.get('stoch_buy_k_max',80))&(sk>=sd)
    vwap_v=c['vwap'].fillna(0).values
    v=close>=vwap_v
    vol=c['vol'].values==1
    conf = (h&hull_ok).astype(int)+e.astype(int)+r.astype(int)+a.astype(int)+m.astype(int)+s.astype(int)+v.astype(int)+vol.astype(int)
    buy = conf >= p.get('min_confirmations_buy',5)
    # Sell
    h2=c['hull'].values<0; e2=c['ema_f'].values<c['ema_s'].values
    r2=c['rsi'].values<=p.get('rsi_sell_max',48)
    m2=(c['macd_l'].values<c['macd_s'].values)&(c['macd_h'].values<0)
    s2=(sk>=p.get('stoch_sell_k_max',80))&(sk<=sd)
    v2=close<=vwap_v
    sell_conf = h2.astype(int)+e2.astype(int)+r2.astype(int)+m2.astype(int)+s2.astype(int)+v2.astype(int)+vol.astype(int)
    sell = sell_conf >= p.get('min_confirmations_sell',4)
    return buy, sell

# ── Parameter grids ──────────────────────────────────────────────────────────
M_GRID = {
    'adx_threshold': [8.0, 15.0, 20.0, 25.0],
    'min_confirmations_buy': [2, 3],
    'rmi_buy_min': [40.0, 48.0, 52.0, 58.0],
    'sr_proximity_pct': [0.5, 1.0, 1.5, 2.0],
    'vol_threshold': [0.7, 0.9, 1.1],
}

S_GRID = {
    'min_confirmations_buy': [3, 4, 5],
    'adx_threshold': [14.0, 18.0, 22.0],
    'rsi_buy_min': [38.0, 42.0, 46.0, 50.0],
    'ema_fast': [7, 9, 11],
}

MACHETE_BASE = dict(
    name='m', enabled=True, fisher_period=10, tema_fast=9, tema_slow=21,
    ao_fast=5, ao_slow=34, adx_period=14, atr_period=14, atr_multiplier=1.5,
    risk_reward=2.0, min_buy_confidence=0.50, min_sell_confidence=0.50,
    min_confirmations_sell=2, enable_relaxed_confirmation=True,
    relaxed_requires_adx_and_volume=True, relaxed_confirmation_delta=1,
    ssl_period=10, rmi_period=14, rmi_momentum=5,
    atr_volatility_cap_pct=4.0, sr_lookback=50,
)

SCALP_BASE = dict(
    name='s', enabled=True, hull_period=16, ema_slow=21,
    rsi_period=14, rsi_buy_max=70.0, rsi_sell_max=48.0,
    macd_fast=12, macd_slow=26, macd_signal=9,
    stoch_period=14, stoch_buy_k_max=80.0, stoch_sell_max=80.0,
    volume_period=20, atr_period=14,
    min_sell_confidence=0.55, min_confirmations_sell=4, min_buy_confidence=0.50,
)

def run_grid(pair, c, px, strat_fn, grid, base, label):
    keys=list(grid.keys()); vals=list(grid.values())
    best=None; best_params=''
    for combo in itertools.product(*vals):
        cfg={**base,**dict(zip(keys,combo))}
        try:
            buy,sell=strat_fn(c,cfg)
            ret,wr,pf,n2,wins,losses,maxc=sim_fast(buy,sell,px)
            if n2>0 and (best is None or ret>best[0]):
                best=(ret,wr,pf,n2,wins,losses,maxc)
                best_params=';'.join(f"{k}={v}" for k,v in zip(keys,combo))
        except: pass
    if best:
        print(f"  [{label}] {len(list(itertools.product(*vals)))} combos → ret={best[0]:.2%} win={best[1]:.1%} pf={best[2]:.2f} tr={best[3]}")
    else:
        print(f"  [{label}] NO VALID RESULTS")
    return best, best_params

# ── MAIN ──────────────────────────────────────────────────────────────────────
print("="*70)
print("  FULL BACKTEST — ALL 5 PAIRS (last ~2000 candles each)")
print("="*70)

summary = []
for pair, df in data.items():
    print(f"\n📊 {pair} | {len(df)} candles {df.index[0].strftime('%m/%d')}→{df.index[-1].strftime('%m/%d')}")
    c = precompute(df)
    px = c['close'].values

    m_best, m_params = run_grid(pair, c, px, machete_signals, M_GRID, MACHETE_BASE, "MACHETE")
    s_best, s_params = run_grid(pair, c, px, scalp_signals, S_GRID, SCALP_BASE, "SCALP+")

    if m_best and s_best:
        winner = "MACHETE" if m_best[0] > s_best[0] else "SCALP+"
        wb = m_best if winner=="MACHETE" else s_best
        wp = m_params if winner=="MACHETE" else s_params
        summary.append({
            'pair': pair,
            'winner': winner,
            'ret': wb[0], 'winrate': wb[1], 'pf': wb[2],
            'trades': wb[3], 'wins': wb[4], 'losses': wb[5],
            'm_ret': m_best[0], 'm_win': m_best[1], 'm_pf': m_best[2], 'm_tr': m_best[3],
            's_ret': s_best[0], 's_win': s_best[1], 's_pf': s_best[2], 's_tr': s_best[3],
            'm_params': m_params, 's_params': s_params,
        })
    elif m_best:
        summary.append({'pair': pair,'winner':'MACHETE','ret':m_best[0],'winrate':m_best[1],'pf':m_best[2],
                        'trades':m_best[3],'wins':m_best[4],'losses':m_best[5],
                        'm_ret':m_best[0],'m_win':m_best[1],'m_pf':m_best[2],'m_tr':m_best[3],
                        's_ret':None,'s_win':None,'s_pf':None,'s_tr':None,'m_params':m_params,'s_params':''})
    elif s_best:
        summary.append({'pair': pair,'winner':'SCALP+','ret':s_best[0],'winrate':s_best[1],'pf':s_best[2],
                        'trades':s_best[3],'wins':s_best[4],'losses':s_best[5],
                        'm_ret':None,'m_win':None,'m_pf':None,'m_tr':None,
                        's_ret':s_best[0],'s_win':s_best[1],'s_pf':s_best[2],'s_tr':s_best[3],'m_params':'','s_params':s_params})

print("\n\n" + "="*70)
print("  🏆 FINAL RESULTS — BEST CONFIG PER PAIR")
print("="*70)

for s in summary:
    print(f"\n{'─'*65}")
    print(f"  📊 {s['pair']} → 🏅 {s['winner']}")
    print(f"{'─'*65}")
    if s.get('m_ret') is not None:
        print(f"  MACHETE  | Ret={s['m_ret']:.2%} | Win={s['m_win']:.1%} | PF={s['m_pf']:.2f} | {s['m_tr']} trades | {s['wins']}W/{s['losses']}L")
        print(f"           | {s['m_params']}")
    if s.get('s_ret') is not None:
        print(f"  SCALP+   | Ret={s['s_ret']:.2%} | Win={s['s_win']:.1%} | PF={s['s_pf']:.2f} | {s.get('s_tr','?')} trades")
        print(f"           | {s['s_params']}")
    print(f"  🏅 WINNER | Ret={s['ret']:.2%} | Win={s['winrate']:.1%} | PF={s['pf']:.2f} | {s['trades']} trades ({s['wins']}W/{s['losses']}L)")

print(f"\n  ⏱️ {time.strftime('%H:%M:%S')}")