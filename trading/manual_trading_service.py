"""Manual BUY / SELL / track-position CLI flows delegated from ``TradingBotApp``."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, Optional

from execution import quantize_decimal
from risk_management import resolve_effective_sl_tp_percentages
from state_management import normalize_buy_quantity
from trading.cli_pair_normalize import normalize_cli_pair as _normalize_cli_pair

logger = logging.getLogger(__name__)


class ManualTradingService:
    __slots__ = ("_app",)

    def __init__(self, app: Any) -> None:
        self._app = app

    def _ensure_trade_allowed(self) -> None:
        app = self._app
        if app.config.get("auth_degraded", False):
            raise RuntimeError("Manual trading is blocked in auth degraded mode")
        if app.config.get("read_only", False) or app.config.get("simulate_only", False):
            raise RuntimeError("Manual trading is blocked in read-only or simulation mode")
        if not app.executor or not app.api_client:
            raise RuntimeError("Trading components are not initialized")

    def _sync_runtime_position_state(self) -> None:
        app = self._app
        executor = app.executor
        if not executor:
            return
        if app.bot and getattr(app.bot, "_state_machine_enabled", False):
            state_manager = getattr(app.bot, "_state_manager", None)
            if state_manager is not None:
                try:
                    state_manager.sync_in_position_states(executor.get_open_orders())
                except Exception as exc:
                    logger.warning("[CLI] Failed to sync state machine after manual command: %s", exc)

    def submit_manual_market_buy(self, pair: str, thb_amount: float) -> Dict[str, Any]:
        """Submit a market buy in quote currency and track it like a runtime position."""
        self._ensure_trade_allowed()
        app = self._app

        symbol = _normalize_cli_pair(pair)
        try:
            amount_value = float(thb_amount)
        except (TypeError, ValueError) as exc:
            raise ValueError("BUY amount must be a number") from exc
        if amount_value < 15.0:
            raise ValueError("BUY amount must be at least 15 quote")

        executor = app.executor
        if not executor:
            raise RuntimeError("Trading executor is not available")

        from trade_executor import OrderRequest, OrderSide, OrderStatus

        result = executor.execute_order(
            OrderRequest(
                symbol=symbol,
                side=OrderSide.BUY,
                amount=float(quantize_decimal(amount_value, 2)),
                price=0.0,
                order_type="market",
            )
        )
        if not result.success or not result.order_id:
            raise RuntimeError(result.message or "Market BUY failed")

        reference_price = float(result.filled_price or app._get_cli_price(symbol) or 0.0)
        filled_amount = normalize_buy_quantity(
            float(result.filled_amount or amount_value), reference_price, amount_value
        )
        market_buy_filled = filled_amount > 0.0 and reference_price > 0.0
        tracked_payload = {
            "symbol": symbol,
            "side": OrderSide.BUY,
            "amount": filled_amount,
            "entry_price": reference_price,
            "stop_loss": None,
            "take_profit": None,
            "timestamp": datetime.now(),
            "is_partial_fill": result.status == OrderStatus.PARTIAL,
            "remaining_amount": 0.0 if market_buy_filled else float(result.remaining_amount or 0.0),
            "total_entry_cost": round(amount_value, 2),
            "filled": result.status == OrderStatus.FILLED or market_buy_filled,
            "filled_amount": filled_amount,
            "filled_price": reference_price,
        }
        executor.register_tracked_position(result.order_id, tracked_payload)
        self._sync_runtime_position_state()
        logger.warning(
            "[CLI] Manual market BUY submitted: %s %.2f quote | order_id=%s", symbol, amount_value, result.order_id
        )
        return {
            "status": "ok",
            "side": "buy",
            "symbol": symbol,
            "thb_amount": round(amount_value, 2),
            "quote_amount": round(amount_value, 2),
            "order_id": result.order_id,
            "filled_amount": float(result.filled_amount or 0.0),
            "filled_price": reference_price,
        }

    def _build_manual_position_sl_tp(self, symbol: str, entry_price: float) -> tuple[Optional[float], Optional[float]]:
        if entry_price <= 0:
            return None, None

        risk_cfg = self._app.config.get("risk", {}) or {}
        stop_loss_pct, take_profit_pct = resolve_effective_sl_tp_percentages(symbol, risk_cfg)

        stop_loss = round(entry_price * (1 + (stop_loss_pct / 100.0)), 6)
        take_profit = round(entry_price * (1 + (take_profit_pct / 100.0)), 6)
        return stop_loss, take_profit

    def track_manual_position(self, pair: str, coin_amount: float, entry_price: float) -> Dict[str, Any]:
        """Register a manually held coin with its real average cost for SL/TP management."""
        self._ensure_trade_allowed()
        app = self._app

        symbol = _normalize_cli_pair(pair)
        try:
            quantity = float(coin_amount)
        except (TypeError, ValueError) as exc:
            raise ValueError("Tracked amount must be a number") from exc
        try:
            avg_cost = float(entry_price)
        except (TypeError, ValueError) as exc:
            raise ValueError("Tracked entry price must be a number") from exc

        if quantity <= 0:
            raise ValueError("Tracked amount must be greater than 0")
        if avg_cost <= 0:
            raise ValueError("Tracked entry price must be greater than 0")
        if (quantity * avg_cost) < 15.0:
            raise ValueError("Tracked position value must be at least 15 quote")

        active_orders = app.list_active_orders()
        existing = next((order for order in active_orders if order.get("symbol") == symbol), None)
        if existing is not None:
            raise ValueError(f"Symbol already tracked: {symbol} ({existing.get('order_id')})")

        stop_loss, take_profit = self._build_manual_position_sl_tp(symbol, avg_cost)
        position_id = f"manual_{symbol}_{int(time.time())}"
        tracked_payload = {
            "symbol": symbol,
            "side": "buy",
            "amount": quantity,
            "entry_price": avg_cost,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "timestamp": datetime.now(),
            "is_partial_fill": False,
            "remaining_amount": quantity,
            "total_entry_cost": round(quantity * avg_cost, 8),
            "filled": True,
            "filled_amount": quantity,
            "filled_price": avg_cost,
            "trigger": "manual_import",
            "notes": "cli_manual_track",
        }
        app.executor.register_tracked_position(position_id, tracked_payload)
        if app.api_client:
            try:
                app.api_client.get_balances(force_refresh=True, allow_stale=False)
            except Exception:
                logger.debug("[CLI] Balance refresh failed after track command", exc_info=True)
        self._sync_runtime_position_state()
        logger.warning(
            "[CLI] Manual position tracked: %s %.8f @ %.4f | order_id=%s | SL=%.4f | TP=%.4f",
            symbol,
            quantity,
            avg_cost,
            position_id,
            float(stop_loss or 0.0),
            float(take_profit or 0.0),
        )
        return {
            "status": "ok",
            "side": "buy",
            "symbol": symbol,
            "amount": quantity,
            "entry_price": avg_cost,
            "order_id": position_id,
            "stop_loss": float(stop_loss or 0.0),
            "take_profit": float(take_profit or 0.0),
            "total_entry_cost": round(quantity * avg_cost, 8),
        }

    def submit_manual_market_sell(self, target: str, amount: Optional[float] = None) -> Dict[str, Any]:
        """Submit a market sell either by pair+amount or by tracked order id."""
        self._ensure_trade_allowed()
        app = self._app

        tracked_order = next((order for order in app.list_active_orders() if order["order_id"] == str(target)), None)
        if tracked_order is not None:
            if amount is not None:
                raise ValueError(
                    "Use close <order_id> for active orders or sell <pair> <amount> for manual quantity sells"
                )
            symbol = tracked_order["symbol"]
            raw_side = tracked_order.get("side")
            side_value = raw_side.value if hasattr(raw_side, "value") else str(raw_side or "").lower()
            if str(side_value).lower() == "buy":
                sell_amount = float(tracked_order.get("filled_amount") or tracked_order.get("amount") or 0.0)
            else:
                sell_amount = float(tracked_order.get("remaining_amount") or tracked_order.get("amount") or 0.0)
            tracked_order_id = tracked_order["order_id"]
        else:
            symbol = _normalize_cli_pair(target)
            tracked_order_id = ""
            if amount is None:
                if re.fullmatch(r"[A-Za-z]+(?:_[A-Za-z]+)?", str(target or "").strip()):
                    raise ValueError("SELL amount must be provided when selling by pair")
                raise ValueError(f"Active order not found: {target}")
            try:
                sell_amount = float(amount)
            except (TypeError, ValueError) as exc:
                raise ValueError("SELL amount must be a number") from exc

        if sell_amount <= 0:
            raise ValueError("SELL amount must be greater than 0")

        executor = app.executor
        if not executor:
            raise RuntimeError("Trading executor is not available")

        from trade_executor import OrderRequest, OrderSide

        result = executor.execute_order(
            OrderRequest(
                symbol=symbol,
                side=OrderSide.SELL,
                amount=sell_amount,
                price=0.0,
                order_type="market",
            )
        )
        if not result.success:
            raise RuntimeError(result.message or "Market SELL failed")

        if tracked_order_id:
            executor.remove_tracked_position(tracked_order_id)
            self._sync_runtime_position_state()

        logger.warning(
            "[CLI] Manual market SELL submitted: %s %.8f | order_id=%s", symbol, sell_amount, result.order_id
        )
        return {
            "status": "ok",
            "side": "sell",
            "symbol": symbol,
            "amount": sell_amount,
            "order_id": result.order_id,
            "closed_order_id": tracked_order_id,
            "filled_amount": float(result.filled_amount or 0.0),
            "filled_price": float(result.filled_price or app._get_cli_price(symbol) or 0.0),
        }
