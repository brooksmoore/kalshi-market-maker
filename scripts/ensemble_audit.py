"""
Ensemble audit: forecast-vs-observed bias by city, plus timezone-fix diff
and a focused look at the Apr 25 LA loss day.

Uses Open-Meteo:
  - archive-api.open-meteo.com  (ERA5 reanalysis -> observed daily max)
  - historical-forecast-api.open-meteo.com  (what the model forecast at lead T-1)
  - ensemble-api.open-meteo.com  (live ensemble; for TZ-fix probe)

No external deps beyond `requests`.
"""
from __future__ import annotations
import sys, time, statistics
from datetime import date, timedelta
sys.path.insert(0, "src")
import requests
from config import CITIES

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HIST_FCST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

CITY_TZ = {
    "NYC": "America/New_York", "Chicago": "America/Chicago", "Miami": "America/New_York",
    "LA": "America/Los_Angeles", "Austin": "America/Chicago", "Denver": "America/Denver",
    "Philadelphia": "America/New_York", "Houston": "America/Chicago",
    "Boston": "America/New_York", "Phoenix": "America/Phoenix", "Dallas": "America/Chicago",
    "Seattle": "America/Los_Angeles", "Atlanta": "America/New_York",
    "SF": "America/Los_Angeles",
}


def get_observed_max(city: str, start: date, end: date, tz: str) -> dict[str, float]:
    """ERA5 observed daily max temp in F for [start, end]."""
    c = CITIES[city]
    r = requests.get(ARCHIVE_URL, params={
        "latitude": c["lat"], "longitude": c["lon"],
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
        "timezone": tz,
    }, timeout=30)
    r.raise_for_status()
    d = r.json().get("daily", {})
    return dict(zip(d.get("time", []), d.get("temperature_2m_max", [])))


def get_historical_forecast(city: str, start: date, end: date, tz: str) -> dict[str, float]:
    """GFS daily max as forecast on the prior day (T-1 lead)."""
    c = CITIES[city]
    r = requests.get(HIST_FCST_URL, params={
        "latitude": c["lat"], "longitude": c["lon"],
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
        "timezone": tz, "models": "gfs_seamless",
    }, timeout=30)
    r.raise_for_status()
    d = r.json().get("daily", {})
    return dict(zip(d.get("time", []), d.get("temperature_2m_max", [])))


def bias_stats(forecast_map, observed_map):
    """Return (n, mean_bias, mae, rmse, pct_under_2F, pct_under_4F, errors)
    where bias = forecast - observed (positive = forecast too warm)."""
    errs = []
    for d, f in forecast_map.items():
        o = observed_map.get(d)
        if f is None or o is None:
            continue
        errs.append(f - o)
    if not errs:
        return None
    n = len(errs)
    mean = sum(errs) / n
    mae = sum(abs(e) for e in errs) / n
    rmse = (sum(e * e for e in errs) / n) ** 0.5
    under2 = sum(1 for e in errs if e < -2) / n
    under4 = sum(1 for e in errs if e < -4) / n
    return n, mean, mae, rmse, under2, under4, errs


def step1_la_backtest():
    print("\n" + "=" * 70)
    print("STEP 1: LA backtest — GFS forecast vs ERA5 observed, last 60 days")
    print("=" * 70)
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=60)
    obs = get_observed_max("LA", start, end, "America/Los_Angeles")
    fcst_la_tz = get_historical_forecast("LA", start, end, "America/Los_Angeles")
    fcst_ny_tz = get_historical_forecast("LA", start, end, "America/New_York")
    print(f"Days: {start} .. {end}  observed={len(obs)} forecast={len(fcst_la_tz)}")
    for label, fc in [("LA-tz (correct)", fcst_la_tz), ("NY-tz (current bug)", fcst_ny_tz)]:
        s = bias_stats(fc, obs)
        if s:
            n, mean, mae, rmse, u2, u4, _ = s
            print(f"  {label:22s} n={n} bias(F-O)={mean:+.2f}°F MAE={mae:.2f} "
                  f"RMSE={rmse:.2f} under-by-2°F={u2:.0%} under-by-4°F={u4:.0%}")


def step2_all_cities():
    print("\n" + "=" * 70)
    print("STEP 2: per-city bias (60-day, correct local TZ)")
    print("=" * 70)
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=60)
    print(f"{'City':<14} {'n':>3} {'bias':>8} {'MAE':>6} {'RMSE':>6} {'%under2F':>9} {'%under4F':>9}")
    for city in CITIES:
        tz = CITY_TZ[city]
        try:
            obs = get_observed_max(city, start, end, tz)
            fc = get_historical_forecast(city, start, end, tz)
            s = bias_stats(fc, obs)
            if s:
                n, mean, mae, rmse, u2, u4, _ = s
                print(f"{city:<14} {n:>3} {mean:>+7.2f}°F {mae:>5.2f} {rmse:>5.2f} "
                      f"{u2:>8.0%} {u4:>8.0%}")
            time.sleep(0.5)
        except Exception as e:
            print(f"{city:<14} ERROR: {e}")


def step3_tz_fix_live():
    print("\n" + "=" * 70)
    print("STEP 3: live ensemble — NY-tz vs LA-tz on the same target dates")
    print("=" * 70)
    targets = [date.today() + timedelta(days=i) for i in range(1, 6)]
    for city in ["LA", "SF", "Seattle", "Phoenix", "Denver", "Chicago", "Dallas", "Houston"]:
        c = CITIES[city]
        for tz_label, tz in [("NY", "America/New_York"), ("local", CITY_TZ[city])]:
            r = requests.get(ENSEMBLE_URL, params={
                "latitude": c["lat"], "longitude": c["lon"],
                "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
                "timezone": tz, "models": "gfs_seamless",
                "start_date": targets[0].isoformat(), "end_date": targets[-1].isoformat(),
            }, timeout=20)
            d = r.json().get("daily", {})
            highs = d.get("temperature_2m_max", [])
            print(f"  {city:<10} tz={tz_label:<6} dailies={[f'{h:.1f}' if h is not None else 'na' for h in highs]}")
            time.sleep(0.3)


def step4_apr25_la():
    print("\n" + "=" * 70)
    print("STEP 4: focused look — LA Apr 25 2026 (lost trade)")
    print("=" * 70)
    target = date(2026, 4, 25)
    obs = get_observed_max("LA", target, target, "America/Los_Angeles")
    fc_la = get_historical_forecast("LA", target, target, "America/Los_Angeles")
    fc_ny = get_historical_forecast("LA", target, target, "America/New_York")
    obs_ny = get_observed_max("LA", target, target, "America/New_York")
    print(f"  Observed (LA-tz, ERA5):  {obs.get(target.isoformat())}°F")
    print(f"  Observed (NY-tz, ERA5):  {obs_ny.get(target.isoformat())}°F")
    print(f"  GFS forecast (LA-tz):    {fc_la.get(target.isoformat())}°F")
    print(f"  GFS forecast (NY-tz):    {fc_ny.get(target.isoformat())}°F")
    print(f"  Trade: BUY NO @ $0.76 on B66.5  ->  bot lost ($-2.32 each on 3 trades)")
    print(f"  ensemble_p was 0.0 (0/31 members ≥66.5°F per the bot's NY-tz fetch)")


if __name__ == "__main__":
    step1_la_backtest()
    step2_all_cities()
    step3_tz_fix_live()
    step4_apr25_la()
