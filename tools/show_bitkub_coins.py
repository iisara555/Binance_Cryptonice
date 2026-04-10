#!/usr/bin/env python3
"""
Display Available Coins on Bitkub
==================================
Shows all THB trading pairs available on Bitkub.

Usage:
  python show_bitkub_coins.py
"""

import sys
import logging
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.WARNING)

# Color codes for terminal output
GREEN = '\033[92m'
BLUE = '\033[94m'
YELLOW = '\033[93m'
RESET = '\033[0m'
BOLD = '\033[1m'


def print_header(title: str):
    """Print a formatted header."""
    print(f"\n{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"{BOLD}{BLUE}{title:^70}{RESET}")
    print(f"{BOLD}{BLUE}{'='*70}{RESET}\n")


def main():
    """Show available coins on Bitkub."""
    try:
        from api_client import BitkubClient
        from config import BITKUB
    except ImportError as e:
        print(f"Error: Could not import required modules: {e}")
        sys.exit(1)

    print_header("BITKUB AVAILABLE TRADING PAIRS")

    try:
        # Create API client
        client = BitkubClient(
            api_key=BITKUB.api_key,
            api_secret=BITKUB.api_secret,
            symbol="THB_BTC",
        )

        # Fetch available symbols
        print("[*] Fetching available trading pairs from Bitkub...")
        symbols_data = client.get_symbols()

        if not symbols_data:
            print("[!] No symbols found on Bitkub")
            return

        # Extract THB pairs
        thb_pairs = []
        for sym in symbols_data:
            symbol = sym.get("symbol", "")
            if symbol.endswith("_THB"):
                coin = symbol.replace("_THB", "")
                thb_pairs.append({
                    "coin": coin,
                    "symbol": symbol,
                    "data": sym
                })

        # Sort by coin name
        thb_pairs.sort(key=lambda x: x["coin"])

        print(f"\n{GREEN}[OK] Found {len(thb_pairs)} THB trading pairs on Bitkub{RESET}\n")

        # Display in columns
        print(f"{BOLD}{'Position':<6} {'Coin':<8} {'Symbol':<12} {'Status':<15}{RESET}")
        print("-" * 70)

        for i, pair in enumerate(thb_pairs, 1):
            coin = pair["coin"]
            symbol = pair["symbol"]
            # Get decimals info if available
            decimals = pair["data"].get("decimals", "?")
            info_status = f"decimals: {decimals}"

            print(f"{i:<6} {coin:<8} {symbol:<12} {info_status:<15}")

            # Print every 10 pairs, also print at end for readability
            if i % 50 == 0:
                print("-" * 70)

        print("\n" + "=" * 70)
        print(f"{BOLD}{GREEN}[TOTAL] {len(thb_pairs)} coins available{RESET}\n")

        # Show some example usage
        print(f"{BOLD}Example Configuration for bot_config.yaml:{RESET}")
        print("""
rebalance:
  enabled: true
  strategy: "combined"
  target_allocation:
    BTC: 40.0   # Bitcoin
    ETH: 30.0   # Ethereum
    BNB: 20.0   # Binance Coin
    SOL: 10.0   # Solana
""")

        print(f"{BOLD}To check if a coin exists in Bitkub, look for it in the list above.{RESET}\n")

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
