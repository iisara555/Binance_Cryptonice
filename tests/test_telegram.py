"""
Test Profit and Loss Alert
==========================
Test various P&L scenarios for Telegram alerts.
"""

import os
import sys
import time

import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env file
from dotenv import load_dotenv

load_dotenv()

from alerts import AlertLevel, AlertSystem, format_trade_alert


def test_pnl_alerts():
    """Opt-in live Telegram smoke test for several P&L alert shapes."""
    if os.environ.get("RUN_LIVE_TELEGRAM_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_TELEGRAM_TESTS=1 to run live Telegram alert tests")

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        pytest.skip("Telegram live test requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

    alert_system = AlertSystem(bot_token, chat_id)

    print("=" * 50)
    print("Profit & Loss Alert Test")
    print("=" * 50)

    # Test 1: Small profit
    print("\n1. 💰 Small Profit (+1.5%)")
    msg = format_trade_alert(
        symbol="THB_BTC",
        side="SELL",
        price=1_522_500.0,
        amount=0.06666667,
        value_thb=101_500.0,
        pnl_amt=1500.0,
        pnl_pct=1.5,
        status="filled",
    )
    result = alert_system.send(AlertLevel.TRADE, msg)
    print(f"   Result: {'✅ Success' if result else '❌ Failed'}")
    assert result
    time.sleep(12)  # Wait for rate limit

    # Test 2: Big profit
    print("\n2. 💰💰 Big Profit (+5.2%)")
    msg = format_trade_alert(
        symbol="THB_ETH",
        side="SELL",
        price=55_000.0,
        amount=0.90909091,
        value_thb=50_000.0,
        pnl_amt=2600.0,
        pnl_pct=5.2,
        status="filled",
    )
    result = alert_system.send(AlertLevel.TRADE, msg)
    print(f"   Result: {'✅ Success' if result else '❌ Failed'}")
    assert result
    time.sleep(12)

    # Test 3: Small loss
    print("\n3. 💸 Small Loss (-0.8%)")
    msg = format_trade_alert(
        symbol="THB_DOGE",
        side="SELL",
        price=2.45,
        amount=20000.0,
        value_thb=49_000.0,
        pnl_amt=-400.0,
        pnl_pct=-0.8,
        status="filled",
    )
    result = alert_system.send(AlertLevel.TRADE, msg)
    print(f"   Result: {'✅ Success' if result else '❌ Failed'}")
    assert result
    time.sleep(12)

    # Test 4: Big loss (stop loss triggered)
    print("\n4. 🛑 Big Loss (-3.5%) - Stop Loss")
    msg = format_trade_alert(
        symbol="THB_SOL",
        side="SELL",
        price=1420.0,
        amount=35.21127,
        value_thb=50_000.0,
        pnl_amt=-1750.0,
        pnl_pct=-3.5,
        status="filled",
        extra="🛑 Stop Loss Triggered",
    )
    result = alert_system.send(AlertLevel.TRADE, msg)
    print(f"   Result: {'✅ Success' if result else '❌ Failed'}")
    assert result
    time.sleep(12)

    # Test 5: Take Profit
    print("\n5. 🎯 Take Profit (+2.0%)")
    msg = format_trade_alert(
        symbol="THB_BNB",
        side="SELL",
        price=4850.0,
        amount=10.30928,
        value_thb=50_000.0,
        pnl_amt=1000.0,
        pnl_pct=2.0,
        status="filled",
        extra="🎯 Take Profit Target",
    )
    result = alert_system.send(AlertLevel.TRADE, msg)
    print(f"   Result: {'✅ Success' if result else '❌ Failed'}")
    assert result

    # Test 6: Breakeven
    print("\n6. ⚖️ Breakeven (0%)")
    msg = format_trade_alert(
        symbol="THB_XRP",
        side="SELL",
        price=18.25,
        amount=2740.0,
        value_thb=50_000.0,
        pnl_amt=0.0,
        pnl_pct=0.0,
        status="filled",
    )
    result = alert_system.send(AlertLevel.TRADE, msg)
    print(f"   Result: {'✅ Success' if result else '❌ Failed'}")
    assert result

    print("\n" + "=" * 50)
    print("✅ P&L Test Complete!")
    print("=" * 50)


if __name__ == "__main__":
    test_pnl_alerts()
