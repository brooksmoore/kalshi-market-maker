# LEDGER — kalshi_bot_2.0 multi-venue work

Per v1 postmortem §9.7, every change ships with a one-paragraph entry: what,
why, what's intentionally not done, what to watch.

---

## 2026-07-07 — Umbrella snapshot + decisions contract wiring (read-only)

**What:** Added `snapshot_emit.py`, `decision_emit.py` (path stub only), and
`tests/test_audit_snapshot_emit_gate.py`. Snapshot maps persisted trades.db stats
when readable; degrades to `STARTING_BANKROLL` when DB unavailable. Emits
`data/state.json` with `killed=true`, `health=down`, FLOORED loop fields in
`extra`, and honest `live_gate=disarmed`. Added `-e ../umbrella` to
`requirements.txt`. Registered in `umbrella/dashboard/sources.json`.

**Why:** Fleet dashboard needs a uniform read surface for paused bots; this bot
is stopped/FLOORED and must not look active.

**Intentionally NOT done:** No decision-event emission (bot not running); no
cron hook; no changes to `src/main.py`, strategy, or executor paths.

**What to watch:** If trades.db moves, update `config.DB_FILE` resolution only —
snapshot_emit already reads via storage module.

---

## 2026-05-24 — Fix cross-bot interference + broken venv launch (ops)

**Context:**
Three local dashboards (this bot on :8082, pure_arb_bot on :8083,
Multi_Agent_Asset_Competitive_Bot on :8081) appeared to "interfere" with
each other — restarting one would take the others down. The ports were
never actually in conflict.

### What we found
- This bot's services were being launched by hand with bare `python3`
  (`nohup python3 src/main.py ...`) instead of `venv/bin/python`, so
  flask/numpy were missing and `main.py` and `dashboard.py` exited
  immediately with `ModuleNotFoundError`. Root cause of the missing modules:
  the venv was built against Xcode's bundled Python 3.8.9
  (`/Applications/Xcode.app/.../usr/bin/python3`), which is unstable across
  Xcode updates; `venv/bin/python` no longer resolves to a working binary.
- The hand-typed restart line used broad kill patterns:
  `pkill -9 -f "src/main.py"; pkill -9 -f "observer"; pkill -9 -f "dashboard.py"`.
  `pkill -f` matches the FULL command line of EVERY process, so
  `dashboard.py` / `observer` also matched the OTHER bots' processes and
  killed them. That cross-killing was the real "interference" (it showed up
  as connection-refused tabs and graceful-shutdown signal cascades on the
  Multi_Agent bot).

### What we changed
- Added `run.sh` ({start|stop|status}) that (a) always launches with
  `venv/bin/python`, and (b) only ever kills processes whose working
  directory is THIS bot's folder (cwd-scoped via `lsof -d cwd`), so it can
  never touch the other bots.

### Intentionally NOT done
- Did not rebuild the venv automatically and did not start/stop the live
  bot — these are live-capital services, so the venv rebuild is run by hand:
  `python@3.11 -m venv venv && venv/bin/pip install -r requirements.txt`.

### What to watch
- After rebuilding the venv, `./run.sh start` should show no
  `ModuleNotFoundError` in `logs/dashboard.log`, and :8082 should load.
- Never reintroduce bare `pkill -f dashboard.py` / `observer` — always scope
  kills to this bot's folder (run.sh does this).

---

## 2026-05-23 — Paper-trade persistence (live paper trading layer)

**Context:**
Phase A gates were shipped but the Phase 0 backtest had a structural
limitation: it used each ticker's first observation as the trade price,
not the cycle when the bot would actually trade. User flagged this and
asked for a real-time paper trading layer before flipping live capital.

The bot already supports `LIVE_TRADING_ENABLED=false` (returns "dry-run"
without placing orders). What was missing: persistence of the dry-run
decisions so they can be scored against real settlements.

### What we found before fixes

- Existing dry-run path (`src/main.py:535`) intentionally does NOT write
  to `trades.db` — would trip the exposure cache into thinking they were
  open positions and halt the bot on next cycle.
- Cycle summary captures dry-run trade *counts* but not per-decision
  detail. Zero ground truth for scoring.

### What shipped

1. **`src/paper_storage.py`** — new isolated DB at `data/paper_trades.db`
   with two tables: `paper_trade` (decision-time data) and `paper_result`
   (settlement-time data). Mirrors the trades/results schema split. Uses
   the same autocommit + 30s-timeout locking pattern as the observer DBs.
   `log_paper_trade(opp)` never raises — failures log a warning, bot
   cycle continues.
2. **`src/main.py:_process_directional`** — when LIVE_TRADING_ENABLED is
   false and an opportunity passes all gates, calls
   `paper_storage.log_paper_trade(opp, bankroll=...)`. Existing comment
   about not writing to trades.db preserved + amended.
3. **`scripts/score_paper_trades.py`** — read tool that joins paper_trade
   rows to Kalshi `/markets/{ticker}` settlements, persists outcomes to
   `paper_result`, reports aggregate + per-direction + per-city + per
   cal_p band stats. Dedupes to FIRST paper trade per (ticker, venue)
   to match what production held-position dedup would do.

### Operational protocol for live paper trading

Pre-flight (one-time):
1. Stop bot if running.
2. Update `.env`: prod URLs, prod KEY_ID, prod pem path.
3. Run `venv/bin/python scripts/reset_performance.py` (NO --wipe-trades) —
   stamps new prod venue signature on performance.json.
4. Set `LIVE_TRADING_ENABLED=false` in `.env`.
5. Start bot.

Watch (continuous):
- Bot will hit prod markets, evaluate Phase A gates, return "dry-run"
  on passes, log each to paper_trades.db.
- Run `venv/bin/python scripts/score_paper_trades.py` periodically to
  see settlement-scored EV. Realistic to see first results within 24h
  (markets that close overnight).

Decision criteria for moving to live:
- Hard test (after ≥30 settled paper trades): EV/contract > +$0.02.
  If yes, flip LIVE_TRADING_ENABLED=true. If no, debug or pivot to v3.
- Per-city sanity (after ≥10 trades/city for any high-volume city):
  no city below −$0.05/contract.

### Intentionally NOT done

- **No paper-side held-position dedup.** The bot will re-log the same
  ticker across cycles if it keeps passing gates. The scoring script
  handles this by taking FIRST paper trade per ticker. Production
  held-set dedup will kick in once we flip to live (reads from trades.db
  which is unaffected by paper logging).
- **No simulation of maker logic.** Paper trade entry_price is the
  observed ask we'd pay as a taker. Real bot does maker-then-taker,
  potentially filling at better prices. Conservative assumption.
- **No adverse-selection modeling.** Treats every paper trade as if
  it fills at observed ask. Real fills are subject to counterparty
  selection. Paper EV is an upper bound on real-fill EV — by how much
  is the open question only real capital can answer.

### What to watch

- First `[STRATEGY] rejections: ...` lines should show non-zero
  `city_excluded` (PHX/LV being filtered).
- `data/paper_trades.db` should grow as the bot finds Phase A-passing
  trades — expect ~7/day from Phase 0 estimate, likely 5-15/day real.
- First settled paper trades arrive ~24h after first cycle (overnight
  east-coast city closes).
- 17/17 kill-switch dry-run still passes.

---

## 2026-05-23 — Phase A gates shipped: BUY_YES_CAL_P_CAP=0.60, BUY_NO band, exclude PHX/LV

**Context:**
Phase 0 validation (n=1186 settled signals, ~234h) and per-city deep dive
established the dominant signal: direction asymmetry, not city. BUY YES
catastrophic at high cal_p across all cities; BUY NO EV-positive in a
narrow cal_p band; PHX/LV specifically broken (0/15 confident YES).

Tried two paths in parallel:
- **Path 1** — hand-tuned gates (BUY NO cal_p∈[0.15,0.30), BUY YES cap 0.60,
  exclude PHX/LV). Full-cohort backtest: 70 trades, 53% wins, EV
  +$0.034/contract, cohort total +$2.39 over 234h.
- **Path 2** — isotonic refit on n=948 train cohort, eval on n=238 test.
  Failed: isotonic's monotonic constraint can't represent our model's
  *inverted* high-confidence signal (model says 0.85, actual is 0.10).
  Plateau at cal_p=0.265 for all raw_p ≥ 0.30 means broken BUY YES
  trades still triggered. Net EV/contract −$0.04.

### Why Path 1 worked and Path 2 didn't

Isotonic regression preserves monotonicity. Our model's raw_p in [0.5, 1.0]
maps to *lower* empirical yes-rates than raw_p in [0.2, 0.3] — the
relationship is non-monotonic. The best monotonic fit pools the entire
high-raw_p range to a single value (0.265), which doesn't kill the broken
regime; the bot would still pick BUY YES at edge=0.265−yes_ask. Phase 1's
hard cal_p cap is the right shape for the underlying signal.

### What shipped

1. **`src/config.py`** — new constants with documented derivation:
   - `BUY_YES_CAL_P_CAP = 0.60` (tightened from previous `>=0.85` guard)
   - `BUY_NO_CAL_P_LO = 0.15`
   - `BUY_NO_CAL_P_HI = 0.30`
   - `EXCLUDE_CITIES = frozenset({"Phoenix", "Las Vegas"})`
   - Multi-paragraph comment block preserves the per-cal_p-band EV
     stratification and Path 1 backtest results.

2. **`src/strategy.py:find_opportunities`** — three gate additions:
   - Early skip + `rej["city_excluded"]` counter when city in
     `EXCLUDE_CITIES`. Observer continues collecting these cities for
     ongoing measurement; trading skips them.
   - `yes_filtered` now uses `BUY_YES_CAL_P_CAP` (was hard-coded 0.85).
   - `no_filtered` adds the cal_p band check on top of the existing
     5/03 disagreement guard.

3. **Phase 0 validation scripts retained** in `scripts/` for repeat
   evaluation: `validate_phase_a_gates.py`, `phase_0_revised.py`,
   `per_city_deep_dive.py`, `deep_analysis_20260517.py`.

### Kill-switch dry-run: 17/17 pass with new gates.

### Intentionally NOT done

- **No isotonic calibration ship.** The fit doesn't help — non-monotonic
  signal can't be repaired by monotonic regression. Revisit only when
  v3 fixes the underlying model (METAR/HRRR), at which point isotonic
  on the v3 forecaster's output is a different problem.
- **No watch-list city exclusion yet.** Oklahoma City, New Orleans, and
  Philadelphia showed negative BUY-NO EV in shadow data but tiny n
  (3-4 trades). Don't pre-emptively exclude; add to EXCLUDE_CITIES if
  real-fill EV confirms negative over n≥10.
- **No bankroll/sizing changes.** $25 bankroll + 5% per-trade cap +
  MIN_POSITION=$0.50 floor produces ~1-contract trades, which is
  appropriate for the validation cohort.
- **No live trading yet.** Phase A is the *pre-flip* hardening. The
  flip (Phase B) is a separate explicit decision.

### What to watch (post-flip, when Phase B runs)

- `[STRATEGY] rejections: ... city_excluded=N` should be non-zero on
  every cycle (PHX/LV are always in the universe; just rejected from
  trading).
- First 20 fills: target EV/contract > 0 cumulative. If < −$0.05,
  halt and review.
- First 50 fills: per-city EV — if OKC/NOLA/PHIL show real negative
  EV, add to `EXCLUDE_CITIES`.
- `cal_p` distribution of fills should cluster in [0.15, 0.30) for
  BUY_NO and rarely exceed 0.60 for BUY_YES. If the bot is making
  trades outside these ranges, the gates aren't wired right.

---

## 2026-05-23 — Per-city deep dive: direction is the signal, not city

**Context:**
After two weeks of shadow-audit data (n=1177 settled signals), surface-level
metrics showed Boston near-parity (Brier ratio 1.06×) and Phoenix/Las Vegas
catastrophic (2.34-2.42×). User asked to investigate the surprising numbers
before acting on them. The dig produced a sharper read.

### What we found

- **The Boston near-parity was sample-size noise.** Bootstrap 90% CI on
  Boston's Brier ratio is [0.84, 1.32]; only 37% of resamples show
  model ≤ market. n=68 is not enough to claim city-restricted edge.
- **The catastrophic cities aren't bad because the climate is hard —
  they're bad because the market is unusually sharp there.** Phoenix
  market Brier = 0.095 (best in cohort) vs 0.135 in Boston. The PHX/LV
  T-ticker market Brier is **0.003** — questions like "will PHX exceed
  105°F in May?" are essentially decided at listing.
- **The dominant signal is direction, not city.** When the model is
  confident BUY NO (`cal_p < 0.3`), it's right 75-79% of the time
  across virtually every city. When it's confident BUY YES (`cal_p ≥
  0.7`), it's right ~13% globally — **0/15** in PHX+LV combined.
- **LAX has the OPPOSITE bias from the global pattern.** 10/12 LAX
  confident-wrong markets had cal_p low / outcome YES (model said
  cool, day was hot). Likely marine-layer dynamics confusing GEFS into
  averaging cool-morning + warm-offshore conditions into a too-cool
  forecast. Most other cities don't show this; PHX/LV show the opposite
  direction (too-warm confident YES).
- **Spread inflation worked partially (1.43× → 1.33× ratio, n=657/520
  pre/post).** Fixed the confident-NO band calibration cleanly (gap
  +0.054 → +0.017 in [0.0, 0.1) cal_p) but only modestly improved the
  confident-YES tail. The remaining BUY-YES failures are structural,
  not pure under-dispersion.

### Tools shipped

- `scripts/deep_analysis_20260517.py` — comprehensive multi-section
  analysis: pre/post inflation calibration, per-city decomp, lead time,
  GEFS run age, realized vs predicted dispersion, METAR overlap,
  outlier markets.
- `scripts/per_city_deep_dive.py` — per-city Brier with bootstrap CIs,
  base-rate normalization, cal_p decisiveness, direction-of-failure
  breakdown, Boston/PHX/LV/LAX targeted analyses.

### Intentionally NOT done

- No live trading yet. The findings justify a flip but only with
  pre-flip hardening (next ledger entry).
- No per-city CLI_BIAS refit. LAX cool-bias hypothesis exists but n=12
  outliers isn't sufficient evidence for a load-bearing per-city shift.
  Would need ≥30 confident-wrong LAX markets to refit confidently.
- No PHX/LV exclusion from trade universe. Without confident-YES
  enabled, PHX/LV harm vanishes — the city restriction was solving
  the wrong problem.

### What to watch

- The asymmetric MIN_EDGE + cal_p cap (next ledger entry) should make
  ~all of the historical confident-YES failures filter out. Validate
  retroactively against the n=1177 cohort before flipping.
- LAX direction-of-failure persists in fresh data: if next 20 LAX
  confident-wrong are still mostly "model cool, day hot," that's a real
  city-specific bias and would justify a per-city correction.

---

## 2026-05-17 — Diagnosis revision: under-dispersion, spread inflation, METAR collection

**Context:**
First two weeks of v3 shadow audit showed v2 model Brier ratio stuck at
1.40-1.43× vs market across n=55→700, with strong-disagreement model
win rate locked at 24-27%. Initial hypothesis was GEFS warm bias; this
was tested and refuted.

### What we found

- **GEFS is NOT warm-biased.** Direct measurement of GEFS forecast vs
  CLI settlement across n=108 events: mean −0.44°F (essentially zero),
  median 0.00°F, stdev 2.23°F. The original `cli_gap_audit.py`
  measured CLI-vs-ASOS gap (~+0.6°F) and applied it as a GEFS shift
  on the untested assumption "GEFS is ASOS-aligned." That assumption
  was wrong.
- **The real failure mode is ensemble under-dispersion.** Predicted
  GEFS ensemble SD median 1.43°F (n=168 fresh signals); realized
  forecast error stdev 2.23°F. Ratio 0.64× → actual forecast
  uncertainty is ~1.55× wider than GEFS suggests. The calibration
  bucket signature (both ends biased away from 0.5) is the classic
  under-dispersion fingerprint.
- **CLI_BIAS correction was making things worse for cities that
  didn't need it.** Original values shifted forecasts up +0.5-1.0°F
  uniformly; actual mean bias is ~0. Zeroed across all cities.

### Fixes shipped

1. **`CLI_BIAS = 0.0` for all 19 cities** in `src/config.py`. Comment
   preserves measurement history and rationale.
2. **`SPREAD_INFLATION_FACTOR = 1.55`** in `src/config.py`, applied in
   `forecast.get_ensemble_high()` after CLI_BIAS, before bracket
   probability calc. Each member moves radially outward from ensemble
   mean: `m' = mean + (m - mean) * 1.55`. Mathematically principled
   correction for under-dispersion.
3. **`compute_market_cal_p_full()`** added to `strategy.py` — returns
   dict with cal_p, raw_p, ensemble_mean, ensemble_sd, ensemble_n.
   Original `compute_market_cal_p()` preserved as thin wrapper.
4. **Shadow logger schema migrated** with `ensemble_mean`, `ensemble_sd`,
   `ensemble_n`, `raw_p` columns via `_apply_additive_columns()`
   (idempotent ALTER TABLE).
5. **`scripts/metar_logger.py`** — new passive daemon, ~15min cycle,
   pulls hourly NWS METAR observations for all 19 city airports into
   `data/metar_observations.db`. NOT used by the model — prep for
   Checkpoint 1. All 19 stations validated on first run.
6. **Hardened `phase2_shadow_logger.py`** with 2-tuple timeouts,
   broader exception catching, persistent session, and a 600s
   wall-clock alarm. Survives network drops cleanly; outer
   `while-true; sleep 3600` loop retries an hour later. Added after
   the 5/12 silent-hang incident during the user's commute.
7. **Locking fix**: shadow logger and poly_observer switched to
   autocommit mode + 30s timeout. Eliminates "database is locked"
   errors when shadow's loop overlaps with kalshi observer writes.
8. **`scripts/run_all_observers.py`** updated to spawn METAR as a
   fourth subprocess. One-terminal management for all four collectors.

### Intentionally NOT done

- **No per-city `SPREAD_INFLATION_FACTOR`** despite suggestive per-city
  variation in measured bias. Per-city sample sizes (n=4-7) too small
  to fit reliably. Refit when n ≥ 30/city.
- **No auto-tuning of the inflation factor.** TODO in `config.py`
  points at a future `scripts/fit_spread_inflation.py`; manual refit
  cadence weekly→monthly. Auto-adjusted load-bearing parameters are
  hard to reason about.
- **No `ensemble_kurtosis` or higher moments** in shadow_signal. The
  ratio of predicted-to-realized SD is the dominant signal; higher
  moments would be fitting noise at current sample sizes.

### What to watch

- Brier ratio change between pre- and post-inflation cohorts as the
  post cohort grows. Initial measurement at small post-n showed
  1.43× → 1.33× — promising but small sample.
- Calibration buckets — [0.0, 0.1) gap should drop toward 0 (it did,
  from +0.054 to +0.017). [0.7, 1.0) gap should also compress (it
  partially did but remains the dominant failure mode).
- METAR DB row count should grow ~600/hr (≈30 obs/hr × 19 stations).
  If it stops growing, NWS API may have changed.

---

## 2026-05-10 — Shadow audit infrastructure (prod observer, Phase 2 logger, harm-reduction)

**Context:**
2026-05-10 demo-vs-prod audit revealed Kalshi demo is not a faithful prod
mirror — different tick grid, 4× wider spreads, 100-1000× thinner depth,
prices uncoupled from real weather. The 5/9 "model broadly miscalibrated"
edge investigation was retroactively invalidated (cohort was demo-priced
counterparties not pricing weather at all). Decision: build shadow audit
infrastructure to measure forecast quality against PROD prices before any
capital flips.

### What we found before fixes

- **Demo→prod divergence.** Demo tick grid is `tapered_deci_cent`; prod is
  `linear_cent`. Demo spreads ~15c; prod 1-3c. Demo top-of-book depth
  often 1-5 contracts; prod typically 50-500+. Most damningly, demo
  prices don't move with real weather — they wander based on demo-counterparty
  positioning.
- **Halt thresholds were demo-era.** Calibrated assuming losses didn't
  matter. Tightened ahead of prod flip.
- **No measurement framework existed.** v2 had no way to know whether the
  model had forecast edge against prod prices without flipping live and
  risking capital — exactly the v1 mistake.

### Fixes shipped

1. **`scripts/prod_observer.py`** — 5-min cadence scraper of Kalshi PROD
   public order books. Writes to `data/prod_observer.db` (separate from
   trades.db; WAL mode for concurrent reader access). 4-worker parallel
   discovery + per-series book fetch. SIGINT-clean shutdown.
2. **`scripts/phase2_shadow_logger.py`** — hourly logger of model
   forecasts paired with prod book prices. New `shadow_signal` table.
   **Discipline:** logs `calibrated_p` + `prod_yes_mid` only. NO synthetic
   P&L column. Joinable to settlement outcomes for forecast-Brier.
3. **`scripts/market_calibration.py`** — analysis tool that joins
   settled markets to observed book prices to measure how informed
   prod pricing is. First finding: market Brier ~0.08 across n=114 settled
   markets — prod is sharply informed.
4. **`scripts/kill_switch_dry_run.py`** — exercises every halt/cap
   against a tempdir performance.json. 17 distinct checks. Re-run
   after any risk.py edit, immediately before any live flip.
5. **`scripts/reset_performance.py`** with `--wipe-trades` flag — archives
   trades.db at demo→prod flip since the existing `venue` column only
   distinguishes Kalshi/Polymarket, not demo-Kalshi/prod-Kalshi.
6. **Venue-signature fail-closed safety** in `src/risk.py`. Bot startup
   refuses to boot if stored sig ≠ current env sig — catches accidental
   demo↔prod credential swap before peak_bankroll baseline is corrupted.
7. **Tightened halt thresholds** in `src/config.py`:
   - `MAX_DRAWDOWN_PCT` 33% → 25%
   - `MAX_SINGLE_BET_PCT` 5% → 2.5% (later reverted 5/11 to 5% after
     contract-granularity issue surfaced; see config comment)
   - `DAILY_LOSS_LIMIT_PCT` 20% → 15%
   - `MIN_POSITION` 1.00 → 0.50 (so absolute floor never overrides %
     cap at small bankroll)
8. **`scripts/poly_observer.py`** — Polymarket cross-venue observer
   built 5-11. Combined observer + shadow logger. First-cycle finding:
   23/44 Polymarket weather markets have empty NO-token book; rest
   show ~100c cross-token spreads. Polymarket weather is essentially
   non-trade-able for our strategy — not a viable second venue.

### Intentionally NOT done

- **No live trading.** Bot stays on demo while shadow data accumulates.
- **No HRRR/METAR ingest yet.** Deferred to v3 plan. Phase 2 logger
  measures the gap that ingesting these would close.
- **No model changes during this phase.** Don't bundle architecture
  changes with measurement infrastructure.

### What to watch

- Daily counts of `book_snapshot` rows (target ~5000/day) and
  `shadow_signal` rows (target ~150/run × 24 runs/day = ~3600/day).
- `data/prod_observer.db` size growth. WAL files are gitignored.
- First settled cohort scoring — wait for n ≥ 50 settled before
  drawing conclusions.

---

## 2026-05-09 — Post-bleed audit: spread gate, Wilson sizing, halt visibility, drawdown reset

**Context:**
2.5 days into post-reset live trading the bot bled $123 of $430 (28%
realized, 33% by realized peak) and tripped the drawdown halt. Audit
unwound two failure modes plus surfaced a UX gap.

### What we found

- **Ghost order books on 1°F bins.** Three of five open positions on
  2026-05-09 had no-side spreads of $0.43–$0.54. The bot was reading
  edge against `no_ask` on books with no real seller — "30–40% edge"
  was largely fictional.
- **Small-N tail-bin overconfidence.** `cal_p` is `k/N` with N≈37 GEFS
  members. For 3/37 (the most common value, 14 of 35 trades) the
  Wilson 95% CI is [0.028, 0.213] — a "70% edge" was a 30% edge under
  a worst-case-but-plausible read of the same data.
- **Pre-reset baseline was overstated.** Strip 5/04–5/05 contamination
  (demo outage backfill + phantom resolution bug) and clean P&L was
  +$31 over 11 days, not +$257. Model-driven 1°F BUY NO had been
  ~breakeven the whole time; the apparent profit engine was T-tickers
  and outage-recovery backfill.
- **Halt invisible to user.** Dashboard pill was wired to env config
  flag, not runtime state — said "LIVE" while bot was being blocked
  every cycle. Telegram halt notification fired once-per-process and
  never re-pinged.

### Fixes shipped

1. **Spread-tightness gate on B-tickers** — `strategy.py` pulls
   `no_bid_dollars` and rejects B-ticker BUY NO if `no_bid <= 0` or
   `(no_ask - no_bid) > 0.10`. New `wide_spread` rejection bucket.
2. **Wilson-shrunk Kelly sizing** — `_wilson_bounds(k, n)` helper;
   `kelly_size` takes optional `p_for_sizing` kwarg. BUY NO sizes
   against Wilson-upper, BUY YES against Wilson-lower. Math is
   correct; at current $300 bankroll the 5% per-bet cap binds first
   for almost any positive-edge trade so this is mostly cosmetic
   *today* — binds for real at higher bankroll or relaxed caps.
3. **Segment P&L dashboard** — new `/api/analytics` field cross-tabs
   resolved trades by `kind (T/B) × action × entry_band × edge_band`.
   Renders as a sortable table on the dashboard. Excludes arb/dry-run/
   paper/backfill.
4. **Halt visibility** — dashboard pill calls `risk.can_trade()` so
   "HALTED — DRAWDOWN: 33.1%" actually shows. Telegram halt re-pings
   every 6h while still halted (not once-per-process). Hourly digest
   includes halt line in header when active.

### Drawdown reset

Rebased `peak_pnl` from +$28.61 to current realized −$123.13, so
peak_bankroll = realized_bankroll = $307.31 and drawdown returns to
0%. `starting_bankroll` unchanged so "vs start" P&L stays honest.
**Trades and results tables intentionally NOT wiped** — the post-reset
data is what informed this audit's segment analysis and is needed for
future calibration. Prior `performance.json` archived to
`data/_archive/performance.json.before_reset_20260509_150448`. A
`peak_reset_note` field in the live `performance.json` documents the
reset inline.

### What we explicitly did NOT do (and why)

- **Sweet-spot entry/edge gate** (entry $0.60–0.75, edge 0.20–0.35).
  N=10 in the "sweet spot" gives Wilson CI [55%, 99%] on hit rate —
  too small to pre-commit a hard rule. The spread gate + Wilson sizing
  target the same failure mode without overfitting.
- **Default shrinkage 0.7.** C1 option A's reasoning still stands:
  M8 ties shrinkage to *measured* calibration error and we have no
  measurement. Wilson is the principled per-trade equivalent;
  stacking 0.7 on top would double-count.
- **Parametric distribution probability.** Right answer long-term —
  fit a smooth distribution to the GEFS members and integrate over
  the bin to lift effective sample size. Deferred to avoid conflating
  two interventions; revisit at ~50 resolved 1°F BUY NO trades under
  the new gates.
- **Wipe trades/results.** Done at the 5/06 reset because data was
  bad; current data is good and informative. Drawdown clock alone
  was reset.

### What to watch

- **B-ticker BUY NO hit rate** under the new gates. Pre-reset clean
  was 67% (-$0.96, breakeven); current was 53% before fixes. Need
  ~30 more resolved B BUY NO trades to evaluate.
- **`wide_spread` rejection counter** in scan_log breakdown — if
  most B-ticker opps now reject for spread, the strategy is shrinking
  to T-tickers in practice and we should plan accordingly.
- **Dashboard drawdown vs halt drawdown.** Dashboard uses MTM-inclusive
  bankroll; risk halt uses realized only. These can diverge by
  several percentage points until open positions resolve. Not a bug
  today; could be aligned later if the discrepancy keeps confusing.

---

## 2026-05-03 — Polymarket universe probe + three correctness fixes

**Context:**
User asked to probe whether Polymarket has tradeable weather markets we
were silently dropping. Probe surfaced **307 weather markets in the active
universe** (vs 33 we were canonicalizing) and uncovered three real bugs
along the way that were independently blocking Polymarket activity AND
silently degrading Kalshi activity.

### What the probe found

- **274 high-temp markets** in the active universe (we were keeping 33)
- **207 international** (Karachi, Wuhan, Madrid, Sao Paulo, Hong Kong, …)
  — correctly dropped, no Kalshi counterpart, would need new city patterns
  + lat/lon. Defer.
- **59 US markets** — all 1°F bins (gate correctly drops for BUY YES,
  user-modified strategy already permits BUY NO on 1°F bins so these
  reach scoring)
- **12 wider US markets** in `"X°F or higher"` / `"X°F or below"` format
  — *previously dropped as `threshold_unparseable`* because the regex only
  matched "reach 75°F" / "above 75°F" forms. Real US tradeable markets
  in cities we already support (SF, Atlanta, NYC, LA, Seattle, Dallas,
  Denver). Fixed.

### Fix 1 — canonicalizer threshold patterns

`resolution_rules._THRESHOLD_GTE` and `_THRESHOLD_LT` now also match
`"<NUMBER><UNIT> or higher"` / `"<NUMBER><UNIT> or below"`. Implemented
as alternation; `canonicalize_polymarket_market` reads `group(1) or
group(2)` since either alternative may match. 6 unit cases pass.
Live re-check: now finds 6 wider tradeable markets (SF<47, SF<47,
Atlanta>=92, Denver<45, NYC>=90, LA>=72) in addition to the 25 1°F bins.

### Fix 2 — strategy `_target_date_from_market` was using `close_time`

This is the **same bug** we caught in `cross_venue.py` yesterday (LAX
phantom arb), in a different file. `_target_date_from_market` was
preferring `close_time[:10]` over title parsing. For ANY US market
(every city is west of UTC), `close_time` is stamped at end-of-local-day
which falls on the NEXT UTC day. Fixed: `_target_date_from_market`
now delegates to `_shared_parse_date` (title-first), with `close_time`
only as a last-ditch fallback.

**Historical-trade impact (verified 2026-05-03 after-the-fact):**
The buggy `_target_date_from_market` only existed from phase-3a
(2026-05-02) onward. Before that, strategy used `_target_date_from_title`
directly, which always parsed the title regex correctly. DB query:
**88 of 91 non-arb resolved trades opened before 2026-05-02** and used
the correct (title-parsed) date. Only **3 trades** opened in the
phase-3a window could have been affected. The 57.1% non-arb win rate
is real signal, not autocorrelation luck. Calibration data is honest.

A previous draft of this entry incorrectly claimed all 91 trades were
generated against wrong-day forecasts — that was a strong claim made
without verification, and it was wrong. Verified by pulling 8 actual
historical Kalshi market titles via `kalshi_client.get_market(ticker)`
and confirming `_target_date_from_title` parses them to the correct
measurement date (2026-05-02 for KXHIGHCHI-26MAY02-T51 etc).

**Live impact going forward:** the fix prevents the bug from accumulating
in any future trades. No retroactive damage to fix. The opportunity-rate
jump observed in the post-fix scan (~11 vs typical 0–2) is most likely
from the broader 228-market universe surfaced by phase-1.5 dynamic
discovery feeding through to scoring, not from the date fix itself —
my earlier attribution was loose and shouldn't be trusted without more
cycles of data.

### Fix 3 — `evaluate_trade` (constitution gate) was reading live Kalshi bankroll

`config.evaluate_trade(opp)` always called `risk.get_active_bankroll()`
to compute the 5%-of-bankroll size cap. For Polymarket paper trades
sized against `STARTING_BANKROLL = $100`, the cap was being computed
against the live Kalshi balance (~$78), so any paper trade ≥ $3.91
was rejected as `OVERSIZE`. This blocked every Polymarket opportunity
the moment Kelly recommended ≥ $4.

Fix: `evaluate_trade(opp, bankroll=None)` accepts an explicit override.
`strategy.find_opportunities` now passes the bankroll it was given;
`main._process_opportunity` passes `STARTING_BANKROLL` for Polymarket.

### What surfaces now

Live re-score with all three fixes (current snapshot):
```
Polymarket opportunities: 3
  [NYC]     BUY NO  58-59°F May 3   edge=+40.1%  entry=$0.41
  [Denver]  BUY NO  <45°F May 5     edge=+19.4%  entry=$0.725
  [Atlanta] BUY NO  78-79°F May 4   edge=+17.4%  entry=$0.61

Kalshi opportunities: 11   (BUY NOs across LA, Las Vegas, OKC, Miami,
                            San Antonio, etc — 17–52% edges)
```

These are sized against last-trade prices, not best ask. The paper
executor refetches the live book at maker-post time and uses real
best ask, so any "fake edge" from stale last-trade prices self-corrects
at posting time (the maker order will land at the right price even if
the strategy's pre-fee edge looked too rosy). Worst case: a maker
order that won't fill because the actual best ask is far below our
limit. Cheap to discover.

**Intentionally NOT done:**
- **Polymarket book-fetch in scoring loop.** Would give true best-ask
  edges but adds ~30 HTTP calls/cycle. Defer until we see whether the
  current pipeline actually fills any orders.
- **International cities (207 markets).** Need new patterns + lat/lon
  + station mapping. Real opportunity but real effort. Wait for the
  US side to prove itself first.
- **Re-fitting calibration on the corrected-date trades.** The 91
  resolved Kalshi trades were generated against wrong-day forecasts.
  Their calibration data is partly noise. Phase-3c-ish work; defer.

**What to watch:**
- Next cycle should show **paper:pending:N** verdicts in the breakdown
  for the 3 Polymarket opps. They'll appear in the `/polymarket`
  dashboard pending-orders panel.
- Kalshi opportunity rate jumps. Demo trades will spike. Monitor:
  if the per-city win rate on dashboard drops below historical 57%,
  the new confident forecasts may be over-trading. Expected behavior:
  same-day forecasts should be MORE accurate, so win rate should hold
  or rise.

---

## 2026-05-03 — Arb segregation across all reporting

**What:**
Arbitrage trades were being lumped into the same KPI rollups as regular
BUY YES / BUY NO / maker / taker trades, distorting every win-rate metric
on the dashboard. By construction, a clean N-leg arb group has exactly
1 leg resolve YES (the bought-YES wins) and N-1 resolve NO (the bought-YES
loses) — so leg-counting always shows arbs as 1W / (N-1)L per group even
when the group is profitable.

This change carves out arb leg accounting end-to-end:

- **`storage.get_arb_group_stats()`** (new): groups by `arb_id` from notes,
  reports per-group win/loss/PnL/cost, plus a separate stranded-leg bucket
  for rollback-failed arbs whose `arb_id` is unfortunately lost in the
  `arb_stranded:` notes prefix.
- **`storage.get_resolved_arb_groups(limit)`** (new): for the dashboard
  history table.
- **`storage.get_cycle_stats()`**: `total_yes` / `total_no` / `total_wins` /
  `total_resolved` / `today_wins` / `today_resolved` / `open_positions`
  now exclude arb legs. Adds `arb_groups_*` and `bundled_*` counters
  (where bundled = non-arb legs + arb groups counted as one trade each).
  `today_pnl` stays inclusive (dollar P&L is leg-additive).
- **`storage.get_venue_pnl()`**: same — count/P&L include arb legs (real
  positions, real money), but `wins`/`resolved`/`win_rate` exclude.
- **Dashboard `/api/kpis`**: BUY YES, BUY NO, maker, taker win-rate
  queries all gain `AND market_type != 'arbitrage'`. New `arb_*` fields.
  The headline "Total win rate" now uses bundled totals (legs + groups).
- **Dashboard `/api/positions`**: returns `market_type`, `is_arb`, and
  `arb_id`. Frontend renders an `arb` pill in place of maker/taker for
  arb legs. Hovering the pill shows the arb_id.
- **Dashboard `/api/trades`** (recent resolved table): excludes arb legs
  — they have their own bundled history in the arb tracker.
- **Dashboard `/api/calibration`**: excludes arb legs. Critical because
  `strategy_arb` sets `calibrated_p == yes_price` by construction (arb
  bypasses the model), so including them would inject N degenerate
  perfectly-calibrated datapoints per arb group and inflate apparent
  reliability.
- **Dashboard `/api/analytics`**: edge scatter, per-city win rate, edge
  calibration, and Brier score queries all gain the arb filter for the
  same reason. Daily P&L stays inclusive.
- **Dashboard `/api/arbs`** (new): summary + open groups + history for
  the new Arb Tracker section.
- **HTML**: new `Arb win rate` KPI panel; `Total win rate` label gains
  "(arb groups bundled as 1)" subtitle; positions table shows `arb`
  pill via new `pill-arb` style; new `Arb tracker` section above the
  Daily P&L chart with side-by-side open-groups / resolved-history tables.

**Why now:**
After overnight running, the user noticed arbs were being executed but
weren't visible as such on the dashboard, and v1's segregation discipline
hadn't carried over to v2. Compounding: the recent paper-trading harnesses
(phases 3a/3b) had also been written without the arb consideration, and
their per-venue P&L queries would have started double-counting arbs the
moment Polymarket execution landed. Caught early.

Real DB state at fix-time:
  - 9 arb legs, 3 distinct groups (1 resolved at +$1.45, 2 open)
  - 0 stranded legs
  - Win rate INCL arbs: 56.4%, EXCL arbs: 57.1% (small contamination
    today, would compound)

**Intentionally NOT done:**
- **Stranded-leg arb_id reconstruction.** The `arb_stranded:` notes
  prefix doesn't preserve the original `arb_id`, so we can't bundle
  stranded legs into their original group. Dashboard surfaces the count
  separately as a warning. Fix would require widening the notes format,
  which would force a parsing migration; defer.
- **Today-only arb stats.** `arb_groups_resolved` etc. are lifetime;
  not split by day. Could add when the user's "today's P&L" framing
  becomes the bottleneck.
- **Polymarket arb path.** Polymarket has no execution yet; cross-venue
  arbs are detection-only. When phase 4 lands, paper Polymarket arbs
  will need similar handling.

**What to watch:**
- Dashboard KPI grid now shows 6 win-rate panels: Total (bundled), BUY
  YES, BUY NO, Maker, Taker, **Arb (group-bundled)**.
- Arb win rate should show **100% by design**. Anything less is a signal
  to investigate (stranded legs, fee-math drift, or reconcile lag where
  one leg resolved before others).
- Open positions table: arbs labeled `arb` (purple pill) instead of
  `maker`/`taker`.
- Arb tracker section: shows your 3 current groups (1 resolved Miami
  +$1.45, 2 open Houston/Philly).

---

## 2026-05-03 — Bugfix: cross-venue date-alignment false positive

**What:**
- New `resolution_rules.parse_resolution_date(text, today=None)`: shared
  date parser. Honors explicit years (`"May 3, 2026"`); infers current
  year + bumps-to-next-year otherwise.
- `cross_venue._target_date()` now parses the title/question instead of
  taking `close_time[:10]`. Markets without a parseable date are dropped
  rather than risk a phantom canonical match.
- `strategy._target_date_from_title()` delegates to the shared parser
  for consistency. Behavior preserved (still infers year when missing).

**Why:**
Overnight detection surfaced a persistent 5¢ "arb" on KLAX 68-69°F that
was actually a date-alignment false positive:
  - Kalshi `KXHIGHLAX-26MAY03-B68.5` resolves on **May 3** LA-local;
    its `close_time` is `2026-05-04T07:59:00Z` because end-of-LA-day
    rolls past midnight UTC.
  - Polymarket asks about **May 4** LA high; `close_time` is also
    `2026-05-04T...`.
  - The `close_time[:10]` heuristic tagged both `target_date='2026-05-04'`
    → canonical match → false-positive arb on independent days.

If phase-2 had been auto-executing rather than detection-only, this would
have been an actual loss vector: BUY NO Kalshi (May 3 high not 68-69)
+ BUY YES Polymarket (May 4 high IS 68-69) is two unrelated bets, not
an arb. Caught only because the conservative phasing kept arbs as
detection-only.

This is the v1 §4.2 1°F-bin trap in a new costume: rules that look the
same but settle differently. Same lesson, different field.

**Smoke verification:**
- 6 unit cases for `parse_resolution_date` pass (explicit year, year
  inference, past-date bump, no-date returns None).
- LAX false positive: keys are now `..., '2026-05-03'` vs `..., '2026-05-04'`
  → no match. ✓
- Live re-detection on current Kalshi+Polymarket snapshot: examined 0
  canonical pairs, 0 arbs. The 21 finds across overnight were all this
  same phantom; honest count is 0 right now.

**Intentionally not done:**
- No backfill of the false-positive log entries — they're just log
  noise. The `data/cross_venue_arb.json` snapshot will overwrite on the
  next cycle.
- Cross-venue arb still requires exact canonical equality including
  date. A separate phase-3c could relax to "same source + threshold,
  different dates → flag as adjacent-day-not-arb" for visibility, but
  that's UI polish, not correctness.

**What to watch:**
- `[CROSS] examined N canonical pairs` should drop near 0 most cycles
  (Polymarket's 1°F-bin universe rarely date-aligns with Kalshi's
  brackets when honest dates are used).
- A nonzero find from now on is meaningful — same canonical rule, same
  resolution day, with cost+fee < $1.00. Worth examining manually.

---

## 2026-05-02 — Phase 3b: paper maker simulation

**What:**
- New `src/maker_sim.py`: `resolve_pending_orders()` walks all
  `paper_orders WHERE status='pending'` once per cycle, re-fetches each
  market's book, and:
  - Marks **filled** if `best_ask < limit_price` (strict-below — see "Why
    strict" below). Writes a normal `trades` row with `mode='paper:maker'`
    and `paper_trade=1`, links the order via `fill_trade_id`. Reconcile
    settles it like any other paper trade.
  - Marks **expired** if `now > expires_at`.
  - Otherwise leaves pending; checked again next cycle.
- New `paper_orders` table: virtual maker orders with status, posted_at,
  expires_at, limit_price, target_contracts, opp_json blob. Indexed on
  status and venue for the per-cycle resolution sweep.
- `src/paper_executor.py`: `mode='maker'` (now default for Polymarket).
  Posts at `best_ask - 1¢`. Edge check at the maker limit ensures the
  posted price still preserves MIN_EDGE. Returns `{filled: False,
  mode: 'paper:maker:pending', order_id: <db_id>}`.
- `src/main.py`:
  - Cycle calls `maker_sim.resolve_pending_orders()` BEFORE strategy
    scoring so today's fills land in this cycle's stats.
  - `_process_opportunity` for Polymarket routes to maker mode and
    handles the new `paper:pending:<id>` verdict.
- `src/dashboard.py`: `/api/polymarket/pending` endpoint, dashboard
  banner updated to "phase 3b — paper maker active," P&L panel adds
  pending/filled/expired counts, expandable table of pending orders.

**Why strict-below (best_ask < limit_price) for fill:**
At cycle granularity (5 min), if we see `best_ask == limit_price` we
don't know whether that ask was on the book before we posted (we sat in
queue behind it, didn't fill) or after (we filled). Conservative answer:
require best_ask to drop strictly below our price — unambiguous evidence
someone wanted to sell cheaper than us. Real continuous polling would
catch equality fills; we trade that for honesty until phase 3c adds a
sub-cycle monitoring thread.

**Why maker for Polymarket:**
Zero fees both ways means waiting costs nothing and saves the spread.
Polymarket weather books typically show 5-10c spreads even on liquid
markets, so a maker order 1c inside saves nearly the full spread when
filled. Real Polymarket doesn't have the v1-style maker rebate Kalshi
has, but the no-fee structure already gives makers the edge.

**Intentionally NOT done in phase 3b:**
- **Adverse-selection accounting (postmortem §3.4).** Cycle granularity
  can't see whether mid moved through our limit and back within seconds.
  We assume the limit-price fill is correct (we got our limit) and don't
  flag picked-off fills. Phase 3c work — needs sub-cycle book history.
- **Queue-priority modeling.** We assume worst-case (last in queue), so
  equality fills are missed. Real CLOB queue position would let us count
  some equality fills correctly. Not load-bearing without higher-frequency
  polling.
- **Maker for Kalshi paper.** Phase 3 is Polymarket-only paper. Kalshi
  still uses the live executor (real maker via demo or live). The
  paper_orders table is venue-agnostic so a Kalshi paper path is a
  small extension when needed.
- **Take-profit / exit logic on filled paper trades.** Once a paper
  maker fills, the trade is held until reconcile settles it at the
  oracle outcome. No mid-life exit yet — phase 3c can add the
  Bayesian-exit equivalent of `executor.should_exit_position`.

**What to watch:**
- `[MAKER_SIM] checked=N filled=K expired=E still_pending=P` log line
  on cycles where pending orders existed.
- `paper:pending:<order_id>` verdicts in cycle breakdown immediately
  after a Polymarket opp is scored.
- Dashboard `/polymarket` pending-orders table populates.
- Cycle N+1 should resolve some N's pending orders if Polymarket books
  moved across our 1c-inside limits — expect MOST to expire (Polymarket
  spreads don't tighten quickly), occasional fills when a counterparty
  posts an aggressive ask.
- `data/trades.db` gains rows with `mode='paper:maker'` once fills land.

---

## 2026-05-02 — Phase 3a: Polymarket paper trading (taker-only honest sim)

**What:**
- New `src/paper_executor.py`: `execute_paper_opportunity(opp, venue)` walks
  the venue's snapshot order book one tick at a time, accumulating
  level-by-level depth via the existing `depth_at_price` callable,
  computing a true VWAP fill, and stopping if the next level would push
  edge below `MIN_EDGE`. Conservative: depth-clamps to the snapshot, no
  fantasy fills. Returns the same shape as `executor.execute_opportunity`
  so storage/log paths don't fork.
- `src/strategy.py`: `find_opportunities(markets, bankroll, venue=...)`.
  - Uses canonical fields (`comparator` / `threshold` / `range_*`) when
    present; falls back to title parsing only as a defensive shim.
  - Venue-aware fee (Kalshi `kalshi_trade_fee`, Polymarket 0.0).
  - Venue-aware calibration via `calibration.calibrate(p, venue=...)`.
  - Held-position dedup keyed by `(venue, ticker)` so cross-venue
    accidental overlap can't happen.
  - Opp dict carries `venue` and `market_id`.
- `src/main.py`:
  - Scores Polymarket markets in the cycle via
    `strategy.find_opportunities(polymarket_markets, STARTING_BANKROLL,
     venue='polymarket')`.
  - `_process_opportunity` routes by `opp.venue`. Polymarket path skips
    Kalshi-specific risk gates (portfolio kelly, cash, cluster — those
    aggregate against the live Kalshi bankroll) and runs the paper
    executor, persisting trades with `paper_trade=1`.
  - Cycle summary gains `polymarket_opps_scored`.
- `src/kalshi_client.py`: `get_all_weather_markets()` now applies the
  canonicalizer in-place. Every market dict downstream carries the
  canonical fields uniformly across venues.
- `src/kalshi_venue.py`: `list_markets()` is now a passthrough — no
  duplicate canonicalization.
- `src/reconcile.py`: rewritten for multi-venue. Reads each open trade's
  `venue` column, calls the right venue's `get_resolution()`, computes
  P&L with the venue's fee schedule, writes results row with venue.
  Resolution outcome is normalized (`Yes`/`yes`/`true`/`1` → `yes`).
  Ambiguous resolutions are left open (no guess).
- `src/storage.py`: `get_venue_pnl(venue, paper_only=False)` aggregates
  trades+results into a per-venue summary for the dashboard.
- `src/dashboard.py`: `/api/polymarket/pnl` endpoint and a P&L panel on
  the `/polymarket` page (paper trades, resolved, win rate, realized P&L,
  open positions). Banner updated to reflect phase 3a.

**Why:**
Two of the user's requests on Polymarket were independent edge finding
and arb. Phase 2 covered cross-venue arb (detection only). Phase 3a
covers independent edge: strategy now scores Polymarket markets, and
the paper executor records trades honestly so we accumulate calibration
data. Per the project memory and v1 postmortem §3, paper trading without
discipline is the largest single source of false signal. The replay-fill
discipline starts here with snapshot-depth VWAP walks; honest enough to
trust, not so complex we can't reason about it.

The Polymarket path persists trades to the same `trades` table with
`paper_trade=1`. Same calibration scaffolding in `calibration.py` will
fit a per-venue isotonic transform when enough resolved trades
accumulate (deferred to a later phase when there's data to fit on).

**Intentionally NOT done in phase 3a:**
- **Maker simulation.** Real maker fills require a replay window with
  adverse-selection accounting (postmortem §3.4). Phase 3a is taker-only
  — every paper trade crosses the spread. Strictly more conservative
  than reality (real maker would pay less), so phase 3a paper P&L is
  pessimistic. Phase 3b adds maker.
- **Dynamic paper bankroll.** Polymarket sizing uses fixed
  `STARTING_BANKROLL = $100` so the per-trade cap holds steady. A real
  paper bankroll that compounds wins/losses comes in phase 3b.
- **Polymarket-specific risk gates.** Polymarket paper trades skip
  `portfolio_kelly_ok` / `cash_ok` / `settlement_cluster_ok` because
  those aggregate against the live Kalshi bankroll. The constitution
  gate (size cap, edge floor, thin-market guard, price window) still
  applies. Cluster cap on Polymarket-only positions is phase-3b work.
- **Polymarket-specific calibration fit.** Scaffold from phase 2 is in
  place. Until ~50 resolved Polymarket paper trades accumulate, the
  identity calibration is the right default.
- **Forecast-health for new cities.** Las Vegas / SAT / OKC / DC / NOLA
  added in phase 1.5 don't have observation history yet, so their
  `city_is_healthy()` defaults to True (fail-open). The 14-day rolling
  refresh will populate them naturally over time.

**What to watch:**
- `[STRATEGY] polymarket scored N opportunit{y,ies}` log line.
  Expectation: most cycles N=0, because most live Polymarket weather
  markets are 1°F bins which the existing 1°F-bin gate filters out
  (same gate as Kalshi). When a wider Polymarket market exists (e.g.
  during a heat wave with `>=` formats), N becomes nonzero.
- `paper:<trade_id>:paper` verdicts in the cycle breakdown — each one
  is a recorded paper trade in the trades table.
- `data/trades.db` rows with `venue='polymarket'` and `paper_trade=1`.
- `[RECONCILE] [polymarket] ...` lines once a Polymarket market resolves.
- `/polymarket` dashboard P&L panel populating as paper trades close.
- If `[STRATEGY] polymarket scored` is consistently 0 over a week, the
  Polymarket weather universe really is mostly 1°F bins. Cross-venue arb
  (phase 2) is the main value vector in that case, and phase-3b maker
  sim becomes the lever for actually trading the 1°F bins as resting
  liquidity provider.

---

## 2026-05-02 — Phase 2: cross-venue arb detection + per-venue calibration scaffold

**What:**
- New `src/cross_venue.py`: `detect_cross_venue_arbs()` pairs Kalshi and
  Polymarket markets via exact canonical equality (resolution_source +
  comparator + threshold + range_low + range_high + target_date). For each
  paired market, fetches the Polymarket book live (cheap — only paired
  markets) and computes both directions (K_YES+P_NO, K_NO+P_YES). An
  opportunity surfaces only if `1.0 - (k_price + p_price + kalshi_fee) >=
  MIN_EDGE_ARB`. Polymarket fee = 0 today.
- `src/main.py`: cycle calls `_detect_cross_venue_arbs()` after both
  venues' markets are fetched. Writes `data/cross_venue_arb.json`. Cycle
  summary gains `cross_venue_arbs` count. **Detection only** — Polymarket
  execution is not built, so even a real arb here can't be auto-acted on.
- `src/dashboard.py`: `/cross` page + `/api/cross/arbs` endpoint. Banner
  makes the no-execution status obvious. Polymarket page now links to
  `/cross`.
- `src/calibration.py`: API gains a `venue` parameter on `calibrate(p, venue)`
  and `shrinkage_factor(venue)`. Per-venue pickle paths via
  `_venue_pickle_path('polymarket')` resolving to
  `data/calibration_polymarket.pkl`. Backward-compat: 'kalshi' uses the
  existing `data/calibration.pkl` so v1's bootstrapped pickle is still
  loaded. Models cached per-venue in `_MODELS` dict.

**Why:**
v1 postmortem §3.1 ("forecast scored against itself") and the project
memory both flag that Kalshi's calibration cannot be applied to
Polymarket — different price formation, different oracle. The scaffold
makes it impossible to accidentally cross the streams once we have
honest Polymarket pickle data.

Cross-venue arb is the load-bearing reason the canonical resolution-
rule field exists. Pure price-difference math, no model risk: if both
legs fill at the prices we observed and both markets settle on the same
underlying observable, profit is guaranteed minus fees. This is the
highest-Sharpe use of a second exchange and the main reason the user
greenlit Polymarket integration.

**Intentionally NOT done in phase 2:**
- No Polymarket-only edge SCORING in `strategy.py`. Scoring without a
  paper-fill simulator generates hypothetical edge numbers that aren't
  graded against reality — the v1 §3 trap. Wait for phase-3 paper sim.
- No execution of cross-venue arbs. Polymarket execution requires a
  Polygon wallet + EIP-712 signing; that's deferred to the user's
  decision to fund a separate trading wallet.
- No `fit_from_v2_history(venue)` function. Calibration scaffolding lets
  us read per-venue pickles when they exist; building the *fit* function
  before we have resolved Polymarket trades would be premature.
- Calibration `calibrate(p, venue=)` is identity for both venues —
  Kalshi's existing pickle is degenerate (see calibration.py docstring),
  and Polymarket has no pickle yet. The signature change is so callers
  can already pass `venue=` and we don't have to do another API
  break when the per-venue pickles get fit.

**What to watch:**
- `[CROSS] examined N canonical pairs; found K arb opportunities` log
  line each cycle. Most cycles will probably show N small (<5) and K=0
  — Polymarket weather brackets are mostly 1°F bins on cities Kalshi
  doesn't bracket that narrow on. A non-zero K is the headline result.
- `data/cross_venue_arb.json` populates each cycle.
- `http://127.0.0.1:8082/cross` shows the table.
- If K is consistently 0 over a week, the canonical-equality requirement
  may be too strict — consider relaxing to "same source + threshold,
  same date" (drop range bounds equality) and reasoning about partial
  matches separately. But the v1 §4.2 1°F-bin lesson says don't.

---

## 2026-05-02 — Phase 1.5: dynamic Kalshi series discovery

**What:**
- New `kalshi_client.discover_weather_series()`: paginates Kalshi's
  `/series?category=Climate%20and%20Weather` (272 series live), filters to
  per-city daily-highs via `resolution_rules.is_kalshi_daily_high_series`,
  derives city via the shared `_CITY_PATTERNS` regex set. Returns
  `(mapped, unmapped)` so the user can see series whose city isn't
  pattern-matched yet. Cached for 30 min per process.
- `kalshi_client.get_all_weather_markets()` now drives off discovery.
  `WEATHER_SERIES` from `config.py` is merged in as a guaranteed-include
  set so a discovery failure can't silently drop known cities.
- Each market dict now carries a `series_ticker` field for traceability.
- `resolution_rules.py`: extended `_CITY_PATTERNS` and `KALSHI_CITY_STATION`
  with Las Vegas, San Antonio, Oklahoma City, DC, New Orleans. Added
  `is_kalshi_daily_high_series()` and `derive_city_from_kalshi_series()`.
- `config.py`: added the same 5 cities to `CITIES` (lat/lon),
  `CITY_TZ`, and `CLI_BIAS` (default 0.0 — uncalibrated; will improve
  with resolved-trade history).
- `forecast_health.py`: added the same 5 cities to `ASOS_STATIONS`.
- `main.py`: cycle now writes `data/kalshi_series.json` each cycle
  showing mapped + unmapped discovery state. Mirrors the polymarket
  snapshot pattern.

**Why:**
v1 hardcoded 14 weather series. Kalshi has actually listed 33 daily-high
series across 19 cities — we were missing Las Vegas, San Antonio, OKC,
DC, New Orleans entirely, plus duplicate-ticker series Kalshi added for
existing cities. Per the postmortem §6.1 "the plumbing is good" point,
the discovery layer should match Polymarket's auto-ingest pattern: pull
broadly, filter precisely. The user's per-city win-rate dashboard chart
is the safety mechanism for monitoring whether new cities help or hurt
overall expectancy.

**Intentionally NOT done:**
- No auto-geocoding for cities beyond the 19 already supported. Future
  cities surface as `unmapped` in `data/kalshi_series.json` and require a
  4-line addition (regex pattern, lat/lon, timezone, ASOS station). If
  this becomes a frequent ask, swap in Open-Meteo's geocoding API.
- `WEATHER_SERIES` in `config.py` is intentionally retained — three
  utility scripts (`preflight_audit.py`, `cli_gap_audit.py`,
  `backfill_trades.py`) reference it, and it serves as a "guaranteed-include"
  baseline for production.
- No deduplication of series that resolve on the same station. Kalshi
  lists e.g. `KXHIGHCHI` and `HIGHCHI` both as "Highest temperature in
  Chicago" — we ingest both. Phase 2's resolution-rule canonicalizer
  collapses these post-hoc when needed for arb.
- New cities default to `CLI_BIAS = 0.0`. Until ~50 resolved trades per
  station accumulate, their probabilities won't be bias-corrected and may
  be slightly off; this is the right conservative default.

**What to watch:**
- `[DISCOVERY] Kalshi weather series: N mapped (cities=M), K unmapped` log
  line on first cycle. Expect M ~= 19, K small (probably 0–3 country-wide
  or odd-format series we filter out).
- `data/kalshi_series.json` should populate with the mapped + unmapped
  lists. If `unmapped_count` grows over time, that's the signal to add a
  city pattern (or wire up the geocoder).
- New cities should appear in the per-city win rate chart over the next
  few resolved trades.

---

## 2026-05-02 — Phase 1: Polymarket read-only ingest

**What:**
- New `src/venue.py`: `Venue` Protocol + `MarketMeta` / `OrderBook` TypedDicts.
  Single source of truth for what every venue must expose.
- New `src/resolution_rules.py`: canonicalizes Kalshi and Polymarket market
  payloads to `(resolution_source, comparator, threshold, range_low, range_high)`.
  `markets_match()` is exact equality on the canonical tuple — no fuzzy match.
- New `src/kalshi_venue.py`: `KalshiVenue` adapter implementing the Protocol
  by delegating to the existing `kalshi_client.py` functions. Zero behavior
  change to Kalshi paths.
- New `src/polymarket_client.py`: `PolymarketVenue`, read-only. Implements
  `list_markets`, `get_book`, `get_market`, `get_resolution`,
  `verify_connection`. All execution methods raise `NotImplementedError` so
  no order can accidentally route to Polymarket.
- `src/storage.py`: schema gains `venue TEXT NOT NULL DEFAULT 'kalshi'` on
  `trades` and `results`. Idempotent ALTER TABLE migrations on boot. `log_trade`
  reads venue from `opp['venue']`; `log_result` accepts a `venue=` kwarg. Existing
  rows backfill to `'kalshi'` automatically. Also fixed a latent
  `NameError: logging not defined` in `get_cycle_stats`'s except branch.
- `src/main.py`: cycle now calls `_ingest_polymarket()` after the Kalshi
  market fetch. Result is written to `data/polymarket_markets.json` and
  surfaced in the cycle summary's `venues:` field. Strategy/arb scoring is
  unchanged — it sees only Kalshi markets in phase 1.
- `src/dashboard.py`: new `/polymarket` page + `/api/polymarket/markets`
  endpoint that read the JSON snapshot. Phase-1 banner makes it obvious
  no trading is happening on this venue yet.

**Why:**
The user wants to add Polymarket weather markets for both independent edge
finding and cross-venue arbitrage. Phase 1's job is to land the venue
abstraction and prove we can pull live Polymarket data without changing any
Kalshi behavior. Doing the abstraction now (rather than bolting Polymarket
onto kalshi_client.py) is the cheap moment — once strategy/risk/executor
become venue-aware, every later phase rides on this seam.

**Why a separate Polygon wallet is required for execution (not just a key):**
Polymarket is non-custodial. Orders are EIP-712 signatures from the wallet
holding the USDC. The iOS app's embedded wallet can't be authoritatively
exported. Plan: separate Polygon wallet funded manually = the hard
blast-radius cap. Phase 1 needs no wallet at all.

**Intentionally NOT done in phase 1:**
- No scoring of Polymarket markets in `strategy.py` / `strategy_arb.py`.
  Adding scoring without a paper-fill simulator would generate fake P&L
  numbers — exactly the v1 §3 mistake.
- No `Venue`-driven refactor of `strategy.py` / `risk.py` / `executor.py`.
  The flat list-of-dicts contract still holds; venue is just a field on
  each dict. Bigger refactor in phase 2 when we wire arb across venues.
- No paper trading sim. That's phase 3, with the replay-fill discipline
  spelled out in `memory/project_polymarket_integration.md`.
- No Polymarket calibration. Separate isotonic pickle per venue is phase 3
  prereq; until then we have nothing to calibrate against.
- No execution code. `PolymarketVenue.place_limit_order` raises.

**What to watch:**
- First live cycle log line `[SCAN] venues: kalshi=N polymarket=M` should
  show non-zero M. If M=0, Gamma API schema may have drifted — check the
  raw payload shape against what `canonicalize_polymarket_market` expects.
- `data/polymarket_markets.json` should grow on each cycle; dashboard
  `/polymarket` should populate.
- No regression in Kalshi behavior: `[SCAN] fetched %d Kalshi weather markets`
  count should match pre-change cycles.
