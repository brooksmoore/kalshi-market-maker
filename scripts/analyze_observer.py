"""
analyze_observer.py — distributional report over prod_observer.db.

Reads `data/prod_observer.db` (written by `scripts/prod_observer.py`) and
prints a human-readable report:

  1. Run / coverage summary
  2. Spread distribution overall and by ticker kind
  3. Depth distribution at top of book
  4. Time-of-day cut (hour-of-day × median spread, median top-of-book size)
  5. Spread autocorrelation across cycles (how much does spread move 5 min later?)
  6. Gate calibration table: at MAX_ENTRY_SPREAD = Xc, what % of markets pass?

This is a pure read tool. Safe to run while the observer is writing
(prod_observer.db uses WAL mode). Run anytime — output adapts to whatever
data you have.

Usage:
    venv/bin/python scripts/analyze_observer.py
    venv/bin/python scripts/analyze_observer.py --kind B    # B-tickers only
    venv/bin/python scripts/analyze_observer.py --hours 6   # last 6 hours only
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "prod_observer.db"


def _q(c: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    return c.execute(sql, args).fetchall()


def _pct(c: sqlite3.Connection, where: str, args: tuple) -> dict:
    """Return percentile summary of spread_no_c under `where`."""
    rows = _q(
        c, f"SELECT spread_no_c FROM book_snapshot WHERE {where} "
           f"AND spread_no_c IS NOT NULL ORDER BY spread_no_c", args,
    )
    if not rows:
        return {}
    vals = [r[0] for r in rows]
    n = len(vals)
    return {
        "n": n,
        "min": vals[0],
        "p10": vals[int(n * 0.10)],
        "p25": vals[int(n * 0.25)],
        "p50": vals[int(n * 0.50)],
        "p75": vals[int(n * 0.75)],
        "p90": vals[int(n * 0.90)],
        "p99": vals[int(min(n - 1, n * 0.99))],
        "max": vals[-1],
        "mean": statistics.fmean(vals),
    }


def _bar(width: int, max_width: int = 40) -> str:
    return "█" * min(width, max_width)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["B", "T"], help="filter to ticker kind")
    ap.add_argument("--hours", type=float, help="only last N hours of data")
    ap.add_argument("--city", help="filter to one city")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"no observer DB at {DB_PATH} — run scripts/prod_observer.py first")
        return

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    where_parts = []
    bind: list = []
    if args.kind:
        where_parts.append("kind = ?")
        bind.append(args.kind)
    if args.hours:
        cutoff = time.time() - args.hours * 3600
        where_parts.append("ts >= ?")
        bind.append(cutoff)
    if args.city:
        where_parts.append("city = ?")
        bind.append(args.city)
    where = " AND ".join(where_parts) if where_parts else "1=1"
    bind_t = tuple(bind)

    # ── 1. Run + coverage summary ────────────────────────────────────────────
    print("=" * 72)
    print("RUN SUMMARY")
    print("=" * 72)
    runs = _q(conn, "SELECT run_id, started_at, stopped_at, snapshots_total, "
                    "cycles_total, notes FROM observer_run ORDER BY run_id")
    for r in runs:
        rid, started, stopped, snaps, cycles, notes = r
        dur = (stopped or time.time()) - started
        status = "RUNNING" if stopped is None else "stopped"
        print(f"  run {rid}: {status}, {dur/3600:.1f}h, {snaps} snapshots, "
              f"{cycles} cycles, notes={notes}")

    total = _q(conn, f"SELECT COUNT(*) FROM book_snapshot WHERE {where}", bind_t)[0][0]
    distinct_t = _q(conn, f"SELECT COUNT(DISTINCT ticker) FROM book_snapshot WHERE {where}", bind_t)[0][0]
    distinct_c = _q(conn, f"SELECT COUNT(DISTINCT city) FROM book_snapshot WHERE {where}", bind_t)[0][0]
    if total == 0:
        print("\n  no snapshots match filter")
        return
    print(f"\n  filtered total: {total} snapshots, {distinct_t} unique tickers, "
          f"{distinct_c} cities")
    if args.kind or args.hours or args.city:
        filters = []
        if args.kind: filters.append(f"kind={args.kind}")
        if args.hours: filters.append(f"last {args.hours}h")
        if args.city: filters.append(f"city={args.city}")
        print(f"  filters: {', '.join(filters)}")

    # ── 2. Spread distribution overall + by kind ─────────────────────────────
    print()
    print("=" * 72)
    print("NO-SIDE SPREAD DISTRIBUTION (cents)")
    print("=" * 72)
    print()
    print(f"  {'group':<14} {'n':>6} {'p10':>5} {'p25':>5} {'p50':>5} "
          f"{'p75':>5} {'p90':>5} {'p99':>5} {'max':>5} {'mean':>6}")
    print(f"  {'-'*14} {'-'*6} {'-'*5} {'-'*5} {'-'*5} "
          f"{'-'*5} {'-'*5} {'-'*5} {'-'*5} {'-'*6}")
    for label, extra in [("all", ""), ("B (1°F bin)", " AND kind='B'"), ("T (threshold)", " AND kind='T'")]:
        s = _pct(conn, where + extra + " AND no_bid > 0 AND no_ask IS NOT NULL", bind_t)
        if s:
            print(f"  {label:<14} {s['n']:>6} {s['p10']:>5.1f} {s['p25']:>5.1f} "
                  f"{s['p50']:>5.1f} {s['p75']:>5.1f} {s['p90']:>5.1f} "
                  f"{s['p99']:>5.1f} {s['max']:>5.1f} {s['mean']:>6.2f}")
        else:
            print(f"  {label:<14}    -")

    # ── 3. Gate calibration table ────────────────────────────────────────────
    print()
    print("=" * 72)
    print("GATE CALIBRATION — at MAX_ENTRY_SPREAD = Xc, what passes?")
    print("=" * 72)
    print()
    # 2026-05-10: gate now applies uniformly across ticker kinds + both
    # actions (strategy.py MAX_ENTRY_SPREAD). spread_yes_c == spread_no_c
    # for any two-sided book (orderbook symmetry), so we count any snapshot
    # with at least one side fully quoted.
    total = _q(
        conn,
        f"SELECT COUNT(*) FROM book_snapshot WHERE {where} "
        f"AND no_bid > 0 AND no_ask IS NOT NULL",
        bind_t,
    )[0][0]
    if total > 0:
        print(f"  base: {total} two-sided snapshots (all kinds, both actions)")
        print()
        print(f"  {'gate':>5} {'pass %':>7} {'reject %':>9}  histogram")
        for gate_c in [1, 2, 3, 4, 5, 6, 8, 10, 15, 20]:
            passed = _q(
                conn,
                f"SELECT COUNT(*) FROM book_snapshot WHERE {where} "
                f"AND no_bid > 0 AND no_ask IS NOT NULL AND spread_no_c <= ?",
                bind_t + (gate_c,),
            )[0][0]
            pct_pass = passed / total * 100
            pct_rej = 100 - pct_pass
            print(f"  {gate_c:>4}c {pct_pass:>6.1f}% {pct_rej:>8.1f}%  {_bar(int(pct_pass / 2.5))}")
    else:
        print("  no two-sided snapshots yet")

    # ── 4. Depth at top of book ──────────────────────────────────────────────
    print()
    print("=" * 72)
    print("TOP-OF-BOOK DEPTH (contracts available at best price)")
    print("=" * 72)
    print()
    print(f"  {'side':<12} {'n':>6} {'p10':>6} {'p50':>7} {'p90':>8} {'p99':>9} {'max':>10}")
    print(f"  {'-'*12} {'-'*6} {'-'*6} {'-'*7} {'-'*8} {'-'*9} {'-'*10}")
    for label, col in [("yes_bid_size", "yes_bid_size"), ("no_bid_size", "no_bid_size")]:
        rows = _q(
            conn,
            f"SELECT {col} FROM book_snapshot WHERE {where} AND {col} IS NOT NULL "
            f"ORDER BY {col}",
            bind_t,
        )
        if not rows:
            print(f"  {label:<12}    -")
            continue
        vals = [r[0] for r in rows]
        n = len(vals)
        print(f"  {label:<12} {n:>6} {vals[int(n*0.10)]:>6.0f} "
              f"{vals[int(n*0.50)]:>7.0f} {vals[int(n*0.90)]:>8.0f} "
              f"{vals[int(min(n-1, n*0.99))]:>9.0f} {vals[-1]:>10.0f}")

    # ── 5. Time-of-day cut ───────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("TIME-OF-DAY CUT (hour UTC × median spread, B-tickers)")
    print("=" * 72)
    print()
    rows = _q(
        conn,
        f"SELECT CAST(strftime('%H', ts, 'unixepoch') AS INT) AS hr, "
        f"COUNT(*) AS n, AVG(spread_no_c), "
        f"AVG(no_bid_size), AVG(no_ask_size) "
        f"FROM book_snapshot WHERE {where} AND kind='B' "
        f"AND no_bid > 0 AND no_ask IS NOT NULL "
        f"GROUP BY hr ORDER BY hr",
        bind_t,
    )
    if rows:
        print(f"  {'hr UTC':>6} {'n':>6} {'avg_spread':>11} {'avg_no_bid_sz':>14} {'avg_no_ask_sz':>14}")
        for hr, n, avg_sp, avg_bs, avg_as in rows:
            sp = f"{avg_sp:.2f}c" if avg_sp is not None else "-"
            bs = f"{avg_bs:.0f}" if avg_bs is not None else "-"
            asz = f"{avg_as:.0f}" if avg_as is not None else "-"
            print(f"  {hr:>6} {n:>6} {sp:>11} {bs:>14} {asz:>14}")
    else:
        print("  insufficient data for hourly cut")

    # ── 6. Spread autocorrelation (cycle-to-cycle change on same ticker) ────
    print()
    print("=" * 72)
    print("SPREAD AUTOCORRELATION — how often does spread move 5 min later?")
    print("=" * 72)
    print()
    # Pair each (ticker, ts) with the next (ticker, ts2) where ts2 > ts and
    # ts2 - ts < 600 (within one cycle gap)
    rows = _q(
        conn,
        f"""
        WITH ordered AS (
            SELECT ticker, ts, spread_no_c,
                   LAG(spread_no_c) OVER (PARTITION BY ticker ORDER BY ts) AS prev_spread,
                   LAG(ts) OVER (PARTITION BY ticker ORDER BY ts) AS prev_ts
            FROM book_snapshot
            WHERE {where} AND kind='B' AND no_bid > 0 AND no_ask IS NOT NULL
        )
        SELECT
            COUNT(*) AS n,
            SUM(CASE WHEN spread_no_c = prev_spread THEN 1 ELSE 0 END) AS unchanged,
            SUM(CASE WHEN ABS(spread_no_c - prev_spread) <= 1 THEN 1 ELSE 0 END) AS within_1c,
            SUM(CASE WHEN ABS(spread_no_c - prev_spread) > 2 THEN 1 ELSE 0 END) AS moved_3c_plus
        FROM ordered
        WHERE prev_spread IS NOT NULL AND ts - prev_ts BETWEEN 250 AND 600
        """,
        bind_t,
    )
    if rows and rows[0][0]:
        n, unchanged, within1, moved3 = rows[0]
        print(f"  pairs: {n}")
        print(f"  spread unchanged 5 min later: {unchanged/n*100:.1f}%")
        print(f"  spread moved ≤1c: {within1/n*100:.1f}%")
        print(f"  spread moved >2c: {moved3/n*100:.1f}%")
        print()
        print("  interpretation: high 'unchanged' → autocorrelation is strong,")
        print("  effective independent samples per market per hour is low.")
    else:
        print("  need ≥2 cycles to compute autocorrelation")

    print()
    print("=" * 72)


if __name__ == "__main__":
    main()
