import sqlite3
import time
from datetime import datetime
from unittest.mock import Mock

import pytest
import requests

from bitkub_websocket import BitkubWebSocket, PriceTick
from database import Database
from helpers import get_current_price
from telegram_bot import TelegramBotHandler
from trading.managed_lifecycle import _resolve_sane_entry_cost as resolve_managed_entry_cost
from trading.position_monitor import _resolve_sane_entry_cost as resolve_monitor_entry_cost


def test_database_migrates_legacy_prices_unique_key_to_include_timeframe(tmp_path):
    db_path = tmp_path / "legacy-prices.db"

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                pair VARCHAR(20) NOT NULL,
                open FLOAT NOT NULL,
                high FLOAT NOT NULL,
                low FLOAT NOT NULL,
                close FLOAT NOT NULL,
                volume FLOAT NOT NULL,
                timeframe TEXT DEFAULT '1h',
                UNIQUE(pair, timestamp)
            )
            """
        )
        cursor.execute(
            """
            INSERT INTO prices (pair, timestamp, timeframe, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ('THB_DOGE', datetime(2026, 4, 5, 10, 0, 0), '1m', 1.0, 1.1, 0.9, 1.05, 10.0),
        )
        conn.commit()

    db = Database(str(db_path))
    inserted = db.insert_price(
        pair='THB_DOGE',
        timestamp=datetime(2026, 4, 5, 10, 0, 0),
        open=1.02,
        high=1.12,
        low=0.95,
        close=1.08,
        volume=11.0,
        timeframe='5m',
    )

    assert inserted is not None

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pair, timestamp, timeframe, close FROM prices ORDER BY timeframe"
        )
        rows = cursor.fetchall()
        cursor.execute("PRAGMA index_list('prices')")
        index_rows = cursor.fetchall()

        unique_indexes = []
        for index_row in index_rows:
            if not index_row[2]:
                continue
            index_name = index_row[1]
            cursor.execute(f"PRAGMA index_info('{index_name}')")
            unique_indexes.append([col[2] for col in cursor.fetchall()])

    assert len(rows) == 2
    assert [row[2] for row in rows] == ['1m', '5m']
    assert ['pair', 'timestamp', 'timeframe'] in unique_indexes


def test_insert_prices_batch_upserts_duplicate_timeframe_rows(tmp_path):
    db = Database(str(tmp_path / 'prices-upsert.db'))
    timestamp = datetime(2026, 4, 5, 10, 0, 0)

    first_batch = [
        {
            'pair': 'THB_DOGE',
            'timestamp': timestamp,
            'open': 1.0,
            'high': 1.1,
            'low': 0.9,
            'close': 1.05,
            'volume': 10.0,
            'timeframe': '1m',
        }
    ]
    second_batch = [
        {
            'pair': 'THB_DOGE',
            'timestamp': timestamp,
            'open': 1.02,
            'high': 1.2,
            'low': 0.95,
            'close': 1.15,
            'volume': 12.0,
            'timeframe': '1m',
        }
    ]

    db.insert_prices_batch(first_batch)
    db.insert_prices_batch(second_batch)

    candles = db.get_candles('THB_DOGE', interval='1m')

    assert len(candles) == 1
    assert candles['close'].iloc[0] == pytest.approx(1.15)
    assert candles['volume'].iloc[0] == pytest.approx(12.0)


def test_telegram_handler_stops_polling_after_409_conflict():
    app_ref = Mock()
    app_ref.alert_system = None
    handler = TelegramBotHandler(app_ref=app_ref, bot_token='token', chat_id='1234')

    response = Mock()
    response.status_code = 409
    handler.telegram.get_updates = Mock(
        side_effect=requests.exceptions.HTTPError('409 Client Error: Conflict', response=response)
    )
    handler._running = True

    handler._poll_loop()

    assert handler._running is False
    assert handler.telegram.get_updates.call_count == 1


def test_websocket_recent_messages_count_as_heartbeat_activity():
    ws = BitkubWebSocket(['THB_BTC'], on_tick=None)
    ws._last_pong_time = 100.0
    ws._last_activity_time = 155.0

    assert ws._seconds_since_last_activity(now=160.0) == pytest.approx(5.0)
    assert ws._seconds_since_last_activity(now=160.0) < ws.HEARTBEAT_INTERVAL * 2


def test_websocket_heartbeat_uses_warning_and_reconnect_grace_windows():
    ws = BitkubWebSocket(['THB_BTC'], on_tick=None)
    ws._last_pong_time = 100.0
    ws._last_activity_time = 100.0

    assert ws._should_warn_heartbeat_stale(now=131.0) is True      # 31s > 15*2=30s warning
    assert ws._should_force_heartbeat_reconnect(now=159.0) is False  # 59s < 15*4=60s reconnect
    assert ws._should_force_heartbeat_reconnect(now=161.0) is True   # 61s > 60s reconnect


def test_websocket_reports_connection_age_and_proactive_recycle_stats():
    ws = BitkubWebSocket(['THB_BTC'], on_tick=None)
    ws._last_connection_time = 100.0
    ws._last_pong_time = 190.0
    ws._last_activity_time = 190.0
    ws._stats['proactive_recycles'] = 2

    now = 200.0
    assert ws._connection_age_seconds(now=now) == pytest.approx(100.0)

    # Keep stats contract stable for dashboards/health endpoints.
    stats = ws.get_stats()
    assert 'connection_age_seconds' in stats
    assert 'proactive_recycles' in stats
    assert stats['proactive_recycles'] == 2


def test_get_current_price_uses_rest_when_ws_tick_is_stale(monkeypatch):
    stale_tick = PriceTick(
        symbol='THB_BTC',
        last=2_300_000.0,
        bid=2_299_900.0,
        ask=2_300_100.0,
        percent_change_24h=0.0,
        timestamp=1.0,
    )
    monkeypatch.setattr('bitkub_websocket.get_latest_ticker', lambda _symbol: stale_tick)

    class _Api:
        @staticmethod
        def get_ticker(_symbol):
            return {'last': 2_350_000.0}

    price, source = get_current_price('THB_BTC', api_client=_Api(), ws_client=object())

    assert source == 'rest'
    assert price == pytest.approx(2_350_000.0)


def test_get_current_price_uses_ws_stale_last_resort_when_rest_unavailable(monkeypatch):
    stale_tick = PriceTick(
        symbol='THB_BTC',
        last=2_300_000.0,
        bid=2_299_900.0,
        ask=2_300_100.0,
        percent_change_24h=0.0,
        timestamp=1.0,
    )
    monkeypatch.setattr('bitkub_websocket.get_latest_ticker', lambda _symbol: stale_tick)

    class _Api:
        @staticmethod
        def get_ticker(_symbol):
            raise RuntimeError('REST down')

    price, source = get_current_price('THB_BTC', api_client=_Api(), ws_client=object())

    assert source == 'ws_stale'
    assert price == pytest.approx(2_300_000.0)


def test_get_current_price_prefers_ws_client_native_getter():
    class _Tick:
        last = 2_456_789.0
        timestamp = time.time()

    class _WsClient:
        @staticmethod
        def get_latest_ticker(_symbol):
            return _Tick()

    class _Api:
        @staticmethod
        def get_ticker(_symbol):
            return {'last': 2_300_000.0}

    price, source = get_current_price('BTCUSDT', api_client=_Api(), ws_client=_WsClient())

    assert source == 'ws'
    assert price == pytest.approx(2_456_789.0)


def test_entry_cost_guard_uses_implied_cost_when_reported_cost_drift_is_large():
    amount = 6.05e-05
    entry_price = 2_293_139.4
    reported_cost = 200.0
    implied_cost = amount * entry_price

    managed_cost = resolve_managed_entry_cost(
        symbol='THB_BTC',
        amount=amount,
        entry_price=entry_price,
        reported_entry_cost=reported_cost,
    )
    monitor_cost = resolve_monitor_entry_cost(
        symbol='THB_BTC',
        amount=amount,
        entry_price=entry_price,
        reported_entry_cost=reported_cost,
    )

    assert managed_cost == pytest.approx(implied_cost)
    assert monitor_cost == pytest.approx(implied_cost)


def test_entry_cost_guard_keeps_reported_cost_when_within_tolerance():
    amount = 0.1
    entry_price = 1000.0
    reported_cost = 101.5  # 1.5% drift, below guard threshold

    managed_cost = resolve_managed_entry_cost(
        symbol='THB_TEST',
        amount=amount,
        entry_price=entry_price,
        reported_entry_cost=reported_cost,
    )
    monitor_cost = resolve_monitor_entry_cost(
        symbol='THB_TEST',
        amount=amount,
        entry_price=entry_price,
        reported_entry_cost=reported_cost,
    )

    assert managed_cost == pytest.approx(reported_cost)
    assert monitor_cost == pytest.approx(reported_cost)