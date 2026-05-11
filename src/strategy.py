"""
strategy.py — weather high-temp core strategy.

For each open market:
  1. Parse the bracket from the title.
  2. Resolve the target settlement date from the title.
  3. Fetch the 31 GEFS members from Open-Meteo.
  4. raw_p = fraction of members satisfying the bracket.
  5. calibrated_p = calibration.calibrate(raw_p).
  6. Edge = max(calibrated_p - yes_ask, (1 - calibrated_p) - no_ask) minus fee.
  7. Kelly-size with calibration shrinkage.
  8. evaluate_trade() pre-trade gate.
  9. Optional Claude veto (fails-open on any error).

Addresses audit items:
  M1 — ensemble probability
  M7 — exact fee deducted from edge (1-contract conservative bound)
  M8 — Kelly shrinkage
  M11 — isotonic calibration applied before edge calc
  2026-05-09 audit — spread-tightness gate on 1°F bins + Wilson-shrunk Kelly
                    sizing. See header comment block below for the failure
                    modes these address and the alternatives we considered
                    and rejected.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections import Counter
from datetime import date, datetime

import calibration
import forecast
import forecast_health
import kalshi_client
import storage
from config import (
    CLAUDE_VETO_ENABLED,
    MAX_PRICE,
    MIN_EDGE,
    MIN_PRICE,
    kalshi_trade_fee,
    evaluate_trade,
)
from risk import kelly_size

# ─── Sample-size gates (audit 2026-05-09) ─────────────────────────────────────
# Two changes shipped 2026-05-09 after a session post-mortem found:
#   (a) BUY NO at scan time was reading edge against `no_ask` on order books
#       with $0.40-0.50 spreads — effectively trading against a ghost. Three
#       of five open positions on 2026-05-09 had no_bid + 0.40+ < no_ask,
#       i.e. no real seller anywhere near the price the bot bought at.
#   (b) `cal_p` is `k/N` with N≈37 GEFS members; on 1°F-wide bins the
#       estimator's noise (~0.04 SE on k=3 of 37) is the same magnitude as
#       the probability being estimated. The bot was sizing on point
#       estimates while the Wilson 95% CI was [0.03, 0.22] — so a "70% edge"
#       was a 30% edge under a worst-case-but-plausible read of the data.
#       NOTE: at current bankroll (~$300) the 5% per-bet cap binds Kelly
#       for almost any positive-edge trade, so Wilson shrinkage is largely
#       cosmetic *today*. The math is wired correctly to bind at higher
#       bankroll or relaxed caps; we are not raising the cap in this audit.
#
# Spread filter (MAX_ENTRY_SPREAD) gates entries; Wilson sizing shrinks
# Kelly on small-k tail bins. Together they target the two failure modes
# without hand-fitting a sweet-spot gate to the 35 historical 1°F BUY NO
# trades — that sample is too small to pre-commit a hard edge/entry-band
# rule (Wilson 95% CI on 9/10 sweet-spot wins is [55%, 99%]).
#
# 2026-05-10: extended from "B-ticker BUY NO only" to ALL ticker kinds + BOTH
# actions. Original audit gated only 1°F bin BUY NO because that's where the
# observed failure mode was, with a hypothesis that "T-tickers self-select to
# deeper books." Prod observer data (~5400 snapshots) shows T-tickers are only
# marginally tighter than B-tickers (mean 1.62c vs 1.80c) and have similar
# tails — uniform liquidity profile across kinds. Extending the gate is
# cheaper than carrying an asymmetric strategy rule whose original rationale
# is no longer supported. Wide-spread rejection on BUY YES catches the
# symmetric failure mode (no resting bid on the side we'd need to exit at).
# Value is still $0.10 here; the empirically-supported tightening to ~$0.03
# is deferred until overnight + morning observer coverage confirms the
# distribution holds across the GEFS-run cycle.
# 2026-05-10: tightened 0.10 → 0.03 based on prod observer data (n=2768
# two-sided snapshots): demo-era 10c gate filtered nothing on prod where
# spreads are 1-3c. 3c passes ~88% of observed markets while rejecting
# the rare 5-10c wide tail. Re-evaluate after overnight + morning observer
# coverage; raise marginally (4c) if morning shows systematic widening.
MAX_ENTRY_SPREAD: float = 0.03  # $0.03 wide max on the side we'd buy
WILSON_Z: float = 1.96  # 95% CI


def _wilson_bounds(k: int, n: int, z: float = WILSON_Z) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion, returned as (lo, hi).

    For BUY NO sizing we want the *upper* bound on cal_p (the YES probability)
    — that is the worst-case for our position, so Kelly shrinks correctly.
    For BUY YES sizing we want the *lower* bound on cal_p (worst-case for the
    win probability). See call site in find_opportunities for direction logic.

    Wilson is preferred over the naive ±z·sqrt(p(1-p)/n) because it stays in
    [0, 1] and behaves correctly at the edges (k=0 or k=n) — both of which
    occur frequently for tail bins where 0/37 or 1/37 members hit the
    bracket.
    """
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1.0 + z * z / n
    center = p + z * z / (2.0 * n)
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    lo = max(0.0, (center - half) / denom)
    hi = min(1.0, (center + half) / denom)
    return lo, hi

# Claude 3.5 Haiku pricing (USD per token): $0.80/M input, $4.00/M output.
_HAIKU_INPUT_USD_PER_TOKEN = 0.80 / 1_000_000
_HAIKU_OUTPUT_USD_PER_TOKEN = 4.00 / 1_000_000
_claude_cost_accum: float = 0.0

# Module-level Anthropic client. Constructing one per veto call leaked an
# httpx connection pool that, on garbage collection, called logger.debug()
# from inside __del__ — which triggered "reentrant call inside <BufferedWriter>"
# crashes when GC happened during an active logging.flush(). One client,
# constructed lazily, eliminates the GC storm.
_anthropic_client: object | None = None
_anthropic_init_failed: bool = False


def _get_anthropic_client():
    global _anthropic_client, _anthropic_init_failed
    if _anthropic_client is not None or _anthropic_init_failed:
        return _anthropic_client
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        _anthropic_init_failed = True
        return None
    try:
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=api_key)
    except Exception as e:
        logging.warning("[STRATEGY] Anthropic client init failed: %s", e)
        _anthropic_init_failed = True
        return None
    return _anthropic_client


def pop_claude_cost() -> float:
    """Return accumulated Claude API cost in USD and reset the counter."""
    global _claude_cost_accum
    v = _claude_cost_accum
    _claude_cost_accum = 0.0
    return v


_MONTH_MAP = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


from resolution_rules import parse_resolution_date as _shared_parse_date


def parse_bracket(title: str) -> tuple[str, float | None, float | None] | None:
    """Extract (kind, low, high) from a Kalshi bracket title.

    Copied/adapted from v1 strategy.py:255.  Handles '>N', '<N', 'N-M' (dash
    or en-dash). Returns None if no bracket is found.
    """
    above = re.search(r">\s*(\d+(?:\.\d+)?)", title)
    below = re.search(r"<\s*(\d+(?:\.\d+)?)", title)
    between = re.search(r"(\d+(?:\.\d+)?)\s*[–-]\s*(\d+(?:\.\d+)?)", title)
    if above:
        return ("above", float(above.group(1)), None)
    if below:
        return ("below", None, float(below.group(1)))
    if between:
        return ("between", float(between.group(1)), float(between.group(2)))
    return None


def _target_date_from_title(title: str) -> date | None:
    """Parse 'Jan 25' / 'Mar 5, 2026' etc. out of the title.

    Delegates to resolution_rules.parse_resolution_date so cross_venue
    and strategy share one date-parsing implementation. Honors explicit
    years when present; infers and bumps-to-next-year otherwise.
    """
    return _shared_parse_date(title)


def _raw_probability(bracket: tuple[str, float | None, float | None],
                     members: list[float]) -> float:
    # NOTE on the estimator (audit 2026-05-09): this returns k/N where k is
    # the count of ensemble members satisfying the bracket and N is the
    # ensemble size (~31-37 GEFS members). For wide brackets ("above 76°F")
    # the variance of this estimator is small relative to the probability
    # being estimated. For 1°F-wide bins, the variance is the same order of
    # magnitude as the probability itself — see the Wilson 95% CI logic in
    # find_opportunities.
    #
    # A natural next step is to fit a smooth distribution (skew-normal or
    # KDE) to the members and integrate over the bin. That borrows
    # information from neighboring bins and lifts effective sample size.
    # Deliberately deferred at the 2026-05-09 audit: we do not yet have
    # enough post-fix data to validate a new estimator's tail behavior
    # against settlement outcomes, and rolling a parametric distribution
    # in alongside the spread/Wilson gates would conflate two
    # interventions. Revisit once we have ~50+ resolved 1°F BUY NO trades
    # under the new gates.
    kind, low, high = bracket
    if kind == "above":
        return forecast.probability_above(members, float(low))
    if kind == "below":
        return forecast.probability_below(members, float(high))
    if kind == "between":
        return forecast.probability_between(members, float(low), float(high))
    return 0.0


_MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]

# Per-cycle cache: (city, target_date_iso) -> bool. Forecast plausibility is
# constant across all brackets of the same market. Cleared at the top of
# find_opportunities so each cycle re-asks (forecasts can change between cycles).
_veto_cache: dict[tuple[str, str], bool] = {}


def _veto_cache_clear() -> None:
    _veto_cache.clear()


def _claude_veto(candidate: dict) -> bool:
    """Sanity-check the FORECAST inputs (not the model output) for plausibility.

    Returns True (keep) on PLAUSIBLE or UNCERTAIN, False (skip) only on a
    confident DATA_ERROR signal. Fails-open on any error/timeout.

    Design (validated by 2026-05-03 backtest on 78 historical resolved trades):
      - Does NOT show market price → no anchoring against our edge.
      - Does NOT show calibrated_p → Claude doesn't get to challenge the model's
        probability estimate, only the forecast plausibility.
      - DOES show ensemble mean / spread + climatological context → leverages
        Claude's actual weather knowledge to catch corrupt forecast data.
      - Default-to-PLAUSIBLE framing + UNCERTAIN escape hatch keeps fail-open
        behaviour even when Claude doesn't have a strong signal.
      - Cached by (city, target_date) since plausibility is per-forecast, not
        per-bracket — saves ~85% of API calls (one call per market vs per leg).

    Backtest result (full history, current vs new prompt):
      - Current prompt vetoed 52/52 historical winners; net P&L on PASS = -$4.75
      - New prompt vetoed 3/52 historical winners; net P&L on PASS = +$26.94
        (vs +$30.67 acting on everything — captures 88% of upside)
    """
    if not CLAUDE_VETO_ENABLED:
        return True
    client = _get_anthropic_client()
    if client is None:
        return True

    city = candidate["city"]
    target_date_str = candidate["target_date"]
    cache_key = (city, target_date_str)
    if cache_key in _veto_cache:
        return _veto_cache[cache_key]

    try:
        from config import CITY_CLIMO_HIGH_F
        members = candidate.get("members") or []
        if not members:
            return True  # no ensemble data — fail open
        ensemble_mean = sum(members) / len(members)
        ensemble_min = min(members)
        ensemble_max = max(members)
        # 10th/90th percentile-ish bounds, no scipy
        sorted_m = sorted(members)
        p10 = sorted_m[max(0, int(0.10 * len(sorted_m)) - 1)]
        p90 = sorted_m[min(len(sorted_m) - 1, int(0.90 * len(sorted_m)))]

        # Parse month from target_date
        try:
            month_idx = int(target_date_str.split("-")[1]) - 1
            month_name = _MONTH_NAMES[month_idx]
        except Exception:
            month_idx, month_name = 0, "(unknown month)"

        climo = CITY_CLIMO_HIGH_F.get(city)
        climo_str = f"{climo[month_idx]}°F" if climo else "(climatology unavailable)"

        prompt = (
            f"Sanity-check this weather forecast for data plausibility. We are "
            f"evaluating a market about the daily high temperature in {city} on "
            f"{target_date_str}.\n\n"
            f"Our ensemble forecast (GEFS members):\n"
            f"- mean predicted high: {ensemble_mean:.0f}°F\n"
            f"- range across members: {p10:.0f}°F to {p90:.0f}°F "
            f"(min {ensemble_min:.0f}°F, max {ensemble_max:.0f}°F)\n"
            f"- climatological average for {city} in {month_name}: {climo_str}\n\n"
            f"Bracket being evaluated: {candidate.get('bracket_str', 'unknown')}.\n\n"
            f"Is the FORECAST itself plausible for this city and date, or does it "
            f"look like a DATA ERROR (forecast outside physically reasonable range, "
            f"or far outside climatological norms without obvious cause)?\n\n"
            f"Answer ONE word: PLAUSIBLE, DATA_ERROR, or UNCERTAIN.\n\n"
            f"Default to PLAUSIBLE — only return DATA_ERROR if the forecast is "
            f"clearly implausible (e.g., snow forecast for Miami in July, or 130°F "
            f"predicted anywhere in the US). UNCERTAIN if you genuinely cannot tell."
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
            timeout=8.0,
        )
        try:
            usage = getattr(msg, "usage", None)
            if usage is not None:
                global _claude_cost_accum
                _claude_cost_accum += (
                    int(getattr(usage, "input_tokens", 0) or 0) * _HAIKU_INPUT_USD_PER_TOKEN
                    + int(getattr(usage, "output_tokens", 0) or 0) * _HAIKU_OUTPUT_USD_PER_TOKEN
                )
        except Exception:
            pass
        text = "".join(
            getattr(b, "text", "") for b in (msg.content or [])
        ).strip().upper()
        # Anything that ISN'T a clear DATA_ERROR is treated as a pass —
        # PLAUSIBLE, UNCERTAIN, partial / unparseable answers all fail-open.
        verdict = not (text.startswith("DATA_ERROR") or text.startswith("DATA ERROR"))
        _veto_cache[cache_key] = verdict
        return verdict
    except Exception as e:
        logging.warning("[STRATEGY] Claude veto failed-open: %s", e)
        return True


def _bracket_from_market(m: dict) -> tuple[str, float | None, float | None] | None:
    """Prefer canonical fields, fall back to title parsing.

    Phase 3: kalshi_client.get_all_weather_markets and PolymarketVenue.list_markets
    both populate canonical (comparator/threshold/range_*) fields. The title-parse
    fallback only fires for markets that somehow reach strategy without
    canonicalization — defensive belt-and-suspenders.
    """
    cmp_ = m.get("comparator")
    if cmp_ in (">=", ">"):
        thr = m.get("threshold")
        if thr is not None:
            return ("above", float(thr), None)
    if cmp_ in ("<=", "<"):
        thr = m.get("threshold")
        if thr is not None:
            return ("below", None, float(thr))
    if cmp_ == "in_range":
        lo, hi = m.get("range_low"), m.get("range_high")
        if lo is not None and hi is not None:
            return ("between", float(lo), float(hi))
    title = m.get("title") or m.get("question") or ""
    return parse_bracket(title)


def _target_date_from_market(m: dict) -> date | None:
    """Parse the resolution date from the market's title/question.

    DO NOT use `close_time[:10]` as the primary source — for any city west
    of UTC the close-time UTC date is ONE DAY AFTER the measurement date
    (NYC May 3 measurement → close_time 2026-05-04T03:59Z). Same bug as
    the LAX false-positive arb in cross_venue.py (2026-05-03 ledger).

    Title parsing handles both venues uniformly (Kalshi "May 3, 2026"
    and Polymarket "on May 4") via the shared resolution-rules parser.
    close_time fallback only fires if the title is unparseable.
    """
    title = m.get("title") or m.get("question") or ""
    d = _shared_parse_date(title)
    if d is not None:
        return d
    # Last-ditch fallback (will be wrong-by-a-day for most markets, but
    # better than dropping the market entirely).
    ts = (m.get("close_time")
          or m.get("expected_expiration_time")
          or m.get("target_settlement")
          or "")
    if ts and len(ts) >= 10:
        try:
            return date.fromisoformat(ts[:10])
        except ValueError:
            pass
    return _target_date_from_title(m.get("title") or m.get("question") or "")


def compute_market_cal_p(market: dict, venue: str = "kalshi") -> float | None:
    """Return the latest calibrated probability for `market`, or None if
    we can't score it (missing bracket, no forecast, etc).

    Same forecast → raw_p → calibrate pipeline as find_opportunities, but
    operates on a single market with no held-position skip and no edge
    gate. Used by executor.process_exits to evaluate "has the forecast
    moved against this open position?" without needing the market to
    surface as a fresh opportunity.
    """
    city = market.get("city", "")
    title = market.get("title") or market.get("question") or ""
    if not city or not title:
        return None
    bracket = _bracket_from_market(market)
    if bracket is None:
        return None
    target = _target_date_from_market(market)
    if target is None:
        return None
    members = forecast.get_ensemble_high(city, target)
    if not members:
        return None
    try:
        raw_p = _raw_probability(bracket, members)
        return float(calibration.calibrate(raw_p, venue=venue))
    except Exception:
        return None


def find_opportunities(markets: list[dict], bankroll: float,
                       venue: str = "kalshi") -> list[dict]:
    """Evaluate every weather market for `venue`, return sorted opportunities.

    Phase 3: venue-aware. Fee, calibration, and held-position dedup all
    routed by venue. The forecast and bracket logic stay venue-agnostic
    because they operate on the canonical resolution rule, not the venue.
    """
    results: list[dict] = []
    rej: Counter[str] = Counter()  # per-call rejection reason tally
    _veto_cache_clear()  # forecast plausibility re-asked once per city/date per cycle

    # Dedup by (venue, ticker). For Kalshi we trust the live positions
    # endpoint over our local DB: a cancel-race can leave a real Kalshi
    # position with no DB row (stranded fill), and the DB-only check would
    # let us re-enter and double our exposure (2026-05-07 CHI-B62.5
    # incident — duplicate 28-NO position from a 6s verify-after-cancel
    # timeout). Live API is the source of truth at execution time.
    #
    # If the API call fails we fail closed and fall back to DB; better to
    # miss an opportunity than to risk doubling up.
    held: set[tuple[str, str]] = set()
    if venue == "kalshi":
        try:
            for p in kalshi_client.get_open_positions() or []:
                # position_fp can be negative (NO holdings) or positive
                # (YES holdings). Zero means flat — don't treat a settled
                # ticker still in the response as held.
                try:
                    fp = float(p.get("position_fp") or 0)
                except (TypeError, ValueError):
                    fp = 0.0
                if fp == 0:
                    continue
                tk = p.get("ticker", "")
                if tk:
                    held.add(("kalshi", tk))
        except Exception as e:
            logging.warning(
                "[STRATEGY] live Kalshi positions fetch failed (%s) — "
                "falling back to DB-only dedup; entries this cycle may "
                "race stranded fills", e,
            )
            held = {
                (p.get("venue", "kalshi"), p.get("ticker", ""))
                for p in storage.load_open_positions()
            }
    else:
        held = {
            (p.get("venue", "kalshi"), p.get("ticker", ""))
            for p in storage.load_open_positions()
        }

    for m in markets:
        ticker = m.get("ticker", "") or m.get("market_id", "")
        city = m.get("city", "")
        title = m.get("title") or m.get("question") or ""
        yes_ask = float(m.get("yes_ask_dollars") or 0.0)
        no_ask = float(m.get("no_ask_dollars") or 0.0)
        # Bids pulled for the spread-tightness gate below. Kalshi exposes
        # these on the same /markets payload that already gives us asks, so
        # this is a free read — no extra round trip. Polymarket's canonical
        # adapter does not yet populate bids; for that venue these stay 0.0
        # and the spread gate below correctly skips B-tickers with no bid
        # (which is the right answer until a real bid is plumbed through).
        yes_bid = float(m.get("yes_bid_dollars") or 0.0)
        no_bid = float(m.get("no_bid_dollars") or 0.0)

        if not ticker or not city or not title:
            rej["missing_meta"] += 1
            continue
        if (venue, ticker) in held:
            rej["already_held"] += 1
            continue
        if not forecast_health.city_is_healthy(city):
            logging.debug("[STRATEGY] %s skipped: %s in forecast-health alert", ticker, city)
            rej["forecast_unhealthy"] += 1
            continue
        # Bail only when BOTH sides are degenerate. A market with yes_ask=$1.00
        # (no YES seller on book) can still be a valid BUY NO opportunity, and
        # vice versa — the inner edge calc handles a one-sided book correctly.
        yes_dead = yes_ask <= 0 or yes_ask >= 1
        no_dead = no_ask <= 0 or no_ask >= 1
        if yes_dead and no_dead:
            rej["both_sides_dead"] += 1
            continue

        bracket = _bracket_from_market(m)
        if bracket is None:
            rej["bracket_parse"] += 1
            continue

        # 1°F-wide "between" bins: settlement-source noise (NWS CLI vs GFS
        # grid) is ~1°F RMSE, which consumes the entire bin width. This makes
        # BUY YES on a 1°F bin untradeable — we'd be integrating P(YES) over a
        # window narrower than our forecast precision.
        #
        # BUY NO is different: we integrate P(NO) over the bin's complement
        # (a huge region), so the 1°F width barely affects our estimate.
        # Historical record (12 trades, $0.50-0.70 NO entry, 11/12 wins,
        # +$11.46) confirms the math. The 2026-05-03 retro originally led to
        # a paper-only sandbox in strategy_bins.py; that was reverted in
        # favor of restoring 1°F BUY NO to the live directional pipeline,
        # gated only on the YES side. The BUY NO disagreement >= 0.15 filter
        # below correctly handles quality control on the NO side and passes
        # 12/12 of the historical winners.
        kind_check, low_check, high_check = bracket
        is_1f_bin = (
            kind_check == "between"
            and high_check is not None and low_check is not None
            and (high_check - low_check) <= 1.0 + 1e-6
        )

        target = _target_date_from_market(m)
        if target is None:
            rej["no_target_date"] += 1
            continue

        members = forecast.get_ensemble_high(city, target)
        if not members:
            rej["no_forecast"] += 1
            continue

        raw_p = _raw_probability(bracket, members)
        calibrated_p = calibration.calibrate(raw_p, venue=venue)

        # Edge for each side.
        edge_yes = calibrated_p - yes_ask if 0 < yes_ask < 1 else -1.0
        edge_no = (1.0 - calibrated_p) - no_ask if 0 < no_ask < 1 else -1.0

        # BUY YES guards (2026-05-03 audit, directional trades only — arbs
        # excluded since they're always tagged BUY YES with calibrated_p =
        # yes_price by construction in strategy_arb.py and would contaminate
        # the bucket stats). Sample: n=12 resolved directional YES, 5W/7L,
        # net -$2.43 (vs +$33.10 on 66 resolved BUY NO over the same window).
        #
        # The shared isotonic calibrator can't correct a directional bias:
        # the ensemble systematically overshoots P(temp > threshold) on
        # "above threshold" weather markets. That manifests two ways, and
        # we filter both. BUY NO is unaffected — same bias makes NO-side
        # predictions slightly *under*-confident, which is why BUY NO at
        # the same edge thresholds is the moneymaker (71% wr directional).
        #
        #   1) calibrated_p >= 0.85 — worst bucket (1/4, -$6.41). When the
        #      model claims near-certainty on YES, the market (priced
        #      0.27-0.72 on these losers) was right.
        #   2) (calibrated_p - yes_ask) < 0.30 — the moderate-disagreement
        #      zone is BUY NO's sweet spot but BUY YES's graveyard. At
        #      >=0.30 disagreement: 3W/2L, +$10.93. Below: 2W/5L, -$13.36.
        #      Same edge size, opposite results across sides => directional
        #      bias, not variance.
        #
        # NOTE: this filter only runs in strategy.find_opportunities. Arb
        # legs from strategy_arb.scan_arbitrage bypass it entirely (they're
        # risk-free and shouldn't be filtered on directional logic).
        #
        # Revisit once we have ~30 more resolved directional YES trades;
        # long-term fix is per-side isotonic calibration (see calibration.py).
        yes_filtered = (
            calibrated_p >= 0.85
            or (calibrated_p - yes_ask) < 0.30
            or is_1f_bin  # 1°F bin BUY YES — noise floor consumes window
        )
        if yes_filtered:
            edge_yes = -1.0

        # BUY NO guard (same 2026-05-03 audit, n=66 resolved NO trades).
        # Disagreement < 0.15 was 9W/6L (60% wr) but -$7.80 — losing despite
        # winning 60%, because avg entry was $0.77 (expensive favorites need
        # ~77% wr to break even). Cutting it lifts BUY NO from +$33 to +$41.
        # Interim form of the price-aware edge floor (gross_edge/entry_price);
        # the disagreement filter implicitly captures the price story because
        # low NO-side disagreement correlates with high NO entry price.
        no_filtered = ((1.0 - calibrated_p) - no_ask) < 0.15
        if no_filtered:
            edge_no = -1.0

        if edge_yes >= edge_no:
            action = "BUY YES"
            gross_edge = edge_yes
            entry_price = yes_ask
        else:
            action = "BUY NO"
            gross_edge = edge_no
            entry_price = no_ask

        if entry_price <= 0 or entry_price >= 1:
            rej["bad_entry_price"] += 1
            continue

        # Conservative 1-contract fee bound (contracts not yet decided).
        # Each venue contributes its own per-contract cost: Kalshi uses
        # its audit-M7 formula; Polymarket weather charges 5% taker on
        # notional. We assume taker here because the strategy gate runs
        # before paper_executor has decided maker vs taker — taker is the
        # worst case for entry-edge math.
        if venue == "kalshi":
            fee = kalshi_trade_fee(1, entry_price)
        elif venue == "polymarket":
            from polymarket_client import polymarket_taker_fee
            fee = polymarket_taker_fee(1, entry_price)
        else:
            fee = 0.0
        fee_pct_of_entry = fee / entry_price if entry_price > 0 else 0.0
        net_edge = gross_edge - fee_pct_of_entry

        if net_edge < MIN_EDGE:
            # Attribute to the directional filter when it caused the bleed-out
            # (edge_yes/edge_no was zeroed). Otherwise it's a genuine no-edge.
            # When a 1°F bin trade gets filtered to BUY NO and still fails
            # MIN_EDGE, that's no_filter / min_edge; when a 1°F bin would have
            # picked BUY YES but got zeroed by is_1f_bin, attribute to bin_gate.
            if action == "BUY YES" and is_1f_bin:
                rej["bin_gate"] += 1
            elif action == "BUY YES" and yes_filtered:
                rej["yes_filter"] += 1
            elif action == "BUY NO" and no_filtered:
                rej["no_filter"] += 1
            else:
                rej["min_edge"] += 1
            continue

        # Price window guard — uses the side we'd actually buy. Previously this
        # checked yes_ask regardless of action, which silently killed every
        # BUY NO opportunity on markets where yes_ask was outside [MIN_PRICE,
        # MAX_PRICE] even when no_ask was perfectly in range.
        if entry_price < MIN_PRICE or entry_price > MAX_PRICE:
            rej["price_window"] += 1
            continue

        # Spread-tightness gate (header comment for full history). Applied
        # uniformly to all ticker kinds and both actions as of 2026-05-10
        # based on prod observer data showing similar spread distributions
        # across kinds. Two failure modes caught by one rule:
        #   • Wide spread on the buy side → entry cost eats the edge.
        #   • Zero bid on the buy side → no exit liquidity if we want out
        #     before settlement (one-sided book).
        if action == "BUY YES":
            side_bid, side_ask = yes_bid, yes_ask
        else:  # BUY NO
            side_bid, side_ask = no_bid, no_ask
        if side_bid <= 0:
            rej["wide_spread"] += 1
            continue
        if (side_ask - side_bid) > MAX_ENTRY_SPREAD:
            rej["wide_spread"] += 1
            continue

        # Wilson-shrunk sizing (audit 2026-05-09, see header comment).
        # Compute k (members in bracket) and N (ensemble size) for the Wilson
        # interval. The bot's edge gate above uses the point estimate
        # (cal_p = k/N) to decide whether to enter; sizing uses the
        # *conservative* end of the Wilson interval to right-size the bet.
        # This deliberately decouples entry from sizing: at small N you can
        # be confident enough to enter without being confident enough to size
        # at full Kelly. Without this, a 3/37 estimate (point 0.081, Wilson
        # 95% CI [0.028, 0.213]) was being sized as if the true probability
        # were exactly 0.081 — so on tail bins where Wilson half-width is
        # ~equal to the point estimate, sizes were ~2x what they should be.
        k = sum(1 for x in members if _satisfies(bracket, x))
        n_ens = len(members)
        wilson_lo, wilson_hi = _wilson_bounds(k, n_ens)
        # cal_p is YES probability throughout. For BUY NO we lose if YES
        # happens, so the worst-case (conservative) cal_p is the *upper*
        # bound. For BUY YES we win if YES happens, so the conservative
        # cal_p is the *lower* bound. kelly_size internally derives win/lose
        # probabilities from p + action, so we just pass the conservative p.
        if action == "BUY NO":
            p_for_sizing = wilson_hi
        else:
            p_for_sizing = wilson_lo

        size = kelly_size(
            calibrated_p, entry_price, bankroll,
            action=action, p_for_sizing=p_for_sizing,
        )

        kind, low, high = bracket
        bracket_str = (
            f">{low}" if kind == "above"
            else f"<{high}" if kind == "below"
            else f"{low}-{high}"
        )

        opp = {
            "venue": venue,
            "ticker": ticker,
            "market_id": ticker,  # paper_executor uses market_id; same as ticker
            "city": city,
            "title": title,
            "market_type": "high_temp",
            "action": action,
            "bracket": bracket,
            "bracket_str": bracket_str,
            "target_date": target.isoformat(),
            "target_settlement": (m.get("close_time")
                                  or m.get("expected_expiration_time")
                                  or m.get("target_settlement")),
            "members": members,
            "raw_probability": round(raw_p, 4),
            "calibrated_p": round(calibrated_p, 4),
            "wilson_lo": round(wilson_lo, 4),
            "wilson_hi": round(wilson_hi, 4),
            "p_for_sizing": round(p_for_sizing, 4),
            "yes_price": yes_ask,
            "no_ask": no_ask,
            "no_bid": no_bid,
            "yes_bid": yes_bid,
            "entry_price": entry_price,
            "edge": round(net_edge, 4),
            "gross_edge": round(gross_edge, 4),
            "recommended_size": size,
            "reasoning": (
                f"[{venue}] {city} {bracket_str} on {target.isoformat()}: "
                f"{k}/{n_ens} GEFS members, raw_p={raw_p:.2f}, "
                f"calibrated_p={calibrated_p:.2f} "
                f"(Wilson 95% CI [{wilson_lo:.2f}, {wilson_hi:.2f}], "
                f"sized at p={p_for_sizing:.2f}), "
                f"net_edge={net_edge:.3f}"
            ),
        }

        # If Kalshi already rejected this ticker for insufficient_balance
        # this process, the order will fail again — skip it (and the Claude
        # veto API call) entirely. Block list is populated in kalshi_client
        # on order rejection and cleared by reset_insufficient_balance_tickers.
        if venue == "kalshi" and kalshi_client.is_blocked_insufficient_balance(ticker):
            opp["notes"] = "kalshi_insufficient_balance"
            rej["insufficient_balance"] += 1
            continue

        # Veto check: fails-open.
        if not _claude_veto(opp):
            opp["notes"] = "claude_veto:NO"
            rej["claude_veto"] += 1
            continue

        # Final constitution gate. Pass the bankroll the opp was sized
        # against so the size cap is computed against the same denominator
        # — important for Polymarket paper, which uses STARTING_BANKROLL
        # rather than the live Kalshi balance.
        ok, violations = evaluate_trade(opp, bankroll=bankroll)
        if not ok:
            opp["notes"] = "constitution_violations:" + "|".join(violations)
            rej["constitution"] += 1
            continue

        results.append(opp)

    # Stash rejection tally for the cycle log to surface. Module-level rather
    # than tuple-returned so existing callers (and the venue-loop in main.py)
    # don't need signature changes — read via get_last_rejections() right
    # after the call.
    global _last_rejections
    _last_rejections = (venue, dict(rej))

    results.sort(key=lambda o: o.get("edge", 0.0), reverse=True)
    return results


_last_rejections: tuple[str, dict[str, int]] = ("", {})


def get_last_rejections() -> tuple[str, dict[str, int]]:
    """Return (venue, rejection_counts) from the most recent find_opportunities call."""
    return _last_rejections


def _satisfies(bracket: tuple[str, float | None, float | None], x: float) -> bool:
    kind, low, high = bracket
    if kind == "above" and low is not None:
        return x >= low
    if kind == "below" and high is not None:
        return x < high
    if kind == "between" and low is not None and high is not None:
        return low <= x <= high
    return False
