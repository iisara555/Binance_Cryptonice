"""
Portfolio Manager Module for Crypto Trading Bot
================================================
Tracks balances, open positions, P&L, and portfolio value.
"""

import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


# ─── Decimal Places per Asset ─────────────────────────────────────────────

# This maps asset symbols to their decimal precision on Bitkub.
# Values come from Bitkub API's 'decimals' field in /api/v3/market/assets
ASSET_DECIMALS = {
    "THB": 2,    # Thai Baht: 2 decimal places
    "BTC": 8,    # Bitcoin: 8 decimal places
    "ETH": 8,    # Ethereum: 8 decimal places
    "XRP": 0,    # Ripple: 0 decimal places
    "ADA": 0,    # Cardano: 0 decimal places
    "DOGE": 0,   # Dogecoin: 0 decimal places
    "BNB": 8,    # Binance Coin: 8 decimal places
    "XAUT": 8,   # Tether Gold: 8 decimal places
    "SOL": 8,    # Solana: 8 decimal places
    "SHIB": 0,   # Shiba Inu: 0 decimal places
    "USDT": 2,   # Tether: 2 decimal places
}


def get_asset_decimals(symbol: str) -> int:
    """Get the decimal places for a given asset symbol."""
    return ASSET_DECIMALS.get(symbol.upper(), 8)


def format_amount(amount: float, symbol: str) -> str:
    """Format an amount with the correct number of decimal places for the asset.
    
    Args:
        amount: The numeric amount to format
        symbol: The asset symbol (e.g., 'BTC', 'THB', 'XRP')
        
    Returns:
        Formatted string with appropriate decimal places
    """
    decimals = get_asset_decimals(symbol)
    if decimals == 0:
        return str(int(round(amount)))
    return f"{round(amount, decimals):.{decimals}f}"


def format_quantity_for_display(quantity: float, symbol: str) -> str:
    """Format a quantity with proper decimal places for logging.
    
    Args:
        quantity: The quantity to format
        symbol: The asset symbol
        
    Returns:
        Formatted string suitable for terminal display
    """
    decimals = get_asset_decimals(symbol)
    if decimals == 0:
        return str(int(quantity))
    # For display, use up to 8 decimal places but strip trailing zeros
    formatted = f"{quantity:.{decimals}f}"
    # Strip trailing zeros after decimal point
    if '.' in formatted:
        formatted = formatted.rstrip('0').rstrip('.')
    return formatted


@dataclass
class Position:
    symbol: str
    side: str           # "long" or "short"
    entry_price: float
    quantity: float
    current_price: float
    stop_loss: float
    take_profit: float
    opened_at: str      # ISO datetime string
    entry_value: float  # quantity * entry_price (USD)
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0

    def update_price(self, new_price: float):
        self.current_price = new_price
        if self.side == "long":
            self.unrealized_pnl = (new_price - self.entry_price) * self.quantity
            self.unrealized_pnl_pct = (
                (new_price - self.entry_price) / self.entry_price * 100
                if self.entry_price else 0
            )
        else:  # short
            self.unrealized_pnl = (self.entry_price - new_price) * self.quantity
            self.unrealized_pnl_pct = (
                (self.entry_price - new_price) / self.entry_price * 100
                if self.entry_price else 0
            )

    def to_dict(self) -> dict:
        d = asdict(self)
        # Format quantity with proper decimal places based on asset type
        # Extract base asset from symbol (e.g., "THB_BTC" -> "BTC")
        base_asset = self.symbol.split("_")[1] if "_" in self.symbol else self.symbol
        d["quantity"] = round(self.quantity, get_asset_decimals(base_asset))
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(**d)


@dataclass
class TradeRecord:
    """A closed/settled trade."""
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    realized_pnl: float            # Gross P&L before fees
    realized_pnl_pct: float        # Gross P&L percentage
    entry_fee: float = 0.0         # Fee paid on entry (THB)
    exit_fee: float = 0.0          # Fee paid on exit (THB)
    net_pnl: float = 0.0          # Net P&L after fees
    exit_reason: str = ""          # "stop_loss", "take_profit", "manual", etc.
    opened_at: str = ""
    closed_at: str = ""

    def __post_init__(self):
        # Auto-calculate net_pnl if not provided
        if self.net_pnl == 0.0 and self.realized_pnl != 0.0:
            self.net_pnl = round(self.realized_pnl - self.entry_fee - self.exit_fee, 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TradeRecord":
        return cls(**d)


@dataclass
class DailySnapshot:
    date: str
    starting_balance: float
    ending_balance: float
    realized_pnl: float
    trades_count: int
    win_count: int
    loss_count: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DailySnapshot":
        return cls(**d)


class PortfolioManager:
    """
    Manages portfolio state: balance, open positions, P&L, and history.
    """

    def __init__(
        self,
        initial_balance: float = 1000.0,
        persist_path: Optional[str] = None,
    ):
        self.initial_balance = initial_balance
        self.current_balance = initial_balance
        self.positions: dict[str, Position] = {}   # symbol -> Position
        self.trade_history: list[TradeRecord] = []
        self.daily_snapshots: list[DailySnapshot] = []

        # Daily tracking
        self._daily_start_balance: Optional[float] = None
        self._daily_start_date: Optional[date] = None
        self._today_trades: int = 0
        self._today_wins: int = 0
        self._today_losses: int = 0

        self.persist_path = persist_path
        if persist_path and Path(persist_path).exists():
            self._load()

    # ── Portfolio Value ─────────────────────────────────────────────────

    def total_portfolio_value(self) -> float:
        """Total value = cash balance + sum of all open positions at current price."""
        positions_value = sum(
            p.current_price * p.quantity if p.side == "long"
            else p.entry_value + p.unrealized_pnl
            for p in self.positions.values()
        )
        return self.current_balance + positions_value

    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    def total_realized_pnl(self) -> float:
        return sum(t.realized_pnl for t in self.trade_history)

    # ── Position Management ────────────────────────────────────────────

    def open_position(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: float,
        stop_loss: float,
        take_profit: float,
    ) -> Position:
        """Open a new position and deduct balance."""
        if symbol in self.positions:
            logger.error(
                f"Position already open for {symbol} — rejecting duplicate open. "
                f"Existing entry_price={self.positions[symbol].entry_price:.2f}"
            )
            return self.positions[symbol]
        entry_value = entry_price * quantity
        pos = Position(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            current_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            opened_at=datetime.now().isoformat(),
            entry_value=entry_value,
        )
        self.positions[symbol] = pos
        # Reserve balance (for long positions, lock the cost)
        if side == "long":
            self.current_balance -= entry_value
        
        # Extract base asset for formatting (e.g., "THB_BTC" -> "BTC")
        base_asset = symbol.split("_")[1] if "_" in symbol else symbol
        qty_formatted = format_quantity_for_display(quantity, base_asset)
        
        logger.info(
            f"Opened position: {symbol} {side} qty={qty_formatted} {base_asset} "
            f"entry={entry_price:.2f} THB"
        )
        self._save()
        return pos

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        reason: str,
        entry_fee: float = 0.0,
        exit_fee: float = 0.0,
    ) -> Optional[TradeRecord]:
        """Close an existing position and record the trade.
        
        Args:
            symbol: Trading pair symbol
            exit_price: Price at which position was closed
            reason: Reason for closing (stop_loss, take_profit, manual)
            entry_fee: Fee paid when opening position (THB)
            exit_fee: Fee paid when closing position (THB)
        """
        pos = self.positions.pop(symbol, None)
        if pos is None:
            logger.warning(f"Attempted to close non-existent position: {symbol}")
            return None

        pos.update_price(exit_price)
        realized = pos.unrealized_pnl

        # Net P&L after fees
        net_pnl = realized - entry_fee - exit_fee

        # Refund balance (for long positions) - include fees in the refund
        if pos.side == "long":
            # Refund: original cost + P&L - ALL fees
            self.current_balance += pos.entry_value + realized - entry_fee - exit_fee
        else:  # short: close at current price
            self.current_balance += pos.entry_value + realized - entry_fee - exit_fee

        record = TradeRecord(
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            realized_pnl=round(realized, 2),
            realized_pnl_pct=round(pos.unrealized_pnl_pct, 2),
            entry_fee=round(entry_fee, 2),
            exit_fee=round(exit_fee, 2),
            net_pnl=round(net_pnl, 2),
            exit_reason=reason,
            opened_at=pos.opened_at,
            closed_at=datetime.now().isoformat(),
        )
        self.trade_history.append(record)

        # Daily stats (based on net pnl)
        self._today_trades += 1
        if net_pnl > 0:
            self._today_wins += 1
        else:
            self._today_losses += 1

        # Format quantity with proper decimal places for logging
        base_asset = pos.symbol.split("_")[1] if "_" in pos.symbol else pos.symbol
        qty_formatted = format_quantity_for_display(pos.quantity, base_asset)
        
        logger.info(
            f"Closed position: {symbol} {pos.side} qty={qty_formatted} {base_asset} "
            f"reason={reason} realized_pnl={realized:.2f} THB "
            f"({pos.unrealized_pnl_pct:.2f}%) fees={entry_fee:.2f}+{exit_fee:.2f} THB "
            f"net_pnl={net_pnl:.2f} THB"
        )
        self._save()
        return record

    def update_position_price(self, symbol: str, current_price: float):
        """Update mark-to-market price for a position."""
        if symbol in self.positions:
            self.positions[symbol].update_price(current_price)

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.positions.get(symbol)

    def get_open_positions(self) -> list[Position]:
        return list(self.positions.values())

    # ── P&L ─────────────────────────────────────────────────────────────

    def daily_pnl(self) -> float:
        """Realized P&L today."""
        today = date.today()
        return sum(
            t.realized_pnl for t in self.trade_history
            if datetime.fromisoformat(t.closed_at).date() == today
        )

    def daily_pnl_pct(self) -> float:
        """Realized P&L % today (vs daily start balance)."""
        start = self._get_daily_start_balance()
        if start == 0:
            return 0.0
        return round(self.daily_pnl() / start * 100, 2)

    def win_rate(self) -> float:
        """Win rate across all closed trades."""
        if not self.trade_history:
            return 0.0
        wins = sum(1 for t in self.trade_history if t.realized_pnl > 0)
        return round(wins / len(self.trade_history) * 100, 2)

    # ── Daily Reset ─────────────────────────────────────────────────────

    def _get_daily_start_balance(self) -> float:
        today = date.today()
        if self._daily_start_date != today:
            self._daily_start_balance = self.total_portfolio_value()
            self._daily_start_date = today
            self._today_trades = 0
            self._today_wins = 0
            self._today_losses = 0
        return self._daily_start_balance or self.current_balance

    def reset_daily_tracking(self, new_start_balance: float):
        """Manually reset daily tracking (e.g., on bot restart)."""
        self._daily_start_balance = new_start_balance
        self._daily_start_date = date.today()
        self._today_trades = 0
        self._today_wins = 0
        self._today_losses = 0

    # ── Summary ──────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        """Return a full portfolio summary dict."""
        total_value = self.total_portfolio_value()
        unrealized = self.total_unrealized_pnl()
        realized = self.total_realized_pnl()
        total_pnl = unrealized + realized
        total_pnl_pct = round((total_pnl / self.initial_balance) * 100, 2) if self.initial_balance else 0

        return {
            "initial_balance": self.initial_balance,
            "current_balance": round(self.current_balance, 2),
            "total_portfolio_value": round(total_value, 2),
            "total_unrealized_pnl": round(unrealized, 2),
            "total_realized_pnl": round(realized, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": total_pnl_pct,
            "open_positions_count": len(self.positions),
            "open_positions": [p.to_dict() for p in self.positions.values()],
            "trade_history_count": len(self.trade_history),
            "win_rate_pct": self.win_rate(),
            "daily_pnl": round(self.daily_pnl(), 2),
            "daily_pnl_pct": self.daily_pnl_pct(),
            "today_trades": self._today_trades,
            "today_wins": self._today_wins,
            "today_losses": self._today_losses,
        }

    def get_summary_with_formatting(self) -> dict:
        """Return a portfolio summary with properly formatted values for all assets.
        
        This method includes formatted strings for quantities with proper decimal places
        for each asset type. Use this for display/logging purposes.
        
        Returns:
            Dict with both numeric values and formatted strings for display
        """
        total_value = self.total_portfolio_value()
        unrealized = self.total_unrealized_pnl()
        realized = self.total_realized_pnl()
        total_pnl = unrealized + realized
        total_pnl_pct = round((total_pnl / self.initial_balance) * 100, 2) if self.initial_balance else 0
        
        # Format open positions with proper decimal places
        formatted_positions = []
        for pos in self.positions.values():
            base_asset = pos.symbol.split("_")[1] if "_" in pos.symbol else pos.symbol
            formatted_positions.append({
                "symbol": pos.symbol,
                "side": pos.side,
                "quantity_raw": pos.quantity,
                "quantity_formatted": format_quantity_for_display(pos.quantity, base_asset),
                "quantity_decimals": get_asset_decimals(base_asset),
                "entry_price": pos.entry_price,
                "current_price": pos.current_price,
                "unrealized_pnl": pos.unrealized_pnl,
                "unrealized_pnl_pct": pos.unrealized_pnl_pct,
            })
        
        return {
            "initial_balance": self.initial_balance,
            "current_balance": self.current_balance,  # Keep raw for calculations
            "current_balance_formatted": format_amount(self.current_balance, "THB"),
            "total_portfolio_value": total_value,
            "total_portfolio_value_formatted": format_amount(total_value, "THB"),
            "total_unrealized_pnl": unrealized,
            "total_unrealized_pnl_formatted": format_amount(unrealized, "THB"),
            "total_realized_pnl": realized,
            "total_realized_pnl_formatted": format_amount(realized, "THB"),
            "total_pnl": total_pnl,
            "total_pnl_formatted": format_amount(total_pnl, "THB"),
            "total_pnl_pct": total_pnl_pct,
            "open_positions_count": len(self.positions),
            "open_positions": formatted_positions,
            "trade_history_count": len(self.trade_history),
            "win_rate_pct": self.win_rate(),
            "daily_pnl": self.daily_pnl(),
            "daily_pnl_formatted": format_amount(self.daily_pnl(), "THB"),
            "daily_pnl_pct": self.daily_pnl_pct(),
            "today_trades": self._today_trades,
            "today_wins": self._today_wins,
            "today_losses": self._today_losses,
            "asset_decimals": dict(ASSET_DECIMALS),  # Include for reference
        }

    # ── Persistence ─────────────────────────────────────────────────────

    def _save(self):
        if not self.persist_path:
            return
        data = {
            "initial_balance": self.initial_balance,
            "current_balance": self.current_balance,
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "trade_history": [t.to_dict() for t in self.trade_history],
            "daily_snapshots": [s.to_dict() for s in self.daily_snapshots],
            "_daily_start_balance": self._daily_start_balance,
            "_daily_start_date": str(self._daily_start_date) if self._daily_start_date else None,
        }
        with open(self.persist_path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def _load(self):
        try:
            with open(self.persist_path, "r") as f:
                data = json.load(f)
            self.initial_balance = data.get("initial_balance", self.initial_balance)
            self.current_balance = data.get("current_balance", self.current_balance)
            self.positions = {
                s: Position.from_dict(p) for s, p in data.get("positions", {}).items()
            }
            self.trade_history = [
                TradeRecord.from_dict(t) for t in data.get("trade_history", [])
            ]
            self.daily_snapshots = [
                DailySnapshot.from_dict(s) for s in data.get("daily_snapshots", [])
            ]
            dsd = data.get("_daily_start_date")
            self._daily_start_date = date.fromisoformat(dsd) if dsd else None
            self._daily_start_balance = data.get("_daily_start_balance")
            logger.info("Portfolio state loaded from disk.")
        except Exception as e:
            logger.error(f"Failed to load portfolio state: {e}")
