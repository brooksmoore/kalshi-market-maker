"""
veto_backtest.py — backtest the current and proposed Claude veto prompts
against historical resolved trades + a synthetic stress test set.

Outputs a contingency table per prompt:
    rows: outcome (winner / loser)
    cols: claude verdict (pass / veto)

Plus per-trade detail logged to data/veto_backtest.csv.

Approximations (called out in the conversation):
  - ensemble_mean is BACK-ESTIMATED from (raw_probability, bracket) using a
    Normal-with-3°F-std assumption; we don't have stored member values.
  - Climatology is a static lookup (typical monthly highs for each city);
    forecast's actual climo input may differ slightly.
"""

from __future__ import annotations

import csv
import math
import os
import re
import sys
import sqlite3
import time
from datetime import date, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anthropic import Anthropic  # noqa: E402

# ── Climo: typical monthly mean high (°F) per city ───────────────────────────
# Hand-coded from public NOAA normals. Plus/minus a couple degrees is fine —
# Claude only uses these as sanity-check context.
CLIMO_HIGH_F: dict[str, list[int]] = {
    # Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec
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


# ── Bracket parsing ──────────────────────────────────────────────────────────
def parse_ticker(ticker: str) -> tuple[str, float | None, float | None] | None:
    """Return (kind, low, high) for the bracket. None if unparseable."""
    m = re.search(r"-([TB])(\d+(?:\.\d+)?)$", ticker)
    if not m:
        return None
    side, num = m.group(1), float(m.group(2))
    # .5 suffix indicates a 1°F between-bin (e.g., B65.5 → [65, 66))
    if abs(num - round(num)) > 0.4:
        return ("between", num - 0.5, num + 0.5)
    if side == "T":
        return ("above", num, None)
    if side == "B":
        return ("below", None, num)
    return None


def bracket_to_human(kind: str, low: float | None, high: float | None) -> str:
    if kind == "above":
        return f"above {low:.0f}°F"
    if kind == "below":
        return f"below {high:.0f}°F"
    if kind == "between":
        return f"between {low:.0f}°F and {high:.0f}°F"
    return "unknown"


# ── Inverse-CDF reconstruction of ensemble mean ──────────────────────────────
def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _phi_inv(p: float) -> float:
    """Approximate inverse Normal CDF via Newton's method."""
    if p <= 0.0:
        return -8.0
    if p >= 1.0:
        return 8.0
    z = 0.0
    for _ in range(50):
        f = _phi(z) - p
        if abs(f) < 1e-6:
            break
        z -= f / max(0.39894 * math.exp(-z * z / 2), 1e-9)
    return z


def estimate_ensemble_mean(raw_p: float, kind: str,
                            low: float | None, high: float | None,
                            std: float = 3.0) -> tuple[float, float, float]:
    """Back-estimate (mean, p10, p90) from (raw_p, bracket) assuming N(mean, 3°F)."""
    raw_p = max(1e-3, min(0.999, raw_p))
    if kind == "above" and low is not None:
        # P(T > low) = raw_p ⇒ mean = low - std * Φ⁻¹(1 - raw_p)
        z = _phi_inv(1 - raw_p)
        mean = low - std * z
    elif kind == "below" and high is not None:
        # P(T < high) = raw_p ⇒ mean = high - std * Φ⁻¹(raw_p)
        z = _phi_inv(raw_p)
        mean = high - std * z
    elif kind == "between" and low is not None and high is not None:
        # Best-estimate: mean is where prob mass is concentrated. If raw_p
        # is high, mean is near (low+high)/2; if low, mean is far. Use a
        # heuristic: mean = midpoint adjusted by raw_p density.
        midpoint = (low + high) / 2
        if raw_p > 0.20:
            mean = midpoint
        else:
            # Mean is far from the bin; we don't know which side. Pick the
            # closer edge of the climo distribution; this is approximate.
            # For backtest we'll just say "mean is well outside this bin."
            mean = midpoint  # use midpoint as a placeholder
    else:
        mean = (low or high or 70.0)
    return mean, mean - 1.28 * std, mean + 1.28 * std


# ── Prompts ──────────────────────────────────────────────────────────────────
CURRENT_PROMPT = (
    "Given this Kalshi market on {city} for "
    "{target_date}, bracket={bracket_str}, "
    "our ensemble-calibrated P(YES)={calibrated_p:.2f}, "
    "market YES ask={yes_price:.2f}. "
    "Is our probability estimate reasonable? Answer YES or NO only."
)

NEW_PROMPT = (
    "Sanity-check this weather forecast for data plausibility. We are "
    "evaluating a market about the daily high temperature in {city} on "
    "{target_date}.\n\n"
    "Our ensemble forecast (5 GEFS members):\n"
    "- mean predicted high: {ensemble_mean:.0f}°F\n"
    "- approximate range across members: {ensemble_p10:.0f}°F to "
    "{ensemble_p90:.0f}°F\n"
    "- climatological average for {city} in {month_name}: {climo_mean}°F\n\n"
    "Bracket being evaluated: temperature {bracket_human}.\n\n"
    "Is the FORECAST itself plausible for this city and date, or does it "
    "look like a DATA ERROR (forecast outside physically reasonable range, "
    "or far outside climatological norms without obvious cause)?\n\n"
    "Answer ONE word: PLAUSIBLE, DATA_ERROR, or UNCERTAIN.\n\n"
    "Default to PLAUSIBLE — only return DATA_ERROR if the forecast is "
    "clearly implausible (e.g., snow forecast for Miami in July, or 130°F "
    "predicted anywhere in the US). UNCERTAIN if you genuinely cannot tell."
)

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


# ── Claude calls ─────────────────────────────────────────────────────────────
client: Anthropic | None = None


def _client() -> Anthropic:
    global client
    if client is None:
        client = Anthropic()
    return client


def call_claude(prompt: str) -> str:
    """Single Claude call with retry on 429 rate-limit."""
    last_err: Exception | None = None
    for attempt in range(6):
        try:
            msg = _client().messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=10,
                messages=[{"role": "user", "content": prompt}],
                timeout=15.0,
            )
            text = "".join(getattr(b, "text", "") for b in (msg.content or [])).strip().upper()
            return text
        except Exception as e:
            last_err = e
            msg = str(e)
            if "rate_limit" in msg or "429" in msg:
                # exponential backoff up to ~30s
                time.sleep(2 ** attempt + 0.5)
                continue
            raise
    raise last_err  # type: ignore[misc]


def current_verdict(case: dict) -> str:
    p = CURRENT_PROMPT.format(
        city=case["city"], target_date=case["target_date"],
        bracket_str=case["bracket_str"],
        calibrated_p=case["calibrated_p"],
        yes_price=case["yes_price"],
    )
    text = call_claude(p)
    return "VETO" if text.startswith("NO") else "PASS"


def new_verdict(case: dict) -> str:
    p = NEW_PROMPT.format(
        city=case["city"], target_date=case["target_date"],
        ensemble_mean=case["ensemble_mean"],
        ensemble_p10=case["ensemble_p10"],
        ensemble_p90=case["ensemble_p90"],
        month_name=case["month_name"],
        climo_mean=case["climo_mean"],
        bracket_human=case["bracket_human"],
    )
    text = call_claude(p)
    if text.startswith("DATA_ERROR") or text.startswith("DATA ERROR"):
        return "VETO"
    if text.startswith("UNCERTAIN"):
        return "UNCERTAIN"
    return "PASS"


# ── Build the test cases ─────────────────────────────────────────────────────
def build_historical_cases() -> list[dict]:
    conn = sqlite3.connect("data/trades.db")
    rows = conn.execute("""
        SELECT t.id, t.ticker, t.city, t.action, t.entry_price,
               t.calibrated_p, t.ensemble_p, t.target_settlement,
               r.outcome, r.profit_loss
        FROM trades t JOIN results r ON r.trade_id=t.id
        WHERE NOT (t.market_type='arbitrage' OR t.notes LIKE 'arb:%')
          AND r.outcome IN ('yes','no')
        ORDER BY t.id
    """).fetchall()
    conn.close()

    cases = []
    for row in rows:
        (tid, ticker, city, action, entry, cal_p, ens_p, settle,
         outcome, pl) = row
        bracket = parse_ticker(ticker)
        if bracket is None:
            continue
        kind, low, high = bracket
        # target date from settle timestamp
        try:
            settle_dt = datetime.fromisoformat(settle.replace("Z", "+00:00"))
            target_date = settle_dt.date()
        except Exception:
            continue
        ens_mean, ens_p10, ens_p90 = estimate_ensemble_mean(
            ens_p or 0.5, kind, low, high
        )
        climo_arr = CLIMO_HIGH_F.get(city)
        if not climo_arr:
            continue
        month_idx = target_date.month - 1
        # yes_price from action + entry
        yes_price = entry if action == "BUY YES" else (1 - entry)

        cases.append({
            "trade_id": tid, "ticker": ticker, "city": city,
            "action": action, "entry_price": entry,
            "calibrated_p": cal_p, "yes_price": yes_price,
            "target_date": target_date.isoformat(),
            "month_name": MONTHS[month_idx],
            "ensemble_mean": ens_mean, "ensemble_p10": ens_p10,
            "ensemble_p90": ens_p90,
            "climo_mean": climo_arr[month_idx],
            "bracket_str": (
                f">{low}" if kind == "above"
                else f"<{high}" if kind == "below"
                else f"{low}-{high}"
            ),
            "bracket_human": bracket_to_human(kind, low, high),
            "outcome": outcome, "profit_loss": pl,
            "winner": pl > 0,
        })
    return cases


def build_synthetic_cases() -> list[dict]:
    """10 plausible + 10 known-bad forecasts for stress test."""
    cases = []
    # Plausible: typical conditions
    plausible = [
        ("NYC", 5, 75, 70, 80, "above 70°F", "PLAUSIBLE_normal"),
        ("Phoenix", 7, 105, 100, 110, "above 100°F", "PLAUSIBLE_phx_summer"),
        ("Miami", 1, 78, 75, 82, "above 75°F", "PLAUSIBLE_mia_winter"),
        ("Boston", 11, 50, 45, 55, "below 60°F", "PLAUSIBLE_bos_fall"),
        ("Chicago", 3, 47, 40, 55, "between 45°F and 50°F", "PLAUSIBLE_chi_spring"),
        ("Seattle", 1, 47, 42, 52, "above 45°F", "PLAUSIBLE_sea_winter"),
        ("Dallas", 4, 78, 73, 83, "above 75°F", "PLAUSIBLE_dal_spring"),
        ("Houston", 8, 95, 92, 98, "above 90°F", "PLAUSIBLE_hou_summer"),
        ("Denver", 12, 45, 40, 50, "above 40°F", "PLAUSIBLE_den_winter"),
        ("LA", 6, 78, 74, 82, "above 75°F", "PLAUSIBLE_la_summer"),
    ]
    for city, month, mean, p10, p90, bracket, label in plausible:
        cases.append({
            "label": label, "city": city,
            "target_date": f"2026-{month:02d}-15",
            "ensemble_mean": mean, "ensemble_p10": p10, "ensemble_p90": p90,
            "month_name": MONTHS[month - 1],
            "climo_mean": CLIMO_HIGH_F[city][month - 1],
            "bracket_human": bracket,
            "calibrated_p": 0.5, "yes_price": 0.5, "bracket_str": bracket,
            "expected": "PASS",
        })
    # Known-bad: things Claude *should* catch
    bad = [
        ("Phoenix", 7, 35, 32, 38, "above 30°F", "BAD_phx_summer_freezing"),
        ("Miami", 7, 30, 25, 35, "above 25°F", "BAD_mia_summer_freezing"),
        ("NYC", 1, 105, 100, 110, "above 100°F", "BAD_nyc_winter_heatwave"),
        ("Boston", 4, 130, 125, 135, "above 125°F", "BAD_implausible_high"),
        ("Chicago", 1, -20, -25, -15, "below -10°F", "BAD_extreme_cold_unusual"),
        ("Atlanta", 6, 110, 105, 115, "above 105°F", "BAD_atl_summer_too_hot"),
        ("Denver", 8, 25, 20, 30, "above 20°F", "BAD_den_summer_freezing"),
        ("Houston", 12, 100, 95, 105, "above 95°F", "BAD_hou_winter_heatwave"),
        ("Seattle", 7, 150, 145, 155, "above 145°F", "BAD_implausible_extreme"),
        ("LA", 1, 5, 0, 10, "above 0°F", "BAD_la_arctic"),
    ]
    for city, month, mean, p10, p90, bracket, label in bad:
        cases.append({
            "label": label, "city": city,
            "target_date": f"2026-{month:02d}-15",
            "ensemble_mean": mean, "ensemble_p10": p10, "ensemble_p90": p90,
            "month_name": MONTHS[month - 1],
            "climo_mean": CLIMO_HIGH_F[city][month - 1],
            "bracket_human": bracket,
            "calibrated_p": 0.5, "yes_price": 0.5, "bracket_str": bracket,
            "expected": "VETO",
        })
    return cases


# ── Runner ───────────────────────────────────────────────────────────────────
def run_pass(cases: list[dict], pass_name: str) -> list[dict]:
    """Call both prompts on every case in parallel."""
    results = []

    def _one(case: dict) -> dict:
        out = dict(case)
        try:
            out["current"] = current_verdict(case)
        except Exception as e:
            out["current"] = f"ERROR:{e}"
        try:
            out["new"] = new_verdict(case)
        except Exception as e:
            out["new"] = f"ERROR:{e}"
        return out

    print(f"\n=== {pass_name}: {len(cases)} cases ===")
    # Throttle to stay under 50 RPM (we make 2 calls per case → max 25 cases/min).
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_one, c) for c in cases]
        for i, f in enumerate(as_completed(futures), 1):
            r = f.result()
            label = r.get("label") or f"#{r.get('trade_id', '?')} {r.get('ticker', '')}"
            print(f"  [{i:>3}/{len(cases)}] {label[:30]:30s} cur={r['current']:10s} new={r['new']:10s}")
            results.append(r)
    return results


def report(results: list[dict], header: str) -> None:
    print(f"\n──── {header} ────")
    if all("expected" in r for r in results):
        # Synthetic
        for prompt in ("current", "new"):
            tp = sum(1 for r in results if r["expected"] == "VETO" and r[prompt] == "VETO")
            tn = sum(1 for r in results if r["expected"] == "PASS" and r[prompt] != "VETO")
            fp = sum(1 for r in results if r["expected"] == "PASS" and r[prompt] == "VETO")
            fn = sum(1 for r in results if r["expected"] == "VETO" and r[prompt] != "VETO")
            print(f"\n{prompt} prompt:")
            print(f"  caught known-bad:    {tp}/10")
            print(f"  passed plausible:    {tn}/10")
            print(f"  false positives:     {fp}/10  (vetoed something plausible)")
            print(f"  missed bad:          {fn}/10  (let bad through)")
    else:
        # Historical: split by winner / loser
        for prompt in ("current", "new"):
            wins_pass = sum(1 for r in results if r["winner"] and r[prompt] == "PASS")
            wins_veto = sum(1 for r in results if r["winner"] and r[prompt] == "VETO")
            wins_unc  = sum(1 for r in results if r["winner"] and r[prompt] == "UNCERTAIN")
            losers_pass = sum(1 for r in results if not r["winner"] and r[prompt] == "PASS")
            losers_veto = sum(1 for r in results if not r["winner"] and r[prompt] == "VETO")
            losers_unc  = sum(1 for r in results if not r["winner"] and r[prompt] == "UNCERTAIN")
            wins_total = sum(1 for r in results if r["winner"])
            losers_total = sum(1 for r in results if not r["winner"])
            wins_pl = sum(r["profit_loss"] for r in results if r["winner"] and r[prompt] == "PASS")
            losers_pl = sum(r["profit_loss"] for r in results if not r["winner"] and r[prompt] == "PASS")
            print(f"\n{prompt} prompt:")
            print(f"  WINNERS  (n={wins_total}): pass={wins_pass} veto={wins_veto} uncertain={wins_unc}")
            print(f"           captured P&L on PASS only: ${wins_pl:.2f}")
            print(f"  LOSERS   (n={losers_total}): pass={losers_pass} veto={losers_veto} uncertain={losers_unc}")
            print(f"           captured P&L on PASS only: ${losers_pl:.2f}")
            print(f"  NET P&L if we acted on PASS only: ${wins_pl + losers_pl:.2f}")
            print(f"  vs full P&L (acting on everything): ${sum(r['profit_loss'] for r in results):.2f}")


def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        # Read from .env
        for line in open(".env"):
            if line.startswith("ANTHROPIC_API_KEY="):
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip()
                break

    t0 = time.time()
    historical = build_historical_cases()
    print(f"Built {len(historical)} historical cases.")
    synthetic = build_synthetic_cases()
    print(f"Built {len(synthetic)} synthetic cases.")

    hist_results = run_pass(historical, "HISTORICAL")
    synth_results = run_pass(synthetic, "SYNTHETIC")

    # CSV detail
    out_path = Path("data/veto_backtest.csv")
    out_path.parent.mkdir(exist_ok=True)
    with out_path.open("w", newline="") as f:
        keys = [
            "trade_id", "label", "ticker", "city", "target_date",
            "bracket_human", "calibrated_p", "yes_price",
            "ensemble_mean", "climo_mean",
            "outcome", "profit_loss", "winner", "expected",
            "current", "new",
        ]
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in hist_results + synth_results:
            w.writerow(r)
    print(f"\nDetail logged to {out_path}")

    report(hist_results, "HISTORICAL TRADES")
    report(synth_results, "SYNTHETIC STRESS")
    print(f"\nElapsed: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
