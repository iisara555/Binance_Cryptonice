#!/usr/bin/env python3
"""
Bitkub Pre-Trade Validation System
==================================
Validates trading configuration against live Bitkub data before starting trades.
Checks:
- Available pairs on Bitkub
- Configured assets exist on Bitkub
- Real portfolio value from Bitkub
- Sufficient balances for trading

Usage:
  python validate_bitkub_config.py
"""

import sys
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Setup logging (skip if main bot already configured the root logger)
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s: %(message)s'
    )
logger = logging.getLogger(__name__)

# Color codes for terminal output
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'
BOLD = '\033[1m'


def print_header(title: str):
    """Print a formatted header."""
    print(f"\n{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"{BOLD}{BLUE}{title:^70}{RESET}")
    print(f"{BOLD}{BLUE}{'='*70}{RESET}\n")


def print_success(msg: str):
    """Print success message."""
    print(f"{GREEN}✓ {msg}{RESET}")


def print_error(msg: str):
    """Print error message."""
    print(f"{RED}✗ {msg}{RESET}")


def print_warning(msg: str):
    """Print warning message."""
    print(f"{YELLOW}⚠ {msg}{RESET}")


def print_info(msg: str):
    """Print info message."""
    print(f"{BLUE}ℹ {msg}{RESET}")


class BitkubValidator:
    """Validates Bitkub configuration and real data."""

    def __init__(self):
        try:
            from api_client import BitkubClient
            from config import BITKUB, TRADING
        except ImportError as e:
            print_error(f"Failed to import required modules: {e}")
            sys.exit(1)

        self.api_key = BITKUB.api_key
        self.api_secret = BITKUB.api_secret
        self.client = BitkubClient(
            api_key=self.api_key,
            api_secret=self.api_secret,
            symbol="THB_BTC",
        )

        # Load trading config
        self.trading_pair = "THB_BTC"
        self.configured_assets = set()
        rebalance_config = {}

        # Try to load bot_config.yaml for more details
        try:
            import yaml
            with open("bot_config.yaml", "r") as f:
                bot_config = yaml.safe_load(f)
            trading_cfg = bot_config.get("trading", {})
            self.trading_pair = trading_cfg.get("trading_pair", "THB_BTC")
            rebalance_config = bot_config.get("rebalance", {})
        except ImportError:
            print_warning("PyYAML not installed, skipping bot_config.yaml parsing")
        except Exception as e:
            print_warning(f"Could not load bot_config.yaml: {e}")

        self.rebalance_config = rebalance_config
        cash_assets = rebalance_config.get("cash_assets", ["THB"])
        self.cash_assets = {str(asset).upper() for asset in cash_assets if asset}
        if not self.cash_assets:
            self.cash_assets = {"THB"}
        self.available_pairs = {}
        self.portfolio_value = 0.0
        self.balances = {}
        self.errors = []
        self.warnings = []

    def get_available_pairs(self) -> bool:
        """Fetch available trading pairs from Bitkub."""
        print_info("Fetching available pairs from Bitkub...")
        try:
            # Get symbols from Bitkub
            symbols_data = self.client.get_symbols()

            if not symbols_data:
                print_error("No symbols found on Bitkub")
                return False

            # Extract THB pairs
            for sym in symbols_data:
                symbol = sym.get("symbol", "")
                if symbol.endswith("_THB"):
                    coin = symbol.replace("_THB", "")
                    self.available_pairs[coin] = sym

            print_success(f"Found {len(self.available_pairs)} THB trading pairs on Bitkub")
            return True

        except Exception as e:
            print_error(f"Failed to fetch pairs: {e}")
            self.errors.append(f"Bitkub API error: {e}")
            return False

    def get_configured_assets(self) -> bool:
        """Extract configured assets from config."""
        print_info("Reading configured assets...")

        # Get from rebalance config if available
        if self.rebalance_config.get("enabled", False):
            target_alloc = self.rebalance_config.get("target_allocation", {})
            self.configured_assets.update(
                asset for asset in target_alloc.keys()
                if str(asset).upper() not in self.cash_assets
            )

        # Also add main trading pair
        if self.trading_pair:
            parts = self.trading_pair.split("_")
            if len(parts) == 2:
                asset = str(parts[1]).upper()
                if asset not in self.cash_assets:
                    self.configured_assets.add(asset)

        print_success(f"Configured assets: {', '.join(sorted(self.configured_assets))}")
        return len(self.configured_assets) > 0

    def validate_assets_exist(self) -> bool:
        """Check if all configured assets exist on Bitkub."""
        print_info("Validating configured assets exist on Bitkub...")

        missing = []
        for asset in self.configured_assets:
            if asset not in self.available_pairs:
                missing.append(asset)
                print_error(f"  {asset} - NOT FOUND on Bitkub")
            else:
                print_success(f"  {asset} - Found on Bitkub")

        if missing:
            self.errors.append(f"Assets not found on Bitkub: {', '.join(missing)}")
            return False

        print_success("All configured assets exist on Bitkub")
        return True

    def get_real_portfolio_value(self) -> bool:
        """Fetch real portfolio value from Bitkub."""
        print_info("Fetching real portfolio value from Bitkub...")

        try:
            # Get balances from Bitkub
            balances_data = self.client.get_balances()

            if not balances_data:
                print_error("Failed to get balances from Bitkub")
                self.errors.append("Could not fetch balances from Bitkub")
                return False

            # Get current prices for all configured assets
            tickers = {}
            for asset in self.configured_assets:
                try:
                    ticker = self.client.get_ticker(f"THB_{asset}")
                    tickers[asset] = float(ticker.get("last", 0))
                except Exception as e:
                    print_warning(f"Could not fetch price for {asset}: {e}")
                    tickers[asset] = 0.0

            # Calculate portfolio value
            thb_value = float(balances_data.get("THB", {}).get("available", 0))
            portfolio_value = thb_value

            print_success(f"THB Balance: {thb_value:,.2f} THB")

            for asset in self.configured_assets:
                available = float(
                    balances_data.get(asset, {}).get("available", 0)
                )
                reserved = float(
                    balances_data.get(asset, {}).get("reserved", 0)
                )
                total = available + reserved
                price = tickers.get(asset, 0)
                value = total * price

                self.balances[asset] = {
                    "available": available,
                    "reserved": reserved,
                    "total": total,
                    "price": price,
                    "value": value,
                }

                portfolio_value += value

                print_info(
                    f"  {asset}: {total:.8f} ({available:.8f} available, "
                    f"{reserved:.8f} reserved) @ {price:,.2f} THB = {value:,.2f} THB"
                )

            self.portfolio_value = portfolio_value
            print_success(f"Real Portfolio Value: {portfolio_value:,.2f} THB")
            return True

        except Exception as e:
            print_error(f"Failed to get portfolio value: {e}")
            self.errors.append(f"Portfolio value fetch error: {e}")
            return False

    def validate_minimum_balance(self) -> bool:
        """Check if portfolio has minimum balance to trade."""
        print_info("Validating minimum balance...")

        MIN_BALANCE = 100.0  # Minimum 100 THB

        if self.portfolio_value < MIN_BALANCE:
            self.errors.append(
                f"Portfolio value ({self.portfolio_value:.2f} THB) "
                f"is below minimum ({MIN_BALANCE:.2f} THB)"
            )
            print_error(f"Portfolio value too low: {self.portfolio_value:.2f} THB (min: {MIN_BALANCE:.2f} THB)")
            return False

        print_success(f"Portfolio value sufficient: {self.portfolio_value:,.2f} THB")
        return True

    def display_summary(self):
        """Display validation summary."""
        print_header("VALIDATION SUMMARY")

        if not self.errors:
            print_success("✓ All validations PASSED")
            print_info(f"Portfolio Value: {self.portfolio_value:,.2f} THB")
            print_info(f"Assets: {', '.join(sorted(self.configured_assets))}")
            print("\nReady to start trading! 🚀\n")
            return True
        else:
            print_error("✗ Validation FAILED with errors:")
            for error in self.errors:
                print_error(f"  - {error}")

            if self.warnings:
                print("\nWarnings:")
                for warning in self.warnings:
                    print_warning(f"  - {warning}")

            print("\nPlease fix the errors before trading.\n")
            return False

    def run_validation(self) -> bool:
        """Run complete validation."""
        print_header("BITKUB PRE-TRADE VALIDATION")
        print_info(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        # Run all validations
        steps = [
            ("Fetching Available Pairs", self.get_available_pairs),
            ("Reading Configured Assets", self.get_configured_assets),
            ("Validating Assets Exist", self.validate_assets_exist),
            ("Getting Real Portfolio Value", self.get_real_portfolio_value),
            ("Validating Minimum Balance", self.validate_minimum_balance),
        ]

        for step_name, step_func in steps:
            print_info(f"\n[Step] {step_name}...")
            if not step_func():
                print_error(f"Step failed: {step_name}")
                break

        # Display summary
        return self.display_summary()


def main():
    """Main entry point."""
    validator = BitkubValidator()
    success = validator.run_validation()

    # Exit with appropriate code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
