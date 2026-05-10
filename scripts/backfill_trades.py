"""
backfill_trades.py — import executed Kalshi orders into the local trades DB.

Run once (idempotent — skips orders already present by order_id):
    python scripts/backfill_trades.py

What it does:
  1. Fetches all executed BUY orders from the Kalshi portfolio API.
  2. Skips any order_id already in the trades table.
  3. Inserts the rest as trades rows (notes='backfilled').
  4. Prints a summary.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import kalshi_client  # noqa: E402
import storage  # noqa: E402
from config import DB_FILE, WEATHER_SERIES  # noqa: E402

# Reverse map: series_ticker_prefix -> city name  (e.g. "KXHIGHMIA" -> "Miami")
_SERIES_TO_CITY: dict[str, str] = {v: k for k, v in WEATHER_SERIES.items()}


def _city_from_ticker(ticker: str) -> str:
    for series, city in _SERIES_TO_CITY.items():
        if ticker.startswith(series):
            return city
    return ""


def _existing_order_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT order_id FROM trades WHERE order_id IS NOT NULL").fetchall()
    return {r[0] for r in rows}


# Secondary dedup window — see backfill_kalshi_positions.py for rationale.
# 180s covers maker rest + cancel + taker placement within one
# execute_opportunity invocation, but doesn't falsely match separate
# trades on the same market placed minutes-to-hours later.
_ATTR_DEDUP_WINDOW_SECONDS = 180


def _attribute_match_exists(conn: sqlite3.Connection, ticker: str,
                            action: str, contracts: int,
                            opened_at: str) -> bool:
    if not (ticker and action and contracts and opened_at):
        return False
    try:
        from datetime import datetime as _dt, timedelta as _td
        t = _dt.fromisoformat(opened_at.replace("Z", "+00:00"))
    except Exception:
        return False
    lo = (t - _td(seconds=_ATTR_DEDUP_WINDOW_SECONDS)).isoformat()
    hi = (t + _td(seconds=_ATTR_DEDUP_WINDOW_SECONDS)).isoformat()
    row = conn.execute(
        """
        SELECT id FROM trades
        WHERE ticker = ? AND action = ? AND contracts = ?
          AND mode IS NOT NULL AND mode NOT IN ('dry-run', 'backfill')
          AND opened_at BETWEEN ? AND ?
        LIMIT 1
        """,
        (ticker, action, int(contracts), lo, hi),
    ).fetchone()
    return row is not None


def _fill_count(order: dict) -> int:
    fp = order.get("fill_count_fp")
    if fp is not None:
        try:
            return int(float(fp))
        except (ValueError, TypeError):
            pass
    return int(order.get("fill_count") or order.get("filled_count") or 0)


def _mode(order: dict) -> str:
    maker_cost = float(order.get("maker_fill_cost_dollars") or 0)
    taker_cost = float(order.get("taker_fill_cost_dollars") or 0)
    if maker_cost > 0 and taker_cost > 0:
        return "mixed"
    if maker_cost > 0:
        return "maker"
    if taker_cost > 0:
        return "taker"
    return "unknown"


def main() -> None:
    storage.init_db()

    print("Fetching executed orders from Kalshi…")
    orders = kalshi_client.get_filled_orders()
    print(f"  {len(orders)} executed BUY order(s) found")

    if not orders:
        print("Nothing to backfill.")
        return

    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        existing = _existing_order_ids(conn)
        new_orders = [o for o in orders if o.get("order_id") not in existing]
        print(f"  {len(existing)} already in DB, {len(new_orders)} new")

        if not new_orders:
            print("All orders already recorded. Nothing to do.")
            return

        inserted = 0
        skipped = 0
        for order in new_orders:
            ticker = order.get("ticker", "")
            side = order.get("side", "yes")
            action = f"BUY {'YES' if side == 'yes' else 'NO'}"
            price_str = order.get("yes_price_dollars") if side == "yes" else order.get("no_price_dollars")
            entry_price = float(price_str or 0)
            contracts = _fill_count(order)

            if contracts < 1 or entry_price <= 0:
                skipped += 1
                continue

            # Secondary dedup — see comment near _attribute_match_exists.
            action_str = f"BUY {'YES' if side == 'yes' else 'NO'}"
            if _attribute_match_exists(
                conn, ticker, action_str, contracts,
                order.get("created_time") or "",
            ):
                print(f"  ⚠ skipping {ticker} BUY {side.upper()} x{contracts}"
                      f" — attr-dupe of existing live row "
                      f"(oid={order.get('order_id')})")
                skipped += 1
                continue

            size_usd = round(entry_price * contracts, 4)
            city = _city_from_ticker(ticker)
            opened_at = order.get("created_time", "")
            order_id = order.get("order_id", "")
            mode = _mode(order)

            conn.execute(
                """
                INSERT INTO trades (
                    ticker, city, market_type, action,
                    entry_price, contracts, size_usd,
                    ensemble_p, calibrated_p, edge_at_entry,
                    mode, opened_at, target_settlement, notes, paper_trade, order_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker, city, "high_temp", action,
                    entry_price, contracts, size_usd,
                    None, None, None,
                    mode, opened_at, "", "backfilled", 0, order_id,
                ),
            )
            inserted += 1

        conn.commit()

    print(f"\nDone. Inserted {inserted} trade(s), skipped {skipped} (zero-fill or bad price).")
    print("Re-open the dashboard to see them.")


if __name__ == "__main__":
    main()
