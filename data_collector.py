"""
Data Collector for Crypto Trading Bot
Fetches price data from Bitkub API and stores in SQLite database
"""

import time
import logging
import atexit
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from database import get_database

logger = logging.getLogger(__name__)

# Module-level executor for parallel API calls (shared across instances)
_executor = ThreadPoolExecutor(max_workers=6)
atexit.register(_executor.shutdown, wait=False)


class BitkubCollector:
    """Collects OHLCV data from Bitkub API"""
    
    BASE_URL = "https://api.bitkub.com"
    
    # Timeframe intervals in seconds
    INTERVALS = {
        '1m': 60,
        '5m': 300,
        '15m': 900,
        '1h': 3600,
        '1d': 86400,
    }

    def __init__(
        self,
        pairs: List[str] = None,
        interval: int = 60,
        multi_timeframe_config: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize collector
        
        Args:
            pairs: List of trading pairs (e.g., ['THB_BTC', 'THB_ETH']). 
                   MUST be provided from config. Falls back to ['THB_BTC'] only if not specified.
            interval: Collection interval in seconds
                 multi_timeframe_config: Optional multi-timeframe collection settings
        """
        # Accept an explicit empty list so runtime auto-detection can disable collection cleanly.
        self.pairs = list(pairs) if pairs is not None else []
        self.interval = interval
        self.db = get_database()
        self.running = False
        self._thread = None
        self._pairs_lock = threading.Lock()
        mtf_config = dict(multi_timeframe_config or {})
        self.multi_timeframe_enabled = bool(mtf_config.get('enabled', False))
        self.multi_timeframes = [
            str(timeframe).strip()
            for timeframe in (mtf_config.get('timeframes') or ['1m', '5m', '15m', '1h'])
            if str(timeframe).strip()
        ]
        if not self.multi_timeframes:
            self.multi_timeframes = ['1m', '5m', '15m', '1h']
        refresh_interval = mtf_config.get('collection_interval_seconds')
        if refresh_interval is None:
            refresh_interval = mtf_config.get('refresh_interval_seconds')
        if refresh_interval is None:
            refresh_interval = interval
        try:
            self.multi_timeframe_interval = max(int(refresh_interval), max(int(interval), 1))
        except (TypeError, ValueError):
            self.multi_timeframe_interval = max(int(interval), 1)
        self._last_multi_timeframe_run: Optional[datetime] = None
        self._last_multi_timeframe_results: Dict[str, Dict[str, int]] = {}
        self._next_multi_timeframe_collect_at = 0.0
        
        logger.info(f"BitkubCollector initialized with pairs: {self.pairs}")

    def set_pairs(self, pairs: List[str]):
        """Update the collector pair set safely at runtime."""
        normalized = [str(pair).upper() for pair in (pairs or []) if str(pair).strip()]
        previous_pairs = self.get_pairs()
        with self._pairs_lock:
            self.pairs = normalized
        logger.info("Collector pairs updated: %s", normalized)
        added_pairs = [pair for pair in normalized if pair not in previous_pairs]
        if self.running and self.multi_timeframe_enabled and added_pairs:
            self._warm_pairs_multi_timeframe(added_pairs)

    def get_pairs(self) -> List[str]:
        """Return a snapshot of the current collector pairs."""
        with self._pairs_lock:
            return list(self.pairs)

    @staticmethod
    def _format_collection_timestamp(value: Optional[datetime]) -> str:
        """Format collector timestamps consistently for human-readable logs."""
        if value is None:
            return "unknown"

        if hasattr(value, 'to_pydatetime'):
            value = value.to_pydatetime()

        if not isinstance(value, datetime):
            return str(value)

        normalized = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return normalized.isoformat(timespec='seconds').replace('+00:00', 'Z')

    def _collect_ohlc_result(self, pair: str, interval: int = 1, timeframe: str = None) -> Dict[str, Any]:
        """Collect OHLC data and return a detailed outcome for richer logging."""
        if timeframe is None:
            tf_map = {1: '1m', 5: '5m', 15: '15m', 60: '1h', 240: '4h', 1440: '1d'}
            timeframe = tf_map.get(interval, f'{interval}m')

        outcome: Dict[str, Any] = {
            'pair': pair,
            'timeframe': timeframe,
            'stored': 0,
            'status': 'no_data',
            'latest_stored': None,
            'latest_fetched': None,
        }

        ohlc_data = self._normalize_ohlc_payload(self.get_ohlc(pair, interval))
        if not ohlc_data:
            return outcome

        latest_stored = self.db.get_latest_price(pair, timeframe=timeframe)
        latest_timestamp = latest_stored.timestamp if latest_stored else None
        outcome['latest_stored'] = latest_timestamp
        if latest_timestamp is not None and latest_timestamp.tzinfo is None:
            latest_timestamp = latest_timestamp.replace(tzinfo=timezone.utc)

        candles_to_insert = []
        latest_fetched = None
        for candle in ohlc_data:
            if len(candle) >= 6:
                try:
                    raw_timestamp = float(candle[0])
                    timestamp_seconds = raw_timestamp / 1000 if raw_timestamp > 1_000_000_000_000 else raw_timestamp
                    candle_timestamp = datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc)
                    if latest_fetched is None or candle_timestamp > latest_fetched:
                        latest_fetched = candle_timestamp
                    if latest_timestamp is not None and candle_timestamp <= latest_timestamp:
                        continue
                    candles_to_insert.append({
                        'pair': pair,
                        'timestamp': candle_timestamp,
                        'open': float(candle[1]),
                        'high': float(candle[2]),
                        'low': float(candle[3]),
                        'close': float(candle[4]),
                        'volume': float(candle[5]),
                        'timeframe': timeframe,
                    })
                except (ValueError, IndexError) as e:
                    logger.warning(f"Invalid candle data: {e}")
                    continue

        outcome['latest_fetched'] = latest_fetched

        if not candles_to_insert:
            if latest_timestamp is not None and latest_fetched is not None and latest_fetched <= latest_timestamp:
                outcome['status'] = 'up_to_date'
            return outcome

        try:
            stored = self.db.insert_prices_batch(candles_to_insert)
        except Exception as e:
            logger.error(f"Batch insert failed for {pair} {timeframe}: {e}")
            stored = 0
            for price_data in candles_to_insert:
                try:
                    result = self.db.insert_price(**price_data)
                    if result:
                        stored += 1
                except Exception:
                    continue

        outcome['stored'] = stored
        outcome['status'] = 'stored' if stored > 0 else 'up_to_date'
        return outcome

    def _log_ohlc_collection_result(self, result: Dict[str, Any]) -> None:
        """Emit a collector log line that explains whether zero inserts are healthy."""
        pair = result.get('pair', 'unknown')
        timeframe = result.get('timeframe', 'unknown')
        stored = int(result.get('stored', 0) or 0)
        status = result.get('status', 'stored')

        if status == 'stored':
            if stored > 0:
                logger.info(f"[{pair}] {timeframe}: stored {stored} new candle(s)")
            else:
                logger.debug(f"[{pair}] {timeframe}: stored 0 new candle(s)")
            return

        if status == 'up_to_date':
            latest_stored = self._format_collection_timestamp(result.get('latest_stored'))
            latest_fetched = self._format_collection_timestamp(result.get('latest_fetched'))
            logger.info(
                f"[{pair}] {timeframe}: no new closed candles, already up to date "
                f"(stored={latest_stored}, fetched={latest_fetched})"
            )
            return

        logger.debug(f"[{pair}] {timeframe}: no candle data returned")
        
    def _make_request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Make API request to Bitkub with error handling"""
        url = f"{self.BASE_URL}/{endpoint}"
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            # v2 format: {"error": 0, "result": ...}
            if isinstance(data, dict) and 'error' in data:
                if data.get('error') == 0:
                    return data.get('result')
                else:
                    logger.error(f"Bitkub API error: {data.get('message')}")
                    return None
            # v3 format: direct list or dict
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {e}")
            return None

    def get_current_ticker(self, pair: str) -> Optional[Dict]:
        """Get current ticker price for a pair using Bitkub v3 API"""
        endpoint = "/api/v3/market/ticker"
        result = self._make_request(endpoint)
        # Convert THB_BTC -> BTC_THB for API
        api_symbol = pair.split('_')[1] + '_' + pair.split('_')[0]
        # v3 returns list of all tickers - filter for our pair
        if isinstance(result, list):
            for ticker in result:
                if ticker.get('symbol') == api_symbol:
                    return {
                        'pair': pair,
                        'last': float(ticker.get('last', 0)),
                        'high': float(ticker.get('high_24_hr', 0)),
                        'low': float(ticker.get('low_24_hr', 0)),
                        'highestBid': float(ticker.get('highest_bid', 0)),
                        'lowestAsk': float(ticker.get('lowest_ask', 0)),
                        'volume': float(ticker.get('base_volume', 0)),
                        'quoteVolume': float(ticker.get('quote_volume', 0)),
                        'timestamp': int(time.time()),
                    }
        return None

    def get_ohlc(self, pair: str, interval: int = 1) -> Optional[List]:
        """
        Get OHLC data from Bitkub TradingView history endpoint

        Args:
            pair: Trading pair (e.g., 'THB_BTC')
            interval: Timeframe in minutes (1, 5, 15, 60, 240, 1440)
        """
        # Map interval to TradingView resolution
        tf_map = {1: "1", 5: "5", 15: "15", 30: "30",
                  60: "60", 240: "240", 1440: "1D"}
        resolution = tf_map.get(interval, "60")
        api_symbol = pair.split('_')[1] + '_' + pair.split('_')[0]
        endpoint = "/tradingview/history"
        now = int(time.time())
        lookback_seconds = max(int(interval), 1) * 60 * 100
        params = {
            "symbol": api_symbol,
            "resolution": resolution,
            "from": max(now - lookback_seconds, 0),
            "to": now,
        }
        return self._make_request(endpoint, params)

    def _normalize_ohlc_payload(self, payload: Any) -> List[List[float]]:
        """Normalize legacy list payloads and TradingView dict payloads into candle rows."""
        if not payload:
            return []

        if isinstance(payload, list):
            return payload

        normalized = payload.get('result') if isinstance(payload, dict) and isinstance(payload.get('result'), dict) else payload
        if not isinstance(normalized, dict):
            return []

        timestamps = normalized.get('t') or []
        opens = normalized.get('o') or []
        highs = normalized.get('h') or []
        lows = normalized.get('l') or []
        closes = normalized.get('c') or []
        volumes = normalized.get('v') or []

        candles: List[List[float]] = []
        for timestamp, open_price, high_price, low_price, close_price, volume in zip(
            timestamps,
            opens,
            highs,
            lows,
            closes,
            volumes,
        ):
            candles.append([timestamp, open_price, high_price, low_price, close_price, volume])

        if not candles and str(normalized.get('s') or '').lower() == 'no_data':
            logger.debug("No OHLC data returned for payload status=no_data")

        return candles

    def collect_price(self, pair: str) -> bool:
        """
        Collect current price and store in database
        
        Returns:
            True if successful, False otherwise
        """
        ticker = self.get_current_ticker(pair)
        
        if ticker:
            price_data = {
                'pair': pair,
                'timestamp': datetime.now(timezone.utc),
                'open': ticker['last'],      # Open price (same as last for snapshot)
                'high': ticker['high'],       # 24h high from ticker
                'low': ticker['low'],         # 24h low from ticker
                'close': ticker['last'],
                'volume': ticker.get('quoteVolume', 0),
            }
            
            try:
                self.db.insert_price(**price_data)
                logger.debug(f"Stored price for {pair}: {ticker['last']}")
                return True
            except Exception as e:
                logger.error(f"Failed to store price for {pair}: {e}")
        
        return False

    def collect_ohlc(self, pair: str, interval: int = 1, timeframe: str = None) -> int:
        """
        Collect OHLC data and store in database using BATCH insert.

        Args:
            pair: Trading pair
            interval: Timeframe in minutes
            timeframe: Timeframe label (e.g., '1m', '5m', '1h').
                      Auto-derived from interval if not provided.

        Returns:
            Number of candles stored
        """
        return int(self._collect_ohlc_result(pair, interval, timeframe)['stored'])

    def collect_multi_timeframe(self, pair: str, timeframes: List[str] = None) -> Dict[str, int]:
        """
        Collect OHLC data for multiple timeframes IN PARALLEL.

        Args:
            pair: Trading pair
            timeframes: List of timeframe labels ['1m', '5m', '15m', '1h', '4h', '1d']

        Returns:
            Dict of {timeframe: count_of_candles}
        """
        if timeframes is None:
            timeframes = ['1m', '5m', '15m', '1h']

        # Map timeframe label to minutes
        tf_to_minutes = {
            '1m': 1, '5m': 5, '15m': 15,
            '1h': 60, '4h': 240, '1d': 1440
        }

        results = {}
        futures = {}

        # Submit all timeframe collection tasks in parallel
        for tf in timeframes:
            minutes = tf_to_minutes.get(tf, 60)
            future = _executor.submit(self._collect_ohlc_result, pair, minutes, tf)
            futures[future] = tf

        # Collect results as they complete
        for future in as_completed(futures):
            tf = futures[future]
            try:
                detail = future.result()
                if isinstance(detail, dict):
                    results[tf] = int(detail.get('stored', 0) or 0)
                    self._log_ohlc_collection_result(detail)
                else:
                    count = int(detail or 0)
                    results[tf] = count
                    if count > 0:
                        logger.info(f"[{pair}] {tf}: stored {count} new candle(s)")
                    else:
                        logger.debug(f"[{pair}] {tf}: stored 0 new candle(s)")
            except Exception as e:
                logger.error(f"[{pair}] {tf} collection failed: {e}")
                results[tf] = 0

        return results

    def _collect_multi_timeframe_for_pairs(self, pairs: Optional[List[str]] = None) -> Dict[str, Dict[str, int]]:
        """Collect multi-timeframe candles across pairs without nesting the shared executor."""
        target_pairs = [str(pair).upper() for pair in (pairs or self.get_pairs()) if str(pair).strip()]
        if not target_pairs:
            return {}

        if len(target_pairs) == 1:
            pair = target_pairs[0]
            try:
                return {pair: self.collect_multi_timeframe(pair, self.multi_timeframes)}
            except Exception as e:
                logger.error("collect_multi_timeframe(%s) failed: %s", pair, e)
                return {pair: {tf: 0 for tf in self.multi_timeframes}}

        results: Dict[str, Dict[str, int]] = {}
        max_workers = min(len(target_pairs), 4)
        with ThreadPoolExecutor(max_workers=max_workers) as pair_executor:
            futures = {
                pair: pair_executor.submit(self.collect_multi_timeframe, pair, self.multi_timeframes)
                for pair in target_pairs
            }
            for pair, future in futures.items():
                try:
                    results[pair] = future.result()
                except Exception as e:
                    logger.error("collect_multi_timeframe(%s) failed: %s", pair, e)
                    results[pair] = {tf: 0 for tf in self.multi_timeframes}

        return results

    def collect_all_multi_timeframe(self) -> Dict[str, Dict[str, int]]:
        """Collect multi-timeframe OHLC data for all configured pairs in parallel."""
        return self._collect_multi_timeframe_for_pairs(self.get_pairs())

    def _warm_pairs_multi_timeframe(self, pairs: Optional[List[str]] = None) -> Dict[str, Dict[str, int]]:
        """Prime missing multi-timeframe candles immediately for startup or newly added pairs."""
        if not self.multi_timeframe_enabled:
            return {}

        target_pairs = [str(pair).upper() for pair in (pairs or self.get_pairs()) if str(pair).strip()]
        if not target_pairs:
            return {}

        results = self._collect_multi_timeframe_for_pairs(target_pairs)

        merged_results = dict(getattr(self, "_last_multi_timeframe_results", {}) or {})
        merged_results.update(results)
        self._last_multi_timeframe_results = merged_results
        self._last_multi_timeframe_run = datetime.now(timezone.utc)
        self._next_multi_timeframe_collect_at = time.time() + self.multi_timeframe_interval
        return results

    def collect_all(self) -> Dict[str, bool]:
        """Collect prices for all configured pairs IN PARALLEL."""
        results = {}
        pairs = self.get_pairs()
        futures = {pair: _executor.submit(self.collect_price, pair) for pair in pairs}
        for pair, future in futures.items():
            try:
                results[pair] = future.result()
            except Exception as e:
                logger.error(f"collect_price({pair}) failed: {e}")
                results[pair] = False
        return results

    def collect_all_ohlc(self, interval: int = 1) -> Dict[str, int]:
        """Collect OHLC data for all configured pairs IN PARALLEL."""
        results = {}
        pairs = self.get_pairs()
        futures = {pair: _executor.submit(self.collect_ohlc, pair, interval) for pair in pairs}
        for pair, future in futures.items():
            try:
                results[pair] = future.result()
            except Exception as e:
                logger.error(f"collect_ohlc({pair}) failed: {e}")
                results[pair] = 0
        return results

    def _collector_loop(self):
        """Main collection loop (runs in background thread)"""
        logger.info(f"Collector started - interval: {self.interval}s, pairs: {self.get_pairs()}")
        
        while self.running:
            try:
                results = self.collect_all()
                current_pairs = self.get_pairs()
                success_count = sum(1 for v in results.values() if v)
                logger.debug(f"Collected {success_count}/{len(current_pairs)} pairs")

                if self.multi_timeframe_enabled and time.time() >= self._next_multi_timeframe_collect_at:
                    mtf_results = self.collect_all_multi_timeframe()
                    self._last_multi_timeframe_results = mtf_results
                    self._last_multi_timeframe_run = datetime.now(timezone.utc)
                    self._next_multi_timeframe_collect_at = time.time() + self.multi_timeframe_interval
                    logger.debug(
                        "Collected multi-timeframe candles for %s pair(s): %s",
                        len(mtf_results),
                        self.multi_timeframes,
                    )
            except Exception as e:
                logger.error(f"Collection error: {e}")
            
            # Sleep in small increments for faster shutdown
            for _ in range(self.interval):
                if not self.running:
                    break
                time.sleep(1)
        
        logger.info("Collector stopped")

    def start(self, blocking: bool = False):
        """
        Start collecting data
        
        Args:
            blocking: If True, run in current thread. If False, run in background.
        """
        self.running = True
        if self.multi_timeframe_enabled:
            self._warm_pairs_multi_timeframe()

        if blocking:
            self._collector_loop()
        else:
            self._thread = threading.Thread(target=self._collector_loop, daemon=True)
            self._thread.start()

    def stop(self):
        """Stop the collector"""
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Collector shutdown complete")

    def get_status(self) -> Dict[str, Any]:
        """Expose collector runtime state for observability."""
        return {
            'running': self.running,
            'pairs': self.get_pairs(),
            'interval_seconds': self.interval,
            'multi_timeframe': {
                'enabled': self.multi_timeframe_enabled,
                'timeframes': list(self.multi_timeframes),
                'interval_seconds': self.multi_timeframe_interval,
                'last_run': self._last_multi_timeframe_run.isoformat() if self._last_multi_timeframe_run else None,
                'last_results': self._last_multi_timeframe_results,
            },
        }


class DataAggregator:
    """Aggregates collected data for daily summaries"""
    
    def __init__(self):
        self.db = get_database()
    
    def get_daily_summary(self, pair: str, days: int = 30) -> List[Dict]:
        """Get daily OHLCV summaries"""
        prices = self.db.get_price_df(pair, days=days)
        
        if not prices:
            return []
        
        # Group by date
        daily_data = {}
        for p in prices:
            date_key = p['timestamp'].date().isoformat()
            
            if date_key not in daily_data:
                daily_data[date_key] = {
                    'date': date_key,
                    'open': p['open'],
                    'high': p['high'],
                    'low': p['low'],
                    'close': p['close'],
                    'volume': p['volume'],
                    'count': 1
                }
            else:
                daily_data[date_key]['high'] = max(daily_data[date_key]['high'], p['high'])
                daily_data[date_key]['low'] = min(daily_data[date_key]['low'], p['low'])
                daily_data[date_key]['close'] = p['close']
                daily_data[date_key]['volume'] += p['volume']
                daily_data[date_key]['count'] += 1
        
        return sorted(daily_data.values(), key=lambda x: x['date'])
    
    def get_pair_stats(self, pair: str, days: int = 30) -> Dict:
        """Get statistical summary for a pair"""
        prices = self.db.get_price_df(pair, days=days)
        
        if not prices:
            return {}
        
        closes = [p['close'] for p in prices]
        
        return {
            'pair': pair,
            'period_days': days,
            'data_points': len(closes),
            'current_price': closes[-1] if closes else 0,
            'highest_price': max(closes) if closes else 0,
            'lowest_price': min(closes) if closes else 0,
            'avg_price': sum(closes) / len(closes) if closes else 0,
            'volatility': self._calculate_volatility(closes),
            'price_change_pct': ((closes[-1] - closes[0]) / closes[0] * 100) if closes and closes[0] > 0 else 0,
        }
    
    def _calculate_volatility(self, prices: List[float]) -> float:
        """Calculate price volatility (standard deviation of returns)"""
        if len(prices) < 2:
            return 0.0
        
        returns = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0:
                ret = (prices[i] - prices[i-1]) / prices[i-1]
                returns.append(ret)
        
        if not returns:
            return 0.0
        
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return variance ** 0.5


# CLI interface
if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Crypto Data Collector')
    parser.add_argument('--pairs', nargs='+', default=['THB_BTC', 'THB_ETH'],
                        help='Trading pairs to collect')
    parser.add_argument('--interval', type=int, default=60,
                        help='Collection interval in seconds')
    parser.add_argument('--once', action='store_true',
                        help='Collect once and exit')
    parser.add_argument('--ohlc', action='store_true',
                        help='Collect OHLC data instead of ticker')
    
    args = parser.parse_args()
    
    collector = BitkubCollector(pairs=args.pairs, interval=args.interval)
    
    if args.once:
        if args.ohlc:
            results = collector.collect_all_ohlc(interval=1)
            for pair, count in results.items():
                print(f"{pair}: {count} candles stored")
        else:
            results = collector.collect_all()
            for pair, success in results.items():
                status = "OK" if success else "FAILED"
                print(f"{pair}: {status}")
    else:
        print(f"Starting collector for {args.pairs}...")
        print("Press Ctrl+C to stop")
        try:
            collector.start(blocking=True)
        except KeyboardInterrupt:
            collector.stop()
