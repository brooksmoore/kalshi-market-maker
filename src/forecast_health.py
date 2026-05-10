"""
forecast_health.py — rolling per-city GFS-vs-ASOS skill monitor.

Computes 14-day MAE / bias / RMSE per city by comparing Open-Meteo's
historical GFS daily-max forecast against ASOS hourly observations from
the Iowa State Mesonet.  Writes results to data/forecast_health.json.

Called from main.py on startup and every FORECAST_HEALTH_REFRESH_HOURS
thereafter via a background daemon thread.  The dashboard reads the JSON
via /api/forecast_health — no shared in-process state.

Alert thresholds (will skip city's markets automatically when tripped):
  MAE  > 3.0°F
  |bias| > 2.0°F
  RMSE > 4.0°F
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import date, timedelta

import requests

from config import CITIES, CITY_TZ, FORECAST_HEALTH_FILE

# ── Thresholds ────────────────────────────────────────────────────────────────
MAE_ALERT   = 3.0   # °F
BIAS_ALERT  = 2.0   # °F absolute
RMSE_ALERT  = 4.0   # °F

WINDOW_DAYS = 14
REFRESH_HOURS = 24  # how often main.py re-runs the computation

# Iowa State Mesonet ASOS station IDs matching our Kalshi CLI stations.
ASOS_STATIONS: dict[str, str] = {
    "NYC":          "NYC",
    "Chicago":      "MDW",
    "Miami":        "MIA",
    "LA":           "LAX",
    "Austin":       "AUS",
    "Denver":       "DEN",
    "Philadelphia": "PHL",
    "Houston":      "IAH",
    "Boston":       "BOS",
    "Phoenix":      "PHX",
    "Dallas":       "DFW",
    "Seattle":      "SEA",
    "Atlanta":      "ATL",
    "SF":           "SFO",
    # Added 2026-05-02 from Kalshi series discovery. ICAO airport codes
    # without the 'K' prefix per the rest of this dict's convention.
    "Las Vegas":    "LAS",
    "San Antonio":  "SAT",
    "Oklahoma City":"OKC",
    "DC":           "DCA",
    "New Orleans":  "MSY",
}

HFCST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
MESONET_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


def _gfs_daily_max(city: str, start: date, end: date) -> dict[str, float]:
    """GFS day-1 forecast daily max (°F) for city, start..end in local tz."""
    c = CITIES[city]
    r = requests.get(HFCST_URL, params={
        "latitude": c["lat"], "longitude": c["lon"],
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
        "timezone": CITY_TZ.get(city, "America/New_York"),
        "models": "gfs_seamless",
    }, timeout=30)
    r.raise_for_status()
    d = r.json().get("daily", {})
    return dict(zip(d.get("time", []), d.get("temperature_2m_max", [])))


def _asos_daily_max(station: str, start: date, end: date, tz: str) -> dict[str, float]:
    """ASOS hourly tmpf daily max (°F) for station, start..end in local tz."""
    r = requests.get(MESONET_URL, params={
        "station": station, "data": "tmpf",
        "year1": start.year, "month1": start.month, "day1": start.day,
        "year2": end.year, "month2": end.month, "day2": end.day,
        "tz": tz, "format": "onlycomma", "missing": "M",
        "trace": "T", "report_type": 3,
    }, timeout=60)
    r.raise_for_status()
    daily: dict[str, list[float]] = defaultdict(list)
    for line in r.text.split("\n"):
        if not line or line.startswith("station"):
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        try:
            v = float(parts[2])
        except ValueError:
            continue
        day = parts[1].split(" ")[0]
        daily[day].append(v)
    return {day: max(vs) for day, vs in daily.items()}


def compute(window_days: int = WINDOW_DAYS) -> dict:
    """Return health dict for all cities; writes to FORECAST_HEALTH_FILE."""
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=window_days - 1)
    results: dict[str, dict] = {}
    alerts: list[str] = []

    for city in CITIES:
        station = ASOS_STATIONS.get(city)
        if not station:
            continue
        tz = CITY_TZ.get(city, "America/New_York")
        try:
            fc = _gfs_daily_max(city, start, end)
            obs = _asos_daily_max(station, start, end, tz)
            errs = []
            for d, f in fc.items():
                o = obs.get(d)
                if f is None or o is None:
                    continue
                errs.append(f - o)
            if not errs:
                continue
            # Trim the single worst day before computing MAE/RMSE/bias. With
            # a 14-day window, one extreme miss (e.g. unforecast frontal
            # passage) otherwise dominates RMSE for two weeks. Trimming makes
            # the metric robust to a single outlier while still catching
            # sustained drift across multiple days. Requires n>=4 to keep the
            # trimmed sample meaningful.
            n_raw = len(errs)
            trimmed_idx = None
            if n_raw >= 4:
                trimmed_idx = max(range(n_raw), key=lambda i: abs(errs[i]))
                errs_used = [e for i, e in enumerate(errs) if i != trimmed_idx]
            else:
                errs_used = errs
            n = len(errs_used)
            mae  = round(sum(abs(e) for e in errs_used) / n, 3)
            bias = round(sum(errs_used) / n, 3)
            rmse = round((sum(e * e for e in errs_used) / n) ** 0.5, 3)
            city_alerts = []
            if mae  > MAE_ALERT:   city_alerts.append(f"MAE {mae:.1f}>{MAE_ALERT}")
            if abs(bias) > BIAS_ALERT: city_alerts.append(f"|bias| {abs(bias):.1f}>{BIAS_ALERT}")
            if rmse > RMSE_ALERT:  city_alerts.append(f"RMSE {rmse:.1f}>{RMSE_ALERT}")
            results[city] = {
                "n": n, "n_raw": n_raw,
                "trimmed_max_err": (round(errs[trimmed_idx], 2)
                                    if trimmed_idx is not None else None),
                "mae": mae, "bias": bias, "rmse": rmse,
                "alert": bool(city_alerts),
                "alert_reasons": city_alerts,
            }
            if city_alerts:
                alerts.append(f"{city}: " + ", ".join(city_alerts))
            time.sleep(0.3)
        except Exception as e:
            logging.warning("[FHEALTH] %s: %s", city, e)
            results[city] = {"n": 0, "mae": None, "bias": None, "rmse": None,
                             "alert": False, "alert_reasons": [], "error": str(e)}

    payload = {
        "computed_at": date.today().isoformat(),
        "window_days": window_days,
        "mae_threshold": MAE_ALERT,
        "bias_threshold": BIAS_ALERT,
        "rmse_threshold": RMSE_ALERT,
        "global_alerts": alerts,
        "cities": results,
    }
    os.makedirs(os.path.dirname(FORECAST_HEALTH_FILE) or ".", exist_ok=True)
    with open(FORECAST_HEALTH_FILE, "w") as f:
        json.dump(payload, f, indent=2)
    if alerts:
        logging.warning("[FHEALTH] alerts: %s", " | ".join(alerts))
    else:
        logging.info("[FHEALTH] all %d cities within thresholds", len(results))
    return payload


def city_is_healthy(city: str) -> bool:
    """Fast path used by strategy.py: return False if city is in alert."""
    if not os.path.exists(FORECAST_HEALTH_FILE):
        return True  # fail-open if health file not yet computed
    try:
        with open(FORECAST_HEALTH_FILE) as f:
            data = json.load(f)
        return not data.get("cities", {}).get(city, {}).get("alert", False)
    except Exception:
        return True  # fail-open on corrupt file


def start_background_refresh() -> threading.Thread:
    """Start a daemon thread that calls compute() on startup and every 24h."""
    def _loop():
        while True:
            try:
                compute()
            except Exception as e:
                logging.warning("[FHEALTH] refresh failed: %s", e)
            time.sleep(REFRESH_HOURS * 3600)

    t = threading.Thread(target=_loop, name="forecast-health", daemon=True)
    t.start()
    logging.info("[FHEALTH] background refresh started (every %dh)", REFRESH_HOURS)
    return t
