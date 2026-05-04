"""
Per-pair cooldown tests.

Verifies that:
- Trading DOGE puts only DOGE in cooldown; other pairs can still trade
- Two pairs can be in cooldown independently with their own remaining time
- Global slot limits (max_open_positions, daily trade limit) still fire
- check_cooldown(symbol=None) returns True if ANY pair is cooling
- get_cooling_down_display() formats correctly for zero, one, and multi-pair cooling
- save/load round-trips per-pair data
"""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from risk_management import RiskConfig, RiskManager


def _make_rm(tmp_path: Path, cool_down_minutes: float = 15.0) -> RiskManager:
    cfg = RiskConfig()
    cfg.cool_down_minutes = cool_down_minutes
    # Patch load_state to prevent reading any real risk_state.json on disk.
    with patch.object(RiskManager, "load_state", return_value=False):
        rm = RiskManager(cfg)
    rm._state_file = tmp_path / "risk_state.json"
    return rm


class TestPerPairCooldownIsolation:
    def test_trade_doge_puts_only_doge_in_cooldown(self, tmp_path):
        rm = _make_rm(tmp_path)

        rm.record_trade("DOGEUSDT")

        assert rm.check_cooldown("DOGEUSDT") is True
        assert rm.check_cooldown("BTCUSDT") is False
        assert rm.check_cooldown("ETHUSDT") is False
        assert rm.check_cooldown("SOLUSDT") is False

    def test_global_check_is_true_when_any_pair_cooling(self, tmp_path):
        rm = _make_rm(tmp_path)

        rm.record_trade("DOGEUSDT")

        assert rm.check_cooldown() is True

    def test_global_check_is_false_when_no_pair_cooling(self, tmp_path):
        rm = _make_rm(tmp_path, cool_down_minutes=15.0)
        # Set DOGE trade time 20 min in the past — cooldown already expired
        rm._last_trade_time_per_pair["DOGEUSDT"] = datetime.now() - timedelta(minutes=20)

        assert rm.check_cooldown() is False

    def test_two_pairs_cooldown_independently(self, tmp_path):
        rm = _make_rm(tmp_path, cool_down_minutes=15.0)

        now = datetime.now()
        # DOGE traded 14 min ago — still cooling
        rm._last_trade_time_per_pair["DOGEUSDT"] = now - timedelta(minutes=14)
        # BTC traded 16 min ago — cooldown expired
        rm._last_trade_time_per_pair["BTCUSDT"] = now - timedelta(minutes=16)

        assert rm.check_cooldown("DOGEUSDT") is True
        assert rm.check_cooldown("BTCUSDT") is False

    def test_record_trade_activity_sets_per_pair(self, tmp_path):
        rm = _make_rm(tmp_path)

        rm.record_trade_activity("ETHUSDT")

        assert rm.check_cooldown("ETHUSDT") is True
        assert rm.check_cooldown("BTCUSDT") is False

    def test_expired_cooldown_returns_false(self, tmp_path):
        rm = _make_rm(tmp_path, cool_down_minutes=15.0)
        rm._last_trade_time_per_pair["DOGEUSDT"] = datetime.now() - timedelta(minutes=16)

        assert rm.check_cooldown("DOGEUSDT") is False


class TestCooldownDisplay:
    def test_no_cooldown_returns_no(self, tmp_path):
        rm = _make_rm(tmp_path)
        assert rm.get_cooling_down_display() == "No"

    def test_single_cooling_pair_shows_symbol_and_minutes(self, tmp_path):
        rm = _make_rm(tmp_path, cool_down_minutes=15.0)
        rm._last_trade_time_per_pair["DOGEUSDT"] = datetime.now() - timedelta(minutes=7)

        display = rm.get_cooling_down_display()

        assert "DOGE" in display
        assert "8m" in display or "7m" in display  # remaining ≈ 8 min

    def test_one_cooling_one_ready_shows_others_ready(self, tmp_path):
        rm = _make_rm(tmp_path, cool_down_minutes=15.0)
        now = datetime.now()
        rm._last_trade_time_per_pair["DOGEUSDT"] = now - timedelta(minutes=7)
        rm._last_trade_time_per_pair["BTCUSDT"] = now - timedelta(minutes=16)  # expired

        display = rm.get_cooling_down_display()

        assert "DOGE" in display
        assert "others ready" in display

    def test_all_expired_returns_no(self, tmp_path):
        rm = _make_rm(tmp_path, cool_down_minutes=15.0)
        rm._last_trade_time_per_pair["DOGEUSDT"] = datetime.now() - timedelta(minutes=20)

        assert rm.get_cooling_down_display() == "No"


class TestCooldownPersistence:
    def test_save_and_load_preserves_per_pair_times(self, tmp_path):
        rm = _make_rm(tmp_path)
        state_file = tmp_path / "rs.json"

        rm.record_trade("DOGEUSDT")
        rm.record_trade("BTCUSDT")
        rm.save_state(str(state_file))

        rm2 = _make_rm(tmp_path)
        rm2.load_state(str(state_file))

        assert "DOGEUSDT" in rm2._last_trade_time_per_pair
        assert "BTCUSDT" in rm2._last_trade_time_per_pair
        assert rm2.check_cooldown("DOGEUSDT") is True
        assert rm2.check_cooldown("BTCUSDT") is True

    def test_no_per_pair_data_falls_back_to_global(self, tmp_path):
        """Old state files without per-pair data fall back to global _last_trade_time."""
        rm = _make_rm(tmp_path, cool_down_minutes=15.0)
        rm._last_trade_time = datetime.now() - timedelta(minutes=5)
        # No per-pair entries

        assert rm.check_cooldown() is True
        assert rm.check_cooldown("ANYUSDT") is False  # unknown pair — not cooling


class TestGlobalSafetyNetsUnchanged:
    def test_max_open_positions_still_blocks(self, tmp_path):
        rm = _make_rm(tmp_path)
        rm.config.max_open_positions = 3
        rm.config.max_daily_trades = 50
        rm.config.min_balance_threshold = 0.0

        result = rm.can_open_position(
            portfolio_value=1000.0,
            open_positions_count=3,
            symbol="DOGEUSDT",
        )

        assert not result.allowed
        assert "max open positions" in result.reason.lower()

    def test_daily_trade_limit_still_blocks(self, tmp_path):
        from datetime import timezone as _tz

        rm = _make_rm(tmp_path)
        rm.config.max_daily_trades = 5
        rm.config.max_open_positions = 10
        rm.config.min_balance_threshold = 0.0
        # Set date to today so check_daily_loss_limit doesn't reset the counter
        rm._daily_loss_date = datetime.now(tz=_tz.utc).date()
        rm._trade_count_today = 5

        result = rm.can_open_position(
            portfolio_value=1000.0,
            open_positions_count=0,
            symbol="DOGEUSDT",
        )

        assert not result.allowed
        assert "max daily trades" in result.reason.lower()

    def test_per_pair_cooldown_blocks_only_that_pair(self, tmp_path):
        rm = _make_rm(tmp_path, cool_down_minutes=15.0)
        rm.config.max_open_positions = 10
        rm.config.max_daily_trades = 50
        rm.config.min_balance_threshold = 0.0
        rm._daily_loss_date = None  # skip daily loss check

        rm.record_trade("DOGEUSDT")

        doge_result = rm.can_open_position(1000.0, 1, symbol="DOGEUSDT")
        btc_result = rm.can_open_position(1000.0, 1, symbol="BTCUSDT")

        assert not doge_result.allowed
        assert "cooldown" in doge_result.reason.lower()
        assert btc_result.allowed
