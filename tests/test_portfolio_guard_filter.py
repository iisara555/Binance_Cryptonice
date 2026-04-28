"""Test to verify Portfolio Guard pair filtering works correctly."""

import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Database


def test_portfolio_guard_filter():
    """Verify that has_ever_held() correctly identifies held vs non-held coins."""

    # Use test database
    db_path = ":memory:"
    db = Database(db_path)

    # Test 1: No coins held yet
    print("✓ Test 1: Initial state - no coins held")
    assert not db.has_ever_held("THB_BTC"), "BTC should not be held initially"
    assert not db.has_ever_held("THB_BNB"), "BNB should not be held initially"
    assert not db.has_ever_held("THB_ETH"), "ETH should not be held initially"
    print("  ✓ All coins correctly return False initially")

    # Test 2: Record a coin as held
    print("\n✓ Test 2: Record BTC as held")
    db.record_held_coin("THB_BTC", 0.1)
    assert db.has_ever_held("THB_BTC"), "BTC should be held after recording"
    print("  ✓ BTC correctly returns True after recording")

    # Test 3: Other coins still not held
    print("\n✓ Test 3: Other coins still not held")
    assert not db.has_ever_held("THB_BNB"), "BNB should still not be held"
    assert not db.has_ever_held("THB_ETH"), "ETH should still not be held"
    print("  ✓ BNB and ETH correctly return False")

    # Test 4: Case insensitivity
    print("\n✓ Test 4: Case insensitivity")
    assert db.has_ever_held("thb_btc"), "Should work with lowercase"
    assert db.has_ever_held("THB_BTC"), "Should work with uppercase"
    assert db.has_ever_held("ThB_BtC"), "Should work with mixed case"
    print("  ✓ Case insensitivity works correctly")

    # Test 5: Simulate pair filtering
    print("\n✓ Test 5: Simulate pair filtering in _run_iteration()")
    trading_pairs = ["THB_BTC", "THB_BNB", "THB_ETH"]
    held_pairs = [pair for pair in trading_pairs if db.has_ever_held(pair)]
    print(f"  Trading pairs: {trading_pairs}")
    print(f"  Held pairs: {held_pairs}")
    print(f"  Skipped pairs: {[p for p in trading_pairs if p not in held_pairs]}")
    assert held_pairs == ["THB_BTC"], "Only BTC should be in held_pairs"
    print("  ✓ Filter correctly identifies only BTC as held")

    print("\n✅ All tests passed! Portfolio Guard filter is working correctly.")


if __name__ == "__main__":
    test_portfolio_guard_filter()
