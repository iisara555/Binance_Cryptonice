"""
SQLAlchemy ORM models for the trading bot.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class Price(Base):
    """Price candle data."""

    __tablename__ = "prices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pair = Column(String(20), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    timeframe = Column(String(10), nullable=False, default="1h")
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float, nullable=False)
    volume = Column(Float)

    __table_args__ = (
        UniqueConstraint("pair", "timestamp", "timeframe", name="uq_prices_pair_timestamp_timeframe"),
        Index("ix_prices_pair_timestamp", "pair", "timestamp"),
        Index("ix_prices_pair_timeframe_timestamp", "pair", "timeframe", "timestamp"),
    )


class Order(Base):
    """Order record."""

    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    pair = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False)  # BUY or SELL
    order_type = Column(String(20), nullable=False)  # MARKET or LIMIT
    status = Column(String(20), nullable=False)  # PENDING, FILLED, CANCELLED, REJECTED
    price = Column(Float)  # Limit price if applicable
    filled_price = Column(Float)
    quantity = Column(Float)
    filled_quantity = Column(Float)
    fee = Column(Float, default=0)
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (Index("ix_orders_pair_status", "pair", "status"),)


class Trade(Base):
    """Trade record (execution of an order)."""

    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pair = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)  # BUY or SELL
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    fee = Column(Float, default=0)
    realized_pnl = Column(Float)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (Index("ix_trades_pair_timestamp", "pair", "timestamp"),)


class Signal(Base):
    """AI signal record."""

    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pair = Column(String(20), nullable=False, index=True)
    signal_type = Column(String(20), nullable=False)  # BUY, SELL, HOLD
    confidence = Column(Float)
    strategy = Column(String(50))
    features = Column(Text)  # JSON
    result = Column(String(20), default="pending")  # pending, executed, rejected
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (Index("ix_signals_pair_timestamp", "pair", "timestamp"),)


class Position(Base):
    """Persisted open position — survives bot restarts."""

    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(64), nullable=False, unique=True, index=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False)  # BUY or SELL
    amount = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    stop_loss = Column(Float)
    take_profit = Column(Float)
    total_entry_cost = Column(Float, default=0.0)
    is_partial_fill = Column(Boolean, default=False)
    remaining_amount = Column(Float, default=0.0)
    trailing_peak = Column(Float)
    opened_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class HeldCoinHistory(Base):
    """Historical record of coins bought by the bot."""

    __tablename__ = "held_coins_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, unique=True, index=True)
    first_held_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_held_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    total_bought_qty = Column(Float, default=0.0)
    last_bought_qty = Column(Float, default=0.0)


class ClosedTrade(Base):
    """Completed trade with full P/L breakdown including fees."""

    __tablename__ = "closed_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)  # BUY (long entry)
    amount = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    entry_cost = Column(Float, nullable=False)  # THB spent on BUY
    gross_exit = Column(Float, nullable=False)  # exit_price × amount
    entry_fee = Column(Float, default=0.0)
    exit_fee = Column(Float, default=0.0)
    total_fees = Column(Float, default=0.0)
    net_pnl = Column(Float, default=0.0)
    net_pnl_pct = Column(Float, default=0.0)
    trigger = Column(String(20))  # TP, SL, TRAILING, MANUAL
    price_source = Column(String(20))  # ws, rest
    opened_at = Column(DateTime)
    closed_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (Index("ix_closed_trades_symbol_closed", "symbol", "closed_at"),)


class TradeState(Base):
    """Persistent state-machine row per symbol for execution lifecycle control."""

    __tablename__ = "trade_states"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, unique=True, index=True)
    state = Column(String(20), nullable=False, default="idle", index=True)
    side = Column(String(10), nullable=False, default="buy")
    entry_order_id = Column(String(64), index=True)
    exit_order_id = Column(String(64), index=True)
    active_order_id = Column(String(64), index=True)
    requested_amount = Column(Float, default=0.0)
    filled_amount = Column(Float, default=0.0)
    entry_price = Column(Float, default=0.0)
    exit_price = Column(Float, default=0.0)
    stop_loss = Column(Float, default=0.0)
    take_profit = Column(Float, default=0.0)
    total_entry_cost = Column(Float, default=0.0)
    signal_confidence = Column(Float, default=0.0)
    signal_source = Column(String(30))
    trigger = Column(String(20))
    notes = Column(Text)
    opened_at = Column(DateTime)
    last_transition_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (Index("ix_trade_states_symbol_state", "symbol", "state"),)
