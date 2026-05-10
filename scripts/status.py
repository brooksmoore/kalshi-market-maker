"""CLI status command — prints bankroll, exposure, open positions, P&L, etc."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import risk  # noqa: E402
import storage  # noqa: E402
from config import CALIBRATION_META, DB_FILE, PERF_FILE  # noqa: E402


def main() -> int:
    if not os.path.exists(DB_FILE):
        print(f"No database yet at {DB_FILE}. Run main.py at least once.")
        return 1

    risk.invalidate_exposure_cache()
    bankroll, age = risk.get_active_bankroll()
    exposure = risk.get_total_exposure()
    today_pnl = risk._todays_pnl()
    dd = risk.drawdown_pct()
    open_positions = storage.load_open_positions()

    print("=== kalshi_bot_2.0 status ===")
    print(f"  Bankroll       : ${bankroll:.2f}   (age {age:.0f}s)")
    print(f"  Exposure       : ${exposure:.2f}")
    print(f"  Open positions : {len(open_positions)}")
    print(f"  Today P&L      : ${today_pnl:+.2f}")
    print(f"  Drawdown       : {dd:.1%}")

    if os.path.exists(PERF_FILE):
        with open(PERF_FILE) as f:
            perf = json.load(f)
        print(f"  peak_pnl       : {perf.get('peak_pnl', 0.0)}")

    print()
    print("--- calibration ---")
    if os.path.exists(CALIBRATION_META):
        with open(CALIBRATION_META) as f:
            meta = json.load(f)
        for k, v in meta.items():
            print(f"  {k}: {v}")
    else:
        print("  no calibration.meta.json — running with identity calibration")

    if open_positions:
        print()
        print("--- open positions ---")
        for p in open_positions:
            print(
                f"  {p['ticker']:<35} {p['action']:<8} @ {p['entry_price']:.2f} "
                f"x{p['contracts']} size=${p['size_usd']:.2f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
