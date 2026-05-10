"""
Q3-deep: measure the NWS-CLI-vs-ASOS gap using Kalshi PRODUCTION settlements.

For every city/day, find the single B-ticker (1°F bin) that Kalshi settled YES.
That bin is what NWS CLI reported as the daily max (rounded into the bin).
Compare to ASOS hourly max from Iowa Mesonet for the same city/day.
Gap = CLI_implied_max  -  ASOS_max.

Production market data is fetched unauthenticated; ASOS via mesonet.
"""
from __future__ import annotations
import sys, time, re
from datetime import date, timedelta
from collections import defaultdict
sys.path.insert(0, "src")
import requests
from config import WEATHER_SERIES

PROD = "https://api.elections.kalshi.com/trade-api/v2"
MESONET = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

CITY_TZ = {
    "NYC": "America/New_York", "Chicago": "America/Chicago", "Miami": "America/New_York",
    "LA": "America/Los_Angeles", "Austin": "America/Chicago", "Denver": "America/Denver",
    "Philadelphia": "America/New_York", "Houston": "America/Chicago",
    "Boston": "America/New_York", "Phoenix": "America/Phoenix", "Dallas": "America/Chicago",
    "Seattle": "America/Los_Angeles", "Atlanta": "America/New_York",
    "SF": "America/Los_Angeles",
}
STATIONS = {
    "NYC": "NYC", "Chicago": "MDW", "Miami": "MIA", "LA": "LAX", "Austin": "AUS",
    "Denver": "DEN", "Philadelphia": "PHL", "Houston": "IAH", "Boston": "BOS",
    "Phoenix": "PHX", "Dallas": "DFW", "Seattle": "SEA", "Atlanta": "ATL", "SF": "SFO",
}

MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
TICKER_RE = re.compile(r"KXHIGH[A-Z]+-(\d{2})([A-Z]{3})(\d{2})-B(\d+(?:\.\d+)?)$")


def fetch_settled_b_tickers(series: str, since: date) -> list[dict]:
    """Page through settled markets for a series, return only B-prefix (1°F bin) markets
    closed on/after `since`."""
    out = []
    cursor = None
    while True:
        params = {"series_ticker": series, "status": "settled", "limit": 200}
        if cursor: params["cursor"] = cursor
        r = requests.get(f"{PROD}/markets", params=params, timeout=30)
        if r.status_code != 200: break
        data = r.json()
        markets = data.get("markets", [])
        if not markets: break
        for m in markets:
            t = m.get("ticker", "")
            mo = TICKER_RE.match(t)
            if not mo: continue
            yr, mn, dy, b = mo.groups()
            try:
                d = date(2000 + int(yr), MONTHS[mn], int(dy))
            except (KeyError, ValueError):
                continue
            if d < since: continue
            out.append({"ticker": t, "date": d, "bin_lo": float(b) - 0.5,
                        "bin_hi": float(b) + 0.5, "result": (m.get("result") or "").lower()})
        cursor = data.get("cursor")
        if not cursor: break
        # Stop early if oldest market in page predates `since` and pages are date-ordered desc.
        if all(o["date"] < since for o in out[-len(markets):]):
            break
        time.sleep(0.1)
    return out


def asos_daily_max_range(station: str, start: date, end: date, tz: str) -> dict[str, float]:
    r = requests.get(MESONET, params={
        "station": station, "data": "tmpf",
        "year1": start.year, "month1": start.month, "day1": start.day,
        "year2": end.year, "month2": end.month, "day2": end.day,
        "tz": tz, "format": "onlycomma", "missing": "M", "trace": "T",
        "report_type": 3}, timeout=120)
    out = defaultdict(list)
    for line in r.text.split("\n"):
        if not line or line.startswith("station"): continue
        p = line.split(",")
        if len(p) < 3: continue
        try: v = float(p[2])
        except: continue
        out[p[1].split(" ")[0]].append(v)
    return {d: max(vs) for d, vs in out.items()}


def main():
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=90)
    print(f"Window: {start} .. {end}  ({(end-start).days} days)")
    print()

    all_gaps = []
    per_city = {}
    for city, series in WEATHER_SERIES.items():
        print(f"--- {city} ({series}) ---", flush=True)
        try:
            b_markets = fetch_settled_b_tickers(series, start)
        except Exception as e:
            print(f"  fetch err: {e}"); continue
        # Group by date; find the single YES bin per day (CLI-reported max bin)
        per_day_yes = {}
        for m in b_markets:
            if m["result"] == "yes":
                per_day_yes[m["date"]] = m  # only one YES per day
        if not per_day_yes:
            print(f"  no settled YES B-bins found"); continue
        # ASOS in one call
        try:
            obs = asos_daily_max_range(STATIONS[city], start, end, CITY_TZ[city])
        except Exception as e:
            print(f"  ASOS err: {e}"); continue
        gaps = []
        for d, m in sorted(per_day_yes.items()):
            asos = obs.get(d.isoformat())
            if asos is None: continue
            cli_implied = (m["bin_lo"] + m["bin_hi"]) / 2.0  # CLI value rounds to bin
            gap = cli_implied - asos
            gaps.append((d, m["bin_lo"], m["bin_hi"], asos, gap))
        if not gaps:
            print(f"  no overlap"); continue
        diffs = [g[4] for g in gaps]
        n = len(diffs)
        mean = sum(diffs)/n
        absmean = sum(abs(x) for x in diffs)/n
        rmse = (sum(x*x for x in diffs)/n)**0.5
        ge1 = sum(1 for x in diffs if abs(x) >= 1.0) / n
        ge2 = sum(1 for x in diffs if abs(x) >= 2.0) / n
        per_city[city] = (n, mean, absmean, rmse, ge1, ge2)
        print(f"  n={n}  mean(CLI-ASOS)={mean:+.2f}°F  mean|gap|={absmean:.2f}°F  "
              f"RMSE={rmse:.2f}  %|gap|≥1°F={ge1:.0%}  %|gap|≥2°F={ge2:.0%}")
        all_gaps.extend(diffs)
        time.sleep(0.5)

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"{'City':<14} {'n':>4} {'bias':>8} {'MAE':>7} {'RMSE':>6} {'%≥1°F':>7} {'%≥2°F':>7}")
    for city, (n, mean, absmean, rmse, ge1, ge2) in sorted(per_city.items(),
                                                            key=lambda kv: -kv[1][2]):
        print(f"{city:<14} {n:>4} {mean:>+7.2f}°F {absmean:>6.2f}°F {rmse:>5.2f} "
              f"{ge1:>6.0%} {ge2:>6.0%}")
    if all_gaps:
        n = len(all_gaps)
        print(f"\nALL CITIES   n={n}  bias={sum(all_gaps)/n:+.2f}°F  "
              f"MAE={sum(abs(x) for x in all_gaps)/n:.2f}°F  "
              f"RMSE={(sum(x*x for x in all_gaps)/n)**0.5:.2f}°F  "
              f"%|gap|≥1°F={sum(1 for x in all_gaps if abs(x)>=1.0)/n:.0%}  "
              f"%|gap|≥2°F={sum(1 for x in all_gaps if abs(x)>=2.0)/n:.0%}")


if __name__ == "__main__":
    main()
