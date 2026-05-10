"""Bootstrap the isotonic calibration from v1's trades.db. One-shot.

If v1 doesn't have enough resolved live trades to fit isotonic (threshold: 30),
this exits successfully with a clear message — the bot runs fine under identity
calibration (calibrate(p) returns p unchanged) and will accumulate its own live
history to refit from later.

Usage:
    PYTHONPATH=src python scripts/bootstrap_calibration.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import calibration  # noqa: E402
from config import CALIBRATION_PKL, V1_DB_PATH  # noqa: E402

MIN_SAMPLES_FOR_FIT = 30


def _count_eligible_live_trades(db_path: str) -> int:
    """How many resolved, live (paper_trade=0), non-excluded trades has v1 logged?"""
    import sqlite3
    if not os.path.exists(db_path):
        return 0
    try:
        with sqlite3.connect(db_path, timeout=10) as c:
            row = c.execute(
                """
                SELECT COUNT(*) FROM trades t JOIN results r ON r.trade_id = t.id
                WHERE t.paper_trade = 0
                  AND t.our_probability IS NOT NULL
                  AND r.outcome IS NOT NULL
                  AND (t.notes IS NULL OR (
                       t.notes NOT LIKE 'invalid:%'
                       AND t.notes NOT LIKE 'void:%'
                       AND t.notes NOT LIKE 'ghost-%'))
                """
            ).fetchone()
            return int(row[0] or 0)
    except Exception as e:
        print(f"[BOOTSTRAP] Could not count eligible trades: {e}")
        return 0


def main() -> int:
    print(f"Reading v1 history: {V1_DB_PATH}")
    if not os.path.exists(V1_DB_PATH):
        print(f"[BOOTSTRAP] v1 db not found at {V1_DB_PATH}.")
        print("            Bot will run under IDENTITY calibration (calibrate(p)=p)")
        print("            and shrinkage_factor=0.7. This is the correct behavior")
        print("            when no calibration training data is available.")
        return 0

    n = _count_eligible_live_trades(V1_DB_PATH)
    print(f"Eligible resolved live trades in v1 (paper_trade=0): {n}")

    if n < MIN_SAMPLES_FOR_FIT:
        print()
        print(f"[BOOTSTRAP] Too few live trades ({n} < {MIN_SAMPLES_FOR_FIT}) to fit isotonic.")
        print("            Skipping fit. Bot will run under IDENTITY calibration")
        print("            (calibrate(p) = p) with shrinkage_factor = 0.7.")
        print()
        print("            Once v2 has logged ~30 resolved live trades of its own,")
        print("            rerun this script and it will fit from the combined history.")
        return 0

    print(f"Writing calibration to: {CALIBRATION_PKL}")
    os.makedirs(os.path.dirname(CALIBRATION_PKL) or ".", exist_ok=True)
    try:
        stats = calibration.fit_from_v1_history(V1_DB_PATH, CALIBRATION_PKL)
    except ValueError as e:
        # Shouldn't happen given the count above, but keep graceful.
        print(f"[BOOTSTRAP] Fit declined: {e}")
        print("            Continuing under identity calibration.")
        return 0
    except ImportError as e:
        print(f"[BOOTSTRAP] sklearn not importable: {e}")
        print("            Install with: pip install scikit-learn")
        return 1

    print()
    print("=== Calibration fit ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
