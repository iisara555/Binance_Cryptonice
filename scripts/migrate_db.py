from __future__ import annotations

import sqlite3
from pathlib import Path


def get_db_path() -> Path:
    # scripts/ is one level below the project root
    return Path(__file__).resolve().parents[1] / "crypto_bot.db"


def get_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    # Uses PRAGMA table_info to get existing column names
    cur = conn.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cur.fetchall()}  # row format: (cid, name, type, notnull, dflt_value, pk)


def main() -> None:
    db_path = get_db_path()
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        columns = get_table_columns(conn, "orders")
        changed = False

        if "created_at" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN created_at TEXT")
            changed = True

        if "updated_at" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN updated_at TEXT")
            changed = True

        conn.commit()

        if changed:
            print("Migration complete")
        else:
            print("Already up to date")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

