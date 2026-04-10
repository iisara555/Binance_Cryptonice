"""
Consolidated Position Checker Utility
Checks current positions from the local database.
"""
import sys
import sqlite3
from pathlib import Path
from dotenv import load_dotenv

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()


def check_all_positions():
    """Check all positions in the database."""
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    cursor.execute('''
     SELECT id, order_id, symbol, side, amount, remaining_amount,
         entry_price, stop_loss, take_profit, opened_at, updated_at
        FROM positions
     ORDER BY opened_at DESC
        LIMIT 50
    ''')
    
    rows = cursor.fetchall()
    
    if not rows:
        print('No positions found in database.')
    else:
        print(f'Found {len(rows)} position(s):')
        print('-' * 100)
        for row in rows:
            print(f"ID: {row[0]}")
            print(f"  Order ID: {row[1]}")
            print(f"  Symbol: {row[2]} | Side: {row[3]} | Amount: {float(row[4] or 0):.8f} | Remaining: {float(row[5] or 0):.8f}")
            print(f"  Entry: {float(row[6] or 0):,.2f} | SL: {float(row[7] or 0):,.2f} | TP: {float(row[8] or 0):,.2f}")
            print(f"  Opened: {row[9]} | Updated: {row[10]}")
            print('-' * 100)
    
    conn.close()
    return rows


def check_position_by_symbol(symbol: str):
    """Check positions for a specific symbol."""
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    cursor.execute('''
     SELECT id, order_id, symbol, side, amount, remaining_amount,
         entry_price, stop_loss, take_profit, opened_at, updated_at
        FROM positions
        WHERE symbol = ?
     ORDER BY opened_at DESC
    ''', (symbol,))
    
    rows = cursor.fetchall()
    
    if not rows:
        print(f'No positions found for {symbol}.')
    else:
        print(f'Found {len(rows)} position(s) for {symbol}:')
        for row in rows:
            print(
                f"  ID: {row[0]} | Order: {row[1]} | {row[3]} {float(row[4] or 0):.8f} "
                f"(remaining {float(row[5] or 0):.8f}) @ {float(row[6] or 0):,.2f}"
            )
    
    conn.close()
    return rows


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Check positions from database')
    parser.add_argument('--symbol', '-s', default=None, help='Filter by symbol (e.g., THB_BTC)')
    parser.add_argument('--all', '-a', action='store_true', help='Show all positions')
    args = parser.parse_args()
    
    if args.symbol:
        check_position_by_symbol(args.symbol.upper())
    else:
        check_all_positions()
