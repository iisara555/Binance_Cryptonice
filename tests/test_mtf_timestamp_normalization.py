from datetime import datetime, timezone

import pandas as pd

from database import Database
from signal_generator import SignalGenerator
from strategy_base import SignalType


class _MixedTimestampDb:
    def get_candles(self, symbol, interval='1h', start_time=None, end_time=None, limit=None):
        base_rows = [
            {
                'timestamp': datetime(2026, 4, 5, 10, minute, 0),
                'pair': symbol,
                'open': 10.0 + minute,
                'high': 10.2 + minute,
                'low': 9.8 + minute,
                'close': 10.1 + minute,
                'volume': 100.0 + minute,
            }
            for minute in range(35)
        ]
        aware_row = {
            'timestamp': datetime(2026, 4, 5, 10, 35, 0, tzinfo=timezone.utc),
            'pair': symbol,
            'open': 45.0,
            'high': 45.5,
            'low': 44.7,
            'close': 45.2,
            'volume': 150.0,
        }
        return pd.DataFrame(base_rows + [aware_row])


def test_generate_mtf_signals_handles_mixed_naive_and_aware_timestamps():
    generator = SignalGenerator(
        {
            'multi_timeframe': {
                'timeframes': ['1m', '5m', '15m'],
                'alignment_threshold': 0.2,
            }
        }
    )

    result = generator.generate_mtf_signals(
        pair='THB_DOGE',
        timeframes=['1m', '5m', '15m'],
        db=_MixedTimestampDb(),
    )

    assert result is not None
    assert result.timestamp.tzinfo is not None
    assert result.timeframes['1m'].latest_timestamp.tzinfo is None
    assert result.timeframes['1m'].candle_count == 36
    assert result.signals['1m'].signal_type in {SignalType.BUY, SignalType.SELL, SignalType.HOLD}


def test_database_get_candles_normalizes_aware_timestamps_for_sorting(tmp_path):
    db = Database(str(tmp_path / 'mtf-normalization.db'))
    db.insert_prices_batch(
        [
            {
                'pair': 'THB_DOGE',
                'timestamp': datetime(2026, 4, 5, 10, 0, 0),
                'open': 1.0,
                'high': 1.1,
                'low': 0.9,
                'close': 1.05,
                'volume': 10.0,
                'timeframe': '1m',
            },
            {
                'pair': 'THB_DOGE',
                'timestamp': datetime(2026, 4, 5, 10, 1, 0, tzinfo=timezone.utc),
                'open': 1.05,
                'high': 1.2,
                'low': 1.0,
                'close': 1.15,
                'volume': 12.0,
                'timeframe': '1m',
            },
        ]
    )

    candles = db.get_candles('THB_DOGE', interval='1m')

    assert list(candles['close']) == [1.05, 1.15]
    assert candles['timestamp'].iloc[0].tzinfo is None
    assert candles['timestamp'].iloc[1].tzinfo is None