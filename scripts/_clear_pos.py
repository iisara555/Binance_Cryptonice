import sqlite3
c = sqlite3.connect('/root/Crypto_Sniper/crypto_bot.db')
deleted = c.execute("DELETE FROM positions WHERE order_id IN ('bootstrap_SOLUSDT_1777758846', 'bootstrap_BTCUSDT_1777729647')").rowcount
c.commit()
print(f'Deleted {deleted} rows from positions')
# Also check trade_states table if it exists
try:
    cols = [r[1] for r in c.execute('PRAGMA table_info(trade_states)').fetchall()]
    if cols:
        rows = c.execute("SELECT symbol FROM trade_states WHERE symbol IN ('SOLUSDT','BTCUSDT')").fetchall()
        print('trade_states rows:', rows)
        d2 = c.execute("DELETE FROM trade_states WHERE symbol IN ('SOLUSDT','BTCUSDT')").rowcount
        c.commit()
        print(f'Deleted {d2} rows from trade_states')
except Exception as e:
    print('trade_states:', e)
c.close()
print('Done.')
