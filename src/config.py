"""
config.py — flat configuration for kalshi_bot_2.0.

Single source of truth for all tunables. The constitution's pre-trade gate
(evaluate_trade) is folded in at the bottom — no separate constitution.py.

Addresses audit items:
  M7 — exact Kalshi per-contract fee formula (kalshi_trade_fee)
  M9 — portfolio Kelly cap (PORTFOLIO_KELLY_CAP)
  M10 — per-settlement-day correlation bucket (CORRELATION_BUCKET_CAP)
  R5 — fail-closed on stale bankroll (BANKROLL_STALE_SECONDS)
  B4 — arbitrage min-edge and per-leg validation (MIN_EDGE_ARB, ARB_MIN_LEGS)
"""

from __future__ import annotations

import math as _math
import os
from pathlib import Path

# Load .env BEFORE any os.getenv() calls. config.py is the first module many
# others import, so .env MUST be parsed here — not in kalshi_client.py — or
# every os.getenv() below silently falls back to its default.
try:
    from dotenv import load_dotenv
    _ROOT = Path(__file__).resolve().parents[1]
    load_dotenv(_ROOT / ".env")
except ImportError:
    # python-dotenv optional for test environments; shell env vars still work.
    pass

# ─── Environment ─────────────────────────────────────────────────────────────
# Demo by default. Flip KALSHI_API_URL in .env to move to production.
KALSHI_API_URL: str = os.getenv(
    "KALSHI_API_URL",
    "https://demo-api.kalshi.co/trade-api/v2",
)

# Trading is HALTED on boot. Flip LIVE_TRADING_ENABLED=true in .env when ready.
LIVE_TRADING_ENABLED: bool = (
    os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
)

# Claude veto filter (single YES/NO call per candidate). Never touches probability.
CLAUDE_VETO_ENABLED: bool = (
    os.getenv("CLAUDE_VETO_ENABLED", "false").lower() == "true"
)

# ─── Bankroll & sizing ───────────────────────────────────────────────────────
STARTING_BANKROLL: float = 100.0     # $100 demo paper
# 2026-05-10: lowered from 0.25 → 0.10 ahead of prod transition. At 0.25
# the 2.5% per-trade cap was binding on almost every positive-edge trade,
# lumping all bets at the cap and erasing Kelly's relative-conviction
# signal (high-edge trades = same size as low-edge trades). 0.10 lets
# the cap relax for mid-distribution trades while still binding for the
# most aggressive ones, preserving position-weighted variance for the
# calibration-cohort analysis. Also smaller avg trade → more attempts
# within the daily loss limit before halt. REVISIT (raise back toward
# 0.25 or higher) once calibration is fit and edge is empirically
# established on prod — at that point larger sizing = more profit and
# this throttle becomes a tax on proven edge.
KELLY_FRACTION: float = 0.10
# 2026-05-11: reverted 2.5% → 5%. The 2.5% tightening (made 5/10 ahead of
# prod flip) created a structural conflict at small bankroll: Kalshi's
# 1-contract granularity means the minimum real-world trade is ~$0.30-0.70,
# which at sub-$30 bankroll silently exceeds 2.5%. 5% accommodates the
# contract-granularity floor at any bankroll ≥ $10 while remaining bounded
# by daily-loss (15%) and drawdown (25%) caps. Per-trade blast radius at
# $200 bankroll = $10, at $15 = $0.78. Revisit if first cohort shows
# any single-trade-driven drawdown event.
MAX_SINGLE_BET_PCT: float = 0.05     # hard 5% per-trade cap
# MIN_POSITION lowered 2026-05-11 from $1.00 → $0.50 so the absolute floor
# never overrides the per-trade % cap at small bankroll. $0.50 ≈ 1 contract
# at typical $0.30-0.70 price. Executor's max(1, int(size/price)) is the
# real granularity floor anyway.
MIN_POSITION: float = 0.50

# Portfolio-level Kelly cap — audit M9.
# Sum of open kellys + any proposed new kelly must not exceed bankroll * this.
PORTFOLIO_KELLY_CAP: float = 0.90

# Settlement-day cluster cap — audit M10.
# Sum of positions settling within a cluster window must not exceed bankroll * this.
CORRELATION_BUCKET_CAP: float = 0.50
CLUSTER_WINDOW_HOURS: int = 6

# ─── Edge thresholds ─────────────────────────────────────────────────────────
MIN_EDGE: float = 0.08               # weather core
MIN_EDGE_ARB: float = 0.02           # fee-inclusive arb edge floor
ARB_MIN_LEGS: int = 3

# ─── Phase A gates (shipped 2026-05-23 after Phase 0 validation) ─────────────
#
# Empirical findings from n=1186 settled shadow signals over ~234h:
#
# - BUY YES is structurally broken in our model's high-confidence regime.
#   At cal_p ∈ [0.5, 1.0], actual yes-rate is 10-25% (deeply inverted).
#   Spread inflation (1.55×) reduced but didn't eliminate this. The
#   existing strategy.py guard was `cal_p >= 0.85`; data shows the
#   failure regime starts at 0.5+. Tightened cap from 0.85 to 0.60.
#
# - BUY NO is EV-positive in a NARROW band of cal_p. Stratified analysis:
#     cal_p ∈ [0.05, 0.10): 57% wins, no_ask~0.66 → EV −$0.09
#     cal_p ∈ [0.10, 0.15): 67% wins, no_ask~0.69 → EV −$0.02
#     cal_p ∈ [0.15, 0.20): 70% wins, no_ask~0.62 → EV +$0.08  ← sweet spot
#     cal_p ∈ [0.20, 0.25): 65% wins, no_ask~0.60 → EV +$0.05  ← sweet spot
#     cal_p ∈ [0.25, 0.30): 43% wins, no_ask~0.57 → EV −$0.14
#   New: restrict BUY NO to cal_p ∈ [BUY_NO_CAL_P_LO, BUY_NO_CAL_P_HI).
#
# - Phoenix and Las Vegas have a hyper-sharp market (T-ticker market Brier
#   0.003) AND model is most decisive there (cal_p_sd 0.25+, confident-
#   wrong 31-35% of predictions). BUY-YES catastrophic (0/15 across PHX+LV).
#   Excluded from trading universe entirely (still observed for shadow data).
#
# Path 1 retroactive validation (n=70 trades over 234h):
#   ALL: 53% win rate, EV +$0.034/contract, cohort total +$2.39
#   BUY_YES: 14% wins, EV +$0.078/contract (positive despite low rate)
#   BUY_NO:  62% wins, EV +$0.023/contract
# Honest CI on +$2.39 cohort total is wide (~−$2 to +$7 at 90%). Point
# estimate supports the hypothesis; real fills confirm.

# BUY YES disabled entirely as of 2026-05-28. Shadow calibration at n=1,824
# (settled outcomes, not synthetic) showed the model's predictions in the
# BUY-YES-eligible region [0.40, 0.60) have actual P(yes) ≈ 0.30 — i.e. the
# model is anti-informative there. Market Brier on this cohort is 0.066 vs
# model 0.353; the market dominates and there is no BUY YES sub-cohort where
# the model beats the market. Paper BUY YES record was 0/2. Setting the cap
# to 0.0 hard-filters every BUY YES (calibrated_p > 0.0 is always true).
# Keep BUY_YES_ENABLED so re-enabling is a one-line flip if the premise changes.
BUY_YES_ENABLED: bool = False
BUY_YES_CAL_P_CAP: float = 0.60      # ignored while BUY_YES_ENABLED is False
BUY_NO_CAL_P_LO: float = 0.15
BUY_NO_CAL_P_HI: float = 0.30
EXCLUDE_CITIES: frozenset[str] = frozenset({"Phoenix", "Las Vegas"})
# Watch list (negative EV in shadow but tiny n=3-4): "Oklahoma City",
# "New Orleans", "Philadelphia". Don't exclude pre-emptively; add to
# EXCLUDE_CITIES if real-fill EV confirms negative over n≥10.

# ─── Price filters ───────────────────────────────────────────────────────────
MIN_PRICE: float = 0.15
MAX_PRICE: float = 0.90
THIN_MARKET_LO: float = 0.03
THIN_MARKET_HI: float = 0.97

# ─── Execution ───────────────────────────────────────────────────────────────
MAKER_REST_SECONDS: int = 180
MAKER_PRICE_OFFSET_CENTS: int = 1
SLIPPAGE_BUFFER_PCT: float = 0.02

# ─── Scan cadence ────────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS: int = 300

# ─── Halts ───────────────────────────────────────────────────────────────────
# 2026-05-10: tightened DRAWDOWN 33→25% and DAILY 20→15% ahead of prod
# transition. Demo-era values assumed losses didn't matter; prod values
# need to fire well before the proof-of-concept cohort burns through too
# much of the small initial bankroll. At $200 bankroll: drawdown halt at
# -$50, daily halt at -$30. Re-evaluate after first live cohort settles.
MAX_DRAWDOWN_PCT: float = 0.25
DAILY_LOSS_LIMIT_PCT: float = 0.15
BANKROLL_STALE_SECONDS: int = 420    # fail-closed if bankroll older than this
# 420s = SCAN_INTERVAL_SECONDS (300) + 2min buffer for slow API cycles

# ─── Kalshi fees ─────────────────────────────────────────────────────────────
KALSHI_FEE_RATE: float = 0.07


def kalshi_trade_fee(contracts: float, price: float) -> float:
    """Per-fill Kalshi fee in dollars, ceiled to the cent.

    Formula (standard tier): fee = ceil(rate * contracts * price * (1-price) * 100) / 100.
    Winner settles at $1.00 so sell-side fee is 0. Copied verbatim from v1 config.py
    (correct per audit M7). Accepts fractional contracts for pre-trade edge calcs.
    """
    if contracts <= 0 or price <= 0 or price >= 1:
        return 0.0
    raw = KALSHI_FEE_RATE * contracts * price * (1.0 - price)
    return _math.ceil(raw * 100) / 100


# ─── Files ───────────────────────────────────────────────────────────────────
DATA_DIR: str = "data"
DB_FILE: str = os.path.join(DATA_DIR, "trades.db")
PERF_FILE: str = os.path.join(DATA_DIR, "performance.json")
CALIBRATION_PKL: str = os.path.join(DATA_DIR, "calibration.pkl")
CALIBRATION_META: str = os.path.join(DATA_DIR, "calibration.meta.json")
LOG_FILE: str = os.path.join("logs", "bot.log")

# v1 database path — read-only source for bootstrapping calibration.
V1_DB_PATH: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "polymarket-bot", "trades.db",
)

# ─── Cities and series ───────────────────────────────────────────────────────
# High-temperature markets on Kalshi. Copied from v1 kalshi.py:18 (14 cities).

# IANA timezone for each city — used to compute daily-max in the correct local
# window.  Previously hardcoded to America/New_York in forecast.py, which caused
# tail errors of up to 5.5°F for non-Eastern cities (Denver, Chicago, Seattle).
CITY_TZ: dict[str, str] = {
    "NYC":          "America/New_York",
    "Chicago":      "America/Chicago",
    "Miami":        "America/New_York",
    "LA":           "America/Los_Angeles",
    "Austin":       "America/Chicago",
    "Denver":       "America/Denver",
    "Philadelphia": "America/New_York",
    "Houston":      "America/Chicago",
    "Boston":       "America/New_York",
    "Phoenix":      "America/Phoenix",
    "Dallas":       "America/Chicago",
    "Seattle":      "America/Los_Angeles",
    "Atlanta":      "America/New_York",
    "SF":           "America/Los_Angeles",
    # Added 2026-05-02 from Kalshi series discovery.
    "Las Vegas":    "America/Los_Angeles",
    "San Antonio":  "America/Chicago",
    "Oklahoma City":"America/Chicago",
    "DC":           "America/New_York",
    "New Orleans":  "America/Chicago",
}

# GEFS forecast bias correction (per city). Zeroed 2026-05-17 after the
# shadow audit revealed the original cli_gap_audit measurement applied the
# wrong fix in the wrong direction.
#
# History: cli_gap_audit.py (2026-04-27) measured the CLI-vs-ASOS gap
# (~+0.6°F — CLI is systematically warmer than ASOS hourly observations
# because CLI captures 5-minute peaks). The original code applied that
# gap as an additive shift to GEFS members, on the assumption "GEFS is
# ASOS-aligned." That assumption was never tested.
#
# Shadow-audit measurement (n=108 events, 2026-05-17): the actual
# GEFS-vs-CLI bias is mean -0.44°F (essentially zero), median 0.00°F,
# stdev 2.23°F. The original CLI_BIAS values are therefore introducing a
# wrong-direction warm shift for cities that don't need one, and not
# addressing the real failure mode (ensemble under-dispersion — see
# v3_architecture_plan_20260512.md).
#
# Zeroed pending: (a) larger per-city sample (need n≥30/city to refit
# reliably) and (b) isotonic calibration refit, which is the proper fix
# for under-dispersion regardless of mean bias.
CLI_BIAS: dict[str, float] = {
    "NYC":          0.0,
    "Chicago":      0.0,
    "Miami":        0.0,
    "LA":           0.0,
    "Austin":       0.0,
    "Denver":       0.0,
    "Philadelphia": 0.0,
    "Houston":      0.0,
    "Boston":       0.0,
    "Phoenix":      0.0,
    "Dallas":       0.0,
    "Seattle":      0.0,
    "Atlanta":      0.0,
    "SF":           0.0,
    "Las Vegas":    0.0,
    "San Antonio":  0.0,
    "Oklahoma City":0.0,
    "DC":           0.0,
    "New Orleans":  0.0,
}

# GEFS ensemble spread inflation factor (shipped 2026-05-17).
#
# Direct measurement from shadow audit: predicted ensemble SD median 1.43°F
# (n=168 fresh signals); realized forecast-error stdev 2.23°F (n=108
# settled events using max-p-bracket vs settled-YES-bracket as proxy for
# forecast vs actual). Predicted/realized = 0.64x → actual forecast
# uncertainty is ~1.55x wider than GEFS members suggest.
#
# Applied in forecast.get_ensemble_high() AFTER CLI_BIAS but BEFORE the
# probability-from-members calculation. Each member is moved radially
# outward from the ensemble mean by this factor:
#     m' = mean + (m - mean) * SPREAD_INFLATION_FACTOR
#
# Effect on bracket probabilities: confident predictions (near 0 or near 1)
# get pulled toward 0.5; bracket probabilities reflect true forecast
# uncertainty rather than the artificially tight GEFS spread. Mathematically
# principled correction for ensemble under-dispersion, well-known issue
# with operational ensemble NWP.
#
# Set to 1.0 to disable inflation (returns to raw GEFS behavior).
#
# TODO (2026-05-17): build scripts/fit_spread_inflation.py — reads current
# shadow_signal table, computes realized-error stdev vs predicted ensemble_sd
# cohort-wide, recommends an updated factor. Per-city factors deferred until
# n≥30/city (currently ~5-7/city). Adaptive auto-tuning rejected — manual
# review preferred for load-bearing calibration parameters.
#
# Refit cadence: weekly while data accumulates (n→1000), monthly once stable.
# Run the script, read the recommended value, update this constant manually.
SPREAD_INFLATION_FACTOR: float = 1.55

# Climatological monthly mean high temperatures (°F) per city. Used by the
# Claude veto sanity-check prompt to give the model a comparison baseline
# for "is this forecast plausible." Hand-coded from public NOAA normals;
# precision is fine to within a couple degrees — Claude only uses these
# as context, not for any quantitative decision.
CITY_CLIMO_HIGH_F: dict[str, list[int]] = {
    # index 0 = January, 11 = December
    "NYC":          [40, 43, 51, 62, 72, 80, 85, 84, 76, 65, 54, 44],
    "Chicago":      [32, 36, 47, 60, 70, 80, 85, 83, 76, 63, 49, 36],
    "Miami":        [76, 78, 81, 84, 87, 90, 91, 91, 89, 86, 81, 77],
    "LA":           [68, 69, 70, 73, 74, 78, 83, 84, 82, 78, 73, 68],
    "Austin":       [62, 66, 73, 80, 87, 92, 96, 97, 91, 82, 71, 63],
    "Denver":       [45, 48, 56, 62, 71, 82, 89, 87, 79, 65, 53, 44],
    "Philadelphia": [42, 45, 53, 64, 74, 83, 87, 85, 78, 67, 56, 46],
    "Houston":      [64, 67, 73, 80, 86, 91, 94, 94, 89, 82, 72, 65],
    "Boston":       [37, 39, 46, 56, 67, 76, 82, 80, 73, 62, 52, 41],
    "Phoenix":      [68, 72, 78, 86, 95, 104, 106, 104, 100, 89, 76, 67],
    "Dallas":       [57, 62, 70, 77, 84, 92, 96, 96, 89, 79, 67, 59],
    "Seattle":      [48, 50, 54, 59, 65, 70, 76, 76, 71, 60, 52, 47],
    "Atlanta":      [54, 58, 65, 73, 80, 87, 90, 89, 83, 73, 64, 56],
    "SF":           [57, 60, 62, 64, 65, 67, 67, 68, 70, 69, 63, 57],
    "Las Vegas":    [58, 64, 71, 79, 89, 99, 105, 103, 96, 83, 69, 58],
    "San Antonio":  [62, 67, 73, 80, 87, 92, 95, 95, 90, 82, 71, 64],
    "Oklahoma City":[51, 56, 64, 72, 79, 87, 93, 92, 84, 73, 62, 53],
    "DC":           [44, 47, 55, 66, 75, 83, 87, 86, 79, 68, 57, 47],
    "New Orleans":  [62, 65, 71, 78, 85, 90, 91, 91, 88, 80, 70, 64],
}


# Path for the cached forecast-health JSON, written by forecast_health.py and
# served by the dashboard's /api/forecast_health endpoint.
FORECAST_HEALTH_FILE: str = str(Path("data/forecast_health.json"))
WEATHER_SERIES: dict[str, str] = {
    "NYC":          "KXHIGHNY",
    "Chicago":      "KXHIGHCHI",
    "Miami":        "KXHIGHMIA",
    "LA":           "KXHIGHLAX",
    "Austin":       "KXHIGHAUS",
    "Denver":       "KXHIGHDEN",
    "Philadelphia": "KXHIGHPHIL",
    "Houston":      "KXHIGHTHOU",
    "Boston":       "KXHIGHTBOS",
    "Phoenix":      "KXHIGHTPHX",
    "Dallas":       "KXHIGHTDAL",
    "Seattle":      "KXHIGHTSEA",
    "Atlanta":      "KXHIGHTATL",
    "SF":           "KXHIGHTSFO",
}

# Airport lat/lon for Open-Meteo ensemble query. From v1 noaa.py:42
# (high-temp cities only — we cut precip and low-temp in v2).
CITIES: dict[str, dict[str, float]] = {
    "NYC":          {"lat": 40.7789, "lon": -73.9692},   # Central Park (KNYC)
    "Chicago":      {"lat": 41.7868, "lon": -87.7522},   # KMDW
    "Miami":        {"lat": 25.7959, "lon": -80.2870},   # KMIA
    "LA":           {"lat": 33.9425, "lon": -118.4081},  # KLAX
    "Austin":       {"lat": 30.1945, "lon": -97.6699},   # KAUS
    "Denver":       {"lat": 39.8561, "lon": -104.6737},  # KDEN
    "Philadelphia": {"lat": 39.8721, "lon": -75.2408},   # KPHL
    "Houston":      {"lat": 29.6454, "lon": -95.2789},   # KHOU
    "Boston":       {"lat": 42.3656, "lon": -71.0096},   # KBOS
    "Phoenix":      {"lat": 33.4373, "lon": -112.0078},  # KPHX
    "Dallas":       {"lat": 32.8998, "lon": -97.0403},   # KDFW
    "Seattle":      {"lat": 47.4502, "lon": -122.3088},  # KSEA
    "Atlanta":      {"lat": 33.6404, "lon": -84.4281},   # KATL
    "SF":           {"lat": 37.6213, "lon": -122.3790},  # KSFO
    # Added 2026-05-02 from Kalshi series discovery.
    "Las Vegas":    {"lat": 36.0840, "lon": -115.1537},  # KLAS
    "San Antonio":  {"lat": 29.5337, "lon": -98.4698},   # KSAT
    "Oklahoma City":{"lat": 35.3931, "lon": -97.6007},   # KOKC
    "DC":           {"lat": 38.8521, "lon": -77.0377},   # KDCA
    "New Orleans":  {"lat": 29.9934, "lon": -90.2580},   # KMSY
}


# ─── Constitution: pre-trade gate (folded in from v1 constitution.py) ────────
def evaluate_trade(opportunity: dict, bankroll: float | None = None) -> tuple[bool, list[str]]:
    """Hard pre-trade rules. Returns (ok, violations).

    Enforces: 5% single-bet sizing cap, thin-market guard, MIN_EDGE floor.
    No per-city-bias zone guards in v2 (replaced by ensemble + isotonic).

    `bankroll`: if provided, use this value for the size cap calculation.
    Otherwise read the live Kalshi bankroll. Polymarket paper trades pass
    STARTING_BANKROLL ($100) since they're sized against a separate paper
    book, not the live Kalshi balance.
    """
    violations: list[str] = []

    if bankroll is None:
        from risk import get_active_bankroll
        bankroll, _age = get_active_bankroll()
    max_bet = bankroll * MAX_SINGLE_BET_PCT
    size = float(opportunity.get("recommended_size", 0.0) or 0.0)
    # Use the price of the side we'd actually buy. The opp's `entry_price` is
    # set by strategy.find_opportunities to no_ask for BUY NO and yes_ask for
    # BUY YES; falling back to yes_price (== yes_ask) preserves prior behavior
    # for arb opps and any caller that doesn't populate entry_price.
    entry_price = float(opportunity.get("entry_price")
                        or opportunity.get("yes_price", 0.5) or 0.5)
    edge = float(opportunity.get("edge", 0.0) or 0.0)
    market_type = opportunity.get("market_type", "")

    # 1. Hard size cap
    if size > max_bet + 0.005:
        violations.append(
            f"OVERSIZE: ${size:.2f} exceeds ${max_bet:.2f} cap "
            f"(bankroll=${bankroll:.0f})"
        )

    # 2. Thin-market guard — checked against the side we'd actually buy.
    if entry_price < THIN_MARKET_LO or entry_price > THIN_MARKET_HI:
        violations.append(
            f"THIN_MARKET: entry_price={entry_price:.3f} — liquidity too low"
        )

    # 3. MIN_EDGE floor (arb uses MIN_EDGE_ARB)
    floor = MIN_EDGE_ARB if market_type == "arbitrage" else MIN_EDGE
    if edge < floor:
        violations.append(
            f"EDGE_FLOOR: edge={edge:.3f} below {floor:.3f} minimum"
        )

    # 4. Price window (skip for arb — arb edge is mechanical).
    #    Checks the buy-side price; previously used yes_price, which silently
    #    rejected every BUY NO trade where yes_ask was outside the window.
    if market_type != "arbitrage":
        if entry_price < MIN_PRICE:
            violations.append(
                f"PRICE_LOW: entry_price={entry_price:.3f} below MIN_PRICE={MIN_PRICE}"
            )
        if entry_price > MAX_PRICE:
            violations.append(
                f"PRICE_HIGH: entry_price={entry_price:.3f} above MAX_PRICE={MAX_PRICE}"
            )

    return (len(violations) == 0), violations
