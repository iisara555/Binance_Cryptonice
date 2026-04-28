from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def backfill_trades(db_path: Path) -> int:
    connection = sqlite3.connect(str(db_path))
    try:
        cursor = connection.cursor()
        cursor.execute("""
            INSERT INTO trades (pair, side, quantity, price, fee, realized_pnl, timestamp)
            SELECT
                o.pair,
                LOWER(o.side),
                COALESCE(NULLIF(o.filled_quantity, 0), o.quantity),
                COALESCE(NULLIF(o.filled_price, 0), o.price),
                COALESCE(o.fee, 0),
                NULL,
                COALESCE(o.created_at, o.timestamp)
            FROM orders o
            WHERE LOWER(COALESCE(o.status, '')) = 'filled'
              AND COALESCE(NULLIF(o.filled_quantity, 0), o.quantity, 0) > 0
              AND COALESCE(NULLIF(o.filled_price, 0), o.price, 0) > 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM trades t
                  WHERE t.pair = o.pair
                    AND LOWER(t.side) = LOWER(o.side)
                    AND ABS(t.quantity - COALESCE(NULLIF(o.filled_quantity, 0), o.quantity)) < 1e-12
                    AND ABS(t.price - COALESCE(NULLIF(o.filled_price, 0), o.price)) < 1e-12
                    AND COALESCE(t.timestamp, '') = COALESCE(o.created_at, o.timestamp)
              )
            """)
        inserted_rows = int(cursor.rowcount or 0)
        connection.commit()
        return inserted_rows
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill trades from filled orders.")
    parser.add_argument("--db", default="crypto_bot.db", help="Path to SQLite database file")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    inserted_rows = backfill_trades(db_path)
    print(f"Backfilled {inserted_rows} trade rows into {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
