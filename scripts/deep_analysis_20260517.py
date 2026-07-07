"""
deep_analysis_20260517.py — comprehensive analysis of accumulated shadow
data after the spread-inflation deploy. One-shot read tool, produces a
structured report. Sections:

  1. Cohort overview (sizes, freshness, pre/post inflation split)
  2. Spread inflation effect (calibration buckets pre vs post)
  3. Per-city Brier / win-rate decomposition
  4. Lead-time gradient at larger n
  5. GEFS run-age effect (does forecast freshness within cycle matter?)
  6. Realized vs predicted dispersion (refined measurement)
  7. METAR retroactive overlap — would observed-so-far have helped?
  8. Outlier markets (specific failures worth examining)

Read-only. Doesn't modify any DB. Run as:
    venv/bin/python scripts/deep_analysis_20260517.py
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
METAR_DB = ROOT / "data" / "metar_observations.db"

# Spread inflation deployed approximately 2026-05-17 01:00 UTC.
INFLATION_DEPLOY_TS = datetime(2026, 5, 17, 1, 0, tzinfo=timezone.utc).timestamp()

# Approximate UTC offset per Kalshi city, for tz-aware "today" METAR lookup.
CITY_UTC_OFFSET_H = {
    "NYC": -4, "Chicago": -5, "Miami": -4, "LA": -7, "Austin": -5,
    "Denver": -6, "Philadelphia": -4, "Houston": -5, "Boston": -4,
    "Phoenix": -7, "Dallas": -5, "Seattle": -7, "Atlanta": -4, "SF": -7,
    "Las Vegas": -7, "San Antonio": -5, "Oklahoma City": -5, "DC": -4,
    "New Orleans": -5,
}

# Map Kalshi series prefix → city (for outcome scoring grouping)
SERIES_CITY = {
    "KXHIGHNY": "NYC", "KXHIGHCHI": "Chicago", "KXHIGHMIA": "Miami",
    "KXHIGHLAX": "LA", "KXHIGHAUS": "Austin", "KXHIGHDEN": "Denver",
    "KXHIGHPHIL": "Philadelphia", "KXHIGHHOU": "Houston", "KXHIGHBOS": "Boston",
    "KXHIGHPHX": "Phoenix", "KXHIGHDAL": "Dallas", "KXHIGHSEA": "Seattle",
    "KXHIGHATL": "Atlanta", "KXHIGHSF": "SF",
    # T-prefixed variants
    "KXHIGHTNY": "NYC", "KXHIGHTCHI": "Chicago", "KXHIGHTMIA": "Miami",
    "KXHIGHTLAX": "LA", "KXHIGHTAUS": "Austin", "KXHIGHTDEN": "Denver",
    "KXHIGHTPHIL": "Philadelphia", "KXHIGHTHOU": "Houston", "KXHIGHTBOS": "Boston",
    "KXHIGHTPHX": "Phoenix", "KXHIGHTDAL": "Dallas", "KXHIGHTSEA": "Seattle",
    "KXHIGHTATL": "Atlanta", "KXHIGHTSFO": "SF",
    "KXHIGHTLV": "Las Vegas", "KXHIGHTSATX": "San Antonio",
    "KXHIGHTOKC": "Oklahoma City", "KXHIGHTDC": "DC", "KXHIGHTNOLA": "New Orleans",
}


def hdr(s):
    print()
    print("=" * 76)
    print(s)
    print("=" * 76)


def fetch_outcomes(tickers):
    """Pull finalized outcomes for a set of tickers from Kalshi public API."""
    sess = requests.Session()
    out = {}
    for tk in tickers:
        try:
            r = sess.get(
                f"https://api.elections.kalshi.com/trade-api/v2/markets/{tk}",
                timeout=(5, 10),
            ).json().get("market", {})
            if r.get("status") == "finalized" and r.get("result") in ("yes", "no"):
                out[tk] = 1 if r["result"] == "yes" else 0
            time.sleep(0.02)
        except Exception:
            pass
    return out


def main():
    if not SHADOW_DB.exists():
        print(f"missing {SHADOW_DB}")
        return

    c = sqlite3.connect(f"file:{SHADOW_DB}?mode=ro", uri=True)
    now = time.time()

    # =====================================================================
    # 1. COHORT OVERVIEW
    # =====================================================================
    hdr("1. COHORT OVERVIEW")
    ss_total = c.execute("SELECT COUNT(*) FROM shadow_signal").fetchone()[0]
    ss_runs = c.execute("SELECT COUNT(DISTINCT ts) FROM shadow_signal").fetchone()[0]
    ss_first = c.execute("SELECT MIN(ts) FROM shadow_signal").fetchone()[0]
    ss_last = c.execute("SELECT MAX(ts) FROM shadow_signal").fetchone()[0]
    print(f"shadow signals: {ss_total} rows across {ss_runs} runs")
    print(f"span: {datetime.fromtimestamp(ss_first, tz=timezone.utc).isoformat()} → "
          f"{datetime.fromtimestamp(ss_last, tz=timezone.utc).isoformat()}")
    print(f"duration: {(ss_last - ss_first) / 3600:.1f}h")

    pre_signals = c.execute(
        "SELECT COUNT(*) FROM shadow_signal WHERE ts < ?",
        (INFLATION_DEPLOY_TS,),
    ).fetchone()[0]
    post_signals = c.execute(
        "SELECT COUNT(*) FROM shadow_signal WHERE ts >= ?",
        (INFLATION_DEPLOY_TS,),
    ).fetchone()[0]
    print(f"pre-inflation:  {pre_signals} signals (ts < 5/17 01z)")
    print(f"post-inflation: {post_signals} signals (ts ≥ 5/17 01z)")

    # =====================================================================
    # Gather all settled signals (pre + post)
    # =====================================================================
    print()
    print("fetching finalized outcomes for all shadow-logged tickers...")
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
    print(f"  past-close tickers: {len(closed)}")
    outcomes = fetch_outcomes(closed)
    print(f"  finalized: {len(outcomes)}")

    # Build paired (ticker, cal_p_first, market_p_first, outcome, signal_ts,
    # ensemble_sd_first, ensemble_mean_first, lead_hours_first, gefs_run_ts_first)
    paired = []
    for tk, outcome in outcomes.items():
        sig = c.execute(
            "SELECT calibrated_p, prod_yes_mid, ts, ensemble_sd, ensemble_mean, "
            "lead_hours, gefs_run_ts, city FROM shadow_signal "
            "WHERE ticker=? AND calibrated_p IS NOT NULL AND prod_yes_mid IS NOT NULL "
            "ORDER BY ts ASC LIMIT 1",
            (tk,),
        ).fetchone()
        if not sig:
            continue
        cp, mp, ts, esd, em, lh, gr, city = sig
        paired.append({
            "ticker": tk, "cal_p": cp, "mkt_p": mp, "outcome": outcome,
            "ts": ts, "ens_sd": esd, "ens_mean": em, "lead_h": lh,
            "gefs_run_ts": gr, "city": city,
        })

    n = len(paired)
    print(f"  paired settled signals: {n}")

    # =====================================================================
    # 2. HEADLINE METRICS + PRE/POST SPLIT
    # =====================================================================
    hdr("2. HEADLINE METRICS")
    mb = stats.fmean((r["cal_p"] - r["outcome"]) ** 2 for r in paired)
    kb = stats.fmean((r["mkt_p"] - r["outcome"]) ** 2 for r in paired)
    m_win = sum(1 for r in paired if abs(r["cal_p"] - r["outcome"]) < abs(r["mkt_p"] - r["outcome"]))
    print(f"  n={n}  model_Brier={mb:.4f}  market_Brier={kb:.4f}  ratio={mb/kb:.2f}x")
    print(f"  model wins overall: {m_win}/{n} = {m_win/n*100:.0f}%")

    big = [r for r in paired if abs(r["cal_p"] - r["mkt_p"]) >= 0.30]
    m_win_b = sum(1 for r in big if abs(r["cal_p"] - r["outcome"]) < abs(r["mkt_p"] - r["outcome"]))
    if big:
        print(f"  strong-disagreement (|gap|>=0.30) n={len(big)}: "
              f"model wins {m_win_b}/{len(big)} = {m_win_b/len(big)*100:.0f}%")

    # =====================================================================
    # 3. SPREAD INFLATION EFFECT
    # =====================================================================
    hdr("3. SPREAD INFLATION EFFECT — pre vs post calibration buckets")

    pre = [r for r in paired if r["ts"] < INFLATION_DEPLOY_TS]
    post = [r for r in paired if r["ts"] >= INFLATION_DEPLOY_TS]
    print(f"  pre-inflation settled signals:  {len(pre)}")
    print(f"  post-inflation settled signals: {len(post)}")

    if pre:
        mb_pre = stats.fmean((r["cal_p"] - r["outcome"]) ** 2 for r in pre)
        kb_pre = stats.fmean((r["mkt_p"] - r["outcome"]) ** 2 for r in pre)
        print(f"  PRE  model={mb_pre:.4f}  market={kb_pre:.4f}  ratio={mb_pre/kb_pre:.2f}x")
    if post:
        mb_post = stats.fmean((r["cal_p"] - r["outcome"]) ** 2 for r in post)
        kb_post = stats.fmean((r["mkt_p"] - r["outcome"]) ** 2 for r in post)
        print(f"  POST model={mb_post:.4f}  market={kb_post:.4f}  ratio={mb_post/kb_post:.2f}x")

    def cal_table(label, group):
        print(f"\n  {label}  (n={len(group)})")
        print(f"  {'bin':<12} {'n':>4} {'mean_p':>7} {'actual':>7} {'gap':>7}")
        for lo, hi in [(0,0.1),(0.1,0.2),(0.2,0.3),(0.3,0.4),(0.4,0.5),
                       (0.5,0.6),(0.6,0.7),(0.7,0.8),(0.8,0.9),(0.9,1.01)]:
            bucket = [r for r in group if lo <= r["cal_p"] < hi]
            if not bucket:
                continue
            mp = stats.fmean(r["cal_p"] for r in bucket)
            ar = stats.fmean(r["outcome"] for r in bucket)
            print(f"  [{lo:.1f},{hi:.1f}) {len(bucket):>4} {mp:>7.3f} {ar:>7.3f} {ar-mp:>+7.3f}")

    if pre:
        cal_table("PRE-inflation calibration", pre)
    if post:
        cal_table("POST-inflation calibration", post)

    # =====================================================================
    # 4. PER-CITY DECOMPOSITION
    # =====================================================================
    hdr("4. PER-CITY DECOMPOSITION")
    by_city = defaultdict(list)
    for r in paired:
        for prefix, city in SERIES_CITY.items():
            if r["ticker"].startswith(prefix + "-"):
                by_city[city].append(r)
                break

    print(f"  {'city':<14} {'n':>4} {'m_Brier':>8} {'k_Brier':>8} {'ratio':>5} {'wins%':>6}")
    print(f"  {'-'*14} {'-'*4} {'-'*8} {'-'*8} {'-'*5} {'-'*6}")
    city_rows = []
    for city, group in by_city.items():
        if len(group) < 8:
            continue
        mb_c = stats.fmean((r["cal_p"] - r["outcome"]) ** 2 for r in group)
        kb_c = stats.fmean((r["mkt_p"] - r["outcome"]) ** 2 for r in group)
        wins = sum(1 for r in group if abs(r["cal_p"] - r["outcome"]) < abs(r["mkt_p"] - r["outcome"]))
        city_rows.append((city, len(group), mb_c, kb_c, mb_c/kb_c if kb_c > 0 else float('inf'), wins/len(group)*100))
    for row in sorted(city_rows, key=lambda x: x[4]):
        print(f"  {row[0]:<14} {row[1]:>4} {row[2]:>8.4f} {row[3]:>8.4f} {row[4]:>5.2f} {row[5]:>5.0f}%")

    # =====================================================================
    # 5. LEAD-TIME GRADIENT (refined)
    # =====================================================================
    hdr("5. LEAD-TIME GRADIENT")
    bins = [(0, 6), (6, 12), (12, 24), (24, 36), (36, 100)]
    print(f"  {'lead band':<14} {'n':>4} {'m_Brier':>8} {'k_Brier':>8} {'ratio':>5}")
    for lo, hi in bins:
        group = [r for r in paired if r["lead_h"] is not None and lo <= r["lead_h"] < hi]
        if not group:
            continue
        mb_g = stats.fmean((r["cal_p"] - r["outcome"]) ** 2 for r in group)
        kb_g = stats.fmean((r["mkt_p"] - r["outcome"]) ** 2 for r in group)
        print(f"  {lo:>2}h - {hi:>2}h     {len(group):>4} {mb_g:>8.4f} {kb_g:>8.4f} {mb_g/kb_g:>5.2f}x")

    # =====================================================================
    # 6. GEFS RUN-AGE EFFECT (within-cycle freshness)
    # =====================================================================
    hdr("6. GEFS RUN-AGE EFFECT")
    print("  (lead time from GEFS run to signal capture; lower = fresher forecast)")
    bins_age = [(0, 1.5), (1.5, 3), (3, 5), (5, 7)]
    print(f"  {'cycle age':<14} {'n':>4} {'m_Brier':>8} {'k_Brier':>8} {'ratio':>5}")
    for lo, hi in bins_age:
        group = []
        for r in paired:
            if r["gefs_run_ts"] is None:
                continue
            age_h = (r["ts"] - r["gefs_run_ts"]) / 3600
            if lo <= age_h < hi:
                group.append(r)
        if not group:
            continue
        mb_g = stats.fmean((r["cal_p"] - r["outcome"]) ** 2 for r in group)
        kb_g = stats.fmean((r["mkt_p"] - r["outcome"]) ** 2 for r in group)
        print(f"  {lo:>3.1f}-{hi:>3.1f}h     {len(group):>4} {mb_g:>8.4f} {kb_g:>8.4f} {mb_g/kb_g:>5.2f}x")

    # =====================================================================
    # 7. REALIZED VS PREDICTED DISPERSION
    # =====================================================================
    hdr("7. REALIZED vs PREDICTED DISPERSION (post-inflation cohort)")
    post_with_ens = [r for r in post if r["ens_sd"] is not None and r["ens_mean"] is not None]
    if post_with_ens:
        # We use the max-p-bin vs settled-YES-bin trick from the earlier
        # audit to estimate realized error. For paired settled signals,
        # we know the outcome (YES/NO) but not the exact settled high
        # temperature. So we can only see distributional consistency.
        # Better: for each post-cohort settled signal, ask whether the
        # outcome is consistent with the model's CDF.
        sds = [r["ens_sd"] for r in post_with_ens]
        print(f"  post-inflation ensemble_sd: n={len(sds)}  "
              f"p25={sorted(sds)[len(sds)//4]:.2f}  p50={stats.median(sds):.2f}  "
              f"p75={sorted(sds)[3*len(sds)//4]:.2f}  p90={sorted(sds)[9*len(sds)//10]:.2f}")
        # Compare to the calibration buckets — if SD is calibrated, the
        # bucket gaps should be ~0
        cal_p_high = [r for r in post_with_ens if r["cal_p"] >= 0.7]
        if cal_p_high:
            actual = stats.fmean(r["outcome"] for r in cal_p_high)
            print(f"  [0.7+] band n={len(cal_p_high)}: model says ≥0.7, actual={actual:.2f}")
        cal_p_low = [r for r in post_with_ens if r["cal_p"] < 0.1]
        if cal_p_low:
            actual = stats.fmean(r["outcome"] for r in cal_p_low)
            print(f"  [0.0,0.1) band n={len(cal_p_low)}: model says <0.1, actual={actual:.2f}")
    else:
        print("  no post-inflation settled signals with ensemble_sd yet")

    # =====================================================================
    # 8. METAR RETROACTIVE OVERLAP
    # =====================================================================
    hdr("8. METAR RETROACTIVE OVERLAP — would METAR floor have helped?")
    if not METAR_DB.exists():
        print("  no METAR DB yet")
    else:
        m = sqlite3.connect(f"file:{METAR_DB}?mode=ro", uri=True)
        m_count = m.execute("SELECT COUNT(*) FROM metar_observation").fetchone()[0]
        m_span = m.execute("SELECT MIN(observation_ts), MAX(observation_ts) FROM metar_observation").fetchone()
        if m_span[0] is None:
            print("  no METAR observations yet")
        else:
            print(f"  METAR observations: {m_count}")
            print(f"  span: {datetime.fromtimestamp(m_span[0], tz=timezone.utc).isoformat()} → "
                  f"{datetime.fromtimestamp(m_span[1], tz=timezone.utc).isoformat()}")
            # For each paired settled signal, check if METAR has overlap
            # for that city on the target date.
            # Extract target date from ticker (YYMMDD format).
            DATE_RE = re.compile(r"(\d{2})([A-Z]{3})(\d{2})")
            MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                      "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
            overlap = 0
            would_floor = 0  # observation > some threshold of model's mean
            for r in paired:
                mo = DATE_RE.search(r["ticker"])
                if not mo:
                    continue
                try:
                    yr, mn_s, dy = mo.groups()
                    tgt = datetime(2000 + int(yr), MONTHS[mn_s], int(dy), tzinfo=timezone.utc)
                except Exception:
                    continue
                # Local-day start/end (rough UTC approximation: city offset + 24h window)
                city = r["city"]
                off = CITY_UTC_OFFSET_H.get(city, -5)
                day_start = tgt.timestamp() - off * 3600
                day_end = day_start + 24 * 3600
                # Find max observed temp in that window for this city
                max_temp = m.execute(
                    "SELECT MAX(temperature_f) FROM metar_observation "
                    "WHERE city=? AND observation_ts BETWEEN ? AND ? AND temperature_f IS NOT NULL",
                    (city, day_start, day_end),
                ).fetchone()[0]
                if max_temp is not None:
                    overlap += 1
                    # Did the observed-so-far exceed where the model thought
                    # the day's high would land? If observed >> model_mean,
                    # market would already know YES on hot brackets, NO on cold.
                    if r["ens_mean"] is not None and max_temp >= r["ens_mean"] + 1.0:
                        would_floor += 1
            print(f"  settled signals with METAR overlap: {overlap}/{n}")
            print(f"  (Note: most METAR overlap requires markets settled SINCE we started")
            print(f"   METAR collection. Coverage will grow over time.)")

    # =====================================================================
    # 9. OUTLIER MARKETS — worst model failures
    # =====================================================================
    hdr("9. OUTLIER MARKETS — worst model failures")
    failures = [(r, abs(r["cal_p"] - r["outcome"])) for r in paired]
    failures.sort(key=lambda x: -x[1])
    print(f"  top 12 confident-wrong (sorted by |cal_p - outcome|):")
    print(f"  {'ticker':<35} {'cal_p':>6} {'mkt_p':>6} {'out':>3} {'ens_mean':>8} {'ens_sd':>6}")
    for r, _ in failures[:12]:
        es = f"{r['ens_sd']:.2f}" if r['ens_sd'] is not None else "-"
        em = f"{r['ens_mean']:.1f}" if r['ens_mean'] is not None else "-"
        print(f"  {r['ticker']:<35} {r['cal_p']:>6.2f} {r['mkt_p']:>6.2f} "
              f"{r['outcome']:>3} {em:>8} {es:>6}")

    print()
    print("=" * 76)


if __name__ == "__main__":
    main()
