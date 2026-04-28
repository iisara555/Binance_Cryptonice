from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd

from database import Database
from signal_generator import SignalGenerator
from strategy_base import SignalType


class _MixedTimestampDb:
    def get_candles(self, symbol, interval="1h", start_time=None, end_time=None, limit=None):
        base_rows = [
            {
                "timestamp": datetime(2026, 4, 5, 10, minute, 0),
                "pair": symbol,
                "open": 10.0 + minute,
                "high": 10.2 + minute,
                "low": 9.8 + minute,
                "close": 10.1 + minute,
                "volume": 100.0 + minute,
            }
            for minute in range(35)
        ]
        aware_row = {
            "timestamp": datetime(2026, 4, 5, 10, 35, 0, tzinfo=timezone.utc),
            "pair": symbol,
            "open": 45.0,
            "high": 45.5,
            "low": 44.7,
            "close": 45.2,
            "volume": 150.0,
        }
        return pd.DataFrame(base_rows + [aware_row])


def test_generate_mtf_signals_handles_mixed_naive_and_aware_timestamps():
    generator = SignalGenerator(
        {
            "multi_timeframe": {
                "timeframes": ["1m", "5m", "15m"],
                "alignment_threshold": 0.2,
            }
        }
    )

    result = generator.generate_mtf_signals(
        pair="THB_DOGE",
        timeframes=["1m", "5m", "15m"],
        db=_MixedTimestampDb(),
    )

    assert result is not None
    assert result.timestamp.tzinfo is not None
    assert result.timeframes["1m"].latest_timestamp.tzinfo is None
    assert result.timeframes["1m"].candle_count == 36
    assert result.signals["1m"].signal_type in {SignalType.BUY, SignalType.SELL, SignalType.HOLD}


def test_database_get_candles_normalizes_aware_timestamps_for_sorting(tmp_path):
    db = Database(str(tmp_path / "mtf-normalization.db"))
    db.insert_prices_batch(
        [
            {
                "pair": "THB_DOGE",
                "timestamp": datetime(2026, 4, 5, 10, 0, 0),
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.05,
                "volume": 10.0,
                "timeframe": "1m",
            },
            {
                "pair": "THB_DOGE",
                "timestamp": datetime(2026, 4, 5, 10, 1, 0, tzinfo=timezone.utc),
                "open": 1.05,
                "high": 1.2,
                "low": 1.0,
                "close": 1.15,
                "volume": 12.0,
                "timeframe": "1m",
            },
        ]
    )

    candles = db.get_candles("THB_DOGE", interval="1m")

    assert list(candles["close"]) == [1.05, 1.15]
    assert candles["timestamp"].iloc[0].tzinfo is None
    assert candles["timestamp"].iloc[1].tzinfo is None


def test_generate_mtf_signals_uses_short_lived_in_memory_cache():
    class _CountingDb:
        def __init__(self):
            self.calls = 0

        def get_candles(self, symbol, interval="1h", start_time=None, end_time=None, limit=None):
            self.calls += 1
            rows = [
                {
                    "timestamp": datetime(2026, 4, 5, 10, minute, 0),
                    "pair": symbol,
                    "open": 10.0 + minute,
                    "high": 10.2 + minute,
                    "low": 9.8 + minute,
                    "close": 10.1 + minute,
                    "volume": 100.0 + minute,
                }
                for minute in range(36)
            ]
            return pd.DataFrame(rows)

    db = _CountingDb()
    generator = SignalGenerator(
        {
            "multi_timeframe": {
                "timeframes": ["1m", "5m", "15m"],
                "alignment_threshold": 0.2,
                "cache_ttl_seconds": 60,
            }
        }
    )

    first = generator.generate_mtf_signals(
        pair="THB_DOGE",
        timeframes=["1m", "5m", "15m"],
        db=db,
    )
    second = generator.generate_mtf_signals(
        pair="THB_DOGE",
        timeframes=["1m", "5m", "15m"],
        db=db,
    )

    assert first is not None
    assert second is not None
    assert db.calls == 3
    assert second is not first


def test_generate_mtf_signals_reuses_closed_candle_indicators_while_forming_candle_moves():
    class _MutableDb:
        def __init__(self):
            self.forming_close = 30.0

        def get_candles(self, symbol, interval="1h", start_time=None, end_time=None, limit=None):
            rows = [
                {
                    "timestamp": datetime(2026, 4, 5, 10, minute, 0),
                    "pair": symbol,
                    "open": 10.0 + minute,
                    "high": 10.4 + minute,
                    "low": 9.8 + minute,
                    "close": 10.1 + minute,
                    "volume": 100.0 + minute,
                }
                for minute in range(35)
            ]
            rows.append(
                {
                    "timestamp": datetime(2026, 4, 5, 10, 35, 0),
                    "pair": symbol,
                    "open": 45.0,
                    "high": max(self.forming_close, 45.0),
                    "low": min(self.forming_close, 45.0),
                    "close": self.forming_close,
                    "volume": 200.0,
                }
            )
            return pd.DataFrame(rows)

    db = _MutableDb()
    generator = SignalGenerator(
        {
            "multi_timeframe": {
                "timeframes": ["1m"],
                "alignment_threshold": 0.2,
                "cache_ttl_seconds": 0,
                "indicator_cache_ttl_seconds": 300,
            }
        }
    )

    with patch(
        "multi_timeframe.TechnicalIndicators.calculate_rsi", side_effect=lambda close: pd.Series([65.0] * len(close))
    ) as mock_rsi, patch(
        "multi_timeframe.TechnicalIndicators.calculate_macd",
        side_effect=lambda close: (
            pd.Series([1.0] * len(close)),
            pd.Series([0.5] * len(close)),
            pd.Series([0.5] * len(close)),
        ),
    ) as mock_macd, patch(
        "multi_timeframe.TechnicalIndicators.calculate_adx",
        side_effect=lambda high, low, close: pd.Series([25.0] * len(close)),
    ) as mock_adx, patch(
        "multi_timeframe.TechnicalIndicators.calculate_atr",
        side_effect=lambda high, low, close: pd.Series([1.0] * len(close)),
    ) as mock_atr:
        first = generator.generate_mtf_signals(
            pair="THB_DOGE",
            timeframes=["1m"],
            db=db,
        )
        generator._mtf_cache.clear()
        db.forming_close = 60.0
        second = generator.generate_mtf_signals(
            pair="THB_DOGE",
            timeframes=["1m"],
            db=db,
        )

    assert first is not None
    assert second is not None
    assert first.timeframes["1m"].latest_close == 30.0
    assert second.timeframes["1m"].latest_close == 60.0
    assert second.signals["1m"].indicators["ema_slow"] == first.signals["1m"].indicators["ema_slow"]
    assert second.signals["1m"].indicators["macd_hist"] == first.signals["1m"].indicators["macd_hist"]
    assert mock_rsi.call_count == 1
    assert mock_macd.call_count == 1
    assert mock_adx.call_count == 1
    assert mock_atr.call_count == 1
