"""
Database connection and queries for Crypto Trading Bot
SQLite with SQLAlchemy ORM

Concurrency Strategy:
  - WAL mode (Write-Ahead Logging) enabled on every new connection.
    This allows concurrent reads while a write is in progress, and avoids
    "database is locked" errors under multi-threaded workloads.
  - All write operations use a lock-and-retry loop (up to 5 attempts, 100ms backoff)
    to handle the occasional BUSY / LOCKED return from SQLite.
  - A module-level threading.Lock serialises writes at the ORM level as a
    secondary safeguard for high-concurrency paths.
"""

import copy
import logging
import os
import sqlite3
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, TypeVar

import pandas as pd
from sqlalchemy import create_engine, event, func, or_  # pylint: disable=unused-import
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

# ── Logger ────────────────────────────────────────────────────────────────────
_logger = logging.getLogger("crypto-bot.database")


T = TypeVar("T")

from models import Base, ClosedTrade, HeldCoinHistory, Order, Position, Price, Signal, Trade, TradeState


def _normalize_utc_naive_timestamp(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize timestamps to naive UTC for consistent SQLite storage and comparisons."""
    if value is None:
        return None

    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()

    if not isinstance(value, datetime):
        return value

    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    return value


class Database:
    """
    SQLite database handler for crypto bot.

    Concurrency:
      - WAL mode is enabled on every connection to allow concurrent readers
        while a write transaction is active.
      - All public write methods use _with_retry() to automatically handle
        "database locked" and "busy" errors with exponential-ish back-off.
      - A module-level _write_lock serialises writes at the ORM level as a
        secondary guard for extremely high-concurrency paths (e.g. multiple
        threads writing OHLCV data simultaneously).
    """

    # Retry parameters
    _WRITE_RETRIES: int = 5
    _RETRY_BASE_DELAY: float = 0.1  # seconds
    _RETRY_MAX_DELAY: float = 2.0  # cap exponential backoff

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.path.join(os.path.dirname(__file__), "crypto_bot.db")

        self.db_path = db_path

        # SQLite URI parameters:
        #  - check_same_thread=False  → allow multi-threaded access
        #  - timeout=30               → wait up to 30s when DB is locked
        uri = f"sqlite:///{db_path}?check_same_thread=False&timeout=30"
        self.engine = create_engine(uri, echo=False)

        # Enable WAL mode + busy_timeout on every new connection
        @event.listens_for(self.engine, "connect")
        def _on_connect(dbapi_conn: sqlite3.Connection, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")  # 30 s
            cursor.execute("PRAGMA synchronous=NORMAL")  # safe + faster than FULL
            cursor.execute("PRAGMA cache_size=-64000")  # 64 MB page cache
            cursor.execute("PRAGMA temp_store=MEMORY")  # Store temp tables and indexes in RAM for faster ORDER BY
            cursor.close()

        self.SessionLocal = sessionmaker(bind=self.engine)

        # Serialise writes at module level
        self._write_lock = threading.Lock()
        self._candle_cache_lock = threading.Lock()
        self._candle_cache: Dict[
            Tuple[str, str, Optional[datetime], Optional[datetime], Optional[int]], Tuple[float, pd.DataFrame]
        ] = {}
        self._candle_cache_ttl: float = 5.0
        self._candle_cache_max_size: int = 256

        # Create all tables (new tables only — does NOT alter existing ones)
        Base.metadata.create_all(self.engine)

        # Auto-migrate: add columns that may be missing from older schemas
        self._migrate_schema()

        _logger.info("Database initialized: %s (WAL mode enabled)", self.db_path)

    def _clear_candle_cache(self) -> None:
        with self._candle_cache_lock:
            self._candle_cache.clear()

    def _make_candle_cache_key(
        self,
        symbol: str,
        interval: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: Optional[int],
    ) -> Tuple[str, str, Optional[datetime], Optional[datetime], Optional[int]]:
        return (
            str(symbol or "").upper(),
            str(interval or "1h"),
            _normalize_utc_naive_timestamp(start_time),
            _normalize_utc_naive_timestamp(end_time),
            int(limit) if limit is not None else None,
        )

    def _get_cached_candles(
        self,
        symbol: str,
        interval: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: Optional[int],
    ) -> Optional[pd.DataFrame]:
        key = self._make_candle_cache_key(symbol, interval, start_time, end_time, limit)
        now = time.time()
        with self._candle_cache_lock:
            cached = self._candle_cache.get(key)
            if not cached:
                return None
            cached_at, cached_frame = cached
            if now - cached_at >= self._candle_cache_ttl:
                self._candle_cache.pop(key, None)
                return None
            return cached_frame.copy(deep=True)

    def _store_cached_candles(
        self,
        symbol: str,
        interval: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: Optional[int],
        frame: pd.DataFrame,
    ) -> None:
        key = self._make_candle_cache_key(symbol, interval, start_time, end_time, limit)
        with self._candle_cache_lock:
            if len(self._candle_cache) >= self._candle_cache_max_size:
                oldest_key = min(self._candle_cache.items(), key=lambda item: item[1][0])[0]
                self._candle_cache.pop(oldest_key, None)
            self._candle_cache[key] = (time.time(), frame.copy(deep=True))

    # ── Retry helper ───────────────────────────────────────────────────────────

    def _with_retry(self, operation: Callable[[], T]) -> T:
        """
        Execute a callable up to _WRITE_RETRIES times, backing off on lock errors.

        Raises the last OperationalError if all retries are exhausted.
        """
        last_error: Exception = None
        for attempt in range(1, self._WRITE_RETRIES + 1):
            try:
                return operation()
            except OperationalError as exc:
                last_error = exc
                err_str = str(exc).lower()
                if "locked" in err_str or "busy" in err_str:
                    delay = min(
                        self._RETRY_BASE_DELAY * (2 ** (attempt - 1)),
                        self._RETRY_MAX_DELAY,
                    )
                    state = "locked" if "locked" in err_str else "busy"
                    _logger.warning(
                        "Database %s (attempt %d/%d) — retrying in %.2fs. Error: %s",
                        state,
                        attempt,
                        self._WRITE_RETRIES,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
                else:
                    raise
        # All retries exhausted
        _logger.error("Database write failed after %d attempts: %s", self._WRITE_RETRIES, last_error)
        raise last_error

    # ── Auto-migration ─────────────────────────────────────────────────────────

    def _migrate_schema(self):
        """
        Add columns that may be missing from older SQLite schemas.

        SQLAlchemy's create_all() creates NEW tables but never alters existing
        ones. This method introspects each table and adds missing columns via
        ALTER TABLE so the ORM insert logic doesn't crash.
        """
        migrations = {
            "prices": [
                ("timeframe", "TEXT DEFAULT '1h'"),
            ],
            "orders": [
                ("order_type", "VARCHAR(20) DEFAULT 'limit'"),
                ("fee", "FLOAT DEFAULT 0"),
                ("filled_price", "FLOAT"),
                ("filled_quantity", "FLOAT"),
                ("error_message", "TEXT"),
                ("created_at", "DATETIME"),
                ("updated_at", "DATETIME"),
            ],
            "trades": [
                ("fee", "FLOAT DEFAULT 0"),
                ("realized_pnl", "FLOAT"),
            ],
        }

        conn = self.engine.raw_connection()
        try:
            cursor = conn.cursor()
            for table_name, columns in migrations.items():
                # Get existing column names
                cursor.execute(f"PRAGMA table_info({table_name})")
                existing = {row[1] for row in cursor.fetchall()}

                for col_name, col_def in columns:
                    if col_name not in existing:
                        stmt = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}"
                        try:
                            cursor.execute(stmt)
                            _logger.info("[Migration] Added column %s.%s", table_name, col_name)
                        except Exception as e:
                            _logger.debug("[Migration] Column %s.%s skipped: %s", table_name, col_name, e)

                if table_name == "trades":
                    self._migrate_legacy_trade_pnl(cursor, existing)

            self._migrate_prices_schema(cursor)
            conn.commit()
        except Exception as e:
            _logger.error("[Migration] Schema migration error: %s", e, exc_info=True)
        finally:
            conn.close()

    def _migrate_legacy_trade_pnl(self, cursor: sqlite3.Cursor, existing_columns: set[str]) -> None:
        """Backfill realized_pnl from legacy pnl column when upgrading older trade tables."""
        if "pnl" not in existing_columns:
            return

        try:
            cursor.execute("UPDATE trades SET realized_pnl = pnl WHERE realized_pnl IS NULL AND pnl IS NOT NULL")
            _logger.info("[Migration] Backfilled trades.realized_pnl from legacy trades.pnl")
        except Exception as exc:
            _logger.debug("[Migration] Failed to backfill trades.realized_pnl: %s", exc)

    def _migrate_prices_schema(self, cursor: sqlite3.Cursor) -> None:
        """Ensure the prices table has a timeframe-aware unique key and supporting indexes."""
        cursor.execute("PRAGMA table_info(prices)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if not existing_columns:
            return

        if "timeframe" not in existing_columns:
            cursor.execute("ALTER TABLE prices ADD COLUMN timeframe TEXT DEFAULT '1h'")
            _logger.info("[Migration] Added column prices.timeframe")

        cursor.execute("PRAGMA index_list('prices')")
        has_timeframe_unique = False
        has_legacy_unique = False

        for index_row in cursor.fetchall():
            index_name = index_row[1]
            is_unique = bool(index_row[2])
            cursor.execute(f"PRAGMA index_info('{index_name}')")
            column_names = [col[2] for col in cursor.fetchall()]

            if is_unique and column_names == ["pair", "timestamp", "timeframe"]:
                has_timeframe_unique = True
            elif is_unique and column_names == ["pair", "timestamp"]:
                has_legacy_unique = True

        if not has_timeframe_unique:
            reason = "legacy pair/timestamp unique key" if has_legacy_unique else "missing timeframe-aware unique key"
            _logger.info("[Migration] Rebuilding prices table to replace %s", reason)
            self._rebuild_prices_table(cursor)

        self._ensure_prices_indexes(cursor)

    def _rebuild_prices_table(self, cursor: sqlite3.Cursor) -> None:
        """Recreate the prices table with a unique key on pair, timestamp, and timeframe."""
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("DROP TABLE IF EXISTS prices__migrated")
        cursor.execute(textwrap.dedent("""
                CREATE TABLE prices__migrated (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair VARCHAR(20) NOT NULL,
                    timestamp DATETIME NOT NULL,
                    timeframe VARCHAR(10) NOT NULL DEFAULT '1h',
                    open FLOAT,
                    high FLOAT,
                    low FLOAT,
                    close FLOAT NOT NULL,
                    volume FLOAT,
                    CONSTRAINT uq_prices_pair_timestamp_timeframe UNIQUE (pair, timestamp, timeframe)
                )
                """).strip())
        cursor.execute(textwrap.dedent("""
                INSERT OR REPLACE INTO prices__migrated (
                    id, pair, timestamp, timeframe, open, high, low, close, volume
                )
                SELECT
                    id,
                    pair,
                    timestamp,
                    COALESCE(timeframe, '1h'),
                    open,
                    high,
                    low,
                    close,
                    volume
                FROM prices
                ORDER BY id
                """).strip())
        cursor.execute("DROP TABLE prices")
        cursor.execute("ALTER TABLE prices__migrated RENAME TO prices")
        cursor.execute("PRAGMA foreign_keys=ON")

    def _ensure_prices_indexes(self, cursor: sqlite3.Cursor) -> None:
        """Create performance indexes expected by runtime candle queries."""
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_prices_pair_timestamp ON prices (pair, timestamp)")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_prices_pair_timeframe_timestamp ON prices (pair, timeframe, timestamp)"
        )
        # Position & trade state indexes for runtime lookups
        for stmt in [
            "CREATE INDEX IF NOT EXISTS ix_positions_symbol ON positions (symbol)",
            "CREATE INDEX IF NOT EXISTS ix_closed_trades_symbol_closed_at ON closed_trades (symbol, closed_at)",
            "CREATE INDEX IF NOT EXISTS ix_trade_states_symbol ON trade_states (symbol)",
            "CREATE INDEX IF NOT EXISTS ix_orders_symbol_timestamp ON orders (symbol, timestamp)",
        ]:
            try:
                cursor.execute(stmt)
            except Exception:
                pass  # Table may not exist yet during initial migration

    def get_session(self) -> Session:
        """Get a new database session"""
        return self.SessionLocal()

    def get_connection(self):
        """Get a raw DB-API connection (compatibility wrapper)."""
        return self.engine.raw_connection()

    def close(self):
        """Close the database engine."""
        self.engine.dispose()

    # ==================== PRICE METHODS ====================

    def insert_price(
        self,
        pair: str,
        timestamp: datetime,
        open: float,
        high: float,
        low: float,  # noqa: A001 (redefining built-in is OK for SQLAlchemy field)
        close: float,
        volume: float,
        timeframe: str = "1h",
    ) -> Optional[Price]:
        """Insert or update a price record (thread-safe with retry)."""

        normalized_timestamp = _normalize_utc_naive_timestamp(timestamp)
        normalized_timeframe = timeframe or "1h"

        def _do_insert() -> Optional[Price]:
            session = self.get_session()
            try:
                price = (
                    session.query(Price)
                    .filter(
                        Price.pair == pair,
                        Price.timestamp == normalized_timestamp,
                        Price.timeframe == normalized_timeframe,
                    )
                    .one_or_none()
                )

                if price is None:
                    price = Price(
                        pair=pair,
                        timestamp=normalized_timestamp,
                        timeframe=normalized_timeframe,
                        open=open,
                        high=high,
                        low=low,
                        close=close,
                        volume=volume,
                    )
                    session.add(price)
                else:
                    price.open = open
                    price.high = high
                    price.low = low
                    price.close = close
                    price.volume = volume

                session.commit()
                session.refresh(price)
                return price
            except IntegrityError:
                session.rollback()
                price = (
                    session.query(Price)
                    .filter(
                        Price.pair == pair,
                        Price.timestamp == normalized_timestamp,
                        Price.timeframe == normalized_timeframe,
                    )
                    .one_or_none()
                )
                if price is None:
                    return None

                price.open = open
                price.high = high
                price.low = low
                price.close = close
                price.volume = volume
                session.commit()
                session.refresh(price)
                return price
            finally:
                session.close()

        with self._write_lock:
            result = self._with_retry(_do_insert)
        self._clear_candle_cache()
        return result

    def insert_prices_batch(self, prices: List[Dict]) -> int:
        """
        Bulk insert multiple price records using raw SQL for speed.
        Much faster than individual inserts when collecting OHLC data.

        Args:
            prices: List of dicts with keys: pair, timestamp, open, high, low, close, volume, timeframe

        Returns:
            Number of records inserted
        """
        if not prices:
            return 0

        def _do_batch() -> int:
            conn = self.get_connection()
            cursor = conn.cursor()
            try:
                normalized_rows = [
                    (
                        p["pair"],
                        _normalize_utc_naive_timestamp(p["timestamp"]),
                        p.get("timeframe", "1h") or "1h",
                        p["open"],
                        p["high"],
                        p["low"],
                        p["close"],
                        p["volume"],
                    )
                    for p in prices
                ]
                cursor.executemany(
                    """INSERT INTO prices (pair, timestamp, timeframe, open, high, low, close, volume)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(pair, timestamp, timeframe) DO UPDATE SET
                           open=excluded.open,
                           high=excluded.high,
                           low=excluded.low,
                           close=excluded.close,
                           volume=excluded.volume""",
                    normalized_rows,
                )
                conn.commit()
                return cursor.rowcount
            except Exception as e:
                conn.rollback()
                raise e
            finally:
                cursor.close()
                conn.close()

        with self._write_lock:
            inserted = self._with_retry(_do_batch)
        self._clear_candle_cache()
        return inserted

    def get_latest_price(self, pair: str, timeframe: str = None) -> Optional[Price]:
        """Get the most recent price for a trading pair"""
        session = self.get_session()
        try:
            query = session.query(Price).filter(Price.pair == pair)
            if timeframe:
                query = query.filter(Price.timeframe == timeframe)
            return query.order_by(Price.timestamp.desc()).first()
        finally:
            session.close()

    def get_earliest_price(self, pair: str, timeframe: str) -> Optional[Price]:
        """Oldest stored candle for a pair and timeframe (used for paged backfill)."""
        session = self.get_session()
        try:
            query = session.query(Price).filter(Price.pair == pair)
            if timeframe:
                query = query.filter(Price.timeframe == timeframe)
            return query.order_by(Price.timestamp.asc()).first()
        finally:
            session.close()

    def count_price_rows(self, pair: str, timeframe: str) -> int:
        """Count OHLC rows for a pair and timeframe."""
        session = self.get_session()
        try:
            q = session.query(func.count(Price.id)).filter(Price.pair == pair)
            if timeframe:
                q = q.filter(Price.timeframe == timeframe)
            return int(q.scalar() or 0)
        finally:
            session.close()

    def get_price_history(
        self,
        pair: str,
        start_time: datetime = None,
        end_time: datetime = None,
        limit: int = 1000,
        timeframe: str = None,
    ) -> List[Price]:
        """Get historical prices for a pair"""
        session = self.get_session()
        try:
            query = session.query(Price).filter(Price.pair == pair)

            if timeframe:
                query = query.filter(Price.timeframe == timeframe)
            if start_time:
                query = query.filter(Price.timestamp >= start_time)
            if end_time:
                query = query.filter(Price.timestamp <= end_time)

            return query.order_by(Price.timestamp.desc()).limit(limit).all()
        finally:
            session.close()

    def get_price_df(self, pair: str, days: int = 30, timeframe: str = None) -> List[Dict[str, Any]]:
        """Get price data as list of dicts for analysis (pandas-ready)"""
        session = self.get_session()
        try:
            start_time = datetime.now(timezone.utc) - timedelta(days=days)
            query = session.query(Price).filter(Price.pair == pair, Price.timestamp >= start_time)
            if timeframe:
                query = query.filter(Price.timeframe == timeframe)
            prices = query.order_by(Price.timestamp.asc()).all()

            return [
                {
                    "timestamp": p.timestamp,
                    "pair": p.pair,
                    "open": p.open,
                    "high": p.high,
                    "low": p.low,
                    "close": p.close,
                    "volume": p.volume,
                }
                for p in prices
            ]
        finally:
            session.close()

    def get_candles(
        self,
        symbol: str,
        interval: str = "1h",
        start_time: datetime = None,
        end_time: datetime = None,
        limit: int = None,
    ) -> pd.DataFrame:
        """Return candle data as a pandas DataFrame for model training/backtesting."""
        cached = self._get_cached_candles(symbol, interval, start_time, end_time, limit)
        if cached is not None:
            return cached

        session = self.get_session()
        try:
            query = session.query(Price).filter(Price.pair == symbol)

            if interval:
                query = query.filter(Price.timeframe == interval)
            if start_time:
                query = query.filter(Price.timestamp >= start_time)
            if end_time:
                query = query.filter(Price.timestamp <= end_time)

            if limit:
                prices = query.order_by(Price.timestamp.desc()).limit(limit).all()
                prices = list(reversed(prices))
            else:
                prices = query.order_by(Price.timestamp.asc()).all()

            rows = [
                {
                    "timestamp": _normalize_utc_naive_timestamp(p.timestamp),
                    "pair": p.pair,
                    "open": p.open,
                    "high": p.high,
                    "low": p.low,
                    "close": p.close,
                    "volume": p.volume,
                }
                for p in prices
            ]
            frame = pd.DataFrame(rows)
            if frame.empty:
                self._store_cached_candles(symbol, interval, start_time, end_time, limit, frame)
                return frame

            frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce", utc=True).dt.tz_localize(None)
            frame = frame.dropna(subset=["timestamp"])
            frame = frame.sort_values("timestamp").reset_index(drop=True)
            self._store_cached_candles(symbol, interval, start_time, end_time, limit, frame)
            return frame.copy(deep=True)
        finally:
            session.close()

    # ==================== ORDER METHODS ====================

    def insert_order(
        self,
        pair: str,
        side: str,
        quantity: float,
        price: float,
        status: str = "pending",
        order_type: str = "limit",
        fee: float = 0.0,
        timestamp: datetime = None,
    ) -> Order:
        """Insert a new order (thread-safe with retry)."""

        def _do_insert() -> Order:
            session = self.get_session()
            try:
                ts = timestamp or datetime.now(timezone.utc)
                order = Order(
                    timestamp=ts,
                    created_at=ts,
                    pair=pair,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status=status,
                    order_type=order_type,  # Aligned with models.py Order.order_type
                    fee=fee,  # Aligned with models.py Order.fee
                )
                session.add(order)
                session.commit()
                session.refresh(order)
                return order
            finally:
                session.close()

        with self._write_lock:
            return self._with_retry(_do_insert)

    def update_order_status(self, order_id: int, status: str) -> Optional[Order]:
        """Update order status (thread-safe with retry)."""

        def _do_update() -> Optional[Order]:
            session = self.get_session()
            try:
                order = session.query(Order).filter(Order.id == order_id).first()
                if order:
                    order.status = status
                    session.commit()
                    session.refresh(order)
                return order
            finally:
                session.close()

        with self._write_lock:
            return self._with_retry(_do_update)

    def get_orders(self, pair: str = None, status: str = None, limit: int = 100) -> List[Order]:
        """Get orders with optional filters"""
        session = self.get_session()
        try:
            query = session.query(Order)
            if pair:
                query = query.filter(Order.pair == pair)
            if status:
                query = query.filter(Order.status == status)
            return query.order_by(Order.created_at.desc()).limit(limit).all()
        finally:
            session.close()

    # ==================== TRADE METHODS ====================

    def insert_trade(
        self,
        pair: str,
        side: str,
        quantity: float,
        price: float,
        realized_pnl: float = None,
        fee: float = 0.0,
        pnl: float = None,
        timestamp: datetime = None,
    ) -> Trade:
        """Insert a new trade record (thread-safe with retry)."""

        def _do_insert() -> Trade:
            session = self.get_session()
            try:
                pnl_value = realized_pnl if realized_pnl is not None else pnl
                trade = Trade(
                    timestamp=timestamp or datetime.now(timezone.utc),
                    pair=pair,
                    side=side,
                    quantity=quantity,
                    price=price,
                    fee=fee,
                    realized_pnl=pnl_value,
                )
                session.add(trade)
                session.commit()
                session.refresh(trade)
                return trade
            finally:
                session.close()

        with self._write_lock:
            return self._with_retry(_do_insert)

    def get_trades(self, pair: str = None, start_time: datetime = None, limit: int = 100) -> List[Trade]:
        """Get trade history"""
        session = self.get_session()
        try:
            query = session.query(Trade)
            if pair:
                query = query.filter(Trade.pair == pair)
            if start_time:
                query = query.filter(Trade.timestamp >= start_time)
            return query.order_by(Trade.timestamp.desc()).limit(limit).all()
        finally:
            session.close()

    def get_total_pnl(self, pair: str = None) -> float:
        """Calculate total P&L"""
        session = self.get_session()
        try:
            query = session.query(func.sum(Trade.realized_pnl))
            if pair:
                query = query.filter(Trade.pair == pair)
            result = query.scalar()
            return result or 0.0
        finally:
            session.close()

    # ==================== HELD COIN HISTORY METHODS ====================

    def record_held_coin(self, symbol: str, quantity: float, timestamp: datetime = None) -> Optional[HeldCoinHistory]:
        """Record a successful BUY for a coin/pair in history."""
        symbol_key = symbol.upper()
        ts = timestamp or datetime.now(timezone.utc)

        def _do_upsert() -> Optional[HeldCoinHistory]:
            session = self.get_session()
            try:
                row = session.query(HeldCoinHistory).filter(func.upper(HeldCoinHistory.symbol) == symbol_key).first()
                if row:
                    row.total_bought_qty = (row.total_bought_qty or 0.0) + quantity
                    row.last_bought_qty = quantity
                    row.last_held_at = ts
                else:
                    row = HeldCoinHistory(
                        symbol=symbol_key,
                        first_held_at=ts,
                        last_held_at=ts,
                        total_bought_qty=quantity,
                        last_bought_qty=quantity,
                    )
                    session.add(row)
                session.commit()
                session.refresh(row)
                return row
            except Exception as e:
                session.rollback()
                _logger.error("Failed to record held coin history: %s", e)
                return None
            finally:
                session.close()

        with self._write_lock:
            return self._with_retry(_do_upsert)

    def has_ever_held(self, symbol: str) -> bool:
        """Return True if the bot has ever bought this coin/pair."""
        symbol_key = symbol.upper()
        session = self.get_session()
        try:
            return session.query(HeldCoinHistory).filter(func.upper(HeldCoinHistory.symbol) == symbol_key).count() > 0
        finally:
            session.close()

    # ==================== SIGNAL METHODS ====================

    def insert_signal(
        self,
        pair: str,
        signal_type: str,
        confidence: float,
        result: str = "pending",
        timestamp: datetime = None,
        strategy: str = None,
    ) -> Signal:
        """Insert a trading signal (thread-safe with retry)."""

        def _do_insert() -> Signal:
            session = self.get_session()
            try:
                signal = Signal(
                    timestamp=timestamp or datetime.now(timezone.utc),
                    pair=pair,
                    signal_type=signal_type,
                    confidence=confidence,
                    result=result,
                    strategy=strategy,
                )
                session.add(signal)
                session.commit()
                session.refresh(signal)
                return signal
            finally:
                session.close()

        with self._write_lock:
            return self._with_retry(_do_insert)

    def update_signal_result(self, signal_id: int, result: str) -> Optional[Signal]:
        """Update signal result after execution (thread-safe with retry)."""

        def _do_update() -> Optional[Signal]:
            session = self.get_session()
            try:
                signal = session.query(Signal).filter(Signal.id == signal_id).first()
                if signal:
                    signal.result = result
                    session.commit()
                    session.refresh(signal)
                return signal
            finally:
                session.close()

        with self._write_lock:
            return self._with_retry(_do_update)

    def get_signals(
        self, pair: str = None, signal_type: str = None, result: str = None, limit: int = 100
    ) -> List[Signal]:
        """Get signals with optional filters"""
        session = self.get_session()
        try:
            query = session.query(Signal)
            if pair:
                query = query.filter(Signal.pair == pair)
            if signal_type:
                query = query.filter(Signal.signal_type == signal_type)
            if result:
                query = query.filter(Signal.result == result)
            return query.order_by(Signal.timestamp.desc()).limit(limit).all()
        finally:
            session.close()

    def get_signal_stats(self, pair: str = None) -> Dict[str, Any]:
        """Get signal performance statistics"""
        session = self.get_session()
        try:
            query = session.query(Signal)
            if pair:
                query = query.filter(Signal.pair == pair)

            signals = query.all()
            total = len(signals)
            if total == 0:
                return {"total": 0, "success_rate": 0.0}

            success = len([s for s in signals if s.result == "success"])
            failure = len([s for s in signals if s.result == "failure"])

            return {
                "total": total,
                "success": success,
                "failure": failure,
                "pending": total - success - failure,
                "success_rate": success / total if total > 0 else 0.0,
            }
        finally:
            session.close()

    def get_strategy_performance(self, days: int = 30) -> Dict[str, Dict[str, Any]]:
        """Get per-strategy win/loss stats from recent signals.

        Returns a dict keyed by strategy name with keys:
        total, success, failure, win_rate (0.0-1.0).
        Only considers signals where the strategy column is populated.
        """
        session = self.get_session()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            signals = (
                session.query(Signal)
                .filter(Signal.strategy.isnot(None))
                .filter(Signal.strategy != "")
                .filter(Signal.timestamp >= cutoff)
                .filter(Signal.result.in_(["success", "failure"]))
                .all()
            )

            stats: Dict[str, Dict[str, Any]] = {}
            for sig in signals:
                name = sig.strategy
                if name not in stats:
                    stats[name] = {"total": 0, "success": 0, "failure": 0}
                stats[name]["total"] += 1
                if sig.result == "success":
                    stats[name]["success"] += 1
                else:
                    stats[name]["failure"] += 1

            for name, data in stats.items():
                data["win_rate"] = data["success"] / data["total"] if data["total"] > 0 else 0.5

            return stats
        finally:
            session.close()

    # ==================== POSITION PERSISTENCE ====================

    def save_position(self, pos_data: Dict[str, Any]) -> Optional[Position]:
        """Save or update an open position (thread-safe with retry)."""

        def _coerce_timestamp(raw_value: Any) -> Optional[datetime]:
            if isinstance(raw_value, datetime):
                return raw_value
            if not raw_value:
                return None
            try:
                return datetime.fromisoformat(str(raw_value))
            except (TypeError, ValueError):
                return None

        def _do_upsert() -> Optional[Position]:
            session = self.get_session()
            try:
                order_id = pos_data.get("order_id", "")
                existing = session.query(Position).filter(Position.order_id == order_id).first()

                side_val = pos_data.get("side", "buy")
                if hasattr(side_val, "value"):
                    side_val = side_val.value
                opened_at = _coerce_timestamp(pos_data.get("timestamp"))

                def _apply_updates(row: Position) -> Position:
                    row.symbol = pos_data.get("symbol", row.symbol)
                    row.side = side_val
                    row.amount = pos_data.get("amount", row.amount)
                    row.entry_price = pos_data.get("entry_price", row.entry_price)
                    row.stop_loss = pos_data.get("stop_loss")
                    row.take_profit = pos_data.get("take_profit")
                    row.total_entry_cost = pos_data.get("total_entry_cost", 0)
                    row.is_partial_fill = pos_data.get("is_partial_fill", False)
                    row.remaining_amount = pos_data.get("remaining_amount", 0)
                    row.trailing_peak = pos_data.get("trailing_peak")
                    if opened_at is not None:
                        row.opened_at = opened_at
                    session.commit()
                    session.refresh(row)
                    return row

                if existing:
                    return _apply_updates(existing)

                dup = session.query(Position).filter(Position.order_id == order_id).first()
                if dup is not None:
                    _logger.warning(
                        "[OMS] Position %s already exists — skipping insert",
                        order_id,
                    )
                    return _apply_updates(dup)

                position = Position(
                    order_id=order_id,
                    symbol=pos_data.get("symbol", ""),
                    side=side_val,
                    amount=pos_data.get("amount", 0),
                    entry_price=pos_data.get("entry_price", 0),
                    stop_loss=pos_data.get("stop_loss"),
                    take_profit=pos_data.get("take_profit"),
                    total_entry_cost=pos_data.get("total_entry_cost", 0),
                    is_partial_fill=pos_data.get("is_partial_fill", False),
                    remaining_amount=pos_data.get("remaining_amount", 0),
                    trailing_peak=pos_data.get("trailing_peak"),
                    opened_at=opened_at,
                )
                session.add(position)
                try:
                    session.commit()
                    session.refresh(position)
                    return position
                except IntegrityError:
                    session.rollback()
                    existing2 = session.query(Position).filter(Position.order_id == order_id).first()
                    if existing2 is not None:
                        _logger.warning(
                            "[OMS] Position %s already exists — skipping insert",
                            order_id,
                        )
                        return _apply_updates(existing2)
                    raise
            except IntegrityError:
                session.rollback()
                raise
            except Exception as e:
                session.rollback()
                _logger.error("Failed to save position: %s", e)
                raise
            finally:
                session.close()

        with self._write_lock:
            return self._with_retry(_do_upsert)

    def delete_invalid_btc_amount_positions(self, limit: float = 1.0) -> int:
        """Remove BTC rows with implausible base size (wrong unit). Any side."""
        btc_symbols = ("BTCUSDT", "THB_BTC", "BTC_THB")

        def _do_delete() -> int:
            session = self.get_session()
            try:
                q = session.query(Position).filter(
                    func.upper(Position.symbol).in_(btc_symbols),
                    Position.amount > limit,
                )
                n = q.delete(synchronize_session=False)
                session.commit()
                return int(n or 0)
            except Exception as e:
                session.rollback()
                _logger.error("Failed to delete invalid BTC amount positions: %s", e)
                raise
            finally:
                session.close()

        with self._write_lock:
            return self._with_retry(_do_delete)

    def delete_positions_zero_remaining(self) -> int:
        """Remove stale rows: closed/ghost positions with no remaining size."""

        def _do_delete() -> int:
            session = self.get_session()
            try:
                q = session.query(Position).filter(
                    or_(
                        Position.remaining_amount.is_(None),
                        Position.remaining_amount == 0,
                    )
                )
                n = q.delete(synchronize_session=False)
                session.commit()
                return int(n or 0)
            except Exception as e:
                session.rollback()
                _logger.error("Failed to delete zero-remaining positions: %s", e)
                raise
            finally:
                session.close()

        with self._write_lock:
            return self._with_retry(_do_delete)

    def delete_position(self, order_id: str) -> bool:
        """Remove a closed position (thread-safe with retry)."""

        def _do_delete() -> bool:
            session = self.get_session()
            try:
                deleted = session.query(Position).filter(Position.order_id == order_id).delete()
                session.commit()
                return deleted > 0
            except Exception as e:
                session.rollback()
                _logger.error("Failed to delete position: %s", e)
                raise
            finally:
                session.close()

        with self._write_lock:
            return self._with_retry(_do_delete)

    def load_all_positions(self) -> List[Dict[str, Any]]:
        """Load all open positions from DB (for startup recovery)."""
        session = self.get_session()
        try:
            positions = session.query(Position).all()
            result = []
            for p in positions:
                result.append(
                    {
                        "order_id": p.order_id,
                        "symbol": p.symbol,
                        "side": p.side,
                        "amount": p.amount,
                        "entry_price": p.entry_price,
                        "stop_loss": p.stop_loss,
                        "take_profit": p.take_profit,
                        "total_entry_cost": p.total_entry_cost or 0,
                        "is_partial_fill": p.is_partial_fill or False,
                        "remaining_amount": p.remaining_amount or 0,
                        "trailing_peak": p.trailing_peak,
                        "timestamp": p.opened_at or p.updated_at,
                        "filled": False,
                    }
                )
            return result
        finally:
            session.close()

    def update_position_sl(self, order_id: str, new_sl: float, trailing_peak: float = None) -> bool:
        """Update stop loss for trailing stop (thread-safe)."""

        def _do_update() -> bool:
            session = self.get_session()
            try:
                pos = session.query(Position).filter(Position.order_id == order_id).first()
                if pos:
                    pos.stop_loss = new_sl
                    if trailing_peak is not None:
                        pos.trailing_peak = trailing_peak
                    session.commit()
                    return True
                return False
            except Exception as e:
                session.rollback()
                _logger.error("Failed to update position SL: %s", e)
                raise
            finally:
                session.close()

        with self._write_lock:
            return self._with_retry(_do_update)

    # ==================== STATE MACHINE PERSISTENCE ====================

    def save_trade_state(self, state_data: Dict[str, Any]) -> Optional[TradeState]:
        """Save or update execution state for a symbol."""

        def _do_upsert() -> Optional[TradeState]:
            session = self.get_session()
            try:
                symbol = str(state_data.get("symbol", "")).upper()
                if not symbol:
                    return None

                existing = session.query(TradeState).filter(TradeState.symbol == symbol).first()

                def _apply_updates(row: TradeState) -> TradeState:
                    row.state = str(state_data.get("state", row.state) or row.state)
                    row.side = str(state_data.get("side", row.side) or row.side)
                    row.entry_order_id = state_data.get("entry_order_id")
                    row.exit_order_id = state_data.get("exit_order_id")
                    row.active_order_id = state_data.get("active_order_id")
                    row.requested_amount = float(state_data.get("requested_amount", row.requested_amount) or 0.0)
                    row.filled_amount = float(state_data.get("filled_amount", row.filled_amount) or 0.0)
                    row.entry_price = float(state_data.get("entry_price", row.entry_price) or 0.0)
                    row.exit_price = float(state_data.get("exit_price", row.exit_price) or 0.0)
                    row.stop_loss = float(state_data.get("stop_loss", row.stop_loss) or 0.0)
                    row.take_profit = float(state_data.get("take_profit", row.take_profit) or 0.0)
                    row.total_entry_cost = float(state_data.get("total_entry_cost", row.total_entry_cost) or 0.0)
                    row.signal_confidence = float(state_data.get("signal_confidence", row.signal_confidence) or 0.0)
                    row.signal_source = state_data.get("signal_source")
                    row.trigger = state_data.get("trigger")
                    row.notes = state_data.get("notes")
                    row.opened_at = state_data.get("opened_at")
                    row.last_transition_at = state_data.get("last_transition_at", datetime.now(timezone.utc))
                    session.commit()
                    session.refresh(row)
                    return row

                if existing:
                    return _apply_updates(existing)

                row = TradeState(
                    symbol=symbol,
                    state=str(state_data.get("state", "idle") or "idle"),
                    side=str(state_data.get("side", "buy") or "buy"),
                    entry_order_id=state_data.get("entry_order_id"),
                    exit_order_id=state_data.get("exit_order_id"),
                    active_order_id=state_data.get("active_order_id"),
                    requested_amount=float(state_data.get("requested_amount", 0.0) or 0.0),
                    filled_amount=float(state_data.get("filled_amount", 0.0) or 0.0),
                    entry_price=float(state_data.get("entry_price", 0.0) or 0.0),
                    exit_price=float(state_data.get("exit_price", 0.0) or 0.0),
                    stop_loss=float(state_data.get("stop_loss", 0.0) or 0.0),
                    take_profit=float(state_data.get("take_profit", 0.0) or 0.0),
                    total_entry_cost=float(state_data.get("total_entry_cost", 0.0) or 0.0),
                    signal_confidence=float(state_data.get("signal_confidence", 0.0) or 0.0),
                    signal_source=state_data.get("signal_source"),
                    trigger=state_data.get("trigger"),
                    notes=state_data.get("notes"),
                    opened_at=state_data.get("opened_at"),
                    last_transition_at=state_data.get("last_transition_at", datetime.now(timezone.utc)),
                )
                session.add(row)
                session.commit()
                session.refresh(row)
                return row
            except Exception as e:
                session.rollback()
                _logger.error("Failed to save trade state: %s", e)
                raise
            finally:
                session.close()

        with self._write_lock:
            return self._with_retry(_do_upsert)

    def get_trade_state(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get execution state for a symbol."""
        session = self.get_session()
        try:
            row = session.query(TradeState).filter(TradeState.symbol == str(symbol).upper()).first()
            if not row:
                return None
            return {
                "symbol": row.symbol,
                "state": row.state,
                "side": row.side,
                "entry_order_id": row.entry_order_id,
                "exit_order_id": row.exit_order_id,
                "active_order_id": row.active_order_id,
                "requested_amount": row.requested_amount or 0.0,
                "filled_amount": row.filled_amount or 0.0,
                "entry_price": row.entry_price or 0.0,
                "exit_price": row.exit_price or 0.0,
                "stop_loss": row.stop_loss or 0.0,
                "take_profit": row.take_profit or 0.0,
                "total_entry_cost": row.total_entry_cost or 0.0,
                "signal_confidence": row.signal_confidence or 0.0,
                "signal_source": row.signal_source,
                "trigger": row.trigger,
                "notes": row.notes,
                "opened_at": row.opened_at,
                "last_transition_at": row.last_transition_at,
            }
        finally:
            session.close()

    def list_trade_states(self, states: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """List persisted trade states, optionally filtered by lifecycle state."""
        session = self.get_session()
        try:
            query = session.query(TradeState)
            if states:
                query = query.filter(TradeState.state.in_([str(state) for state in states]))
            rows = query.all()
            return [
                {
                    "symbol": row.symbol,
                    "state": row.state,
                    "side": row.side,
                    "entry_order_id": row.entry_order_id,
                    "exit_order_id": row.exit_order_id,
                    "active_order_id": row.active_order_id,
                    "requested_amount": row.requested_amount or 0.0,
                    "filled_amount": row.filled_amount or 0.0,
                    "entry_price": row.entry_price or 0.0,
                    "exit_price": row.exit_price or 0.0,
                    "stop_loss": row.stop_loss or 0.0,
                    "take_profit": row.take_profit or 0.0,
                    "total_entry_cost": row.total_entry_cost or 0.0,
                    "signal_confidence": row.signal_confidence or 0.0,
                    "signal_source": row.signal_source,
                    "trigger": row.trigger,
                    "notes": row.notes,
                    "opened_at": row.opened_at,
                    "last_transition_at": row.last_transition_at,
                }
                for row in rows
            ]
        finally:
            session.close()

    def delete_trade_state(self, symbol: str) -> bool:
        """Delete persisted execution state for a symbol."""

        def _do_delete() -> bool:
            session = self.get_session()
            try:
                deleted = session.query(TradeState).filter(TradeState.symbol == str(symbol).upper()).delete()
                session.commit()
                return bool(deleted)
            except Exception as e:
                session.rollback()
                _logger.error("Failed to delete trade state: %s", e)
                raise
            finally:
                session.close()

        with self._write_lock:
            return self._with_retry(_do_delete)

    # ==================== CLOSED TRADE LOGGING ====================

    def log_closed_trade(self, trade_data: Dict[str, Any]) -> Optional[ClosedTrade]:
        """Log a completed trade with full P/L breakdown (thread-safe)."""

        def _do_insert() -> Optional[ClosedTrade]:
            session = self.get_session()
            try:
                side_val = trade_data.get("side", "buy")
                if hasattr(side_val, "value"):
                    side_val = side_val.value

                ct = ClosedTrade(
                    symbol=trade_data.get("symbol", ""),
                    side=side_val,
                    amount=trade_data.get("amount", 0),
                    entry_price=trade_data.get("entry_price", 0),
                    exit_price=trade_data.get("exit_price", 0),
                    entry_cost=trade_data.get("entry_cost", 0),
                    gross_exit=trade_data.get("gross_exit", 0),
                    entry_fee=trade_data.get("entry_fee", 0),
                    exit_fee=trade_data.get("exit_fee", 0),
                    total_fees=trade_data.get("total_fees", 0),
                    net_pnl=trade_data.get("net_pnl", 0),
                    net_pnl_pct=trade_data.get("net_pnl_pct", 0),
                    trigger=trade_data.get("trigger", ""),
                    price_source=trade_data.get("price_source", ""),
                    opened_at=trade_data.get("opened_at"),
                    closed_at=trade_data.get("closed_at", datetime.now(timezone.utc)),
                )
                session.add(ct)
                session.commit()
                session.refresh(ct)
                return ct
            except Exception as e:
                session.rollback()
                _logger.error("Failed to log closed trade: %s", e)
                raise
            finally:
                session.close()

        with self._write_lock:
            return self._with_retry(_do_insert)

    def get_closed_trades(self, symbol: str = None, start_time: datetime = None, limit: int = 50) -> List[ClosedTrade]:
        """Get closed trade history."""
        session = self.get_session()
        try:
            query = session.query(ClosedTrade)
            if symbol:
                query = query.filter(ClosedTrade.symbol == symbol)
            if start_time:
                query = query.filter(ClosedTrade.closed_at >= start_time)

            query = query.order_by(ClosedTrade.closed_at.desc())
            if limit is not None:
                query = query.limit(limit)
            return query.all()
        finally:
            session.close()

    def closed_trades_repository(self):
        """Thin repository wrapper for gradual migration away from calling ``get_closed_trades`` directly everywhere."""
        from persistence.closed_trades import ClosedTradesRepository

        return ClosedTradesRepository(self)

    def get_performance_summary(self, symbol: str = None) -> Dict[str, Any]:
        """Get aggregate performance statistics from closed trades."""
        session = self.get_session()
        try:
            query = session.query(ClosedTrade)
            if symbol:
                query = query.filter(ClosedTrade.symbol == symbol)
            trades = query.all()

            if not trades:
                return {"total_trades": 0, "net_pnl": 0, "win_rate": 0}

            wins = [t for t in trades if t.net_pnl > 0]
            losses = [t for t in trades if t.net_pnl <= 0]

            return {
                "total_trades": len(trades),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": len(wins) / len(trades) * 100 if trades else 0,
                "total_net_pnl": sum(t.net_pnl for t in trades),
                "total_fees": sum(t.total_fees for t in trades),
                "avg_pnl": sum(t.net_pnl for t in trades) / len(trades),
                "best_trade": max(t.net_pnl for t in trades),
                "worst_trade": min(t.net_pnl for t in trades),
            }
        finally:
            session.close()

    # ==================== UTILITY METHODS ====================

    def get_stats(self) -> Dict[str, Any]:
        """Get overall database statistics"""
        session = self.get_session()
        try:
            return {
                "total_prices": session.query(Price).count(),
                "total_orders": session.query(Order).count(),
                "total_trades": session.query(Trade).count(),
                "total_signals": session.query(Signal).count(),
                "total_pnl": self.get_total_pnl(),
                "db_path": self.db_path,
            }
        finally:
            session.close()

    def cleanup_old_data(self, days: int = 90) -> Dict[str, int]:
        """Remove data older than specified days (thread-safe with retry)."""

        def _do_cleanup() -> Dict[str, int]:
            session = self.get_session()
            try:
                cutoff = datetime.now(timezone.utc) - timedelta(days=days)

                deleted_prices = session.query(Price).filter(Price.timestamp < cutoff).delete()

                deleted_orders = session.query(Order).filter(Order.timestamp < cutoff).delete()

                deleted_trades = session.query(Trade).filter(Trade.timestamp < cutoff).delete()

                deleted_signals = session.query(Signal).filter(Signal.timestamp < cutoff).delete()

                session.commit()

                return {
                    "prices": deleted_prices,
                    "orders": deleted_orders,
                    "trades": deleted_trades,
                    "signals": deleted_signals,
                }
            except Exception as e:
                session.rollback()
                raise e
            finally:
                session.close()

        with self._write_lock:
            deleted = self._with_retry(_do_cleanup)
        if deleted.get("total", 0) > 0:
            self._clear_candle_cache()
        return deleted

    def cleanup_price_history_by_timeframe(self, retention_days_by_timeframe: Mapping[str, int]) -> Dict[str, int]:
        """Prune old candle rows using timeframe-specific retention windows."""

        normalized_policy: Dict[str, int] = {}
        for timeframe, raw_days in dict(retention_days_by_timeframe or {}).items():
            normalized_timeframe = str(timeframe or "").strip()
            if not normalized_timeframe:
                continue
            try:
                days = int(raw_days)
            except (TypeError, ValueError):
                _logger.warning(
                    "[Retention] Ignoring invalid retention for timeframe %s: %r", normalized_timeframe, raw_days
                )
                continue
            if days <= 0:
                _logger.warning(
                    "[Retention] Ignoring non-positive retention for timeframe %s: %s", normalized_timeframe, days
                )
                continue
            normalized_policy[normalized_timeframe] = days

        if not normalized_policy:
            return {"total": 0}

        def _do_cleanup() -> Dict[str, int]:
            session = self.get_session()
            deleted_counts: Dict[str, int] = {}
            try:
                now_utc = datetime.now(timezone.utc)
                total_deleted = 0
                for timeframe, days in normalized_policy.items():
                    cutoff = now_utc - timedelta(days=days)
                    deleted = (
                        session.query(Price)
                        .filter(
                            func.coalesce(Price.timeframe, "1h") == timeframe,
                            Price.timestamp < cutoff,
                        )
                        .delete(synchronize_session=False)
                    )
                    deleted_counts[timeframe] = int(deleted or 0)
                    total_deleted += int(deleted or 0)

                session.commit()
                deleted_counts["total"] = total_deleted
                return deleted_counts
            except Exception as e:
                session.rollback()
                raise e
            finally:
                session.close()

        with self._write_lock:
            return self._with_retry(_do_cleanup)

    def vacuum(self) -> bool:
        """Run VACUUM on the SQLite database file.

        This is intentionally separate from retention cleanup because VACUUM can
        be expensive on live runtimes.
        """

        for attempt in range(1, self._WRITE_RETRIES + 1):
            connection: Optional[sqlite3.Connection] = None
            try:
                with self._write_lock:
                    connection = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
                    cursor = connection.cursor()
                    cursor.execute("PRAGMA busy_timeout=30000")
                    cursor.execute("PRAGMA temp_store=MEMORY")
                    cursor.execute("VACUUM")
                    connection.commit()
                return True
            except sqlite3.OperationalError as exc:
                err_str = str(exc).lower()
                if "locked" not in err_str and "busy" not in err_str:
                    raise
                delay = min(
                    self._RETRY_BASE_DELAY * (2 ** (attempt - 1)),
                    self._RETRY_MAX_DELAY,
                )
                _logger.warning(
                    "Database vacuum %s (attempt %d/%d) — retrying in %.2fs. Error: %s",
                    "locked" if "locked" in err_str else "busy",
                    attempt,
                    self._WRITE_RETRIES,
                    delay,
                    exc,
                )
                time.sleep(delay)
            finally:
                if connection is not None:
                    connection.close()

        return False


def init_db(db_path: str = None) -> Database:
    """
    Initialize database and create all tables.

    Args:
        db_path: Path to SQLite database file.
                 Defaults to 'crypto_bot.db' in the same directory.
                 Use ':memory:' for in-memory database.

    Returns:
        Database instance
    """
    return Database(db_path)


# Singleton instance
_db_instance = None


def get_database(db_path: str = None) -> Database:
    """Get or create database singleton"""
    # pylint: disable=global-statement
    global _db_instance
    if _db_instance is None:
        _db_instance = Database(db_path)
    return _db_instance
