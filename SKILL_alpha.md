# SKILL_alpha.md — Maker rulebook and graveyard

This file is the maker's persistent memory across loop cycles. The prescan
and checker are pure-Python arithmetic; this is where *judgment* accumulates.

**Machine-append only** for graveyard entries (see change_log.jsonl for the
raw record). Human edits allowed for the Active Rules section.

---

## Active Rules

*(none yet — loop has not proposed any changes)*

When the maker promotes a change, it documents the rule here with:
- What changed (parameter name, old → new value)
- Why (which loss pattern it closes)
- Gate that passed (t-stat, EV delta, Brier delta)
- Promoted at (date, n_oos at promotion)

---

## Graveyard

Approaches that have been tested to definitive NO at large n. The maker must
NOT re-propose these without a fundamentally different premise.

### G1 — GEFS weather forecast edge (thesis dead, n=1,824)
- **What**: Use GEFS 31-member ensemble probability to find mispriced Kalshi
  weather-bracket markets.
- **Why it failed**: Market Brier 0.060 vs model Brier 0.157. Market is
  sharper in every city/kind/confidence-band/lead-time partition. Zero
  sub-cohorts where model wins. Tested May–June 2026 on prod data.
- **Closed**: 2026-05-28 (see `no_edge_finding_20260528.md` in memory/)
- **Do not retry**: BUY YES on weather brackets. Model is anti-informative
  on the YES side (high cal_p → market more confident NO).

### G2 — Maker/liquidity provision on Kalshi weather
- **What**: Post resting limit orders inside the spread, collect maker rebate.
- **Why it failed**: Backtested adverse selection > spread in every bucket.
  Maker fee is 25% of taker (not zero), retail = back-of-queue = always
  adverse-selected.
- **Closed**: 2026-05-29 (see `direction_investigation_20260529.md`)
- **Do not retry**: Maker provision without evidence of fill quality > 50%.

### G3 — BUY YES direction
- **What**: Take the YES side of weather brackets.
- **Why it failed**: 0/2 paper trades won. BUY_YES_ENABLED hard-disabled
  after 5/23 because model signal is inverted on high-confidence YES calls.
- **Closed**: 2026-05-23

---

## Loop parameters (current)

| Parameter | Value | Notes |
|-----------|-------|-------|
| MIN_OOS_SAMPLES | 30 | Trades required to open prescan gate |
| CALIB_MIN_OOS | 200 | Higher bar for calibration refit |
| KILL_N | 100 | OOS trades before FLOORED declared |
| T_STAT_BAR | 2.0 | Minimum t-stat for checker PROMOTE |
| MAX_PROPOSALS | 2 | Max changes the maker may propose per cycle |

---

## How to read this file (for the maker)

1. **Active Rules** — apply these as constraints on proposals. Don't re-open.
2. **Graveyard** — don't re-propose. If a graveyard approach seems promising
   again, write it in the `reason` field of the proposal rather than silently
   re-opening — the checker will reject it on holdout anyway.
3. **Loop parameters** — these are the gate values. Do not propose changing
   KILL_N or T_STAT_BAR without a strong prior; they exist to prevent
   overfitting.
