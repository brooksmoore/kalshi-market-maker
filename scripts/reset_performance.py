"""
reset_performance.py — archive performance.json and write a fresh baseline.

Use this when the bot's local state (peak_pnl, drawdown baseline) needs to be
rebased against a different Kalshi account — most commonly the demo→prod
transition, when the demo-era peak would otherwise corrupt prod drawdown math.

What it does:
  1. Archives the current performance.json to data/_archive/ with a timestamp.
  2. Writes a fresh performance.json: peak_pnl=0, bankroll/cash placeholders
     of 0 (the bot's first cycle will repopulate from Kalshi), the current
     env's venue_signature stamped, and a reset_note recording when/why.

What it does NOT do by default:
  - Touch trades.db (use --wipe-trades for that — needed at the demo→prod
    flip, since the existing `venue` column only distinguishes Kalshi from
    Polymarket, not demo Kalshi from prod Kalshi).
  - Touch calibration.pkl. If you want a fresh calibration too, remove
    data/calibration.pkl manually (or it'll stay identity until refit).

Usage:
    venv/bin/python scripts/reset_performance.py
    venv/bin/python scripts/reset_performance.py --force          # skip prompt
    venv/bin/python scripts/reset_performance.py --wipe-trades    # also wipe trades.db

Always asks for confirmation unless --force is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config import DB_FILE, PERF_FILE  # noqa: E402
from risk import _current_venue_signature  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="skip the interactive confirmation prompt")
    ap.add_argument("--wipe-trades", action="store_true",
                    help="also archive and remove data/trades.db (use at "
                         "demo→prod flip; storage.init_db() recreates it)")
    args = ap.parse_args()

    perf_path = Path(PERF_FILE)
    db_path = Path(DB_FILE)
    archive_dir = perf_path.parent / "_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    sig = _current_venue_signature()
    if not sig:
        print(
            "WARNING: KALSHI_API_URL or KALSHI_API_KEY_ID is not set in .env.\n"
            "The new performance.json will have no venue_signature, so the\n"
            "bot's mismatch check will skip on next boot until credentials\n"
            "are set and the file is re-stamped.\n"
        )

    # Show current state so user knows what they're nuking
    current = {}
    if perf_path.exists():
        try:
            current = json.loads(perf_path.read_text())
        except Exception as e:
            print(f"could not parse existing {perf_path}: {e}")
    else:
        print(f"note: {perf_path} does not exist yet (nothing to archive)")

    if current:
        print("Current performance.json:")
        print(f"  peak_pnl:           {current.get('peak_pnl')}")
        print(f"  bankroll:           {current.get('bankroll')}")
        print(f"  cash:               {current.get('cash')}")
        print(f"  starting_bankroll:  {current.get('starting_bankroll')}")
        print(f"  venue_signature:    {current.get('venue_signature') or '(unset)'}")
        print(f"  updated_at:         {current.get('updated_at')}")
        print()
    print(f"New venue_signature will be: {sig or '(none — env unset)'}")
    print()

    # Trades.db preview (only if --wipe-trades)
    trade_count = 0
    if args.wipe_trades and db_path.exists():
        import sqlite3
        try:
            with sqlite3.connect(db_path) as c:
                trade_count = c.execute(
                    "SELECT COUNT(*) FROM trades"
                ).fetchone()[0]
        except Exception as e:
            print(f"warning: could not count trades.db rows: {e}")
        print(f"--wipe-trades: will archive trades.db ({trade_count} rows) and remove it")
        print(f"  bot's next boot will recreate empty schema via storage.init_db()")
        print()

    if not args.force:
        prompt = "Archive performance.json"
        if args.wipe_trades:
            prompt += f" AND wipe trades.db ({trade_count} rows)"
        prompt += " and write fresh baseline? [y/N] "
        try:
            ans = input(prompt)
        except EOFError:
            ans = ""
        if ans.strip().lower() not in ("y", "yes"):
            print("aborted.")
            return 1

    # Archive existing
    if perf_path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = archive_dir / f"performance.json.before_reset_{stamp}"
        shutil.copy2(perf_path, dest)
        print(f"archived → {dest}")

    # Write fresh baseline. Zero financials; bot's first cycle will populate.
    fresh = {
        "peak_pnl": 0.0,
        "peak_updated_at": datetime.now().isoformat(),
        "starting_bankroll": 0.0,
        "bankroll": 0.0,
        "cash": 0.0,
        "updated_at": datetime.now().isoformat(),
        "peak_reset_note": (
            f"Performance reset by scripts/reset_performance.py on "
            f"{datetime.now().isoformat()}. Trade history (trades.db) preserved."
        ),
    }
    if sig:
        fresh["venue_signature"] = sig
        fresh["venue_signature_stamped_at"] = datetime.now().isoformat()

    perf_path.parent.mkdir(parents=True, exist_ok=True)
    perf_path.write_text(json.dumps(fresh, indent=2))
    print(f"wrote fresh → {perf_path}")

    # Wipe trades.db if requested (after perf reset to avoid half-state)
    if args.wipe_trades and db_path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        db_archive = archive_dir / f"trades.db.before_reset_{stamp}"
        shutil.copy2(db_path, db_archive)
        print(f"archived trades.db → {db_archive}")
        # Also remove any sqlite WAL/SHM journal artifacts so next boot
        # starts cleanly (storage.init_db creates fresh schema).
        for ext in ("", "-wal", "-shm", "-journal"):
            p = db_path.parent / (db_path.name + ext)
            if p.exists():
                p.unlink()
        print(f"removed {db_path} (and any -wal/-shm journals)")

    print()
    print("Next bot boot will populate bankroll/cash from the live Kalshi API.")
    if args.wipe_trades:
        print("Trades table will be recreated empty by storage.init_db().")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
