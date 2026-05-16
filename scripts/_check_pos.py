import sqlite3
c = sqlite3.connect('/root/Crypto_Sniper/crypto_bot.db')
cols = [r[1] for r in c.execute('PRAGMA table_info(positions)').fetchall()]
print('COLUMNS:', cols)
rows = c.execute('SELECT * FROM positions').fetchall()
print('ROWS:', len(rows))
for r in rows:
    print(r)
c.close()
