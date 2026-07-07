"""
score_paper_trades.py — match logged paper trades to real Kalshi settlements
and report per-trade and aggregate performance.

Reads data/paper_trades.db (written by src/paper_storage.py whenever the
bot is running with LIVE_TRADING_ENABLED=false). Fetches finalized status
from Kalshi's public /markets/{ticker} endpoint, computes per-trade P&L,
writes settlement rows to paper_result, then aggregates.

DEDUPE RULE:
  The bot logs a new paper_trade row each cycle a market passes all gates
  (no cross-cycle held-set dedup for paper trades — intentional). For
  scoring purposes, we use the FIRST paper_trade per (ticker, venue),
  matching what the bot would have done in production where held-position
  dedup IS active.

P&L MODEL:
  Per-contract: +(1 - entry_price) if win, −entry_price if lose.
  Assumes fill at observed entry_price (the ask we picked). Real fills
  include adverse selection we can't simulate; this is an upper bound.

Usage:
    venv/bin/python scripts/score_paper_trades.py
    venv/bin/python scripts/score_paper_trades.py --since-hours 48
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics as stats
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config import kalshi_trade_fee, MAKER_PRICE_OFFSET_CENTS  # noqa: E402
from paper_storage import log_paper_result  # noqa: E402

DB_PATH = ROOT / "data" / "paper_trades.db"
MAIN_DB_PATH = ROOT / "data" / "trades.db"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def load_settled_from_main_db() -> list[dict]:
    """Return first-per-ticker settled paper trades from trades.db (canonical).

    This is the primary loader the loop uses. Normalizes to the same dict
    shape as load_settled_trades() so callers are interchangeable.

    Fields: id, ticker, venue, city, action, cal_p, entry_price, contracts,
    net_edge, cycle_ts, outcome, pnl_per_contract (GROSS), pnl_total (GROSS),
    settled_at, net_pnl_per_contract (net of Kalshi taker fee).
    """
    if not MAIN_DB_PATH.exists():
        return []
    conn = sqlite3.connect(f"file:{MAIN_DB_PATH}?mode=ro", uri=True)
    rows = conn.execute("""
        SELECT t.id, t.ticker, t.venue, t.city, t.action, t.calibrated_p,
               t.entry_price, t.contracts, t.edge_at_entry, t.opened_at,
               r.outcome, r.profit_loss, r.resolved_at
        FROM trades t
        JOIN results r ON r.trade_id = t.id
        WHERE t.paper_trade = 1
          AND t.id = (
              SELECT MIN(id) FROM trades
              WHERE ticker = t.ticker AND venue = t.venue AND paper_trade = 1
          )
        ORDER BY r.resolved_at ASC
    """).fetchall()
    conn.close()
    trades = []
    for row in rows:
        (tid, ticker, venue, city, action, cal_p,
         ep, contracts, net_edge, opened_at,
         outcome, profit_loss, resolved_at) = row
        contracts = max(1, int(contracts or 1))
        pnl_total = float(profit_loss or 0.0)
        pnl_per = pnl_total / contracts
        ep = float(ep or 0.0)
        fee = kalshi_trade_fee(1, ep) if 0 < ep < 1 else 0.0
        trades.append({
            "id": tid,
            "ticker": ticker,
            "venue": venue or "kalshi",
            "city": city,
            "action": action,
            "cal_p": float(cal_p) if cal_p is not None else None,
            "entry_price": ep,
            "contracts": contracts,
            "net_edge": float(net_edge or 0.0),
            "cycle_ts": opened_at,
            "outcome": outcome,
            "pnl_per_contract": pnl_per,
            "pnl_total": pnl_total,
            "settled_at": resolved_at,
            "net_pnl_per_contract": pnl_per - fee,
        })
    return trades


def score_new_settlements() -> tuple[int, int]:
    """Find and score unresolved past-close paper trades in trades.db.

    Fetches the Kalshi API for each unscored trade whose target_settlement
    has passed. Writes a result row for any that are now finalized.
    Returns (n_scored, n_not_finalized).

    Called by loop_prescan at the start of each cycle so scoring is
    automatic and the holdout count is always fresh.
    """
    if not MAIN_DB_PATH.exists():
        return 0, 0
    conn = sqlite3.connect(f"file:{MAIN_DB_PATH}?mode=ro", uri=True)
    now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    unscored = conn.execute("""
        SELECT t.id, t.ticker, t.venue, t.action, t.entry_price, t.contracts
        FROM trades t
        WHERE t.paper_trade = 1
          AND t.target_settlement < ?
          AND NOT EXISTS (SELECT 1 FROM results r WHERE r.trade_id = t.id)
    """, (now_iso,)).fetchall()
    conn.close()

    if not unscored:
        return 0, 0

    sess = requests.Session()
    n_scored = 0
    n_not_finalized = 0
    rw_conn = sqlite3.connect(str(MAIN_DB_PATH), timeout=10)
    try:
        for (tid, ticker, venue, action, ep, contracts) in unscored:
            outcome = fetch_outcome(sess, ticker)
            if not outcome:
                n_not_finalized += 1
                time.sleep(0.04)
                continue
            ep = float(ep or 0.0)
            contracts = max(1, int(contracts or 1))
            won = (
                (action == "BUY NO" and outcome == "no")
                or (action == "BUY YES" and outcome == "yes")
            )
            exit_price = 1.0 if won else 0.0
            pnl = ((1.0 - ep) if won else (-ep)) * contracts
            resolved_at = datetime.now(timezone.utc).strftime(
                '%Y-%m-%dT%H:%M:%S.%f') + 'Z'
            rw_conn.execute(
                """INSERT INTO results
                   (trade_id, outcome, exit_price, profit_loss, resolved_at, venue)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (tid, outcome, exit_price, round(pnl, 4), resolved_at,
                 venue or "kalshi"),
            )
            rw_conn.commit()
            n_scored += 1
            time.sleep(0.04)
    finally:
        rw_conn.close()
    return n_scored, n_not_finalized


def load_settled_trades(db_path: Path | None = None) -> list[dict]:
    """Return all settled paper trades with net-of-cost EV, ordered by settled_at.

    Used by loop_checker.run_gate for walk-forward evaluation. Applies the same
    first-per-ticker dedup as the main scoring loop so callers get a clean view.

    Each returned dict has:
      ticker, venue, city, action, cal_p, entry_price, contracts, net_edge,
      cycle_ts, outcome, pnl_per_contract (GROSS), net_pnl_per_contract (net of
      Kalshi taker fee), pnl_total (GROSS), settled_at.
    """
    if db_path is None:
        db_path = DB_PATH
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute("""
        SELECT pt.id, pt.ticker, pt.venue, pt.city, pt.action, pt.cal_p,
               pt.entry_price, pt.contracts, pt.net_edge, pt.cycle_ts,
               pr.outcome, pr.pnl_per_contract, pr.pnl_total, pr.settled_at
        FROM paper_trade pt
        JOIN paper_result pr ON pr.paper_trade_id = pt.id
        WHERE pt.id = (
            SELECT MIN(id) FROM paper_trade
            WHERE ticker = pt.ticker AND venue = pt.venue
        )
        ORDER BY pr.settled_at ASC
    """).fetchall()
    conn.close()
    cols = ("id", "ticker", "venue", "city", "action", "cal_p",
            "entry_price", "contracts", "net_edge", "cycle_ts",
            "outcome", "pnl_per_contract", "pnl_total", "settled_at")
    trades = []
    for r in rows:
        t = dict(zip(cols, r))
        ep = float(t["entry_price"] or 0.0)
        fee = kalshi_trade_fee(1, ep) if 0 < ep < 1 else 0.0
        t["net_pnl_per_contract"] = float(t["pnl_per_contract"] or 0.0) - fee
        trades.append(t)
    return trades


def hdr(s):
    print()
    print("=" * 78)
    print(s)
    print("=" * 78)


def fetch_outcome(sess: requests.Session, ticker: str) -> str | None:
    """Return 'yes' or 'no' if finalized, None otherwise. NEVER raises."""
    try:
        r = sess.get(
            f"{KALSHI_BASE}/markets/{ticker}", timeout=(5, 10)
        ).json().get("market", {})
        if r.get("status") == "finalized" and r.get("result") in ("yes", "no"):
            return r["result"]
    except Exception:
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--since-hours", type=float, default=None,
        help="Only score paper trades logged in the last N hours",
    )
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"no paper_trades.db yet at {DB_PATH}")
        print("(bot needs to have run with LIVE_TRADING_ENABLED=false against prod first)")
        return

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    where_clause = ""
    bind: tuple = ()
    if args.since_hours:
        cutoff = time.time() - args.since_hours * 3600
        where_clause = "WHERE cycle_ts >= ?"
        bind = (cutoff,)

    total_rows = conn.execute(
        f"SELECT COUNT(*) FROM paper_trade {where_clause}", bind
    ).fetchone()[0]
    distinct_tickers = conn.execute(
        f"SELECT COUNT(DISTINCT ticker) FROM paper_trade {where_clause}", bind
    ).fetchone()[0]
    cycle_ts_min, cycle_ts_max = conn.execute(
        f"SELECT MIN(cycle_ts), MAX(cycle_ts) FROM paper_trade {where_clause}", bind
    ).fetchone()
    print(f"paper_trade rows: {total_rows}  unique tickers: {distinct_tickers}")
    if cycle_ts_min:
        print(f"span: {datetime.fromtimestamp(cycle_ts_min, tz=timezone.utc).isoformat()}"
              f"  →  {datetime.fromtimestamp(cycle_ts_max, tz=timezone.utc).isoformat()}")
        print(f"duration: {(cycle_ts_max - cycle_ts_min)/3600:.1f}h")

    # Take FIRST paper_trade per (ticker, venue) — matches what the bot would
    # actually have done with held-position dedup active. Multiple per-ticker
    # rows happen when the bot re-evaluates the same market across cycles.
    rows = conn.execute(f"""
        SELECT id, ticker, venue, city, action, cal_p, entry_price,
               contracts, recommended_size_usd, close_time, cycle_ts
        FROM paper_trade pt
        WHERE pt.id = (
            SELECT MIN(id) FROM paper_trade
            WHERE ticker = pt.ticker AND venue = pt.venue
        )
        {('AND cycle_ts >= ?' if args.since_hours else '')}
        ORDER BY pt.cycle_ts ASC
    """, bind).fetchall()
    cols = ("id", "ticker", "venue", "city", "action", "cal_p",
            "entry_price", "contracts", "recommended_size", "close_time",
            "cycle_ts")
    deduped = [dict(zip(cols, r)) for r in rows]
    print(f"deduped trades (first per ticker): {len(deduped)}")

    # Filter to past-close trades worth checking for finalization.
    now = time.time()
    past_close = []
    open_yet = []
    for t in deduped:
        if not t["close_time"]:
            continue
        try:
            ct_epoch = datetime.fromisoformat(
                t["close_time"].replace("Z", "+00:00")
            ).timestamp()
            if ct_epoch < now:
                past_close.append(t)
            else:
                open_yet.append(t)
        except Exception:
            pass

    print(f"past close (potentially scoreable): {len(past_close)}")
    print(f"still open (paper-pending):        {len(open_yet)}")

    # Identify already-scored vs new
    existing_results = set(
        r[0] for r in conn.execute(
            "SELECT paper_trade_id FROM paper_result"
        ).fetchall()
    )
    new_to_score = [t for t in past_close if t["id"] not in existing_results]
    already = [t for t in past_close if t["id"] in existing_results]
    print(f"already scored: {len(already)}, new to score: {len(new_to_score)}")
    conn.close()

    # Score new ones — calls Kalshi API
    if new_to_score:
        print()
        print(f"fetching outcomes for {len(new_to_score)} trades...")
        sess = requests.Session()
        added = 0
        not_finalized = 0
        for t in new_to_score:
            outcome = fetch_outcome(sess, t["ticker"])
            if outcome:
                # If contracts wasn't populated at log-time (paper trades
                # never go through executor where contracts is computed),
                # derive it the same way executor does:
                #   max(1, int(recommended_size / entry_price))
                contracts = t["contracts"]
                if not contracts:
                    entry = float(t["entry_price"] or 0.0)
                    size = float(t["recommended_size"] or 0.0)
                    contracts = max(1, int(size / entry)) if entry > 0 else 1
                ok = log_paper_result(
                    t["id"], outcome,
                    t["entry_price"] or 0.5,
                    contracts,
                )
                if ok:
                    added += 1
            else:
                not_finalized += 1
            time.sleep(0.04)
        print(f"  scored: {added}  not-yet-finalized: {not_finalized}")

    # ─── Aggregate report ──────────────────────────────────────────────
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    results = conn.execute("""
        SELECT pt.ticker, pt.venue, pt.city, pt.action, pt.cal_p,
               pt.entry_price, pt.contracts, pt.cycle_ts,
               pr.outcome, pr.pnl_per_contract, pr.pnl_total
        FROM paper_trade pt
        JOIN paper_result pr ON pr.paper_trade_id = pt.id
        WHERE pt.id = (
            SELECT MIN(id) FROM paper_trade
            WHERE ticker = pt.ticker AND venue = pt.venue
        )
    """).fetchall()
    cols = ("ticker", "venue", "city", "action", "cal_p", "entry_price",
            "contracts", "cycle_ts", "outcome", "pnl_per_contract", "pnl_total")
    settled = [dict(zip(cols, r)) for r in results]

    hdr("RESULTS")
    if not settled:
        print("  no settled paper trades yet — give it more time")
        return

    n = len(settled)
    wins = sum(1 for r in settled
               if (r["action"] == "BUY YES" and r["outcome"] == "yes")
               or (r["action"] == "BUY NO" and r["outcome"] == "no"))
    avg_ev = stats.fmean(r["pnl_per_contract"] for r in settled)
    total_pnl = sum(r["pnl_total"] for r in settled)
    print(f"  settled paper trades: {n}")
    print(f"  win rate: {wins}/{n} = {wins/n*100:.0f}%")
    print(f"  EV per contract: {avg_ev:+.4f}")
    print(f"  total P&L (modeled fills at observed ask): {total_pnl:+.2f}")

    # Net-of-cost reporting (Kalshi taker fee deducted).
    # This is the number the walk-forward gate uses — gross EV is an upper bound.
    fee_per = [kalshi_trade_fee(1, r["entry_price"] or 0.5) for r in settled]
    net_evs = [r["pnl_per_contract"] - f for r, f in zip(settled, fee_per)]
    net_avg_ev = stats.fmean(net_evs)
    net_total = sum(nev * (r["contracts"] or 1) for nev, r in zip(net_evs, settled))
    print(f"  Net-of-cost EV/contract (taker fee deducted): {net_avg_ev:+.4f}")
    print(f"  Net-of-cost total P&L: {net_total:+.2f}")
    # Maker hypothesis: what if we'd posted 1¢ inside at zero maker fee?
    # Context only — paper fills are taker; this shows the ceiling if maker-biased.
    maker_offset = MAKER_PRICE_OFFSET_CENTS / 100
    maker_net_evs = []
    for r in settled:
        ep = max(0.01, (r["entry_price"] or 0.5) - maker_offset)
        won = ((r["action"] == "BUY YES" and r["outcome"] == "yes")
               or (r["action"] == "BUY NO" and r["outcome"] == "no"))
        maker_net_evs.append((1.0 - ep) if won else (-ep))
    print(f"  Maker-hypothetical EV/contract (zero fee, −1¢ entry): "
          f"{stats.fmean(maker_net_evs):+.4f}")

    # By direction
    print()
    print("  by direction:")
    for action in ("BUY YES", "BUY NO"):
        group = [r for r in settled if r["action"] == action]
        if not group:
            continue
        gw = sum(1 for r in group
                 if (r["action"] == "BUY YES" and r["outcome"] == "yes")
                 or (r["action"] == "BUY NO" and r["outcome"] == "no"))
        gpnl = sum(r["pnl_total"] for r in group)
        gev = stats.fmean(r["pnl_per_contract"] for r in group)
        gnet_ev = stats.fmean(
            r["pnl_per_contract"] - kalshi_trade_fee(1, r["entry_price"] or 0.5)
            for r in group
        )
        print(f"    {action}: n={len(group)}  wins={gw} ({gw/len(group)*100:.0f}%)  "
              f"EV/contract={gev:+.4f}  net-EV={gnet_ev:+.4f}  total={gpnl:+.2f}")

    # Per-city (n>=3)
    print()
    print("  per-city (n>=3):")
    by_city = defaultdict(list)
    for r in settled:
        by_city[r["city"] or "unknown"].append(r)
    if any(len(g) >= 3 for g in by_city.values()):
        print(f"    {'city':<14} {'n':>3} {'wins':>5} {'rate':>5} {'EV/ctr':>8} {'total':>7}")
        for city, group in sorted(by_city.items(), key=lambda x: -len(x[1])):
            if len(group) < 3:
                continue
            gw = sum(1 for r in group
                     if (r["action"] == "BUY YES" and r["outcome"] == "yes")
                     or (r["action"] == "BUY NO" and r["outcome"] == "no"))
            gpnl = sum(r["pnl_total"] for r in group)
            gev = stats.fmean(r["pnl_per_contract"] for r in group)
            print(f"    {city:<14} {len(group):>3} {gw:>5} {gw/len(group)*100:>4.0f}% "
                  f"{gev:>+8.4f} {gpnl:>+7.2f}")
    else:
        print("    (n<3 per city — wait for more settlements)")

    # Per cal_p band
    print()
    print("  by cal_p band:")
    bands = [(0.0, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.25),
             (0.25, 0.30), (0.30, 0.50), (0.50, 0.61)]
    print(f"    {'cal_p':<14} {'n':>3} {'wins':>5} {'rate':>5} {'EV/ctr':>8}")
    for lo, hi in bands:
        group = [r for r in settled
                 if r["cal_p"] is not None and lo <= r["cal_p"] < hi]
        if not group:
            continue
        gw = sum(1 for r in group
                 if (r["action"] == "BUY YES" and r["outcome"] == "yes")
                 or (r["action"] == "BUY NO" and r["outcome"] == "no"))
        gev = stats.fmean(r["pnl_per_contract"] for r in group)
        print(f"    [{lo:.2f}, {hi:.2f}) {len(group):>3} {gw:>5} "
              f"{gw/len(group)*100:>4.0f}% {gev:>+8.4f}")

    print()
    print("=" * 78)


if __name__ == "__main__":
    main()
