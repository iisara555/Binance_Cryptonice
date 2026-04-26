import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()
from api_client import BinanceThClient

client = BinanceThClient()
balances = client.get_balances()

print('=== All Non-Zero Balances ===')
for asset, data in balances.items():
    avail = data.get('available', 0)
    reserved = data.get('reserved', 0)
    total = avail + reserved
    if total > 0:
        print(f'{asset}: available={avail}, reserved={reserved}, total={total}')
