"""
Order Cleanup Utility
Manages ghost orders, closed positions, and database maintenance.
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


def clear_ghost_orders(dry_run: bool = True):
    """
    Remove ghost orders from the database.
    Ghost orders are entries where remaining_amount = 0.
    """
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    # Find ghost orders
    cursor.execute('''
        SELECT id, symbol, side, amount, entry_price, status
        FROM positions
        WHERE remaining_amount = 0 OR remaining_amount IS NULL
    ''')
    ghosts = cursor.fetchall()
    
    if not ghosts:
        print('No ghost orders found.')
        conn.close()
        return
    
    print(f'Found {len(ghosts)} ghost order(s):')
    for g in ghosts:
        print(f"  ID: {g[0]} | {g[1]} | {g[2]} | Amount: {g[3]:.8f} | Status: {g[5]}")
    
    if dry_run:
        print('\n[DRY RUN] No changes made. Use --execute to actually delete.')
    else:
        cursor.execute('''
            DELETE FROM positions
            WHERE remaining_amount = 0 OR remaining_amount IS NULL
        ''')
        conn.commit()
        print(f'\nDeleted {len(ghosts)} ghost order(s).')
    
    conn.close()


def clear_all_positions(dry_run: bool = True):
    """Clear all positions from the database."""
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT COUNT(*) FROM positions')
    count = cursor.fetchone()[0]
    
    if count == 0:
        print('No positions to clear.')
        conn.close()
        return
    
    print(f'Found {count} position(s) in database.')
    
    if dry_run:
        print('[DRY RUN] No changes made. Use --execute to actually clear.')
    else:
        cursor.execute('DELETE FROM positions')
        conn.commit()
        print(f'Cleared {count} position(s).')
    
    conn.close()


def fix_btc_limit(dry_run: bool = True):
    """
    Remove BTC positions with invalid base amounts.
    BTC amounts should be <= 1.0 for BTC trading pairs.
    """
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, symbol, amount, side, entry_price
        FROM positions
        WHERE UPPER(symbol) IN ('BTCUSDT', 'THB_BTC', 'BTC_THB')
        AND CAST(amount AS REAL) > 1.0
    ''')
    invalid = cursor.fetchall()
    
    if not invalid:
        print('No invalid BTC amounts found.')
        conn.close()
        return
    
    print(f'Found {len(invalid)} invalid position(s) with BTC amount > 1.0:')
    for pos in invalid:
        print(f"  ID: {pos[0]} | {pos[1]} | Amount: {pos[2]:.8f} | Entry: {pos[4]:,.2f}")
    
    if dry_run:
        print('\n[DRY RUN] No changes made. Use --execute to fix.')
    else:
        cursor.execute('''
            DELETE FROM positions
            WHERE UPPER(symbol) IN ('BTCUSDT', 'THB_BTC', 'BTC_THB')
            AND CAST(amount AS REAL) > 1.0
        ''')
        conn.commit()
        print(f'\nFixed {len(invalid)} invalid position(s).')
    
    conn.close()


def vacuum_database():
    """Run VACUUM to optimize the database."""
    conn = sqlite3.connect('crypto_bot.db')
    print('Running VACUUM on database...')
    conn.execute('VACUUM')
    conn.close()
    print('Database vacuumed successfully.')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Order cleanup utility')
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Ghost orders
    subparsers.add_parser('ghosts', help='Remove ghost orders (remaining_amount = 0)')
    subparsers.add_parser('all', help='Clear all positions')
    subparsers.add_parser('fix-btc', help='Fix invalid BTC position amounts > 1.0')
    subparsers.add_parser('vacuum', help='Optimize database with VACUUM')
    
    # Add execute flag
    parser.add_argument('--execute', '-e', action='store_true', 
                       help='Actually execute changes (default is dry-run)')
    
    args = parser.parse_args()
    
    dry_run = not args.execute
    
    if args.command == 'ghosts':
        clear_ghost_orders(dry_run)
    elif args.command == 'all':
        clear_all_positions(dry_run)
    elif args.command == 'fix-btc':
        fix_btc_limit(dry_run)
    elif args.command == 'vacuum':
        vacuum_database()
    else:
        parser.print_help()
