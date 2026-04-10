"""
Consolidated Order Checker Utility
Checks open orders across multiple trading pairs.
"""
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

from api_client import BitkubClient


def check_all_orders():
    """Check open orders for all tracked pairs."""
    client = BitkubClient()
    
    pairs = ['THB_BTC', 'THB_DOGE', 'THB_ETH', 'THB_XRP']
    
    for pair in pairs:
        print(f'\n--- {pair} ---')
        orders = client.get_open_orders(pair)
        if not orders:
            print('  No open orders')
        else:
            for o in orders:
                side = o.get('side', 'unknown').upper()
                amount = o.get('amount', 0)
                rate = o.get('rate', 0)
                order_id = o.get('id', 'N/A')
                print(f"  {side} {amount:.8f} @ {rate:,.2f} | id={order_id}")


def check_order(pair: str):
    """Check open orders for a specific pair."""
    client = BitkubClient()
    orders = client.get_open_orders(pair)
    print(f'Open orders for {pair}:')
    if not orders:
        print('  No open orders')
    else:
        for o in orders:
            print(f"  {o}")
    return orders


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Check open orders')
    parser.add_argument('--pair', '-p', default='THB_BTC', help='Trading pair (default: THB_BTC)')
    parser.add_argument('--all', '-a', action='store_true', help='Check all pairs')
    args = parser.parse_args()
    
    if args.all:
        check_all_orders()
    else:
        check_order(args.pair)
