"""
exit_and_reset.py — Exit all open Kalshi demo positions and wipe local state.

Steps:
  1. Fetch live positions from Kalshi demo API
  2. Market-sell every position (yes and no sides)
  3. Wipe all rows from trades.db (preserves schema)
  4. Fetch final balance from Kalshi
  5. Reset performance.json to the new starting bankroll
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# Add src/ to path so we can import the bot modules directly
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import kalshi_client as kc
from config import DB_FILE

PERF_FILE = ROOT / "data" / "performance.json"


def exit_all_positions() -> list[dict]:
    print("Fetching open positions from Kalshi demo…")
    positions = kc.get_open_positions()
    if not positions:
        print("  No open positions found.")
        return []

    exited = []
    for pos in positions:
        ticker = pos.get("ticker", "")
        # Kalshi v2 returns net YES contracts as a decimal string in "position_fp".
        # Negative = NO position, positive = YES position.
        try:
            net = float(pos.get("position_fp") or 0)
        except (ValueError, TypeError):
            net = 0.0
        yes_qty = int(net) if net > 0 else 0
        no_qty = int(abs(net)) if net < 0 else 0

        if yes_qty > 0:
            print(f"  Selling {yes_qty} YES contracts on {ticker}…")
            order_id = kc.sell_position(ticker, "yes", yes_qty)
            if order_id:
                print(f"    ✓ Order placed: {order_id}")
                exited.append({"ticker": ticker, "side": "yes", "qty": yes_qty, "order_id": order_id})
            else:
                print(f"    ✗ sell_position returned None for {ticker} YES")

        if no_qty > 0:
            print(f"  Selling {no_qty} NO contracts on {ticker}…")
            order_id = kc.sell_position(ticker, "no", no_qty)
            if order_id:
                print(f"    ✓ Order placed: {order_id}")
                exited.append({"ticker": ticker, "side": "no", "qty": no_qty, "order_id": order_id})
            else:
                print(f"    ✗ sell_position returned None for {ticker} NO")

        time.sleep(0.2)

    return exited


def wipe_database() -> None:
    print(f"\nWiping database: {DB_FILE}")
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        for table in ("results", "trades", "performance_snapshots", "scan_log"):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.execute(f"DELETE FROM {table}")
            print(f"  Deleted {count} rows from {table}")
        conn.commit()
    print("  Database wiped (schema preserved).")


def reset_performance_json(new_bankroll: float) -> None:
    data = {
        "peak_pnl": 0.0,
        "peak_updated_at": None,
        "starting_bankroll": round(new_bankroll, 2),
        "bankroll": round(new_bankroll, 2),
        "updated_at": None,
        "cash": round(new_bankroll, 2),
    }
    with open(PERF_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  performance.json reset — new starting bankroll: ${new_bankroll:.2f}")


def main() -> None:
    print("=" * 60)
    print("Kalshi Demo — Exit All Positions + Dashboard Reset")
    print("=" * 60)

    # Step 1 — verify API connectivity
    if not kc.verify_api_connection():
        print("\n[ERROR] Cannot reach Kalshi demo API. Check .env credentials.")
        sys.exit(1)
    print("API connection: OK\n")

    # Step 2 — exit positions
    exited = exit_all_positions()
    if exited:
        print(f"\nWaiting 3s for fills to settle…")
        time.sleep(3)

    # Step 3 — wipe local DB
    wipe_database()

    # Step 4 — fetch final balance
    print("\nFetching final balance from Kalshi…")
    bal = kc.get_portfolio_balance()
    if bal:
        total_cents = bal["balance_cents"]
        cash_cents = bal["cash_cents"]
        total_dollars = total_cents / 100.0
        cash_dollars = cash_cents / 100.0
        print(f"  Cash:          ${cash_dollars:.2f}")
        print(f"  Total equity:  ${total_dollars:.2f}")
        new_bankroll = total_dollars
    else:
        print("  [WARN] Could not fetch balance — defaulting to $100.00")
        new_bankroll = 100.0

    # Step 5 — reset performance.json
    print()
    reset_performance_json(new_bankroll)

    print("\n" + "=" * 60)
    print("Done. You can now restart the bot for a fresh session.")
    print(f"Starting bankroll: ${new_bankroll:.2f}")
    if exited:
        print(f"Positions exited: {len(exited)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
