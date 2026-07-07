"""
flb_analyze.py — favorite-longshot return-by-price-band analyzer.

Joins flb_observer's price snapshots to realized settlements and reports, for
each entry-price band, the realized YES rate and the net-of-fee EV of both
sides (buy YES / sell YES). This is the go/no-go for the FLB strategy: if the
0-10c band shows negative buy-YES EV (longshots overpriced -> SELL edge) and/or
the 90-100c band shows positive buy-YES EV (favorites underpriced), net of
fees, in markets we can actually trade, the edge is real and deployable.

Method (mirrors the weather shadow audit that killed the forecast thesis):
  - For each settled market, take the snapshot closest to --lead hours before
    close (default 24h) as the "entry" price. Avoids the price collapse near
    settlement that would manufacture fake edge.
  - Bucket by entry yes-mid; report n, mean price, realized YES rate, gap,
    and EV per contract for buy-YES and sell-YES, net of the venue fee.
  - Kalshi fees shown at BOTH maker (0.25x) and taker rates; Polymarket = 0.

Usage:
    venv/bin/python scripts/flb_analyze.py [--lead 24] [--min-vol 20] [--venue both]

Runs clean with zero settlements (prints "no settled markets yet, collect
longer") so it is safe to run the moment the observer starts.
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "flb_observer.db"


def taker_fee(p: float) -> float:
    return 0.0 if p <= 0 or p >= 1 else 0.07 * p * (1 - p)


def maker_fee(p: float) -> float:
    return 0.25 * taker_fee(p)


def _entry_price_kalshi(conn, ticker, close_time, lead_hours):
    """yes-mid of the snapshot closest to `lead_hours` before close_time."""
    from datetime import datetime
    try:
        ct = datetime.fromisoformat(close_time.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None, None
    target = ct - lead_hours * 3600
    rows = conn.execute(
        "SELECT ts, yes_bid, yes_ask, volume_24h FROM market_snapshot "
        "WHERE ticker=? AND yes_bid>0 AND yes_ask>0 AND yes_ask<1", (ticker,)).fetchall()
    best = None
    for ts, yb, ya, vol in rows:
        d = abs(ts - target)
        if best is None or d < best[0]:
            best = (d, (yb + ya) / 2, vol or 0)
    if best is None:
        return None, None
    return best[1], best[2]


def _entry_price_pm(conn, market_id, end_date, lead_hours):
    from datetime import datetime
    try:
        ct = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None, None
    target = ct - lead_hours * 3600
    rows = conn.execute(
        "SELECT ts, yes_price, volume_24h FROM pm_snapshot "
        "WHERE market_id=? AND yes_price>0 AND yes_price<1", (market_id,)).fetchall()
    best = None
    for ts, p, vol in rows:
        d = abs(ts - target)
        if best is None or d < best[0]:
            best = (d, p, vol or 0)
    if best is None:
        return None, None
    return best[1], best[2]


def _report(label, pts, fee_fn):
    """pts: list of (entry_price, outcome 0/1, category). fee_fn(price)->fee."""
    if not pts:
        print(f"\n{label}: no settled markets with a pre-close snapshot yet.")
        return
    print(f"\n=== {label}  (n={len(pts)}) ===")
    print("band    n    mean_p  realYES   gap     EV_buyYES   EV_sellYES   (net fee)")
    buckets = defaultdict(list)
    for p, o, _cat in pts:
        buckets[min(int(p * 10), 9)].append((p, o))
    fav, longshot = [], []
    for b in range(10):
        arr = buckets[b]
        if not arr:
            continue
        n = len(arr)
        mp = st.mean(p for p, _ in arr)
        rr = st.mean(o for _, o in arr)
        ev_buy = st.mean(o - p - fee_fn(p) for p, o in arr)
        ev_sell = st.mean((1 - o) - (1 - p) - fee_fn(p) for p, o in arr)
        flag = ""
        if b == 0 and ev_sell > 0:
            flag = "  <- longshot SELL edge"
        if b == 9 and ev_buy > 0:
            flag = "  <- favorite BUY edge"
        print(f"{b*10:2d}-{b*10+10:<3d} {n:4d}  {mp:.3f}  {rr:.3f}  {rr-mp:+.3f}  "
              f"${ev_buy:+.4f}   ${ev_sell:+.4f}{flag}")
        if mp >= 0.80:
            fav.append((n, ev_buy))
        if mp < 0.20:
            longshot.append((n, ev_sell))

    def wmean(pairs):
        N = sum(n for n, _ in pairs)
        return (sum(n * e for n, e in pairs) / N, N) if N else (0.0, 0)
    fe, fn = wmean(fav)
    le, ln = wmean(longshot)
    print(f"  FAVORITES (p>=0.80): buy-YES EV=${fe:+.4f}/contract (n={fn})")
    print(f"  LONGSHOTS (p<0.20):  sell-YES EV=${le:+.4f}/contract (n={ln})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lead", type=float, default=24.0, help="hours before close to sample entry price")
    ap.add_argument("--min-vol", type=float, default=20.0, help="min 24h volume at entry")
    ap.add_argument("--venue", choices=["kalshi", "pm", "both"], default="both")
    args = ap.parse_args()

    if not DB.exists():
        print(f"no db yet at {DB} — start scripts/flb_observer.py first.")
        return
    conn = sqlite3.connect(DB)

    n_set = conn.execute("SELECT COUNT(*) FROM settlement").fetchone()[0]
    n_pm = conn.execute("SELECT COUNT(*) FROM pm_settlement").fetchone()[0]
    n_snap = conn.execute("SELECT COUNT(*) FROM market_snapshot").fetchone()[0]
    print(f"db: {n_snap} kalshi snapshots, {n_set} kalshi settlements, {n_pm} pm settlements")
    print(f"entry = snapshot closest to {args.lead:.0f}h before close; min 24h vol={args.min_vol:.0f}")

    if args.venue in ("kalshi", "both"):
        pts, by_cat = [], defaultdict(list)
        # Crypto excluded: efficient near-martingale micro-markets, not the FLB
        # cohort (see flb_observer EXCLUDE_CATEGORIES). Old crypto rows may exist
        # in the DB from before the exclusion; drop them from the headline numbers.
        for ticker, result, cat in conn.execute(
                "SELECT ticker, result, category FROM settlement "
                "WHERE category IS NULL OR category != 'Crypto'"):
            p, vol = _entry_price_kalshi(conn, ticker,
                                         conn.execute("SELECT close_time FROM settlement WHERE ticker=?",
                                                      (ticker,)).fetchone()[0], args.lead)
            if p is None or vol < args.min_vol:
                continue
            o = 1.0 if result == "yes" else 0.0
            pts.append((p, o, cat))
            by_cat[cat].append((p, o, cat))
        _report("KALSHI — maker fee (0.25x taker)", pts, maker_fee)
        _report("KALSHI — taker fee", pts, taker_fee)
        # per-category favorite/longshot snapshot (maker fee)
        if pts:
            print("\n--- KALSHI by category (maker fee, p<0.20 sell / p>=0.80 buy) ---")
            for cat, arr in sorted(by_cat.items(), key=lambda x: -len(x[1])):
                ls = [(1 - o) - (1 - p) - maker_fee(p) for p, o, _ in arr if p < 0.20]
                fv = [o - p - maker_fee(p) for p, o, _ in arr if p >= 0.80]
                s_ls = f"sell_LS=${st.mean(ls):+.4f}(n={len(ls)})" if ls else "sell_LS=n/a"
                s_fv = f"buy_FAV=${st.mean(fv):+.4f}(n={len(fv)})" if fv else "buy_FAV=n/a"
                print(f"  {cat:22.22} total_n={len(arr):4d}  {s_ls}  {s_fv}")

    if args.venue in ("pm", "both"):
        pts = []
        for mid, result, end_date in conn.execute(
                "SELECT market_id, result, end_date FROM pm_settlement"):
            p, vol = _entry_price_pm(conn, mid, end_date, args.lead)
            if p is None or vol < args.min_vol:
                continue
            o = 1.0 if result == "yes" else 0.0
            pts.append((p, o, "pm"))
        _report("POLYMARKET — zero fee", pts, lambda p: 0.0)

    conn.close()
    if n_set + n_pm == 0:
        print("\nNo settled markets yet — collect for a few days, then re-run. "
              "First settlements arrive as tracked markets close.")


if __name__ == "__main__":
    main()
