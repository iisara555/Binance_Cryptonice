import sqlite3
from datetime import datetime
from unittest.mock import Mock

import pytest
import requests

from bitkub_websocket import BitkubWebSocket
from database import Database
from telegram_bot import TelegramBotHandler


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