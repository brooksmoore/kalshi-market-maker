"""
forecast.py — GEFS 31-member ensemble via Open-Meteo.

v1 used a single NOAA point forecast plus a hand-tuned Gaussian. The audit
(M1, M2) shows single-point forecasts are systematically overconfident and
cannot beat the marginal informed counterparty.

v2: fetch all 31 GEFS members (1 control + 30 perturbed) from Open-Meteo's
/v1/ensemble endpoint, then compute probabilities as the fraction of members
on each side of the bracket. No Gaussian, no LLM blend.

Addresses audit items:
  M1 — true ensemble dispersion
  M2 — ensemble is the forecast (no separate Gaussian)

2026-04-27 fixes (from cli_gap_audit.py / ensemble_audit.py):
  TZ    — per-city timezone instead of hardcoded America/New_York.
           Without this, Denver/Chicago/Seattle showed tail errors up to 5.5°F.
  BIAS  — additive CLI bias correction applied to all members before returning.
           NWS CLI (Kalshi settlement source) runs ~0.6–1.1°F warmer than the
           ASOS-aligned GFS grid; CLI_BIAS corrects so probabilities are
           anchored to the settlement value, not the model's native output.
  LAPLACE — Laplace smoothing (α=3.0) on all bin-fraction estimates.
           Replaces k/n with (k+3)/(n+6), giving ~8% floor on 0/31 and
           ~92% ceiling on 31/31 — matching the empirical 8% rate at which
           CLI settlements land ≥2 bins from the ASOS-implied bin.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import requests

from config import CITIES, CITY_TZ, CLI_BIAS, SPREAD_INFLATION_FACTOR

OPEN_METEO_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ENSEMBLE_MODEL = "gfs_seamless"  # Open-Meteo GEFS-seamless = 1 control + 30 members
EXPECTED_MEMBERS = 31

# Laplace smoothing alpha: (k + α) / (n + 2α).
# α=3.0 → 0/31 ≈ 8.1%,  31/31 ≈ 91.9%.  Chosen to match the empirical
# cross-bin settlement noise floor measured over 779 Kalshi production markets.
_LAPLACE_ALPHA = 3.0

_CACHE: dict[tuple[str, str], tuple[float, list[float]]] = {}
_CACHE_TTL = 3600.0  # 1 hour


def _cache_get(city: str, target: date) -> list[float] | None:
    key = (city, target.isoformat())
    hit = _CACHE.get(key)
    if hit is None:
        return None
    ts, members = hit
    if time.time() - ts < _CACHE_TTL:
        return members
    return None


def _cache_put(city: str, target: date, members: list[float]) -> None:
    _CACHE[(city, target.isoformat())] = (time.time(), members)


def get_ensemble_high(city: str, target_date: date) -> list[float]:
    """Return the 31 GEFS member forecasts for target_date's daily max temp (F).

    Returns an empty list on any failure — callers MUST treat that as a
    no-trade signal (fail-closed on forecast errors).
    """
    if city not in CITIES:
        logging.warning("[FORECAST] Unknown city %s", city)
        return []

    max_date = date.today() + timedelta(days=35)
    if target_date > max_date:
        logging.debug("[FORECAST] %s %s beyond forecast horizon (%s) — skipping", city, target_date, max_date)
        return []

    cached = _cache_get(city, target_date)
    if cached is not None:
        return cached

    coords = CITIES[city]
    params = {
        "latitude": coords["lat"],
        "longitude": coords["lon"],
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": CITY_TZ.get(city, "America/New_York"),
        "models": ENSEMBLE_MODEL,
        "start_date": target_date.isoformat(),
        "end_date": target_date.isoformat(),
    }

    try:
        resp = requests.get(OPEN_METEO_URL, params=params, timeout=20)
        if resp.status_code != 200:
            logging.warning("[FORECAST] %d for %s %s: %s", resp.status_code, city, target_date, resp.text[:120])
            return []
        data = resp.json()
    except Exception as e:
        logging.warning("[FORECAST] Error fetching %s %s: %s", city, target_date, e)
        return []

    daily = data.get("daily", {}) or {}
    # Control run lives under 'temperature_2m_max'; perturbed runs under
    # 'temperature_2m_max_member01' ... 'temperature_2m_max_member30'.
    members: list[float] = []
    control = daily.get("temperature_2m_max")
    if control and control[0] is not None:
        members.append(float(control[0]))
    for i in range(1, 31):
        key = f"temperature_2m_max_member{i:02d}"
        arr = daily.get(key)
        if arr and arr[0] is not None:
            members.append(float(arr[0]))

    if not members:
        logging.warning("[FORECAST] Empty ensemble for %s %s", city, target_date)
        return []

    if len(members) != EXPECTED_MEMBERS:
        # Open-Meteo occasionally drops a member; warn but still use what we got
        # provided there is a credible majority.
        logging.warning("[FORECAST] %s %s: got %d members (expected %d)",
                        city, target_date, len(members), EXPECTED_MEMBERS)
        if len(members) < 20:
            return []

    # Apply NWS CLI bias correction: shift all members up by the city-specific
    # offset so probabilities are anchored to the settlement value, not the
    # raw GFS grid output.  Bias measured over 779 Kalshi production settlements
    # (cli_gap_audit.py, 2026-04-27); refreshed quarterly.
    bias = CLI_BIAS.get(city, 0.0)
    if bias:
        members = [m + bias for m in members]
        logging.debug("[FORECAST] %s: applied CLI bias +%.2f°F to %d members",
                      city, bias, len(members))

    # Ensemble spread inflation (shipped 2026-05-17, see config.SPREAD_INFLATION_FACTOR
    # for measurement rationale). Inflates each member radially around the
    # ensemble mean to correct for GEFS under-dispersion. Mathematically:
    # the bracket probabilities downstream now reflect the realized forecast
    # error variance (~2.23°F) rather than GEFS's artificially tight spread
    # (~1.43°F). Cheap, principled, addresses the dominant v2 failure mode.
    if SPREAD_INFLATION_FACTOR != 1.0 and len(members) >= 2:
        ens_mean = sum(members) / len(members)
        members = [
            ens_mean + (m - ens_mean) * SPREAD_INFLATION_FACTOR
            for m in members
        ]
        logging.debug("[FORECAST] %s: inflated ensemble spread by %.2fx (n=%d)",
                      city, SPREAD_INFLATION_FACTOR, len(members))

    _cache_put(city, target_date, members)
    return members


def _laplace(k: int, n: int) -> float:
    """Laplace-smoothed fraction: (k + α) / (n + 2α)."""
    return (k + _LAPLACE_ALPHA) / (n + 2 * _LAPLACE_ALPHA)


def probability_above(members: list[float], threshold: float) -> float:
    if not members:
        return 0.0
    return _laplace(sum(1 for m in members if m >= threshold), len(members))


def probability_below(members: list[float], threshold: float) -> float:
    if not members:
        return 0.0
    return _laplace(sum(1 for m in members if m < threshold), len(members))


def probability_between(members: list[float], low: float, high: float) -> float:
    if not members:
        return 0.0
    return _laplace(sum(1 for m in members if low <= m <= high), len(members))
