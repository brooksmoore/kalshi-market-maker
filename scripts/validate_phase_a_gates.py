"""
validate_phase_a_gates.py — Phase 0 of the 5/23 implementation plan.

Retroactively apply the proposed Phase A trading gates to the existing
shadow cohort and report what behavior the bot would have produced.

CURRENT gates (post 5/17, what bot has today):
  - MAX_ENTRY_SPREAD = 3c (cents)
  - MIN_EDGE = 0.08 (net of fees)  ← symmetric across BUY YES / BUY NO
  - SPREAD_INFLATION_FACTOR = 1.55 (already in shadow calibration)
  - CLI_BIAS = 0.0 (already in shadow forecasts)

PROPOSED Phase A gates:
  - MAX_ENTRY_SPREAD = 3c (unchanged)
  - MIN_EDGE_BUY_NO  = 0.08 (unchanged)
  - MIN_EDGE_BUY_YES = 0.30 (NEW — much stricter)
  - HARD_CAP_CAL_P_BUY_YES = 0.60 (NEW — refuse confident-YES entirely)

For each settled signal in the n≥1000 cohort:
  1. Reconstruct the bot's edge calc (cal_p − yes_ask, (1−cal_p) − no_ask)
  2. Pick the action (higher-edge side) — same logic find_opportunities uses
  3. Apply CURRENT gates: which signals pass?
  4. Apply PROPOSED gates: which signals pass?
  5. For passing signals, compute realized win/loss (we know outcome)
  6. Compare projected trade volume + win rate in each gate regime

OUTPUT: a clear pass/fail signal for whether Phase A should ship.

This is read-only. Does not modify any DB or config.
"""

from __future__ import annotations

import re
import sqlite3
import statistics as stats
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
SHADOW_DB = ROOT / "data" / "prod_observer.db"

# Current production-config values.
MAX_ENTRY_SPREAD_C = 3.0  # cents
MIN_EDGE_CURRENT = 0.08
FEE_APPROX = 0.01  # ~1c per contract, conservative

# Proposed Phase A values.
MIN_EDGE_BUY_NO_NEW = 0.08
MIN_EDGE_BUY_YES_NEW = 0.30
HARD_CAP_CAL_P_BUY_YES = 0.60


def hdr(s):
    print()
    print("=" * 80)
    print(s)
    print("=" * 80)


def compute_action_and_edges(cal_p, yes_ask, no_ask):
    """Replicate find_opportunities logic for which side + claimed edge.
    Returns (action, claimed_edge) where action is 'BUY_YES' or 'BUY_NO'
    and edge is net-of-fee. Returns None if neither side has a valid book."""
    if yes_ask is None or no_ask is None:
        return None, None
    if yes_ask <= 0 or yes_ask >= 1 or no_ask <= 0 or no_ask >= 1:
        return None, None
    edge_yes = cal_p - yes_ask - FEE_APPROX
    edge_no = (1.0 - cal_p) - no_ask - FEE_APPROX
    if edge_yes >= edge_no:
        return "BUY_YES", edge_yes
    else:
        return "BUY_NO", edge_no


def passes_current_gate(action, edge, spread_c):
    """Today's bot would enter if:
      - spread_c <= MAX_ENTRY_SPREAD_C
      - edge >= MIN_EDGE_CURRENT (symmetric)
    """
    if spread_c is not None and spread_c > MAX_ENTRY_SPREAD_C:
        return False
    return edge >= MIN_EDGE_CURRENT


def passes_phase_a_gate(action, edge, cal_p, spread_c):
    """Proposed Phase A gates:
      - spread_c <= MAX_ENTRY_SPREAD_C  (unchanged)
      - BUY_YES: cal_p <= HARD_CAP AND edge >= MIN_EDGE_BUY_YES_NEW
      - BUY_NO:  edge >= MIN_EDGE_BUY_NO_NEW
    """
    if spread_c is not None and spread_c > MAX_ENTRY_SPREAD_C:
        return False
    if action == "BUY_YES":
        if cal_p > HARD_CAP_CAL_P_BUY_YES:
            return False
        return edge >= MIN_EDGE_BUY_YES_NEW
    else:  # BUY_NO
        return edge >= MIN_EDGE_BUY_NO_NEW


def realized_win(action, outcome):
    """Did the trade actually win? BUY YES wins on outcome=1, BUY NO wins on outcome=0."""
    if action == "BUY_YES":
        return outcome == 1
    else:
        return outcome == 0


def main():
    if not SHADOW_DB.exists():
        print(f"missing {SHADOW_DB}")
        return

    c = sqlite3.connect(f"file:{SHADOW_DB}?mode=ro", uri=True)
    now = time.time()

    print("Fetching paired settled signals + book data...")

    # Get all settled signals joined with their book at observation time.
    # Use the FIRST observation per ticker (matches earlier scoring methodology).
    rows = c.execute("""
        SELECT DISTINCT s.ticker,
               (SELECT MIN(close_time) FROM book_snapshot b WHERE b.ticker=s.ticker)
        FROM shadow_signal s
        WHERE s.calibrated_p IS NOT NULL AND s.prod_yes_mid IS NOT NULL
    """).fetchall()
    closed = [
        tk for tk, ct in rows
        if ct and datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp() < now
    ]

    sess = requests.Session()
    paired = []
    span_hours = 0

    for tk in closed:
        try:
            r = sess.get(
                f"https://api.elections.kalshi.com/trade-api/v2/markets/{tk}",
                timeout=(5, 10),
            ).json().get("market", {})
            if r.get("status") != "finalized" or r.get("result") not in ("yes", "no"):
                continue
            outcome = 1 if r["result"] == "yes" else 0
            # Join shadow first-signal with concurrent book snapshot.
            sig = c.execute("""
                SELECT s.calibrated_p, s.prod_yes_mid, s.ts, s.book_ts, s.city,
                       b.yes_ask, b.no_ask, b.spread_no_c
                FROM shadow_signal s
                LEFT JOIN book_snapshot b
                  ON b.ticker = s.ticker AND b.ts = s.book_ts
                WHERE s.ticker = ?
                  AND s.calibrated_p IS NOT NULL
                  AND s.prod_yes_mid IS NOT NULL
                ORDER BY s.ts ASC
                LIMIT 1
            """, (tk,)).fetchone()
            if not sig:
                continue
            cp, mid, ts, bts, city, ya, na, sp = sig
            paired.append({
                "ticker": tk, "cal_p": cp, "yes_mid": mid, "ts": ts,
                "yes_ask": ya, "no_ask": na, "spread_c": sp,
                "city": city, "outcome": outcome,
            })
            time.sleep(0.02)
        except Exception:
            pass

    n = len(paired)
    if not n:
        print("no paired data; abort")
        return

    ts_min = min(r["ts"] for r in paired)
    ts_max = max(r["ts"] for r in paired)
    span_hours = (ts_max - ts_min) / 3600

    print(f"paired settled signals: {n}")
    print(f"collection span: {span_hours:.1f}h ({span_hours/24:.1f} days)")

    # For each signal, compute action + claimed edge (need yes_ask/no_ask)
    decided = 0
    no_book = 0
    decisions = []
    for r in paired:
        action, edge = compute_action_and_edges(r["cal_p"], r["yes_ask"], r["no_ask"])
        if action is None:
            no_book += 1
            continue
        decided += 1
        decisions.append({**r, "action": action, "edge": edge})

    print(f"decisions with book at first-observation: {decided}/{n}  (no-book: {no_book})")
    print()

    # ───────────────────────────────────────────────────────────────────
    # COMPARE GATE REGIMES
    # ───────────────────────────────────────────────────────────────────
    def evaluate(label, gate_fn):
        passed = [d for d in decisions if gate_fn(d)]
        buy_yes = [d for d in passed if d["action"] == "BUY_YES"]
        buy_no = [d for d in passed if d["action"] == "BUY_NO"]

        def stat_block(group, name):
            if not group:
                return f"  {name}: 0 trades"
            wins = sum(1 for d in group if realized_win(d["action"], d["outcome"]))
            win_rate = wins / len(group) * 100
            mean_edge = stats.fmean(d["edge"] for d in group)
            mean_cal_p = stats.fmean(d["cal_p"] for d in group)
            return (f"  {name}: {len(group)} trades  "
                    f"wins={wins} ({win_rate:.0f}%)  "
                    f"avg_claimed_edge={mean_edge:.3f}  "
                    f"avg_cal_p={mean_cal_p:.2f}")

        hdr(label)
        print(f"  TOTAL passed gate: {len(passed)} / {len(decisions)} "
              f"({len(passed)/len(decisions)*100:.0f}%)")
        print(stat_block(passed, "  ALL"))
        print(stat_block(buy_yes, "  BUY_YES"))
        print(stat_block(buy_no, "  BUY_NO"))
        # Daily volume estimate
        if span_hours > 0:
            tpd = len(passed) / span_hours * 24
            print(f"  projected: {tpd:.1f} trades/day  "
                  f"(vs 10/day cap → cap binds: {'YES' if tpd >= 10 else 'no'})")
        return passed, buy_yes, buy_no

    cur_passed, cur_yes, cur_no = evaluate(
        "REGIME 1 — CURRENT GATES (MIN_EDGE=0.08 symmetric, no cal_p cap)",
        lambda d: passes_current_gate(d["action"], d["edge"], d["spread_c"]),
    )
    new_passed, new_yes, new_no = evaluate(
        "REGIME 2 — PROPOSED PHASE A (MIN_EDGE_NO=0.08, MIN_EDGE_YES=0.30, cap cal_p<=0.6)",
        lambda d: passes_phase_a_gate(d["action"], d["edge"], d["cal_p"], d["spread_c"]),
    )

    # ───────────────────────────────────────────────────────────────────
    # DELTA: what did Phase A change?
    # ───────────────────────────────────────────────────────────────────
    hdr("REGIME DELTA — what Phase A specifically changes")
    cur_set = {d["ticker"] for d in cur_passed}
    new_set = {d["ticker"] for d in new_passed}
    filtered_out = cur_set - new_set
    still_in = cur_set & new_set
    print(f"  signals filtered OUT by Phase A: {len(filtered_out)}")
    print(f"  signals still allowed:           {len(still_in)}")

    filtered_decisions = [d for d in cur_passed if d["ticker"] in filtered_out]
    if filtered_decisions:
        fy = [d for d in filtered_decisions if d["action"] == "BUY_YES"]
        fn_ = [d for d in filtered_decisions if d["action"] == "BUY_NO"]
        wins_y = sum(1 for d in fy if realized_win(d["action"], d["outcome"]))
        wins_n = sum(1 for d in fn_ if realized_win(d["action"], d["outcome"]))
        print(f"    of those filtered: BUY_YES n={len(fy)} actual wins={wins_y} ({wins_y/len(fy)*100:.0f}%)" if fy else "    no BUY_YES filtered")
        print(f"                       BUY_NO  n={len(fn_)} actual wins={wins_n} ({wins_n/len(fn_)*100:.0f}%)" if fn_ else "    no BUY_NO filtered")

    # ───────────────────────────────────────────────────────────────────
    # PER-CITY check on the new regime
    # ───────────────────────────────────────────────────────────────────
    hdr("PER-CITY breakdown of Phase A-allowed trades")
    print(f"  (cities with ≥3 Phase A trades only)")
    print()
    by_city = defaultdict(list)
    for d in new_passed:
        by_city[d["city"]].append(d)
    print(f"  {'city':<14} {'n':>4} {'wins':>5} {'rate':>6} {'avg_edge':>9}")
    print(f"  {'-'*14} {'-'*4} {'-'*5} {'-'*6} {'-'*9}")
    for city, group in sorted(by_city.items(), key=lambda x: -len(x[1])):
        if len(group) < 3:
            continue
        wins = sum(1 for d in group if realized_win(d["action"], d["outcome"]))
        rate = wins / len(group) * 100
        avg_edge = stats.fmean(d["edge"] for d in group)
        print(f"  {city:<14} {len(group):>4} {wins:>5} {rate:>5.0f}% {avg_edge:>9.3f}")

    # ───────────────────────────────────────────────────────────────────
    # PHASE 0 PASS/FAIL VERDICT
    # ───────────────────────────────────────────────────────────────────
    hdr("PHASE 0 VERDICT")
    if not new_passed:
        print("  ❌ FAIL — Phase A gates filtered out all signals. Re-tune thresholds.")
        return

    new_wins = sum(1 for d in new_passed if realized_win(d["action"], d["outcome"]))
    new_rate = new_wins / len(new_passed) * 100
    new_tpd = len(new_passed) / span_hours * 24 if span_hours > 0 else 0
    print(f"  Phase A allows: {len(new_passed)} trades over {span_hours:.0f}h "
          f"= {new_tpd:.1f}/day")
    print(f"  Phase A win rate: {new_wins}/{len(new_passed)} = {new_rate:.0f}%")
    print()
    print(f"  Target gate: ≥70% win rate at ≥10 signals/day")
    pass_rate = new_rate >= 70
    pass_vol = new_tpd >= 10
    if pass_rate and pass_vol:
        print(f"  ✅ PASS on both criteria.")
        print(f"     → ship Phase A. Then kill-switch dry-run. Then flip when ready.")
    elif pass_rate and not pass_vol:
        print(f"  ⚠  PASS on win rate, BELOW volume target.")
        print(f"     → win rate is good but trade volume thin. Consider whether the daily")
        print(f"       cap is binding (means we'd hit the 10/day limit) or whether thin")
        print(f"       volume means small dollar P&L. Worth shipping anyway, monitor.")
    elif not pass_rate and pass_vol:
        print(f"  ❌ FAIL on win rate.")
        print(f"     → projected real win rate likely lower than {new_rate:.0f}%. Don't flip.")
    else:
        print(f"  ❌ FAIL on both.")
        print(f"     → Phase A gates are wrong; reconsider thresholds or skip to v3.")

    print()
    print("=" * 80)


if __name__ == "__main__":
    main()
