import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()
from api_client import BinanceThClient
import sqlite3

client = BinanceThClient()

orders_to_cancel = [
    ('BTCUSDT',  '69cf3fe7dfee6b084db4f024m8a2qe', 'sell'),
    ('BTCUSDT',  '69ce772d2c558a67c31b566em8a2qe', 'buy'),
    ('BTCUSDT',  '69cd4ff08b3befcc66838a5cm8a2qe', 'sell'),
    ('DOGEUSDT', '69cf40d74ca5b83b061a47611o0oz7',  'sell'),
    ('DOGEUSDT', '69cd52446b652af21bbd385d1o0oz7',  'sell'),
    ('DOGEUSDT', '69cd51188b3befcc66839bad1o0oz7',  'sell'),
]

for symbol, oid, side in orders_to_cancel:
    try:
        result = client.cancel_order(symbol, oid, side)
        print('OK:', symbol, side, oid[:16])
    except Exception as e:
        print('ERR:', symbol, side, oid[:16], '|', e)

conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()
cur.execute("DELETE FROM positions")
conn.commit()
print('DB positions cleared:', cur.rowcount, 'rows')
conn.close()
