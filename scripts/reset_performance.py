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

What it does NOT do:
  - Touch trades.db. Trade history is preserved for calibration analysis.
  - Touch calibration.pkl. If you want a fresh calibration too, remove
    data/calibration.pkl manually (or it'll stay identity until refit).

Usage:
    venv/bin/python scripts/reset_performance.py
    venv/bin/python scripts/reset_performance.py --force   # skip confirmation

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

from config import PERF_FILE  # noqa: E402
from risk import _current_venue_signature  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="skip the interactive confirmation prompt")
    args = ap.parse_args()

    perf_path = Path(PERF_FILE)
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

    if not args.force:
        try:
            ans = input("Archive current performance.json and write fresh baseline? [y/N] ")
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
    print()
    print("Next bot boot will populate bankroll/cash from the live Kalshi API.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
