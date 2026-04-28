"""
Tests for C2 remediation: DB writes must fail-loud.

Verifies that OperationalError (e.g. "database is locked") and other
non-IntegrityError exceptions propagate up through _with_retry instead
of being silently swallowed by inner try/except blocks.
"""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from database import Database


@pytest.fixture
def db(temp_db):
    """Create a real Database instance on a temp SQLite file."""
    return Database(temp_db)


# ── save_position ────────────────────────────────────────────────────────────


class TestSavePositionFailLoud:
    """save_position must raise on non-integrity DB errors."""

    def test_operational_error_propagates(self, db):
        """An OperationalError('database is locked') inside save_position
        must bubble up (after retries) instead of returning None silently."""
        pos_data = {
            "order_id": "test_order_1",
            "symbol": "THB_BTC",
            "side": "buy",
            "amount": 0.001,
            "entry_price": 1_000_000,
            "remaining_amount": 0.001,
        }

        lock_err = OperationalError("database is locked", params=None, orig=Exception("database is locked"))

        with patch.object(db, "get_session") as mock_get:
            mock_session = MagicMock()
            mock_session.query.return_value.filter.return_value.first.return_value = None
            mock_session.commit.side_effect = lock_err
            mock_get.return_value = mock_session

            with pytest.raises(OperationalError):
                db.save_position(pos_data)

    def test_successful_save_still_works(self, db):
        """Normal save_position must still return a Position object."""
        pos = db.save_position(
            {
                "order_id": "normal_save_1",
                "symbol": "THB_BTC",
                "side": "buy",
                "amount": 0.001,
                "entry_price": 1_000_000,
                "remaining_amount": 0.001,
            }
        )
        assert pos is not None
        assert pos.order_id == "normal_save_1"


# ── delete_position ──────────────────────────────────────────────────────────


class TestDeletePositionFailLoud:
    """delete_position must raise on non-integrity DB errors."""

    def test_operational_error_propagates(self, db):
        lock_err = OperationalError("database is locked", params=None, orig=Exception("database is locked"))

        with patch.object(db, "get_session") as mock_get:
            mock_session = MagicMock()
            mock_session.query.return_value.filter.return_value.delete.side_effect = lock_err
            mock_get.return_value = mock_session

            with pytest.raises(OperationalError):
                db.delete_position("order_xyz")

    def test_successful_delete_still_works(self, db):
        """delete_position on nonexistent ID returns False (not an error)."""
        result = db.delete_position("nonexistent_order")
        assert result is False


# ── log_closed_trade ─────────────────────────────────────────────────────────


class TestLogClosedTradeFailLoud:
    """log_closed_trade must raise on non-integrity DB errors."""

    def test_operational_error_propagates(self, db):
        trade_data = {
            "symbol": "THB_BTC",
            "side": "sell",
            "amount": 0.001,
            "entry_price": 1_000_000,
            "exit_price": 1_050_000,
        }

        lock_err = OperationalError("database is locked", params=None, orig=Exception("database is locked"))

        with patch.object(db, "get_session") as mock_get:
            mock_session = MagicMock()
            mock_session.commit.side_effect = lock_err
            mock_get.return_value = mock_session

            with pytest.raises(OperationalError):
                db.log_closed_trade(trade_data)

    def test_successful_log_still_works(self, db):
        ct = db.log_closed_trade(
            {
                "symbol": "THB_BTC",
                "side": "sell",
                "amount": 0.001,
                "entry_price": 1_000_000,
                "exit_price": 1_050_000,
            }
        )
        assert ct is not None
        assert ct.symbol == "THB_BTC"


# ── save_trade_state ─────────────────────────────────────────────────────────


class TestSaveTradeStateFailLoud:
    """save_trade_state must raise on non-integrity DB errors."""

    def test_operational_error_propagates(self, db):
        lock_err = OperationalError("database is locked", params=None, orig=Exception("database is locked"))

        with patch.object(db, "get_session") as mock_get:
            mock_session = MagicMock()
            mock_session.query.return_value.filter.return_value.first.return_value = None
            mock_session.commit.side_effect = lock_err
            mock_get.return_value = mock_session

            with pytest.raises(OperationalError):
                db.save_trade_state({"symbol": "THB_BTC", "state": "idle"})

    def test_successful_save_still_works(self, db):
        ts = db.save_trade_state(
            {
                "symbol": "THB_BTC",
                "state": "pending_buy",
                "side": "buy",
            }
        )
        assert ts is not None
        assert ts.symbol == "THB_BTC"


# ── delete_trade_state ───────────────────────────────────────────────────────


class TestDeleteTradeStateFailLoud:
    """delete_trade_state must raise on non-integrity DB errors."""

    def test_operational_error_propagates(self, db):
        lock_err = OperationalError("database is locked", params=None, orig=Exception("database is locked"))

        with patch.object(db, "get_session") as mock_get:
            mock_session = MagicMock()
            mock_session.query.return_value.filter.return_value.delete.side_effect = lock_err
            mock_get.return_value = mock_session

            with pytest.raises(OperationalError):
                db.delete_trade_state("THB_BTC")


# ── update_position_sl ───────────────────────────────────────────────────────


class TestUpdatePositionSLFailLoud:
    """update_position_sl must raise on non-integrity DB errors."""

    def test_operational_error_propagates(self, db):
        lock_err = OperationalError("database is locked", params=None, orig=Exception("database is locked"))

        with patch.object(db, "get_session") as mock_get:
            mock_session = MagicMock()
            mock_pos = MagicMock()
            mock_session.query.return_value.filter.return_value.first.return_value = mock_pos
            mock_session.commit.side_effect = lock_err
            mock_get.return_value = mock_session

            with pytest.raises(OperationalError):
                db.update_position_sl("order_1", 95000.0)
