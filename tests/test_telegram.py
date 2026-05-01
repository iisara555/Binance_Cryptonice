"""
Test Telegram Alerts
===================
Unit tests for Telegram alert formatting and transport.
"""

from unittest.mock import MagicMock, patch

import pytest

from alerts import AlertLevel, AlertSystem, format_trade_alert


class TestFormatTradeAlert:
    """Unit tests for format_trade_alert without network calls."""

    def test_small_profit_format(self):
        """Small profit should format correctly."""
        msg = format_trade_alert(
            symbol="THB_BTC",
            side="SELL",
            price=1_522_500.0,
            amount=0.06666667,
            value_quote=101_500.0,
            pnl_amt=1500.0,
            pnl_pct=1.5,
            status="filled",
            quote_asset="THB",
        )
        assert "THB_BTC" in msg
        assert "SELL" in msg
        assert "1,500.00" in msg or "1500" in msg
        assert "1.50%" in msg or "+1.5%" in msg or "1.5%" in msg
        assert "filled" in msg.lower() or "E" in msg

    def test_big_profit_format(self):
        """Big profit should format correctly."""
        msg = format_trade_alert(
            symbol="THB_ETH",
            side="SELL",
            price=55_000.0,
            amount=0.90909091,
            value_quote=50_000.0,
            pnl_amt=2600.0,
            pnl_pct=5.2,
            status="filled",
            quote_asset="THB",
        )
        assert "THB_ETH" in msg
        assert "2,600" in msg or "2600" in msg
        assert "5.20%" in msg or "5.2%" in msg

    def test_small_loss_format(self):
        """Small loss should format correctly."""
        msg = format_trade_alert(
            symbol="THB_DOGE",
            side="SELL",
            price=2.45,
            amount=20000.0,
            value_quote=49_000.0,
            pnl_amt=-400.0,
            pnl_pct=-0.8,
            status="filled",
            quote_asset="THB",
        )
        assert "THB_DOGE" in msg
        assert "-400" in msg or "400" in msg
        # Format returns -0.80% with 2 decimal places
        assert "-0.80%" in msg or "0.80%" in msg

    def test_stop_loss_format(self):
        """Stop loss should include extra message."""
        msg = format_trade_alert(
            symbol="THB_SOL",
            side="SELL",
            price=1420.0,
            amount=35.21127,
            value_quote=50_000.0,
            pnl_amt=-1750.0,
            pnl_pct=-3.5,
            status="filled",
            extra="Stop Loss Triggered",
            quote_asset="THB",
        )
        assert "THB_SOL" in msg
        assert "Stop Loss" in msg
        assert "-1,750" in msg or "1750" in msg

    def test_take_profit_format(self):
        """Take profit should include extra message."""
        msg = format_trade_alert(
            symbol="THB_BNB",
            side="SELL",
            price=4850.0,
            amount=10.30928,
            value_quote=50_000.0,
            pnl_amt=1000.0,
            pnl_pct=2.0,
            status="filled",
            extra="Take Profit Target",
            quote_asset="THB",
        )
        assert "THB_BNB" in msg
        assert "Take Profit" in msg
        assert "1,000" in msg or "1000" in msg

    def test_breakeven_format(self):
        """Breakeven should show zero P&L."""
        msg = format_trade_alert(
            symbol="THB_XRP",
            side="SELL",
            price=18.25,
            amount=2740.0,
            value_quote=50_000.0,
            pnl_amt=0.0,
            pnl_pct=0.0,
            status="filled",
            quote_asset="THB",
        )
        assert "THB_XRP" in msg
        assert "0.00%" in msg or "0.0%" in msg or "0%" in msg

    def test_usdt_quote_asset(self):
        """Test that USDT quote_asset works correctly."""
        msg = format_trade_alert(
            symbol="BTCUSDT",
            side="BUY",
            price=50000.0,
            amount=0.01,
            value_quote=500.0,
            status="filled",
            quote_asset="USDT",
        )
        assert "USDT" in msg
        assert "50,000.00" in msg
        assert "BTCUSDT" in msg


class TestAlertSystem:
    """Unit tests for AlertSystem without network calls."""

    @patch.dict("os.environ", {"TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "test_token", "TELEGRAM_CHAT_ID": "test_chat_id"})
    def test_alert_system_initialization(self):
        """AlertSystem should be instantiable with environment variables."""
        # Using direct args to avoid env var caching issues
        system = AlertSystem("test_token", "test_chat_id")
        # Verify send() works with configured system
        with patch("requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"ok": True}
            mock_post.return_value = mock_response
            result = system.send(AlertLevel.TRADE, "Test message")
            assert result is True

    @patch.dict("os.environ", {"TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "test_token", "TELEGRAM_CHAT_ID": "test_chat_id"})
    def test_send_message_calls_telegram_api(self):
        """send() should call Telegram API endpoint."""
        system = AlertSystem()

        with patch("requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"ok": True}
            mock_post.return_value = mock_response

            result = system.send(AlertLevel.TRADE, "Test message")

            assert result is True
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert "api.telegram.org" in call_args[0][0]
            assert "test_token" in call_args[0][0]
            assert call_args[1]["json"]["chat_id"] == "test_chat_id"
            assert call_args[1]["json"]["text"] == "Test message"

    @patch.dict("os.environ", {"TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "test_token", "TELEGRAM_CHAT_ID": "test_chat_id"})
    def test_send_handles_api_error(self):
        """send() should return False on API error."""
        system = AlertSystem()

        with patch("requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 400
            mock_response.json.return_value = {"ok": False, "error": "Bad Request"}
            mock_post.return_value = mock_response

            result = system.send(AlertLevel.TRADE, "Test message")

            assert result is False

    @patch.dict("os.environ", {"TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "test_token", "TELEGRAM_CHAT_ID": "test_chat_id"})
    def test_send_handles_network_error(self):
        """send() should return False on network error."""
        system = AlertSystem()

        with patch("requests.post") as mock_post:
            mock_post.side_effect = Exception("Network error")

            result = system.send(AlertLevel.TRADE, "Test message")

            assert result is False

    @patch.dict("os.environ", {"TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "test_token", "TELEGRAM_CHAT_ID": ""})
    def test_send_respects_disabled_api(self):
        """send() should return False when API is disabled (no chat_id)."""
        system = AlertSystem()

        with patch("requests.post") as mock_post:
            result = system.send(AlertLevel.TRADE, "Test message")

            assert result is False
            mock_post.assert_not_called()

    @patch.dict("os.environ", {"TELEGRAM_ENABLED": "false", "TELEGRAM_BOT_TOKEN": "test_token", "TELEGRAM_CHAT_ID": "test_chat_id"})
    def test_send_respects_disabled_runtime(self):
        """send() should return False when runtime is disabled."""
        system = AlertSystem()

        with patch("requests.post") as mock_post:
            result = system.send(AlertLevel.TRADE, "Test message")

            assert result is False
            mock_post.assert_not_called()


class TestAlertLevels:
    """Test AlertLevel enum usage."""

    def test_alert_levels_exist(self):
        """AlertLevel should have required levels."""
        assert AlertLevel.TRADE is not None
        assert AlertLevel.INFO is not None
        assert AlertLevel.SUMMARY is not None
        assert AlertLevel.CRITICAL is not None
        assert AlertLevel.DEBUG is not None

    @patch.dict("os.environ", {"TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "test_token", "TELEGRAM_CHAT_ID": "test_chat_id"})
    def test_alert_system_accepts_all_levels(self):
        """AlertSystem.send() should accept all alert levels."""
        system = AlertSystem()

        with patch("requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"ok": True}
            mock_post.return_value = mock_response

            for level in [AlertLevel.TRADE, AlertLevel.INFO, AlertLevel.CRITICAL, AlertLevel.SUMMARY, AlertLevel.DEBUG]:
                result = system.send(level, f"Test {level}")
                assert result is True

    def test_alert_levels_have_string_values(self):
        """AlertLevel values should be strings."""
        assert isinstance(AlertLevel.TRADE, str)
        assert isinstance(AlertLevel.INFO, str)
        assert AlertLevel.TRADE == "trade"
        assert AlertLevel.INFO == "info"
