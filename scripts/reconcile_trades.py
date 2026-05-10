"""
reconcile_trades.py — one-shot settlement reconciliation.

Walks every open trade in the local DB, checks Kalshi for whether the
underlying market has finalized, and writes a results row for each one
that has. Safe to re-run: skipped if a trade already has a results row.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import reconcile  # noqa: E402
import storage  # noqa: E402


def main() -> None:
    storage.init_db()
    print("Reconciling open trades against Kalshi markets…")
    summary = reconcile.reconcile_settled_trades()
    print(f"  checked:    {summary['checked']}")
    print(f"  settled:    {summary['settled']}")
    print(f"  still open: {summary['still_open']}")
    print(f"  api errors: {summary['api_errors']}")


if __name__ == "__main__":
    main()
