#!/usr/bin/env python3
"""Fast backtest - fixed Fisher, minimal combos."""
import sys, os, itertools, time
os.chdir('/root/Crypto_Sniper')
sys.path.insert(0, '/root/Crypto_Sniper')
import warnings; warnings.filterwarnings('ignore')
import numpy as np, pandas as pd

raw = pd.read_csv('/root/Crypto_Sniper/backtest_data_15m.csv', index_col='timestamp', parse_dates=True)
N = min(200, len(raw))
data = raw.tail(N).copy()
print(f"[DATA] {len(data)} candles: {data.index[0].strftime('%m/%d %H:%M')} → {data.index[-1].strftime('%m/%d %H:%M')}")

close = data['close']; high = data['high']; low = data['low']; volume = data['volume']
hlc3 = (high + low + close) / 3

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
    """Fisher Transform - avoid rolling.apply, use expanding window."""
    med = (h+l)/2
    # Use expanding min/max for simplicity (faster)
    mn = med.expanding(p).min()
    mx = med.expanding(p).max()
    norm = 2*(med-mn)/(mx-mn+1e-10) - 1
    norm = norm.clip(-0.999, 0.999)
    fisher = 0.5 * np.log((1+norm)/(1-norm))
    return fisher.diff().apply(np.sign)
def tema_sig(c, f=9, s=21):
    ef = ema(ema(ema(c,f),f),f)
    es = ema(ema(ema(c,s),s),s)
    return (ef - es).apply(np.sign)
def ao_sig(hlc3, f=5, s=34):
    sma_f = hlc3.rolling(f).mean(); sma_s = hlc3.rolling(s).mean()
    return (sma_f - sma_s).diff().apply(np.sign)
def rmi_ta(c, p=14, m=5):
    d = c - c.shift(m); up = d.clip(lower=0); dn = (-d).clip(lower=0)
    au = up.ewm(alpha=1/p, adjust=False).mean()
    ad = dn.ewm(alpha=1/p, adjust=False).mean()
    return (100 - 100/(1+au/ad.replace(0,np.nan))).fillna(50)
def hull_ma(s, p=16):
    h = p//2; sp = int(np.sqrt(p))
    wh = s.rolling(h).apply(lambda x: np.dot(x,range(1,len(x)+1))/sum(range(1,len(x)+1)), raw=True) if len(s)>=h else s
    wf = s.rolling(p).apply(lambda x: np.dot(x,range(1,len(x)+1))/sum(range(1,len(x)+1)), raw=True) if len(s)>=p else s
    hull = 2*wh - wf
    return hull.rolling(sp).apply(lambda x: np.dot(x,range(1,len(x)+1))/sum(range(1,len(x)+1)), raw=True) if len(hull)>=sp else hull
def sr_ch(h, l, lb=50):
    return l.rolling(lb).min(), h.rolling(lb).max()

# Precompute common indicators
print("⏳ Precomputing indicators...", end=' ', flush=True)
atr_v = atr_ta(high, low, close, 14)
print("done")

def machete_buy_sell(params):
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
    
    # SR proximity (use shifted to avoid lookahead)
    sr_lo = ssl_up.shift(2) * (1 + p.get('sr_proximity_pct',2)/100)
    sr_hi = ssl_dn.shift(2) * (1 - 0.002)
    sr_ok = (close >= sr_lo.fillna(0)) & (close <= sr_hi.fillna(99999))
    
    # Bull confirmations
    c = pd.DataFrame({
        'f':(fish==1).fillna(False), 't':(tema>0).fillna(False),
        'a':(ao>0).fillna(False), 'ad':(adx>p.get('adx_threshold',25)).fillna(False),
        'v':(v_ok==1).fillna(False), 'sl':ssl_up.fillna(0)>ssl_dn.fillna(0),
        'rm':(rmi>=p.get('rmi_buy_min',52)).fillna(False),
    })
    cnt = c.sum(axis=1)
    buy = (cnt >= p.get('min_confirmations_buy',3)) & vc & sr_ok
    
    # Bear for sell
    cb = pd.DataFrame({'f':(fish==-1).fillna(False),'t':(tema<0).fillna(False),'a':(ao<0).fillna(False)})
    sell = cb.sum(axis=1) >= p.get('min_confirmations_sell',2)
    return buy, sell


def scalp_buy_sell(params):
    p = params
    h = hull_ma(close, p.get('hull_period',16))
    ef = ema(close, p.get('ema_fast',9))
    es = ema(close, p.get('ema_slow',21))
    rs = rsi_ta(close, p.get('rsi_period',14))
    ad = adx_ta(high, low, close, p.get('adx_period',14))
    ml,ms,mh = macd_ta(close, p.get('macd_fast',12), p.get('macd_slow',26), p.get('macd_signal',9))
    sk,sd = stoch_ta(high, low, close, p.get('stoch_period',14))
    v_ok = vol_ma(volume, p.get('volume_period',20), p.get('volume_threshold',1.05))
    vwap = (hlc3*volume).rolling(p.get('volume_period',20)).sum()/volume.rolling(p.get('volume_period',20)).sum()
    
    c = pd.DataFrame({
        'h':(h>0).fillna(False) if h.notna().any() else False,
        'e':(ef>es).fillna(False),
        'r':((rs>=p.get('rsi_buy_min',50))&(rs<=p.get('rsi_buy_max',70))).fillna(False),
        'a':(ad>=p.get('adx_threshold',18)).fillna(False),
        'm':((ml>ms)&(mh>0)).fillna(False),
        's':((sk>=p.get('stoch_buy_k_min',20))&(sk<=p.get('stoch_buy_k_max',80))&(sk>=sd)).fillna(False),
        'v':(close>=vwap.fillna(close)).fillna(False),
        'vol':(v_ok==1).fillna(False),
    })
    cnt = c.sum(axis=1)
    buy = cnt >= p.get('min_confirmations_buy',5)
    
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
    port, pos, entry, wins, losses, cons, maxc = cap, 0.0, 0.0, 0, 0, 0, 0
    eq = [cap]; pnl = []; entries = 0
    # Convert to numpy for safe positional indexing
    px_arr = px.values if hasattr(px, 'values') else np.array(px)
    buy_arr = buy.values if hasattr(buy, 'values') else np.array(buy)
    sell_arr = sell.values if hasattr(sell, 'values') else np.array(sell)
    for i in range(len(px_arr)):
        v = pos*px_arr[i] if pos>0 else port
        eq.append(v)
        if buy_arr[i] and pos==0:
            pos = port/px_arr[i]; entry = px_arr[i]; pe = port
        elif sell_arr[i] and pos>0:
            ev = pos*px_arr[i]; p = (ev-pe)/pe; pnl.append(p)
            if p>=0: wins+=1; cons=0
            else: losses+=1; cons+=1; maxc=max(maxc,cons)
            port=ev; pos=0; entries+=1
    if pos>0: port = pos*px_arr[-1]
    ret = (port-cap)/cap; n=wins+losses; wr=wins/n if n else 0
    gp=sum(x for x in pnl if x>0); gl=sum(abs(x) for x in pnl if x<0)
    pf=gp/gl if gl>0 else (999 if gp>0 else 0)
    r=np.diff(eq)/np.clip(eq[:-1],1e-10,None); r=r[np.isfinite(r)]
    sh=(np.mean(r)/np.std(r)*np.sqrt(252*96)) if len(r)>1 and np.std(r)>0 else 0
    dd=max((p-e)/p for p,e in zip(np.maximum.accumulate(eq),eq)) if eq else 0
    return dict(ret=ret,sharpe=sh,maxdd=dd,winrate=wr,pf=pf,
                trades=n,wins=wins,losses=losses,max_cons=maxc,final_eq=port,entries=entries)

def show(t, rows):
    print(f"\n{'─'*82}\n  {t}\n{'─'*82}")
    print(f"{'#':<4}{'Ret':>8}{'Sharpe':>8}{'Win%':>7}{'PF':>7}{'Tr':>5}{'MaxDD':>8}")
    print(f"{'─'*82}")
    for i,r in enumerate(rows):
        print(f"{i+1:<4}{r['ret']:>7.2%}{r['sharpe']:>8.2f}{r['winrate']:>6.1%}{r['pf']:>7.2f}{r['trades']:>5d}{r['maxdd']:>7.2%}  {r.get('params','')}")

t0=time.time()

# ============ MACHETE ============
print("\n"+"="*70+"\n  🗡️ MACHETE V8B-LITE\n"+"="*70)
buy0, sell0 = machete_buy_sell({})
r0 = sim(buy0, sell0, close)
show("MACHETE [DEFAULT]", [r0])

# Sweep: atr_mult, adx, minconf, rmi
print("\n▸ Sweep 1")
mg = {'atr_multiplier':[1.5,1.8,2.0,2.5,3.0],
      'adx_threshold':[10.0,15.0,20.0,25.0],
      'min_confirmations_buy':[2,3],
      'rmi_buy_min':[40.0,48.0,52.0,60.0]}
ma=[]
for combo in itertools.product(*mg.values()):
    c=dict(zip(mg.keys(),combo))
    try:
        b,s=machete_buy_sell(c)
        r=sim(b,s,close)
        r['params']=';'.join(f"{k}={v}" for k,v in c.items())
        ma.append(r)
    except: pass
ma.sort(key=lambda x:x['ret'],reverse=True)
m_all=ma
show("MACHETE SWEEP (atr×adx×conf×rmi)", ma[:10])

# Fine-tune
if ma:
    bp=dict(x.split('=') for x in ma[0]['params'].split(';'))
    bp={k:float(v) if '.' in v else int(v) for k,v in bp.items()}
    print(f"\n▸ Fine-tune: {bp}")
    fg={'sr_proximity_pct':[1.0,1.5,2.0,2.5],'vol_threshold':[0.8,0.9,1.0,1.1],'risk_reward':[1.5,2.0,2.5]}
    ma2=[]
    for combo in itertools.product(*fg.values()):
        c={**bp,**dict(zip(fg.keys(),combo))}
        try:
            b,s=machete_buy_sell(c)
            r=sim(b,s,close)
            r['params']=';'.join(f"{k}={v}" for k,v in c.items())
            ma2.append(r)
        except: pass
    ma2.sort(key=lambda x:x['ret'],reverse=True)
    show("MACHETE FINE (SR+Vol+RR)", ma2[:8])
    m_all+=ma2

# ============ SCALP+ ============
print("\n\n"+"="*70+"\n  🔪 SIMPLE SCALP+\n"+"="*70)
buy0, sell0 = scalp_buy_sell({})
r0 = sim(buy0, sell0, close)
show("SCALP+ [DEFAULT]", [r0])

print("\n▸ Sweep 1")
sg = {'atr_multiplier':[0.8,1.0,1.2,1.5],
      'min_confirmations_buy':[3,4,5],
      'adx_threshold':[10.0,14.0,18.0],
      'ema_fast':[7,9,11]}
sa=[]
for combo in itertools.product(*sg.values()):
    c=dict(zip(sg.keys(),combo))
    try:
        b,s=scalp_buy_sell(c)
        r=sim(b,s,close)
        r['params']=';'.join(f"{k}={v}" for k,v in c.items())
        sa.append(r)
    except: pass
sa.sort(key=lambda x:x['ret'],reverse=True)
s_all=sa
show("SCALP+ SWEEP (atr×conf×adx×ema)", sa[:10])

if sa:
    bp=dict(x.split('=') for x in sa[0]['params'].split(';'))
    bp={k:float(v) if '.' in v else int(v) for k,v in bp.items()}
    print(f"\n▸ Fine-tune: {bp}")
    ef=int(bp.get('ema_fast',9))
    fg={'bollinger_std':[1.5,2.0,2.5],'volume_threshold':[0.9,1.0,1.05],
        'stoch_buy_k_min':[15.0,18.0,22.0],'ema_slow':[18,20,22,24]}
    sa2=[]
    for combo in itertools.product(*fg.values()):
        c={**bp,**dict(zip(fg.keys(),combo))}
        try:
            b,s=scalp_buy_sell(c)
            r=sim(b,s,close)
            r['params']=';'.join(f"{k}={v}" for k,v in {**bp,**dict(zip(fg.keys(),combo))}.items())
            sa2.append(r)
        except: pass
    sa2.sort(key=lambda x:x['ret'],reverse=True)
    show("SCALP+ FINE (Bol+Vol+Stoch+EMA)", sa2[:8])
    s_all+=sa2

# ============ FINAL ============
all_m=sorted(m_all,key=lambda x:(x['ret'],x['sharpe']),reverse=True)
all_s=sorted(s_all,key=lambda x:(x['ret'],x['sharpe']),reverse=True)

print(f"\n{'='*70}\n  🏆 TOP 5 MACHETTE\n{'='*70}")
for i,r in enumerate(all_m[:5]):
    print(f"  {i+1}. Ret={r['ret']:.2%} Sh={r['sharpe']:.2f} W={r['winrate']:.0%} PF={r['pf']:.2f} Tr={r['trades']} DD={r['maxdd']:.2%} | {r['params']}")

print(f"\n{'='*70}\n  🏆 TOP 5 SCALP+\n{'='*70}")
for i,r in enumerate(all_s[:5]):
    print(f"  {i+1}. Ret={r['ret']:.2%} Sh={r['sharpe']:.2f} W={r['winrate']:.0%} PF={r['pf']:.2f} Tr={r['trades']} DD={r['maxdd']:.2%} | {r['params']}")

if all_m and all_s:
    bm,bs=all_m[0],all_s[0]
    print(f"\n{'='*70}\n  ⚖️  HEAD-TO-HEAD\n{'='*70}")
    print(f"     Return: Machete={bm['ret']:.2%} vs Scalp+={bs['ret']:.2%} → {'Machete' if bm['ret']>bs['ret'] else 'Scalp+'} wins")
    print(f"     Sharpe: Machete={bm['sharpe']:.2f} vs Scalp+={bs['sharpe']:.2f} → {'Machete' if bm['sharpe']>bs['sharpe'] else 'Scalp+'} wins")
    print(f"     PF:     Machete={bm['pf']:.2f} vs Scalp+={bs['pf']:.2f} → {'Machete' if bm['pf']>bs['pf'] else 'Scalp+'} wins")
    print(f"     MaxDD:  Machete={bm['maxdd']:.2%} vs Scalp+={bs['maxdd']:.2%} → {'Machete' if bm['maxdd']<bs['maxdd'] else 'Scalp+'} safer")
    print(f"\n  ⏱️ {time.strftime('%H:%M:%S')} | {len(data)} candles | {len(all_m)+len(all_s)} combos")