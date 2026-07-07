"""
market_calibration.py — measure how well prod-market prices forecast outcomes.

Reads `data/prod_observer.db` for every ticker whose close_time has passed,
fetches the finalized outcome from Kalshi's public market endpoint, and
pairs the outcome with the market's implied YES probability at a chosen
moment before close. Then computes:

  1. Brier score on (market_p, outcome) pairs vs naive baseline p=0.5
  2. Calibration buckets — for each 10% bin of market_p, the empirical
     yes-rate
  3. Decomposition by time-to-close (T-30, T-60, T-180) to show price
     informativeness sharpening as close approaches

WHAT THIS DOES NOT DO:
  - This is NOT a measurement of OUR model's calibration. It measures
    how well-informed prod prices are — the precondition question
    "is there meaningful disagreement to capture edge against?"
  - There is no synthetic P&L computation. Forecast quality only.
    (See prod_transition_plan_20260510.md shadow-logger discipline.)

Usage:
    venv/bin/python scripts/market_calibration.py
    venv/bin/python scripts/market_calibration.py --t-minus 30  # minutes before close
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "prod_observer.db"
PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _iso_to_epoch(s: str) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fetch_market(ticker: str) -> dict | None:
    """Unauth GET /markets/{ticker}. Returns dict or None."""
    try:
        r = requests.get(f"{PROD_BASE}/markets/{ticker}", timeout=10)
        if r.status_code != 200:
            return None
        return r.json().get("market")
    except (requests.ConnectionError, requests.Timeout):
        return None


def market_mid(snap: tuple) -> float | None:
    """YES probability from a snapshot row (prices stored as dollars 0–1).

    Preference order:
      1. Two-sided yes book → mean of yes_bid, yes_ask
      2. Two-sided no book  → 1 − mean(no_bid, no_ask)
      3. One-sided fallback: when market is at the edge (e.g. consensus
         no_bid=0.99, yes_ask=0.01, yes_bid=None), the implied YES is
         still well-defined (~0.01) and tossing this data is wrong.
         Use the tightest available signal.
    """
    yes_bid, yes_ask, no_bid, no_ask = snap

    if yes_bid is not None and yes_ask is not None and yes_bid > 0 and yes_ask > 0:
        return (yes_bid + yes_ask) / 2.0
    if no_bid is not None and no_ask is not None and no_bid > 0 and no_ask > 0:
        return 1.0 - (no_bid + no_ask) / 2.0

    # One-sided fallback: derive from whichever side is quoted.
    # yes_ask = lowest price someone'll sell YES = upper bound on p_yes
    # no_bid  = highest price someone'll buy NO  → 1 - no_bid is upper bound on p_yes
    candidates = []
    if yes_ask is not None and yes_ask > 0:
        candidates.append(yes_ask)
    if no_bid is not None and no_bid > 0:
        candidates.append(1.0 - no_bid)
    if yes_bid is not None and yes_bid > 0:
        candidates.append(yes_bid)
    if no_ask is not None and no_ask > 0:
        candidates.append(1.0 - no_ask)
    if not candidates:
        return None
    return statistics.fmean(candidates)


def collect_cohort(t_minus_minutes: float) -> list[dict]:
    """For every settled ticker in the observer DB, pair with the snapshot
    closest to (close_time - t_minus_minutes). Returns list of records."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    tickers = conn.execute(
        "SELECT DISTINCT ticker, MIN(close_time), kind, city "
        "FROM book_snapshot GROUP BY ticker"
    ).fetchall()
    now = time.time()

    cohort = []
    skipped = {"not_closed": 0, "not_finalized": 0, "no_book": 0, "fetch_fail": 0}

    for i, (ticker, close_iso, kind, city) in enumerate(tickers):
        close_epoch = _iso_to_epoch(close_iso) if close_iso else None
        if not close_epoch or close_epoch > now:
            skipped["not_closed"] += 1
            continue

        target_ts = close_epoch - t_minus_minutes * 60.0
        # pick closest snapshot to target_ts
        # Only pre-close snapshots (post-close books are uninformative).
        snap = conn.execute(
            "SELECT yes_bid, yes_ask, no_bid, no_ask, ts FROM book_snapshot "
            "WHERE ticker=? AND ts <= ? ORDER BY ABS(ts - ?) LIMIT 1",
            (ticker, close_epoch, target_ts),
        ).fetchone()
        if not snap:
            skipped["no_book"] += 1
            continue
        p_mkt = market_mid(snap[:4])
        if p_mkt is None:
            skipped["no_book"] += 1
            continue

        m = fetch_market(ticker)
        if not m:
            skipped["fetch_fail"] += 1
            continue
        if m.get("status") != "finalized" or m.get("result") not in ("yes", "no"):
            skipped["not_finalized"] += 1
            continue

        outcome = 1 if m["result"] == "yes" else 0
        actual_dt = snap[4] - target_ts  # signed seconds off-target

        cohort.append({
            "ticker": ticker,
            "city": city,
            "kind": kind,
            "close_epoch": close_epoch,
            "p_mkt": p_mkt,
            "outcome": outcome,
            "snap_ts": snap[4],
            "offset_sec": actual_dt,
        })

        # rate-limit
        if (i + 1) % 20 == 0:
            time.sleep(0.5)

    conn.close()
    print(f"  fetched {len(cohort)} settled+priced markets; skipped={skipped}",
          file=sys.stderr)
    return cohort


def _collect_earliest_cohort() -> list[dict]:
    """For each settled ticker, pair its FIRST snapshot price with outcome."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    tickers = conn.execute(
        "SELECT DISTINCT ticker, MIN(close_time), kind, city "
        "FROM book_snapshot GROUP BY ticker"
    ).fetchall()
    now = time.time()
    out = []
    for i, (ticker, close_iso, kind, city) in enumerate(tickers):
        close_epoch = _iso_to_epoch(close_iso) if close_iso else None
        if not close_epoch or close_epoch > now:
            continue
        snap = conn.execute(
            "SELECT yes_bid, yes_ask, no_bid, no_ask, ts FROM book_snapshot "
            "WHERE ticker=? AND ts <= ? ORDER BY ts ASC LIMIT 1",
            (ticker, close_epoch),
        ).fetchone()
        if not snap:
            continue
        p = market_mid(snap[:4])
        if p is None:
            continue
        m = fetch_market(ticker)
        if not m or m.get("status") != "finalized" or m.get("result") not in ("yes", "no"):
            continue
        out.append({
            "ticker": ticker, "p_mkt": p,
            "outcome": 1 if m["result"] == "yes" else 0,
            "lead_hours": (close_epoch - snap[4]) / 3600.0,
        })
        if (i + 1) % 20 == 0:
            time.sleep(0.5)
    conn.close()
    return out


def brier(records: list[dict]) -> float:
    return statistics.fmean((r["p_mkt"] - r["outcome"]) ** 2 for r in records)


def report_calibration(records: list[dict]) -> None:
    bins = [(i / 10, (i + 1) / 10) for i in range(10)]
    print()
    print(f"  {'bin':<12} {'n':>5} {'mean p':>8} {'actual':>8} {'gap':>6}")
    print(f"  {'-'*12} {'-'*5} {'-'*8} {'-'*8} {'-'*6}")
    for lo, hi in bins:
        bucket = [r for r in records if lo <= r["p_mkt"] < hi or (hi == 1.0 and r["p_mkt"] == 1.0)]
        if not bucket:
            continue
        mean_p = statistics.fmean(r["p_mkt"] for r in bucket)
        actual = statistics.fmean(r["outcome"] for r in bucket)
        gap = actual - mean_p
        print(f"  [{lo:.1f},{hi:.1f}) {len(bucket):>5} {mean_p:>8.3f} "
              f"{actual:>8.3f} {gap:>+6.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--t-minus", type=float, default=30.0,
                    help="minutes before close to read market price (default 30)")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"no observer DB at {DB_PATH}")
        return

    print("=" * 72)
    print(f"MARKET-IMPLIED CALIBRATION — price at T-{args.t_minus:.0f}min vs outcome")
    print("=" * 72)

    # Run a few T-minus values for the informativeness curve
    print()
    print(f"  {'T-minus':>10} {'n':>5} {'Brier':>8} {'baseline':>10} {'skill':>7}")
    print(f"  {'-'*10} {'-'*5} {'-'*8} {'-'*10} {'-'*7}")
    cohort_for_calib = None
    for tm in (480, 360, 180, 60, 30, 5):
        records = collect_cohort(tm)
        if not records:
            print(f"  T-{tm:>3}min     no data")
            continue
        # Restrict to records where the chosen snapshot was actually within
        # 30 min of target (avoid same-snapshot collapsing all T-minus values)
        records = [r for r in records if abs(r["offset_sec"]) <= 1800]
        if not records:
            print(f"  T-{tm:>3}min     no in-window snaps")
            continue
        b = brier(records)
        naive = statistics.fmean((0.5 - r["outcome"]) ** 2 for r in records)
        skill = 1 - b / naive if naive > 0 else 0
        n_uncertain = sum(1 for r in records if 0.05 < r["p_mkt"] < 0.95)
        print(f"  T-{tm:>3}min  {len(records):>5} {b:>8.4f} "
              f"{naive:>10.4f} {skill:>7.3f}   uncertain(.05-.95)={n_uncertain}")
        if tm == int(args.t_minus):
            cohort_for_calib = records

    # Also: "earliest observation" cohort — the first snapshot for each
    # ticker, however far from close that is. Captures uncertainty cases
    # where the observation window started while the question was still live.
    print()
    print("  earliest-observed cohort (uses first snapshot per ticker)")
    early = _collect_earliest_cohort()
    if early:
        b = brier(early)
        naive = statistics.fmean((0.5 - r["outcome"]) ** 2 for r in early)
        skill = 1 - b / naive if naive > 0 else 0
        n_uncertain = sum(1 for r in early if 0.05 < r["p_mkt"] < 0.95)
        print(f"  earliest    {len(early):>5} {b:>8.4f} "
              f"{naive:>10.4f} {skill:>7.3f}   uncertain(.05-.95)={n_uncertain}")
        # Focused subcohort: only uncertain-at-first markets
        unc = [r for r in early if 0.05 < r["p_mkt"] < 0.95]
        if unc:
            b = brier(unc)
            naive = statistics.fmean((0.5 - r["outcome"]) ** 2 for r in unc)
            skill = 1 - b / naive if naive > 0 else 0
            print(f"  uncertain   {len(unc):>5} {b:>8.4f} "
                  f"{naive:>10.4f} {skill:>7.3f}   "
                  f"(only first-snap in [.05,.95])")

    if cohort_for_calib is None:
        cohort_for_calib = collect_cohort(args.t_minus)

    if not cohort_for_calib:
        print("\n  no cohort for calibration buckets")
        return

    print()
    print("=" * 72)
    print(f"CALIBRATION BUCKETS @ T-{args.t_minus:.0f}min (n={len(cohort_for_calib)})")
    print("=" * 72)
    print("  gap > 0 → market underprices YES   gap < 0 → market overprices YES")
    report_calibration(cohort_for_calib)

    # Base rate
    print()
    yes_rate = statistics.fmean(r["outcome"] for r in cohort_for_calib)
    print(f"  base rate: {yes_rate:.1%} YES across {len(cohort_for_calib)} markets")
    print(f"  → 'always predict {yes_rate:.0%}' Brier = {yes_rate * (1 - yes_rate):.4f}")

    print()
    print("=" * 72)


if __name__ == "__main__":
    main()
