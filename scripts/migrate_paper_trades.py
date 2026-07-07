"""
migrate_paper_trades.py — one-shot migration from data/paper_trades.db
to data/trades.db with paper_trade=1.

Reads each row from paper_trade, inserts an equivalent row into trades
with paper_trade=1 and mode='paper:dry-run'. Dedupes within trades.db
on (ticker, mode, opened_at) — re-running this script is safe.

Run once after deploying the new main.py paper-trade-in-trades.db path,
then optionally `rm data/paper_trades.db` to retire the old DB.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import storage  # noqa: E402

PAPER_DB = ROOT / "data" / "paper_trades.db"


def main():
    if not PAPER_DB.exists():
        print(f"no {PAPER_DB} — nothing to migrate")
        return
    storage.init_db()  # ensure trades.db schema exists
    src = sqlite3.connect(f"file:{PAPER_DB}?mode=ro", uri=True)
    rows = src.execute("""
        SELECT cycle_ts, ticker, venue, city, title, action,
               cal_p, raw_p, entry_price, net_edge, contracts,
               recommended_size_usd, close_time
        FROM paper_trade ORDER BY cycle_ts ASC
    """).fetchall()
    src.close()

    print(f"found {len(rows)} rows in paper_trade")
    migrated = 0
    skipped = 0
    with sqlite3.connect(storage.DB_FILE, timeout=10) as dst:
        for r in rows:
            (cycle_ts, ticker, venue, city, title, action,
             cal_p, raw_p, entry_price, net_edge, contracts,
             size_usd, close_time) = r

            # Derive contracts if missing (same logic main.py + paper_storage use)
            if not contracts:
                if entry_price and size_usd and entry_price > 0:
                    contracts = max(1, int(size_usd / entry_price))
                else:
                    contracts = 1

            opened_at = (
                datetime.fromtimestamp(cycle_ts, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
            )
            # Skip if a paper trade for this (ticker, opened_at) already exists.
            existing = dst.execute(
                "SELECT id FROM trades WHERE ticker=? AND opened_at=? AND paper_trade=1",
                (ticker, opened_at),
            ).fetchone()
            if existing:
                skipped += 1
                continue

            dst.execute("""
                INSERT INTO trades (
                    ticker, city, market_type, action,
                    entry_price, contracts, size_usd,
                    ensemble_p, calibrated_p, edge_at_entry,
                    mode, opened_at, target_settlement, notes,
                    paper_trade, order_id, venue
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                ticker, city, None, action,
                entry_price, contracts, size_usd,
                raw_p, cal_p, net_edge,
                "paper:dry-run", opened_at, close_time, "paper_trade (migrated)",
                1, None, venue or "kalshi",
            ))
            migrated += 1
        dst.commit()

    print(f"migrated: {migrated}  skipped (dupe): {skipped}")
    print()
    print(f"verify with:")
    print(f'  sqlite3 {storage.DB_FILE} "SELECT COUNT(*) FROM trades WHERE paper_trade=1"')
    print()
    print(f"once you've verified, you can safely remove the old DB:")
    print(f"  rm {PAPER_DB}*")


if __name__ == "__main__":
    main()
