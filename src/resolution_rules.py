"""
resolution_rules.py — canonicalize venue-specific market metadata.

Each venue describes its weather markets differently. The strategy + arb code
needs a uniform shape so it can:
  - decide whether two markets across venues resolve on the *same* underlying
    event (required for arbitrage)
  - apply the GEFS forecast probability to either venue
  - grade trades against the venue's own settlement source (never our forecast)

This module is the single place that translates raw market payloads to the
canonical `(resolution_source, threshold, comparator, range_low, range_high)`
tuple. If a payload can't be canonicalized, we return None and the market is
silently dropped — better to ignore than mis-grade.

Canonical resolution_source format:
  "<authority>:<station_id>:<observable>"

Examples:
  "NWS:KNYC:daily_high"        — NWS Central Park station, daily high temperature
  "NWS:KORD:daily_high"        — NWS O'Hare
  "NWS:KMDW:daily_high"        — NWS Midway (Kalshi uses Midway for Chicago)

Two markets across venues match iff their canonical tuples are equal.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

_MONTH_MAP = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

_DATE_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
    r"\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?",
    re.IGNORECASE,
)


def parse_resolution_date(text: str, today: date | None = None) -> date | None:
    """Extract the resolution date from a market title or question.

    Honors an explicit year if present ('May 3, 2026'); otherwise infers
    the current year, bumping forward by one if the implied date is past.

    Used by cross_venue.py and strategy.py — never fall back to close_time
    as a date proxy: for cities west of UTC, an evening-local close rolls
    into the next UTC day, which yesterday's KLAX false-positive arb
    proved is a real correctness bug.
    """
    if not text:
        return None
    today = today or date.today()
    m = _DATE_RE.search(text)
    if not m:
        return None
    month = _MONTH_MAP[m.group(1).title()[:3]]
    day = int(m.group(2))
    year_explicit = m.group(3)
    try:
        if year_explicit:
            return date(int(year_explicit), month, day)
        target = date(today.year, month, day)
        if target < today:
            target = date(today.year + 1, month, day)
        return target
    except ValueError:
        return None

# City -> NWS station ID used by Kalshi for resolution. Must match the
# settlement source the venue actually uses; the original 14 are derived
# from v2's existing CITIES dict in config.py. The remainder were added
# when dynamic series discovery (kalshi_client.discover_weather_series)
# surfaced new daily-high markets Kalshi has listed but v1 never picked up.
# Adding a city here is one of three coordinated edits needed to onboard
# a new market: (a) here, (b) config.CITIES + CITY_TZ + CLI_BIAS,
# (c) forecast_health.ASOS_STATIONS. Future improvement: auto-geocode and
# eliminate (b) and (c).
KALSHI_CITY_STATION: dict[str, str] = {
    # Original 14 (v2 baseline)
    "NYC":          "KNYC",
    "Chicago":      "KMDW",
    "Miami":        "KMIA",
    "LA":           "KLAX",
    "Austin":       "KAUS",
    "Denver":       "KDEN",
    "Philadelphia": "KPHL",
    "Houston":      "KHOU",
    "Boston":       "KBOS",
    "Phoenix":      "KPHX",
    "Dallas":       "KDFW",
    "Seattle":      "KSEA",
    "Atlanta":      "KATL",
    "SF":           "KSFO",
    # Added 2026-05-02 from Kalshi series discovery — daily highs Kalshi
    # already lists that v1's hardcoded WEATHER_SERIES omitted.
    "Las Vegas":    "KLAS",
    "San Antonio":  "KSAT",
    "Oklahoma City":"KOKC",
    "DC":           "KDCA",
    "New Orleans":  "KMSY",
}


_BRACKET_ABOVE = re.compile(r">\s*(\d+(?:\.\d+)?)")
_BRACKET_BELOW = re.compile(r"<\s*(\d+(?:\.\d+)?)")
_BRACKET_BETWEEN = re.compile(r"(\d+(?:\.\d+)?)\s*[–-]\s*(\d+(?:\.\d+)?)")


def parse_bracket_from_title(title: str
) -> tuple[str, float | None, float | None] | None:
    """(kind, low, high) where kind is 'above' | 'below' | 'between'.

    Same shape returned by strategy.parse_bracket(); duplicated here so the
    canonicalizer doesn't depend on strategy.py (avoids import cycles when
    canonicalization runs inside venue clients).
    """
    above = _BRACKET_ABOVE.search(title)
    below = _BRACKET_BELOW.search(title)
    between = _BRACKET_BETWEEN.search(title)
    if above:
        return ("above", float(above.group(1)), None)
    if below:
        return ("below", None, float(below.group(1)))
    if between:
        return ("between", float(between.group(1)), float(between.group(2)))
    return None


def canonicalize_kalshi_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Add resolution_source / threshold / comparator / range_* to a Kalshi
    market dict. Returns the dict (mutated copy) or None if uncanonicalizable.

    Input is the raw dict returned by kalshi_client.get_all_weather_markets()
    — already tagged with `city` and `market_type`.
    """
    city = raw.get("city")
    title = raw.get("title", "")
    if not city or city not in KALSHI_CITY_STATION:
        return None
    bracket = parse_bracket_from_title(title)
    if bracket is None:
        return None

    kind, low, high = bracket
    out = dict(raw)
    out["resolution_source"] = f"NWS:{KALSHI_CITY_STATION[city]}:daily_high"
    if kind == "above":
        out["comparator"] = ">="
        out["threshold"] = float(low) if low is not None else None
        out["range_low"] = None
        out["range_high"] = None
    elif kind == "below":
        out["comparator"] = "<"
        out["threshold"] = float(high) if high is not None else None
        out["range_low"] = None
        out["range_high"] = None
    elif kind == "between":
        out["comparator"] = "in_range"
        out["threshold"] = None
        out["range_low"] = float(low) if low is not None else None
        out["range_high"] = float(high) if high is not None else None
    else:
        return None
    return out


# Word-boundary city patterns. Each entry is (compiled regex, city). The
# regex must match in a question that has ALREADY been confirmed weather-
# related (see _is_weather_question) — relying on weather context first
# avoids false matches like " la " inside "La Liga". Multi-word forms come
# first so "new york" beats a bare "york" anywhere else.
#
# Also used by derive_city_from_kalshi_series_title() — a new entry here
# is the regex half of onboarding a new city from Kalshi discovery.
_CITY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bnew york\b",      re.IGNORECASE), "NYC"),
    (re.compile(r"\blos angeles\b",   re.IGNORECASE), "LA"),
    (re.compile(r"\bsan francisco\b", re.IGNORECASE), "SF"),
    (re.compile(r"\bsan antonio\b",   re.IGNORECASE), "San Antonio"),
    (re.compile(r"\boklahoma city\b", re.IGNORECASE), "Oklahoma City"),
    (re.compile(r"\bnew orleans\b",   re.IGNORECASE), "New Orleans"),
    (re.compile(r"\blas vegas\b",     re.IGNORECASE), "Las Vegas"),
    (re.compile(r"\bwashington(?:\s+d\.?c\.?)?\b", re.IGNORECASE), "DC"),
    (re.compile(r"\bphiladelphia\b",  re.IGNORECASE), "Philadelphia"),
    (re.compile(r"\bphoenix\b",       re.IGNORECASE), "Phoenix"),
    (re.compile(r"\bseattle\b",       re.IGNORECASE), "Seattle"),
    (re.compile(r"\batlanta\b",       re.IGNORECASE), "Atlanta"),
    (re.compile(r"\bhouston\b",       re.IGNORECASE), "Houston"),
    (re.compile(r"\bchicago\b",       re.IGNORECASE), "Chicago"),
    (re.compile(r"\bdenver\b",        re.IGNORECASE), "Denver"),
    (re.compile(r"\bboston\b",        re.IGNORECASE), "Boston"),
    (re.compile(r"\baustin\b",        re.IGNORECASE), "Austin"),
    (re.compile(r"\bdallas\b",        re.IGNORECASE), "Dallas"),
    (re.compile(r"\bmiami\b",         re.IGNORECASE), "Miami"),
    (re.compile(r"\bnyc\b",           re.IGNORECASE), "NYC"),
    (re.compile(r"\bd\.?c\.?\b",      re.IGNORECASE), "DC"),
    (re.compile(r"\bla\b",            re.IGNORECASE), "LA"),
    (re.compile(r"\bsf\b",            re.IGNORECASE), "SF"),
]


# Series-title patterns that indicate a daily HIGH temperature market.
# Used to filter Kalshi's Climate-and-Weather series to the subset v2 trades.
_DAILY_HIGH_TITLE = re.compile(
    r"\b(?:high(?:est)?|max(?:imum)?)\b.*\b(?:temp(?:erature)?)\b|"
    r"\b(?:temp(?:erature)?)\b.*\b(?:high(?:est)?|max)\b",
    re.IGNORECASE,
)
# Series titles / tickers we explicitly exclude. v2 only handles
# per-city daily highs.
_EXCLUDE_SERIES_TITLE = re.compile(
    r"\bhourly\b|\blow(?:est)?\b|\bmin(?:imum)?\b|\bmonthly\b|"
    r"\baverage\b|\bavg\b|\brange\b|\bwater\b|\blake\b|\bglobal\b|"
    r"\bdirectional\b|\binflation\b|\bunited states\b|\bus\b",
    re.IGNORECASE,
)
_EXCLUDE_SERIES_TICKER = re.compile(
    r"LOW|MIN|MONTH|HOUR|RANGE|AVG|WATER|GLOBAL|INFLATION",
    re.IGNORECASE,
)


def is_kalshi_daily_high_series(ticker: str, title: str) -> bool:
    """True if the series is a per-city daily-high market this bot trades."""
    if _EXCLUDE_SERIES_TICKER.search(ticker or ""):
        return False
    if _EXCLUDE_SERIES_TITLE.search(title or ""):
        return False
    if not _DAILY_HIGH_TITLE.search(title or ""):
        return False
    return True


def derive_city_from_kalshi_series(ticker: str, title: str) -> str | None:
    """Return the canonical city key for a Kalshi daily-high series,
    or None if the city isn't in our supported set.

    Same pattern matching used for Polymarket questions; reuses
    _CITY_PATTERNS so a city added once works for both venues.
    """
    for pat, city in _CITY_PATTERNS:
        if pat.search(title or ""):
            return city
    return None

# A question only counts as a weather market if it mentions a temperature
# unit OR the word "temperature" / "high"+"reach" pattern. This is the
# first-line filter that keeps "La Liga" / "Lakers" / sports markets out.
_WEATHER_SIGNAL = re.compile(
    r"(?:°\s*F\b|°\s*C\b|\bfahrenheit\b|\bcelsius\b|\btemperature\b|"
    r"\bdegrees?\s+f\b|\bdegrees?\s+c\b|\bhigh\s+temp(?:erature)?\b|"
    r"\bweather\b|\brainfall\b|\bsnowfall\b)",
    re.IGNORECASE,
)

# v2 only handles HIGH temperature markets. Drop anything that explicitly
# asks about a low / minimum / overnight temperature — settling these as
# high-temp markets would mis-grade every trade.
_LOW_TEMP_SIGNAL = re.compile(
    r"\b(?:lowest|low|minimum|min|overnight|coldest)\s+(?:temp|temperature)\b",
    re.IGNORECASE,
)


def _city_from_question(question: str) -> str | None:
    for pat, city in _CITY_PATTERNS:
        if pat.search(question):
            return city
    return None


def _is_weather_question(question: str) -> bool:
    return bool(_WEATHER_SIGNAL.search(question))


# Threshold patterns: each REQUIRES a temperature-unit anchor (°F, F, degrees)
# adjacent to the number. A bare "75" or a range like "2025-26" without a
# unit is rejected. This is a deliberate trade-off: we miss some real
# weather markets that omit the unit, but we never false-positive on years,
# scores, or other numeric ranges that aren't temperatures.
_TEMP_UNIT = r"(?:°\s*F|°\s*C|\bF\b|\bC\b|\sdeg(?:rees)?\b|\sfahrenheit\b|\scelsius\b)"

# `>=` patterns. The "<NUMBER><UNIT> or higher" form is Polymarket's open-
# ended-above format ("Will the highest temperature in Atlanta be 92°F or
# higher on May 5?"). It's the right shape for a tradeable single-side bet
# — wider than a 1°F bin, gateable by our forecast.
_THRESHOLD_GTE = re.compile(
    rf"(?:reach|above|at least|over|hit|exceed)\s*(\d+(?:\.\d+)?)\s*{_TEMP_UNIT}|"
    rf"(\d+(?:\.\d+)?)\s*{_TEMP_UNIT}\s*(?:or\s+(?:higher|above|more|greater))",
    re.IGNORECASE,
)
# `<` patterns — symmetrically include the "<NUMBER><UNIT> or below" form.
_THRESHOLD_LT = re.compile(
    rf"(?:below|under|less than|at most|stay below)\s*(\d+(?:\.\d+)?)\s*{_TEMP_UNIT}|"
    rf"(\d+(?:\.\d+)?)\s*{_TEMP_UNIT}\s*(?:or\s+(?:below|less|lower|under))",
    re.IGNORECASE,
)
_THRESHOLD_RANGE = re.compile(
    rf"(\d+(?:\.\d+)?)\s*(?:to|-|–|—)\s*(\d+(?:\.\d+)?)\s*{_TEMP_UNIT}",
    re.IGNORECASE,
)


def canonicalize_polymarket_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Translate a Polymarket market payload to the canonical shape.

    Two-stage filter:
      1. Question must mention a temperature unit / weather keyword. Without
         this, "La Liga" / "Lakers" / sports markets falsely matched in the
         first phase-1 ingest.
      2. Threshold must be anchored to a temperature unit (°F / F / degrees).
         A bare "75" or "2025-26" is rejected.

    Returns None on any uncertainty — better to drop a real weather market
    than mis-canonicalize a non-weather one and grade ourselves against the
    wrong oracle outcome.
    """
    question = raw.get("question") or raw.get("title") or ""
    if not question or not _is_weather_question(question):
        return None
    if _LOW_TEMP_SIGNAL.search(question):
        # v2 only trades daily highs. Low-temp markets resolve on a different
        # observable (overnight min) and would be mis-graded as high_temp.
        return None
    city = _city_from_question(question)
    if not city:
        return None

    # Range check first — "70 to 75°F" should be a range, not a >=70 threshold.
    range_match = _THRESHOLD_RANGE.search(question)
    threshold_match = _THRESHOLD_GTE.search(question)
    below_match = _THRESHOLD_LT.search(question)

    out = dict(raw)
    out["city"] = city
    out["market_type"] = "high_temp"
    out["resolution_source"] = f"NWS:{KALSHI_CITY_STATION[city]}:daily_high"

    if range_match:
        out["comparator"] = "in_range"
        out["threshold"] = None
        out["range_low"] = float(range_match.group(1))
        out["range_high"] = float(range_match.group(2))
    elif threshold_match:
        # Two alternation forms: "reach 75°F" → group(1); "75°F or higher"
        # → group(2). Take whichever matched.
        thr = threshold_match.group(1) or threshold_match.group(2)
        out["comparator"] = ">="
        out["threshold"] = float(thr)
        out["range_low"] = None
        out["range_high"] = None
    elif below_match:
        thr = below_match.group(1) or below_match.group(2)
        out["comparator"] = "<"
        out["threshold"] = float(thr)
        out["range_low"] = None
        out["range_high"] = None
    else:
        logging.debug(
            "[CANON] Polymarket market dropped — couldn't parse threshold from %r",
            question[:120],
        )
        return None

    return out


def markets_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True iff two canonicalized markets resolve on the same underlying event.

    Used by phase 2's cross-venue arb scanner. Equality is exact on the
    canonical tuple — close-but-not-equal thresholds (e.g. Kalshi 75 vs
    Polymarket 76) are NOT a match. The 1°F-bin trap from §4.2 applies
    across venues too: if the rules differ, the trades are independent
    bets on different events even when they look similar.
    """
    keys = ("resolution_source", "comparator", "threshold",
            "range_low", "range_high", "target_settlement")
    return all(a.get(k) == b.get(k) for k in keys)
