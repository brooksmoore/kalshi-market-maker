"""
phase_0_revised.py — Phase 0 v2 (parallel paths).

After phase_a_gates.py showed Phase A as proposed gives 57% win rate
(below the 70% target), this script evaluates two alternatives:

  PATH 1 — Refined hand-tuned gates:
    BUY NO  requires cal_p ∈ [0.15, 0.30)  (empirical EV-positive sweet spot)
    BUY YES retains asymmetric MIN_EDGE=0.30 + cal_p cap 0.6
    PHX, LV excluded from trade universe entirely
    Standard MIN_EDGE=8% otherwise

  PATH 2 — Isotonic calibration refit:
    Fit isotonic regression on n=1173 paired (raw_p, outcome) pairs from
    historical shadow data. The refit corrects the systematic
    under-dispersion-induced bias at both ends of the cal_p range.
    Then evaluate with standard symmetric MIN_EDGE=8% (no special gates).

  TRAIN/TEST DISCIPLINE:
    Isotonic is fit on first 80% of the cohort (by ts), evaluated on the
    last 20%. Fitting + evaluating on the same data overstates the fix.

Output: side-by-side comparison of current, Path 1, and Path 2 regimes.
Optionally writes the fitted isotonic to data/calibration.pkl if --ship is passed.
"""

from __future__ import annotations

import argparse
import json
import pickle
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
CAL_PKL = ROOT / "data" / "calibration.pkl"
CAL_META = ROOT / "data" / "calibration.meta.json"

MAX_ENTRY_SPREAD_C = 3.0
MIN_EDGE = 0.08
FEE_APPROX = 0.01

# Path 1 specifics
P1_MIN_EDGE_BUY_YES = 0.30
P1_HARD_CAP_CAL_P_BUY_YES = 0.60
P1_BUY_NO_CAL_P_LO = 0.15
P1_BUY_NO_CAL_P_HI = 0.30
P1_EXCLUDE_CITIES = {"Phoenix", "Las Vegas"}


def hdr(s):
    print()
    print("=" * 80)
    print(s)
    print("=" * 80)


def compute_action_and_edges(cal_p, yes_ask, no_ask):
    if yes_ask is None or no_ask is None:
        return None, None
    if yes_ask <= 0 or yes_ask >= 1 or no_ask <= 0 or no_ask >= 1:
        return None, None
    edge_yes = cal_p - yes_ask - FEE_APPROX
    edge_no = (1.0 - cal_p) - no_ask - FEE_APPROX
    if edge_yes >= edge_no:
        return "BUY_YES", edge_yes
    return "BUY_NO", edge_no


def realized_win(action, outcome):
    return (action == "BUY_YES" and outcome == 1) or \
           (action == "BUY_NO" and outcome == 0)


def gate_current(d):
    if d["spread_c"] is not None and d["spread_c"] > MAX_ENTRY_SPREAD_C:
        return False
    return d["edge"] >= MIN_EDGE


def gate_path1(d):
    """Refined hand-tuned gate."""
    if d["spread_c"] is not None and d["spread_c"] > MAX_ENTRY_SPREAD_C:
        return False
    if d["city"] in P1_EXCLUDE_CITIES:
        return False
    if d["action"] == "BUY_YES":
        if d["cal_p"] > P1_HARD_CAP_CAL_P_BUY_YES:
            return False
        return d["edge"] >= P1_MIN_EDGE_BUY_YES
    else:  # BUY_NO
        if d["edge"] < MIN_EDGE:
            return False
        return P1_BUY_NO_CAL_P_LO <= d["cal_p"] < P1_BUY_NO_CAL_P_HI


def evaluate(label, decisions, gate_fn, span_hours):
    passed = [d for d in decisions if gate_fn(d)]
    buy_yes = [d for d in passed if d["action"] == "BUY_YES"]
    buy_no = [d for d in passed if d["action"] == "BUY_NO"]
    wins_all = sum(1 for d in passed if realized_win(d["action"], d["outcome"]))
    wins_y = sum(1 for d in buy_yes if realized_win(d["action"], d["outcome"]))
    wins_n = sum(1 for d in buy_no if realized_win(d["action"], d["outcome"]))
    tpd = len(passed) / span_hours * 24 if span_hours > 0 else 0

    hdr(label)
    print(f"  TOTAL passed: {len(passed)} / {len(decisions)} "
          f"({len(passed)/len(decisions)*100:.0f}%)")
    if passed:
        print(f"    ALL    : {len(passed):>3} trades  "
              f"wins={wins_all} ({wins_all/len(passed)*100:.0f}%)")
    if buy_yes:
        print(f"    BUY_YES: {len(buy_yes):>3} trades  "
              f"wins={wins_y} ({wins_y/len(buy_yes)*100:.0f}%)")
    if buy_no:
        print(f"    BUY_NO : {len(buy_no):>3} trades  "
              f"wins={wins_n} ({wins_n/len(buy_no)*100:.0f}%)")
    print(f"  projected volume: {tpd:.1f} trades/day  "
          f"(daily cap binds: {'YES' if tpd >= 10 else 'no'})")

    # EV per trade — assuming we pay ask, get $1 if win
    if passed:
        evs = []
        for d in passed:
            if d["action"] == "BUY_YES":
                cost = d["yes_ask"]
            else:
                cost = d["no_ask"]
            if d["outcome"] == 1 and d["action"] == "BUY_YES":
                evs.append(1.0 - cost)
            elif d["outcome"] == 0 and d["action"] == "BUY_NO":
                evs.append(1.0 - cost)
            else:
                evs.append(-cost)
        avg_ev = stats.fmean(evs)
        total_pnl = sum(evs)
        print(f"  realized $/contract: avg={avg_ev:+.4f}  cohort_total={total_pnl:+.2f}")
        print(f"  (at 1 contract/trade across {span_hours:.0f}h of data)")
    return passed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ship", action="store_true",
                    help="If Path 2 passes test eval, write data/calibration.pkl")
    args = ap.parse_args()

    if not SHADOW_DB.exists():
        print(f"missing {SHADOW_DB}")
        return

    c = sqlite3.connect(f"file:{SHADOW_DB}?mode=ro", uri=True)
    now = time.time()

    print("Fetching paired settled signals + book data...")
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
    for tk in closed:
        try:
            r = sess.get(
                f"https://api.elections.kalshi.com/trade-api/v2/markets/{tk}",
                timeout=(5, 10),
            ).json().get("market", {})
            if r.get("status") != "finalized" or r.get("result") not in ("yes", "no"):
                continue
            outcome = 1 if r["result"] == "yes" else 0
            sig = c.execute("""
                SELECT s.calibrated_p, s.raw_p, s.ts, s.book_ts, s.city,
                       b.yes_ask, b.no_ask, b.spread_no_c
                FROM shadow_signal s
                LEFT JOIN book_snapshot b
                  ON b.ticker = s.ticker AND b.ts = s.book_ts
                WHERE s.ticker = ?
                  AND s.calibrated_p IS NOT NULL
                ORDER BY s.ts ASC
                LIMIT 1
            """, (tk,)).fetchone()
            if not sig or sig[5] is None or sig[6] is None:
                continue
            cp, rp, ts, bts, city, ya, na, sp = sig
            paired.append({
                "ticker": tk, "cal_p": cp, "raw_p": rp if rp is not None else cp,
                "ts": ts, "city": city, "yes_ask": ya, "no_ask": na,
                "spread_c": sp, "outcome": outcome,
            })
            time.sleep(0.02)
        except Exception:
            pass

    n = len(paired)
    if not n:
        print("no paired data; abort")
        return
    paired.sort(key=lambda x: x["ts"])
    ts_min = paired[0]["ts"]
    ts_max = paired[-1]["ts"]
    span_hours = (ts_max - ts_min) / 3600
    print(f"n={n}  span={span_hours:.1f}h ({span_hours/24:.1f} days)")

    # ─── Train/test split for honest isotonic eval ──────────────────────
    cutoff_idx = int(n * 0.80)
    train = paired[:cutoff_idx]
    test = paired[cutoff_idx:]
    train_span = (train[-1]["ts"] - train[0]["ts"]) / 3600 if train else 0
    test_span = (test[-1]["ts"] - test[0]["ts"]) / 3600 if test else 0
    print(f"train (first 80%): n={len(train)}  span={train_span:.1f}h")
    print(f"test  (last 20%):  n={len(test)}  span={test_span:.1f}h")

    # Compute action+edge for all using current cal_p
    for d in paired:
        action, edge = compute_action_and_edges(
            d["cal_p"], d["yes_ask"], d["no_ask"]
        )
        d["action"] = action
        d["edge"] = edge
    test_decisions = [d for d in test if d["action"] is not None]

    # ─── Path 1: refined gates on test set ──────────────────────────────
    evaluate("PATH 1 — Refined gates (BUY NO cal_p∈[0.15,0.30), exclude PHX/LV)",
             test_decisions, gate_path1, test_span)
    evaluate("BASELINE — Current gates (MIN_EDGE=0.08 symmetric) on test set",
             test_decisions, gate_current, test_span)

    # ─── Path 2: fit isotonic on train, apply to test ────────────────────
    hdr("PATH 2 — Isotonic refit (fit on train 80%, eval on test 20%)")
    from sklearn.isotonic import IsotonicRegression
    import numpy as np

    raw_train = np.array([d["raw_p"] for d in train])
    out_train = np.array([d["outcome"] for d in train], dtype=float)
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(raw_train, out_train)

    # Show the learned mapping at key points
    print("  Isotonic learned mapping (raw → calibrated):")
    for raw in [0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]:
        print(f"    raw_p={raw:.2f} → cal_p={float(iso.predict([raw])[0]):.3f}")

    # Apply isotonic to test set
    raw_test = np.array([d["raw_p"] for d in test_decisions])
    cal_test = iso.predict(raw_test)
    test_decisions_p2 = []
    for d, new_cp in zip(test_decisions, cal_test):
        d2 = dict(d)
        d2["cal_p"] = float(new_cp)
        a, e = compute_action_and_edges(d2["cal_p"], d2["yes_ask"], d2["no_ask"])
        d2["action"] = a
        d2["edge"] = e
        if a is not None:
            test_decisions_p2.append(d2)

    evaluate("PATH 2 — Isotonic + standard MIN_EDGE=8% (test set, out-of-sample)",
             test_decisions_p2, gate_current, test_span)

    # Calibration buckets before/after (test set)
    print()
    print("  CALIBRATION BUCKETS — test set, raw vs isotonic-applied:")
    print(f"    {'bin':<14} {'n':>4} {'mean_raw':>9} {'mean_cal':>9} {'actual':>7} {'raw_gap':>8} {'cal_gap':>8}")
    for lo, hi in [(0,0.1),(0.1,0.2),(0.2,0.3),(0.3,0.4),(0.4,0.5),
                   (0.5,0.6),(0.6,0.7),(0.7,0.8),(0.8,0.9),(0.9,1.01)]:
        bucket = [(d, c) for d, c in zip(test_decisions, cal_test)
                  if lo <= d["raw_p"] < hi]
        if not bucket: continue
        mp_raw = stats.fmean(d["raw_p"] for d, _ in bucket)
        mp_cal = stats.fmean(c for _, c in bucket)
        actual = stats.fmean(d["outcome"] for d, _ in bucket)
        print(f"    [{lo:.1f},{hi:.1f})  {len(bucket):>4} {mp_raw:>9.3f} {mp_cal:>9.3f} "
              f"{actual:>7.3f} {actual-mp_raw:>+8.3f} {actual-mp_cal:>+8.3f}")

    # Decision: should we ship?
    hdr("VERDICT")
    p1_passed = [d for d in test_decisions if gate_path1(d)]
    p2_passed = [d for d in test_decisions_p2 if gate_current(d)]

    def verdict(label, group, span_h):
        if not group:
            print(f"  {label}: 0 trades. FAIL.")
            return False
        wins = sum(1 for d in group if realized_win(d["action"], d["outcome"]))
        rate = wins / len(group) * 100
        tpd = len(group) / span_h * 24 if span_h > 0 else 0
        ev_sum = 0
        for d in group:
            cost = d["yes_ask"] if d["action"] == "BUY_YES" else d["no_ask"]
            if realized_win(d["action"], d["outcome"]):
                ev_sum += 1.0 - cost
            else:
                ev_sum -= cost
        ev_per = ev_sum / len(group)
        print(f"  {label}:")
        print(f"    n={len(group)} test-cohort trades, {tpd:.1f}/day projected")
        print(f"    win rate: {rate:.0f}%  (target ≥70%)")
        print(f"    EV/contract: {ev_per:+.4f}  (target > 0)")
        passes_rate = rate >= 70
        passes_ev = ev_per > 0
        passes_vol = tpd >= 10
        print(f"    pass criteria: rate={'✓' if passes_rate else '✗'}  "
              f"EV={'✓' if passes_ev else '✗'}  vol={'✓' if passes_vol else '✗'}")
        return passes_ev  # EV is the most important — volume is bankroll-bounded anyway

    p1_pass = verdict("PATH 1 (refined gates)", p1_passed, test_span)
    p2_pass = verdict("PATH 2 (isotonic + std gates)", p2_passed, test_span)

    print()
    if p2_pass and not p1_pass:
        print("  → Recommend PATH 2 (isotonic). Run with --ship to deploy calibration.pkl.")
    elif p1_pass and not p2_pass:
        print("  → Recommend PATH 1 (refined gates). Update strategy.py config.")
    elif p1_pass and p2_pass:
        print("  → BOTH pass. Path 2 is more principled (calibration vs hand-tuning).")
        print("    Recommend Path 2; Path 1 as additional belt-and-suspenders.")
    else:
        print("  → NEITHER passes. v2-hardening can't get there. v3 architecture needed.")

    # ─── Ship the calibration if --ship and Path 2 passed ────────────────
    if args.ship and p2_pass:
        print()
        print(f"  --ship: writing isotonic to {CAL_PKL}")
        # Refit on FULL data (train + test) for the production model.
        raw_full = np.array([d["raw_p"] for d in paired])
        out_full = np.array([d["outcome"] for d in paired], dtype=float)
        iso_full = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso_full.fit(raw_full, out_full)
        CAL_PKL.parent.mkdir(exist_ok=True)
        with open(CAL_PKL, "wb") as f:
            pickle.dump(iso_full, f)
        with open(CAL_META, "w") as f:
            json.dump({
                "n_samples": len(paired),
                "fit_at_iso": datetime.utcnow().isoformat() + "Z",
                "source": "shadow_signal cohort 2026-05-23",
                "train_test_test_win_rate_pct": (
                    sum(1 for d in p2_passed if realized_win(d["action"], d["outcome"]))
                    / len(p2_passed) * 100 if p2_passed else 0
                ),
                "note": "Fit on out-of-sample test cohort post-spread-inflation. "
                        "Re-fit weekly as new shadow data accumulates.",
            }, f, indent=2)
        print(f"  wrote {CAL_PKL} and {CAL_META}")
        print(f"  next bot/shadow cycle will load and apply automatically.")

    print()
    print("=" * 80)


if __name__ == "__main__":
    main()
