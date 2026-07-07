"""
per_city_deep_dive.py — investigate the surprising per-city signal patterns.

Surface-level finding from deep_analysis_20260517.py: Boston ratio 1.06×
(near-parity), Phoenix/LV ratio 2.4× (catastrophic). Before deciding next
steps, we need to know WHY.

Hypotheses to test:
  H1: "Boston is near-parity because the MODEL is good there"
      → check absolute model Brier per city
  H2: "Boston is near-parity because the MARKET is loose there"
      → check absolute market Brier per city
  H3: "Boston is near-parity because the BOT is uncommittal there
      (cal_p stays near base rate)"
      → check cal_p distribution and confidence per city
  H4: "PHX/LV is bad because market is SHARP there (not model unusually bad)"
      → compare model Brier in PHX/LV to other cities, normalized

Plus statistical CIs to know which differences are real vs noise.
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

SERIES_CITY = {
    "KXHIGHNY": "NYC", "KXHIGHCHI": "Chicago", "KXHIGHMIA": "Miami",
    "KXHIGHLAX": "LA", "KXHIGHAUS": "Austin", "KXHIGHDEN": "Denver",
    "KXHIGHPHIL": "Philadelphia", "KXHIGHHOU": "Houston", "KXHIGHBOS": "Boston",
    "KXHIGHPHX": "Phoenix", "KXHIGHDAL": "Dallas", "KXHIGHSEA": "Seattle",
    "KXHIGHATL": "Atlanta", "KXHIGHSF": "SF",
    "KXHIGHTNY": "NYC", "KXHIGHTCHI": "Chicago", "KXHIGHTMIA": "Miami",
    "KXHIGHTLAX": "LA", "KXHIGHTAUS": "Austin", "KXHIGHTDEN": "Denver",
    "KXHIGHTPHIL": "Philadelphia", "KXHIGHTHOU": "Houston", "KXHIGHTBOS": "Boston",
    "KXHIGHTPHX": "Phoenix", "KXHIGHTDAL": "Dallas", "KXHIGHTSEA": "Seattle",
    "KXHIGHTATL": "Atlanta", "KXHIGHTSFO": "SF",
    "KXHIGHTLV": "Las Vegas", "KXHIGHTSATX": "San Antonio",
    "KXHIGHTOKC": "Oklahoma City", "KXHIGHTDC": "DC", "KXHIGHTNOLA": "New Orleans",
}


def ticker_city(tk):
    for prefix, city in SERIES_CITY.items():
        if tk.startswith(prefix + "-"):
            return city
    return None


def bootstrap_ci(values, fn, n_resamples=500, ci=0.90):
    """Percentile bootstrap CI on `fn(values)`."""
    import random
    if not values:
        return None, None
    samples = []
    n = len(values)
    for _ in range(n_resamples):
        resample = [values[random.randrange(n)] for _ in range(n)]
        samples.append(fn(resample))
    samples.sort()
    lo_i = int((1 - ci) / 2 * n_resamples)
    hi_i = int((1 + ci) / 2 * n_resamples) - 1
    return samples[lo_i], samples[hi_i]


def main():
    c = sqlite3.connect(f"file:{SHADOW_DB}?mode=ro", uri=True)
    now = time.time()

    print("Fetching paired settled signals...")
    rows = c.execute("""
        SELECT DISTINCT s.ticker,
               (SELECT MIN(close_time) FROM book_snapshot b WHERE b.ticker=s.ticker)
        FROM shadow_signal s
        WHERE s.calibrated_p IS NOT NULL AND s.prod_yes_mid IS NOT NULL
    """).fetchall()
    closed = []
    for tk, ct in rows:
        if not ct:
            continue
        try:
            if datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp() < now:
                closed.append(tk)
        except Exception:
            pass

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
            sig = c.execute(
                "SELECT calibrated_p, prod_yes_mid, ts, ensemble_sd, ensemble_mean "
                "FROM shadow_signal WHERE ticker=? AND calibrated_p IS NOT NULL "
                "AND prod_yes_mid IS NOT NULL ORDER BY ts ASC LIMIT 1",
                (tk,),
            ).fetchone()
            if not sig:
                continue
            cp, mp, ts, esd, em = sig
            city = ticker_city(tk)
            if not city:
                continue
            paired.append({
                "ticker": tk, "city": city, "cal_p": cp, "mkt_p": mp,
                "outcome": outcome, "ts": ts, "ens_sd": esd, "ens_mean": em,
            })
            time.sleep(0.02)
        except Exception:
            pass

    print(f"Paired settled signals: {len(paired)}")

    # ──────────────────────────────────────────────────────────────────────
    # 1. ABSOLUTE MODEL vs MARKET BRIER PER CITY (decomposed)
    # ──────────────────────────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("1. ABSOLUTE BRIER DECOMPOSITION — is model bad, or is market good?")
    print("=" * 90)
    print()
    print(f"  {'city':<14} {'n':>4} {'m_Brier':>8} {'k_Brier':>8} {'m_CI(90%)':<16} {'k_CI(90%)':<16}")
    print(f"  {'-'*14} {'-'*4} {'-'*8} {'-'*8} {'-'*16} {'-'*16}")

    by_city = defaultdict(list)
    for r in paired:
        by_city[r["city"]].append(r)

    rows_for_sort = []
    for city, group in by_city.items():
        if len(group) < 30:
            continue
        m_errs = [(r["cal_p"] - r["outcome"]) ** 2 for r in group]
        k_errs = [(r["mkt_p"] - r["outcome"]) ** 2 for r in group]
        m_brier = stats.fmean(m_errs)
        k_brier = stats.fmean(k_errs)
        m_lo, m_hi = bootstrap_ci(m_errs, lambda v: stats.fmean(v))
        k_lo, k_hi = bootstrap_ci(k_errs, lambda v: stats.fmean(v))
        rows_for_sort.append((city, len(group), m_brier, k_brier, m_lo, m_hi, k_lo, k_hi))

    # Sort by absolute model Brier — smallest = best model performance
    rows_for_sort.sort(key=lambda x: x[2])
    print("  sorted by MODEL Brier (ascending — model best at top):")
    for city, n, mb, kb, mlo, mhi, klo, khi in rows_for_sort:
        m_ci = f"[{mlo:.3f}, {mhi:.3f}]"
        k_ci = f"[{klo:.3f}, {khi:.3f}]"
        print(f"  {city:<14} {n:>4} {mb:>8.4f} {kb:>8.4f} {m_ci:<16} {k_ci:<16}")

    print()
    print("  sorted by MARKET Brier (ascending — market sharpest at top):")
    for city, n, mb, kb, mlo, mhi, klo, khi in sorted(rows_for_sort, key=lambda x: x[3]):
        m_ci = f"[{mlo:.3f}, {mhi:.3f}]"
        k_ci = f"[{klo:.3f}, {khi:.3f}]"
        print(f"  {city:<14} {n:>4} {mb:>8.4f} {kb:>8.4f} {m_ci:<16} {k_ci:<16}")

    # ──────────────────────────────────────────────────────────────────────
    # 2. PER-CITY OUTCOME BASE RATE + CAL_P DISTRIBUTION
    # ──────────────────────────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("2. PER-CITY OUTCOME BASE RATE + MODEL COMMITMENT")
    print("=" * 90)
    print()
    print("  'baseline Brier' = base_rate * (1-base_rate); a model predicting")
    print("  base_rate uniformly achieves this Brier. Below baseline = model adds")
    print("  signal. Cal_p stdev: how decisive the model is (high = many confident")
    print("  predictions; low = mostly hovering near 0.5).")
    print()
    print(f"  {'city':<14} {'n':>4} {'base_rate':>9} {'baseline':>9} {'cal_p_sd':>9} "
          f"{'cal_p_med':>10} {'%conf':>6} {'%confw':>7}")
    print(f"  {'-'*14} {'-'*4} {'-'*9} {'-'*9} {'-'*9} {'-'*10} {'-'*6} {'-'*7}")
    for city, group in sorted(by_city.items(), key=lambda x: -len(x[1])):
        if len(group) < 30:
            continue
        base_rate = stats.fmean(r["outcome"] for r in group)
        baseline_brier = base_rate * (1 - base_rate)
        cal_ps = [r["cal_p"] for r in group]
        cal_sd = stats.stdev(cal_ps) if len(cal_ps) > 1 else 0
        cal_med = stats.median(cal_ps)
        # Confident predictions
        conf = [r for r in group if r["cal_p"] >= 0.7 or r["cal_p"] < 0.1]
        conf_wrong = [r for r in conf if abs(r["cal_p"] - r["outcome"]) > 0.5]
        pct_conf = len(conf) / len(group) * 100
        pct_conf_wrong = (len(conf_wrong) / len(conf) * 100) if conf else 0
        print(f"  {city:<14} {len(group):>4} {base_rate:>9.2f} {baseline_brier:>9.4f} "
              f"{cal_sd:>9.3f} {cal_med:>10.3f} {pct_conf:>5.0f}% {pct_conf_wrong:>6.0f}%")

    # ──────────────────────────────────────────────────────────────────────
    # 3. PHOENIX/LAS VEGAS DEEP DIVE
    # ──────────────────────────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("3. PHOENIX & LAS VEGAS — why are they catastrophic?")
    print("=" * 90)

    for target in ["Phoenix", "Las Vegas"]:
        group = by_city.get(target, [])
        if not group:
            continue
        print()
        print(f"  --- {target} (n={len(group)}) ---")
        # cal_p × outcome distribution
        cp_high_yes = [r for r in group if r["cal_p"] >= 0.7 and r["outcome"] == 1]
        cp_high_no  = [r for r in group if r["cal_p"] >= 0.7 and r["outcome"] == 0]
        cp_low_yes  = [r for r in group if r["cal_p"] <  0.3 and r["outcome"] == 1]
        cp_low_no   = [r for r in group if r["cal_p"] <  0.3 and r["outcome"] == 0]
        print(f"    confident YES (cal_p≥0.7): {len(cp_high_yes)} right, {len(cp_high_no)} wrong")
        print(f"    confident NO  (cal_p<0.3): {len(cp_low_no)} right, {len(cp_low_yes)} wrong")
        # Ensemble SD distribution (where available)
        with_ens = [r for r in group if r["ens_sd"] is not None]
        if with_ens:
            sds = [r["ens_sd"] for r in with_ens]
            print(f"    ensemble_sd (post-inflation): n={len(sds)}  "
                  f"median={stats.median(sds):.2f}°F")
        # Per-bracket-type breakdown (B vs T)
        b_tk = [r for r in group if "-B" in r["ticker"]]
        t_tk = [r for r in group if "-T" in r["ticker"] and "-B" not in r["ticker"]]
        if b_tk:
            b_brier = stats.fmean((r["cal_p"] - r["outcome"]) ** 2 for r in b_tk)
            b_market = stats.fmean((r["mkt_p"] - r["outcome"]) ** 2 for r in b_tk)
            print(f"    B-tickers (1°F bins): n={len(b_tk)}  model={b_brier:.4f}  market={b_market:.4f}")
        if t_tk:
            t_brier = stats.fmean((r["cal_p"] - r["outcome"]) ** 2 for r in t_tk)
            t_market = stats.fmean((r["mkt_p"] - r["outcome"]) ** 2 for r in t_tk)
            print(f"    T-tickers (thresholds): n={len(t_tk)}  model={t_brier:.4f}  market={t_market:.4f}")

    # ──────────────────────────────────────────────────────────────────────
    # 4. BOSTON DEEP DIVE — is it real or random?
    # ──────────────────────────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("4. BOSTON — is the near-parity real or sample-size noise?")
    print("=" * 90)
    group = by_city.get("Boston", [])
    if group:
        print()
        print(f"  n={len(group)}, base_rate={stats.fmean(r['outcome'] for r in group):.2f}")
        # Bootstrap CI on the ratio
        import random
        ratios = []
        for _ in range(2000):
            resample = [group[random.randrange(len(group))] for _ in range(len(group))]
            mb = stats.fmean((r["cal_p"] - r["outcome"]) ** 2 for r in resample)
            kb = stats.fmean((r["mkt_p"] - r["outcome"]) ** 2 for r in resample)
            ratios.append(mb / kb if kb > 0 else 999)
        ratios.sort()
        print(f"  Bootstrap 90% CI on model/market Brier ratio:")
        print(f"    [{ratios[100]:.2f}, {ratios[1899]:.2f}]")
        print(f"    point estimate: {ratios[1000]:.2f}")
        # How many of the 2000 resamples have ratio <= 1.0?
        beat = sum(1 for r in ratios if r <= 1.0)
        print(f"  bootstrap samples where model beats market (ratio≤1.0): {beat}/2000 = {beat/2000*100:.0f}%")
        # Cal_p distribution for Boston
        cps = [r["cal_p"] for r in group]
        print(f"  Boston cal_p distribution: median={stats.median(cps):.2f}  "
              f"sd={stats.stdev(cps) if len(cps)>1 else 0:.3f}")
        conf = [r for r in group if r["cal_p"] >= 0.7 or r["cal_p"] < 0.1]
        print(f"  Boston confident predictions (cal_p≥0.7 or <0.1): {len(conf)}/{len(group)}")
        if conf:
            conf_right = sum(1 for r in conf if (r["cal_p"] >= 0.7 and r["outcome"] == 1) or
                                                (r["cal_p"] < 0.1 and r["outcome"] == 0))
            print(f"    of which correct: {conf_right}/{len(conf)} = {conf_right/len(conf)*100:.0f}%")

    # ──────────────────────────────────────────────────────────────────────
    # 5. LAX OUTLIER CLUSTERING — what's special about its failures?
    # ──────────────────────────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("5. LAX OUTLIERS — pattern in the failures")
    print("=" * 90)
    lax = by_city.get("LA", [])
    if lax:
        print()
        # Find all confident-wrong LAX markets
        bad = [r for r in lax if abs(r["cal_p"] - r["outcome"]) > 0.5]
        good = [r for r in lax if abs(r["cal_p"] - r["outcome"]) < 0.2]
        print(f"  LA cohort: {len(lax)} settled")
        print(f"  confident-wrong (|err|>0.5):  {len(bad)} ({len(bad)/len(lax)*100:.0f}%)")
        print(f"  good predictions (|err|<0.2): {len(good)} ({len(good)/len(lax)*100:.0f}%)")
        print()
        print(f"  bad-prediction breakdown — outcome was YES vs NO:")
        bad_yes = sum(1 for r in bad if r["outcome"] == 1)
        bad_no = sum(1 for r in bad if r["outcome"] == 0)
        print(f"    cal_p low, actual YES (model said cool, day was hot): {bad_yes}")
        print(f"    cal_p high, actual NO (model said hot, day was cool): {bad_no}")
        # Ticker breakdown
        print()
        print(f"  worst LA failures:")
        bad_sorted = sorted(bad, key=lambda r: -abs(r["cal_p"] - r["outcome"]))
        for r in bad_sorted[:8]:
            em = f"{r['ens_mean']:.1f}" if r["ens_mean"] is not None else "-"
            esd = f"{r['ens_sd']:.2f}" if r["ens_sd"] is not None else "-"
            print(f"    {r['ticker']:<32}  cal_p={r['cal_p']:.2f}  mkt={r['mkt_p']:.2f}  "
                  f"out={r['outcome']}  ens={em}±{esd}")

    print()
    print("=" * 90)


if __name__ == "__main__":
    main()
