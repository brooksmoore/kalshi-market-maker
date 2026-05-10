"""
Preflight audit: answer the 4 open questions before live trading.

Q1 already answered (rules_primary text per series — NWS CLI every city).
Q2 — Timezone bug numeric impact, last 60 days, all cities.
Q3 — Demo settlement vs ASOS truth on the 24 resolved trades.
Q4 — Forecast skill drift metric (design + baseline).
"""
from __future__ import annotations
import sys, time, sqlite3
from datetime import date, timedelta
from collections import defaultdict
sys.path.insert(0, "src")
import requests
from config import CITIES, WEATHER_SERIES

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
HFCST = "https://historical-forecast-api.open-meteo.com/v1/forecast"

CITY_TZ = {
    "NYC": "America/New_York", "Chicago": "America/Chicago", "Miami": "America/New_York",
    "LA": "America/Los_Angeles", "Austin": "America/Chicago", "Denver": "America/Denver",
    "Philadelphia": "America/New_York", "Houston": "America/Chicago",
    "Boston": "America/New_York", "Phoenix": "America/Phoenix", "Dallas": "America/Chicago",
    "Seattle": "America/Los_Angeles", "Atlanta": "America/New_York",
    "SF": "America/Los_Angeles",
}

# Likely NWS CLI station codes (Kalshi rules name some, others ambiguous).
KALSHI_STATIONS = {
    "NYC": "NYC",   # Central Park (KNYC = LGA-area NWS, special id 'NYC' in mesonet)
    "Chicago": "MDW",
    "Miami": "MIA",
    "LA": "LAX",
    "Austin": "AUS",
    "Denver": "DEN",       # ambiguous in rules
    "Philadelphia": "PHL",
    "Houston": "IAH",      # ambiguous; IAH is the Houston NWS forecast office
    "Boston": "BOS",       # ambiguous
    "Phoenix": "PHX",      # ambiguous
    "Dallas": "DFW",       # ambiguous
    "Seattle": "SEA",      # ambiguous
    "Atlanta": "ATL",      # ambiguous
    "SF": "SFO",           # ambiguous
}


def daily(url, lat, lon, start, end, tz, extra=None):
    p = {"latitude": lat, "longitude": lon, "start_date": start.isoformat(),
         "end_date": end.isoformat(), "daily": "temperature_2m_max",
         "temperature_unit": "fahrenheit", "timezone": tz}
    if extra: p.update(extra)
    r = requests.get(url, params=p, timeout=30)
    r.raise_for_status()
    d = r.json().get("daily", {})
    return dict(zip(d.get("time", []), d.get("temperature_2m_max", [])))


def asos_daily_max(station, start, end, tz):
    """Pull ASOS hourly tmpf, compute daily max in local tz."""
    r = requests.get("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py",
        params={"station": station, "data": "tmpf",
                "year1": start.year, "month1": start.month, "day1": start.day,
                "year2": end.year, "month2": end.month, "day2": end.day,
                "tz": tz, "format": "onlycomma", "missing": "M",
                "trace": "T", "report_type": 3}, timeout=60)
    out = defaultdict(list)
    for line in r.text.split("\n"):
        if not line or line.startswith("station"): continue
        parts = line.split(",")
        if len(parts) < 3: continue
        try: v = float(parts[2])
        except: continue
        out[parts[1].split(" ")[0]].append(v)
    return {d: max(vs) for d, vs in out.items()}


def q2_timezone_impact():
    print("\n" + "=" * 78)
    print("Q2 — TIMEZONE BUG: numeric impact of forecast.py:75 'America/New_York'")
    print("=" * 78)
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=60)
    print(f"{'City':<14} {'tz_local':<22} {'n_days':>6} {'mean|Δ|':>8} {'max|Δ|':>7} {'days|Δ|>1':>10}")
    rows = []
    for city in CITIES:
        try:
            c = CITIES[city]; tz_local = CITY_TZ[city]
            f_local = daily(HFCST, c["lat"], c["lon"], start, end, tz_local, {"models": "gfs_seamless"})
            f_ny = daily(HFCST, c["lat"], c["lon"], start, end, "America/New_York", {"models": "gfs_seamless"})
            diffs = [abs(f_local[d] - f_ny[d]) for d in f_local if f_local.get(d) is not None and f_ny.get(d) is not None]
            if diffs:
                rows.append((city, tz_local, len(diffs), sum(diffs)/len(diffs), max(diffs),
                             sum(1 for x in diffs if x > 1.0)))
            time.sleep(0.4)
        except Exception as e:
            print(f"{city}: ERR {e}")
    rows.sort(key=lambda r: r[4], reverse=True)
    for city, tz, n, mean, mx, gt1 in rows:
        print(f"{city:<14} {tz:<22} {n:>6} {mean:>7.3f}°F {mx:>6.2f}°F {gt1:>10}")


def q3_demo_settlement_truth():
    print("\n" + "=" * 78)
    print("Q3 — DEMO SETTLEMENT vs ASOS TRUTH for the 24 resolved trades")
    print("=" * 78)
    db = sqlite3.connect("data/trades.db")
    rows = db.execute("""
        SELECT t.id, t.ticker, t.city, t.action, t.entry_price, r.outcome
        FROM trades t JOIN results r ON r.trade_id=t.id ORDER BY t.id
    """).fetchall()
    # Parse target date from ticker like KXHIGHLAX-26APR25-B66.5
    import re
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    print(f"{'tid':>3} {'city':<8} {'date':<11} {'bin(°F)':<10} {'action':<7} "
          f"{'demo_set':<8} {'asos_max':>8} {'asos_in_bin':<11} {'agrees':<6}")
    agree = disagree = unknown = 0
    for tid, ticker, city, action, entry, outcome in rows:
        m = re.match(r"KXHIGH[A-Z]+-(\d{2})([A-Z]{3})(\d{2})-B(\d+(?:\.\d+)?)", ticker)
        if not m:
            print(f"{tid:>3} {city:<8} ticker parse fail {ticker}")
            continue
        yr, mo, dy, b = m.groups()
        target = date(2000 + int(yr), months[mo], int(dy))
        b = float(b); lo, hi = b - 0.5, b + 0.5  # B66.5 = 66°-67° bin
        station = KALSHI_STATIONS.get(city)
        if not station:
            print(f"{tid:>3} {city:<8} no station mapping"); unknown += 1; continue
        try:
            obs = asos_daily_max(station, target, target, CITY_TZ[city])
            v = obs.get(target.isoformat())
        except Exception as e:
            print(f"{tid:>3} {city:<8} {target} ASOS err: {e}"); unknown += 1; continue
        if v is None:
            print(f"{tid:>3} {city:<8} {target} no ASOS data"); unknown += 1; continue
        in_bin = (lo <= v < hi)
        agrees = ((outcome == "yes") == in_bin)
        if agrees: agree += 1
        else: disagree += 1
        flag = "✓" if agrees else "✗"
        print(f"{tid:>3} {city:<8} {str(target):<11} {f'{lo:.0f}-{hi:.0f}':<10} {action:<7} "
              f"{outcome:<8} {v:>7.1f} {('YES' if in_bin else 'NO'):<11} {flag}")
        time.sleep(0.3)
    print(f"\nAgree (Kalshi demo == ASOS bin): {agree}")
    print(f"Disagree:                        {disagree}")
    print(f"Unknown (no data):               {unknown}")


def q4_skill_drift_baseline():
    print("\n" + "=" * 78)
    print("Q4 — FORECAST SKILL DRIFT: baseline rolling Brier-style metric")
    print("=" * 78)
    print("Metric design: for each city, weekly compute over the past 14 days:")
    print("  • mean abs error (MAE)  of GFS-day-1 forecast vs ASOS observed daily max")
    print("  • bias  = mean(forecast − observed)")
    print("  • RMSE")
    print("Trip thresholds (alert if):")
    print("  • MAE > 3.0°F  (any city; worse than April baseline by >40%)")
    print("  • |bias| > 2.0°F  (systematic over/underforecast appearing)")
    print("  • week-over-week MAE delta > +1.0°F  (regime change)")
    print()
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=14)
    print(f"{'City':<14} {'station':<8} {'n':>3} {'MAE':>6} {'bias':>8} {'RMSE':>6}")
    for city in CITIES:
        try:
            c = CITIES[city]; tz = CITY_TZ[city]; station = KALSHI_STATIONS.get(city, "")
            fc = daily(HFCST, c["lat"], c["lon"], start, end, tz, {"models": "gfs_seamless"})
            obs = asos_daily_max(station, start, end, tz) if station else {}
            errs = []
            for d, f in fc.items():
                o = obs.get(d)
                if f is None or o is None: continue
                errs.append(f - o)
            if errs:
                n = len(errs); mae = sum(abs(e) for e in errs)/n
                bias = sum(errs)/n; rmse = (sum(e*e for e in errs)/n)**0.5
                print(f"{city:<14} {station:<8} {n:>3} {mae:>5.2f}°F {bias:>+7.2f}°F {rmse:>5.2f}")
            time.sleep(0.4)
        except Exception as e:
            print(f"{city:<14} ERR: {e}")


if __name__ == "__main__":
    q2_timezone_impact()
    q3_demo_settlement_truth()
    q4_skill_drift_baseline()
