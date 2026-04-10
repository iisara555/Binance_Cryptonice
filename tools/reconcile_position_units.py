"""
Reconcile unit mismatches between THB cost and base-asset quantity in SQLite state.

Usage:
  python tools/reconcile_position_units.py           # dry-run
  python tools/reconcile_position_units.py --apply   # apply fixes
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from state_management import normalize_buy_quantity

DB_PATH = Path(__file__).resolve().parent.parent / "crypto_bot.db"


def reconcile_positions(conn: sqlite3.Connection, apply_changes: bool) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, symbol, side, amount, remaining_amount, entry_price, total_entry_cost
        FROM positions
        ORDER BY id ASC
        """
    )
    rows = cur.fetchall()

    updated = 0
    for row in rows:
        row_id, symbol, side, amount, remaining_amount, entry_price, total_entry_cost = row
        side = (side or "").lower()
        amount = float(amount or 0.0)
        remaining_amount = float(remaining_amount or 0.0)
        entry_price = float(entry_price or 0.0)
        total_entry_cost = float(total_entry_cost or 0.0)

        if side != "buy" or remaining_amount > 0 or entry_price <= 0 or total_entry_cost <= 0 or amount <= 0:
            continue

        implied_qty = normalize_buy_quantity(amount, entry_price, total_entry_cost)
        if implied_qty == amount:
            continue

        print(
            f"[positions] id={row_id} {symbol}: amount {amount:.8f} -> {implied_qty:.8f} "
            f"(cost={total_entry_cost:.2f}, price={entry_price:.6f})"
        )
        if apply_changes:
            cur.execute("UPDATE positions SET amount = ? WHERE id = ?", (implied_qty, row_id))
        updated += 1

    return updated


def reconcile_trade_states(conn: sqlite3.Connection, apply_changes: bool) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, symbol, state, side, requested_amount, filled_amount, entry_price, total_entry_cost
        FROM trade_states
        ORDER BY id ASC
        """
    )
    rows = cur.fetchall()

    updated = 0
    for row in rows:
        row_id, symbol, state, side, requested_amount, filled_amount, entry_price, total_entry_cost = row
        state = (state or "").lower()
        side = (side or "").lower()
        requested_amount = float(requested_amount or 0.0)
        filled_amount = float(filled_amount or 0.0)
        entry_price = float(entry_price or 0.0)
        total_entry_cost = float(total_entry_cost or 0.0)

        if side != "buy" or state not in {"in_position", "pending_sell"}:
            continue
        if entry_price <= 0 or total_entry_cost <= 0 or filled_amount <= 0:
            continue

        implied_qty = normalize_buy_quantity(filled_amount, entry_price, total_entry_cost)
        reference_cost = requested_amount if requested_amount > 0 else total_entry_cost
        normalized_from_reference = normalize_buy_quantity(filled_amount, entry_price, reference_cost)
        if implied_qty == filled_amount and normalized_from_reference == filled_amount:
            continue

        if normalized_from_reference != filled_amount:
            implied_qty = normalized_from_reference

        print(
            f"[trade_states] id={row_id} {symbol} ({state}): filled_amount {filled_amount:.8f} -> {implied_qty:.8f} "
            f"(requested={requested_amount:.2f}, cost={total_entry_cost:.2f}, price={entry_price:.6f})"
        )
        if apply_changes:
            cur.execute("UPDATE trade_states SET filled_amount = ? WHERE id = ?", (implied_qty, row_id))
        updated += 1

    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile THB/base unit mismatches in DB state")
    parser.add_argument("--apply", action="store_true", help="Apply updates to database")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    try:
        pos_updates = reconcile_positions(conn, args.apply)
        state_updates = reconcile_trade_states(conn, args.apply)
        total = pos_updates + state_updates

        if args.apply:
            conn.commit()
            print(f"Applied updates: positions={pos_updates}, trade_states={state_updates}, total={total}")
        else:
            conn.rollback()
            print(f"Dry-run only: positions={pos_updates}, trade_states={state_updates}, total={total}")
            print("Run with --apply to persist updates.")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
