# LOOP_SPEC.md — Self-Improving Alpha Loop (kalshi_bot_2.0)

> Status: SPEC / not yet built. This document is the design contract for an
> autonomous loop that incrementally improves paper-trading profitability.
> No code, config, or trading gate is changed by reading this file.
>
> Scope: **paper only** (`LIVE_TRADING_ENABLED=false`). The loop never touches
> live capital. Going live is a separate, human-only decision and is out of
> scope for this spec.

---

## 0. What this loop is — and the one thing it must never become

> **Reframed 2026-06-24 after reading `kalshi_bot_1.0/POSTMORTEM_V1.md`.** The
> weather-forecast *edge* thesis is established-dead: ~1,500 v1 trades, every
> iteration narrowed the bleed without flipping its sign, and §5 of the
> postmortem gives seven *structural* reasons "just trade the forecast" cannot
> pay (you compete against the marginal informed trader who has the same NOAA
> plus a better ensemble, not against the weather). This loop does **NOT** try
> to rediscover or manufacture that edge. A loop with memory cannot conjure a
> price-beating signal out of consensus information — memory refines *use* of
> information, it does not add information. Pointing the loop at "find alpha in
> NOAA residuals" would just re-run v1's six-week mistake faster.

What a loop with memory **can** provably do — and what this one is for — is
**compound at eliminating self-inflicted losses.** Execution slippage, paying
taker fees in a market that pays makers (v1 was pure-taker — postmortem §9.4),
sizing into calibration error, trading below the venue's cost-noise floor,
racing the book intraday. These are real, learnable, *objectively verifiable*
leaks, and closing them improves net-of-cost EV on ANY strategy, marginal ones
included, by bleeding less. This is the layer where "gets better over time" is
true rather than wishful.

So this loop's job is **not** "make more money" by finding signal. It is:
**propose a change that closes a measurable cost/execution/calibration leak →
prove it improves out-of-sample *net-of-cost* EV on settled paper trades the
change never saw → apply it → keep watching whether it still helps, and roll it
back itself if it doesn't.**

The historical wound this must not reopen: v1 fit an isotonic curve to its own
resolved trades, the curve went degenerate (collapsed every market to BUY NO on
26 samples), and the strategy lost 44% of bankroll. `src/calibration.py` is
hardcoded to identity **today** because of that. The leak-closing frame plus the
gates below are what keep the loop from re-cutting it.

The single inviolable rule:

> **The data that justifies a change must never include the data the change was
> derived from.** Walk-forward only. The maker proposes on the training window;
> the gate scores on a held-out window that comes strictly *after* it in time.

Everything below exists to enforce that one sentence.

---

## 0.5 The kill criterion (pre-committed, the hardest discipline)

From postmortem §9.8 — *"v1 ran six weeks past the point where evidence said the
thesis was wrong, because each fix produced a plausible local improvement. The
instinct to keep iterating on a falsified thesis is the most expensive bias in
this entire project."* This loop is structurally vulnerable to exactly that: a
leak-closing loop will always find *some* leak to close, and could keep itself
busy forever on a strategy whose floor is still negative.

So the kill criterion is defined **in advance**, and the loop checks it every
cycle and reports it loudly:

> **If, after `KILL_N` held-out settlements (default 100), the strategy's best
> achievable OOS net-of-cost EV — i.e. with every promoted leak-fix active — is
> still <= 0, the loop declares the strategy floored and STOPS proposing.**

On a floored verdict the loop does not silently keep spinning. It writes a
prominent FLOORED banner to `STATE_LOOP.md` and stops the maker (the checker /
reversal monitor may keep running read-only). This is a *success* of the
experiment: it means the loop closed every mechanical leak it could find and the
residual is still negative — which is the postmortem's finding, now confirmed
mechanically and cheaply, in paper, with the leaks actually removed rather than
assumed. That is the honest answer to "can a loop with memory trade this into
profit?": it gets the strategy to its true cost-minimized floor, and then tells
you where that floor is.

(If the floor comes back *positive* net of cost, that is a real, earned result —
not alpha, but a strategy that stopped paying more to trade than it takes in. At
that point the live-gate decision is yours and human-only, per §9.)

---

## 1. The maker / checker split

Two roles, deliberately separated (the prop-shop maker-checker pattern, already
the convention across this portfolio):

- **MAKER** — diagnoses recent paper losses, forms a hypothesis, proposes ONE
  change (a rule or a parameter). Has full access to the *training window* of
  settled trades. Never scores its own proposal.
- **CHECKER** — a separate evaluation step (ideally a different model on the
  reasoning parts; deterministic on the math parts) that scores the proposed
  change on the *held-out window only*. Has NO exposure to the maker's reasoning
  or to the training-window outcomes. Returns a binary verdict + the OOS metric
  delta. The checker is the only thing allowed to promote a change.

The maker can be wrong as often as it likes. The checker is what makes that safe.

---

## 2. The feedback signal (already built — reuse, do not reinvent)

The out-of-sample signal this loop needs **already exists**:

- `scripts/score_paper_trades.py` — matches logged paper trades to **real Kalshi
  settlements** (`/markets/{ticker}`), computes per-trade P&L, win rate, EV per
  contract, and breakdowns by direction / city / cal_p band. These settlements
  are ground truth the bot never saw at trade time → genuinely out-of-sample.
- `data/paper_trades.db` (`paper_trade` + `paper_result`) — the trade + outcome
  store.
- `scripts/fit_calibration_from_shadow.py` — existing calibration refit path.
- `src/calibration.py` — `fit_from_v1_history` already computes `brier_before`,
  `brier_after`, and a reliability calibration error, and floors/caps shrinkage
  at [0.3, 0.7]. The loop reuses these honesty checks; it does not write its own.

The loop is an **orchestration layer over this machinery**, not a new pipeline.

### Walk-forward windowing (the heart of the gate)

Settled paper trades are ordered by settlement time and split:

```
[ ……… TRAIN window ……… | …… HOLDOUT window …… ]
                         ^ cut point moves forward each cycle
```

- MAKER sees TRAIN only.
- CHECKER scores on HOLDOUT only.
- The cut advances over time, so today's holdout becomes tomorrow's train —
  every settlement is eventually used for learning, but never for grading the
  change that learned from it.

---

## 3. Leak taxonomy — what the maker is allowed to hunt

The maker does **not** hunt alpha. It hunts **leaks** — places where the
strategy gives up net-of-cost EV for reasons that are *mechanical, not
informational*, and therefore learnable. Every leak below is drawn from the v1
postmortem; each is objectively measurable on held-out settlements.

| Leak | Signal it shows up in | Lever the maker may propose |
|---|---|---|
| **Taker fees in a maker-paid market** (postmortem §9.4 — v1 was pure-taker; Kalshi charges 0 maker fees + LIP rebate through Sep 2026) | net-of-cost EV >> taker EV on the same trades | shift entry toward maker-fill posture; reject signals only profitable as taker |
| **Sub-noise-floor edge** (§5.4 — anything < ~5¢ clean edge after calibration is below venue noise) | trades clustered in low-edge bands lose net of fees+spread | raise the min-edge gate to where net-of-cost EV turns positive OOS |
| **Sizing into calibration error** (§9.3 — discount edge by k·σ_p) | high-confidence bands are the ones that lose | widen the uncertainty discount; flatten size where calibration MAE is high |
| **Asymmetric direction trap** (§5.3 + LEDGER "direction is the signal, not city" — BUY YES had 8.5% lifetime WR; model overconfident on too-warm YES) | one direction systematically negative net of cost | tighten or disable the losing direction (gated like any change) |
| **Intraday book-race** (§5.6 — edge at 9am is gone by 2pm) | EV decays by trade timestamp-of-day | restrict entry to windows where OOS net-of-cost EV survives |

Two routing rules that keep the loop honest:

- **Software bugs are NOT leaks.** Fee miscalc, wrong dedup, stale fetch → fixed
  in `src/` with a test, never papered over with a rule.
- **Noise is NOT a leak.** A single loss with no diagnosable mechanical cause, or
  a bucket below sample threshold → recorded, not learned from. Most single
  losses are noise; a loop that writes a rule after every loss overfits variance.

> The reframe in one line: the maker may only propose changes that reduce
> **how much the strategy pays to trade**, never changes that claim to predict
> the weather better. The first is verifiable and compounds; the second is the
> dead thesis.

---

## 4. The change lifecycle (propose → gate → apply → watch → revert)

Per your decision: **full autonomy on all params**, with a self-correcting
reversal check. No human in the chair for paper. The safety comes from the gate
and the reversal monitor, not from a human approval step.

```
1. DIAGNOSE   Maker reads TRAIN-window losses, classifies (§3), forms ONE hypothesis.
2. PROPOSE    Maker emits a change record (§6) — rule text and/or a param delta.
3. GATE       Checker scores the change on HOLDOUT only:
                 - OOS **net-of-cost** EV/contract must improve vs. baseline
                   (net of fees + modeled spread/slippage, NOT gross EV — a leak
                   only counts as closed if the money survives costs; that is the
                   entire point of the reframe)
                 - on >= MIN_OOS_SAMPLES held-out settlements (default 30)
                 - by MORE than the holdout's standard error (a t-stat-like bar,
                   default >= 2.0 — see §5.1; "EV after > EV before" alone is NOT
                   enough — with enough hypotheses one passes on pure noise)
                 - and not worsen Brier / calibration-error on holdout
              Fail any -> change is rejected, logged, NOT applied.
4. APPLY      Pass -> change goes live in paper. Old value snapshotted (§6) for revert.
5. WATCH      Each subsequent cycle, the reversal check re-scores every active
              change on fresh holdout: "would reverting this improve OOS EV?"
              If yes for REVERT_PATIENCE consecutive cycles -> auto-roll-back.
6. RECORD     Every step appended to STATE_LOOP.md + change_log.jsonl (§6).
```

Hard stops (any one halts promotion for that cycle):

- `< MIN_OOS_SAMPLES` (30) new held-out settlements since the last change → propose nothing.
- Token/iteration budget per cycle exceeded → stop, log, resume next cycle.
- Checker cannot compute a clean holdout metric (missing settlements, fetch failure) → abstain, never fail-open into a change.
- Any change that would flip `LIVE_TRADING_ENABLED` or touch a kill switch → **forbidden**, hard error. The loop has no authority over the live gate.

---

## 5. Calibration quarantine (the v1-specific guardrail)

You chose "auto-apply everything," and the reversal check makes that survivable
for most params. But the isotonic calibration curve is the one knob that already
caused a 44% drawdown, so it gets one extra constraint **on top of** the normal
gate — not a human gate, an automatic one:

- A calibration **refit** only promotes if the holdout window has
  `>= CALIB_MIN_OOS` settlements (default **200**, vs. 30 for other params),
  AND the refit is non-degenerate: it must not collapse any cal_p band to a
  near-constant (the exact v1 failure — see `calibration.py:214` docstring).
  A degeneracy check (no output band variance below epsilon) is mandatory.
- AND it must pass **rolling-origin cross-validation** (§5.3) — the piece that
  closes the remaining hole. The two bullets above catch *crude* overfit; CV
  catches the *subtle* kind isotonic regression is prone to.
- Until those bars are met, `calibrate()` stays identity, exactly as today.

Rationale: numeric calibration is where overfitting is most seductive and most
expensive. Higher sample bar + degeneracy guard + cross-validation = the loop
can still refit autonomously, but only when the evidence is strong enough that
it isn't fitting noise. This is the institutional lesson already written into the
codebase, enforced mechanically.

### 5.3 Rolling-origin CV for calibration refits (closing the isotonic hole)

A single train/holdout split can't catch the failure mode isotonic regression is
*specifically* prone to: a monotonic curve will happily bend to fit the wiggles
of one contiguous block of settled trades, clear a single-window t-stat, and then
fail to generalize to the next block. One holdout is just one draw of the noise.

So a calibration refit is validated across **multiple forward-in-time folds**,
never a single split:

```
fold 1:  train [ ---- ] test [ -- ]
fold 2:  train [ ------- ] test [ -- ]
fold 3:  train [ ---------- ] test [ -- ]
            (origin rolls forward; train is always strictly BEFORE its test)
```

- Train on the past, test on the *next* block, advance the origin, repeat.
  Time-ordering is preserved every fold — no future settlement ever informs a
  curve graded on an earlier one (the leakage k-fold would allow).
- The refit promotes **only if it improves out-of-sample Brier vs. identity in a
  MAJORITY of folds** (e.g. >= 3 of 5), not just on aggregate. A curve that wins
  big on one fold and loses on the others is overfitting one window — exactly
  what this rejects, and what a single holdout would have waved through.

**Fallback policy (stateless — recommended).** When no refit clears CV,
`calibrate()` runs **identity**, exactly as today. Crucially, a *promoted* curve
is not permanent: it must **keep re-passing the rolling-origin check on fresh
folds every cycle**. The moment it stops clearing the bar, the loop falls back to
**identity — never to a prior curve.** This compounds learning while the evidence
holds, but the fallback is always the stateless honest default, never a stale
artifact from a different market regime. (The rejected alternative, "keep the
last promoted curve," reintroduces path dependence: a curve that passed weeks ago
keeps trading as conditions drift until the reversal monitor belatedly catches it
— a slower, sneakier rerun of the v1 failure. Identity has no such drift.)

### 5.1 The multiple-comparisons guard (why a t-stat bar, not just "better")

Walk-forward stops *in-sample* overfitting. It does NOT stop **multiple-
comparisons** overfitting: if the maker tries 20 hypotheses, ~1 will beat a
30-sample holdout by chance, because EV noise at n=30 is large. A loop that
promotes on "OOS EV improved at all" will therefore promote noise at a steady
rate — slowly recreating v1 with extra steps.

Two mechanical defenses:

- **Effect size, not just sign.** Promote only if the OOS EV improvement exceeds
  ~2× its holdout standard error (the `>= 2.0` bar in §4 step 3). This is the
  same discipline the sibling bots encode as "Newey-West t-stat > 2.0."
- **Proposal budget + graveyard.** Cap proposals per cycle, and record every
  *tried* hypothesis (pass or fail) in the graveyard. The reversal monitor (§4
  step 5) is the backstop: a change that passed the gate by chance will, on
  fresh holdout, fail the "would reverting help?" check and auto-revert. So even
  a false positive that slips the t-stat bar is temporary, not permanent.

### 5.2 Paper-vs-live fidelity caveat (known, contained)

`score_paper_trades.py` models fills at the observed ask and its own docstring
calls this "an upper bound" — real fills carry adverse selection it cannot
simulate. So the loop optimizes against a signal that is systematically rosier
than live. This is fine for proving the *mechanism* in paper, but it means:
**a gate-passing change in paper is necessary, not sufficient, for live edge.**
Any future promotion of these results to live capital must re-validate against
real fills — it is explicitly NOT something this loop is authorized to infer.

---

## 6. State & memory (so the loop has intelligence across runs)

Per your decision, the loop carries memory and reasons with it — it does not
re-derive from zero each cycle.

- **`STATE_LOOP.md`** — human-readable spine. Last run, active changes + their
  live OOS deltas, pending diagnoses, what's on the watch-for-reversal list,
  hard-stops hit. Read at the start of every cycle, written at the end. (Mirrors
  the existing `STATUS.md` convention.)
- **`SKILL_alpha.md`** — the accumulating rulebook the maker reads each cycle.
  Holds promoted rules + the "we tried X, it failed the gate, don't re-propose"
  graveyard (so the loop doesn't rediscover dead ideas forever).
- **`change_log.jsonl`** — append-only machine record. One line per proposal:
  `{ts, class, hypothesis, param_path, old_value, new_value, train_window,
  holdout_window, oos_ev_before, oos_ev_after, brier_delta, verdict, applied,
  reverted_ts?}`. This is the audit trail and the revert source of truth.

The graveyard + change_log are what turn "memory" from a slogan into something
that actually prevents repeated mistakes: a hypothesis that failed the gate
twice is not re-proposed; a change that was reverted is flagged so a near-
identical re-proposal is treated with suspicion.

---

## 7. Tunable constants (one place, so the loop's behavior is legible)

```
MIN_OOS_SAMPLES   = 30     # held-out settlements required to promote a normal change
CALIB_MIN_OOS     = 200    # higher bar for a calibration refit (§5)
CALIB_CV_FOLDS    = 5      # rolling-origin folds a refit must be tested across (§5.3)
CALIB_CV_MAJORITY = 3      # folds (of CALIB_CV_FOLDS) the refit must win to promote (§5.3)
REVERT_PATIENCE   = 3      # consecutive underperforming cycles before auto-rollback
KILL_N            = 100    # held-out settlements after which a still-<=0 net-of-cost floor = FLOORED (§0.5)
CADENCE           = daily  # matches weather-market settlement rate
CYCLE_TOKEN_CAP   = <set>  # hard per-cycle budget; exceed -> stop and log
EPS_DEGENERACY    = <set>  # min per-band output variance for a calib refit to be non-degenerate
MAX_PROPOSALS     = 2      # hypotheses per cycle (overfitting AND cost cap, see §5.1/§7.1)
MAKER_MODEL       = sonnet # judgment step — capable model
CHECKER_MODEL     = none   # gate is PURE PYTHON, zero model calls (see §7.1)
PRESCAN_MODEL     = none   # cheap-exit settlement count is deterministic, zero model calls
```

These live in one config block so a future reviewer can see the loop's entire
risk posture at a glance.

---

## 7.1 Cost efficiency (engineered, not hoped for)

A daily loop is ~30 agent sessions/month. The cost is dominated by the **maker's
reasoning**, not the trading bot and not the settlement fetches (those are free
Kalshi API calls). Three rules keep spend honest:

### Cheap-exit ordering — the single biggest lever

Each cycle MUST run in this order, and STOP at the first gate it fails. The
expensive model call only fires on days that have earned it:

```
1. PRESCAN   (pure Python, ~$0)  Count new settled trades since last change.
                                 < MIN_OOS_SAMPLES  -> write 1 line to STATE, EXIT.
2. CHECKER   (pure Python, ~$0)  Re-score active changes on fresh holdout
                                 (reversal monitor). No model call.
3. MAKER     (model, the cost)   ONLY reached if PRESCAN cleared. Diagnose
                                 TRAIN-window losses, emit <= MAX_PROPOSALS.
```

The failure mode this prevents: a maker that reasons every cycle pays full price
30 days/month instead of only on threshold-met days. Weather markets settle
slowly, so most days SHOULD exit at step 1 for near-zero cost. **If the build
reasons before counting, the loop's cost roughly triples for zero added value.**

### Model-tier split

- **Checker = pure Python, no model.** The gate is arithmetic (OOS EV delta, its
  standard error, Brier). It must never call a model — that's both a cost win and
  a correctness win (deterministic, reproducible, can't hallucinate a verdict).
- **Maker = capable model (Sonnet-class default).** Diagnosis is judgment, so it
  wants reasoning quality, but it only runs on full-work days.
- **Prescan = pure Python.** A row count, not a reasoning task.

### Estimated cost per run (current prices, June 2026)

Rates: Haiku 4.5 $1/$5, Sonnet 4.6 $3/$15, Opus 4.8 $5/$25 per M in/out.
Prompt caching (STATE/SKILL/graveyard are re-read every cycle) cuts cached
input ~90%; batch API cuts 50% — both apply and are not yet priced in below,
so these are conservative upper bounds.

| Cycle type | Tokens (in / out) | Sonnet maker | Opus maker |
|---|---|---|---|
| **Short-circuit** (PRESCAN exits) | ~0 / ~0 (pure Python) | **~$0.00** | **~$0.00** |
| **Full-work** (maker reasons) | ~40–80K / ~5K | **~$0.20–$0.31** | **~$0.33–$0.53** |

Monthly, assuming ~1/3 of days clear the threshold (~10 full-work cycles, ~20
short-circuits):

- **Sonnet maker: ~$2–$3/month.**
- **Opus maker: ~$3.50–$5.50/month.**

With prompt caching on the re-read context, both drop further. The trading bot's
own paper operation is separate and unchanged by the loop.

**Caveats:** token *counts* are modeled, not measured — the graveyard grows over
time, nudging input up; large training/holdout windows push toward the high end.
Prices verified June 2026 but change over time. The numbers to trust are the
*shape* (short-circuit ≈ free; cost lives entirely in full-work days) and the
*lever* (cheap-exit ordering decides whether you pay for 10 days or 30).

---

## 8. Build order (smallest working loop first — do not skip ahead)

0. **Net-of-cost scorer (prerequisite).** `score_paper_trades.py` models fills
   at the observed ask (GROSS). The gate needs NET-of-cost EV. Wire the existing
   cost functions into the scorer first: `config.kalshi_trade_fee()` (audit M7),
   `SLIPPAGE_BUFFER_PCT`, `MAKER_PRICE_OFFSET_CENTS`, and the maker/taker
   distinction already in `strategy.py`. These exist — this is wiring, not
   inventing. Until net-of-cost EV is computable, the §4 gate cannot run.
1. **Manual dry run.** Run the net-of-cost scorer, hand-split a walk-forward
   window, hand-check that "OOS net-of-cost EV improved on holdout" is computable
   and honest on the *current* `paper_trades.db`. No automation yet.
2. **Checker first, not maker.** Build the gate (§4 step 3) as a standalone
   scorer, PURE PYTHON (§7.1). Prove it rejects a deliberately bad change and
   passes a known-good one. The gate is the whole experiment; build it before
   anything proposes.
3. **Prescan + state + log.** Wire the pure-Python settlement counter (the
   cheap-exit, §7.1) and `STATE_LOOP.md` / `change_log.jsonl` / graveyard. The
   prescan must gate the maker BEFORE any model call — verify a below-threshold
   day exits at ~$0 with no maker invocation.
4. **Maker.** Add the diagnosis + single-proposal step on the TRAIN window,
   reached ONLY when prescan clears.
5. **Reversal monitor.** Add §4 step 5 so changes self-correct.
6. **Schedule.** Only once 1–5 are reliable, wrap in the daily scheduled task.

Metric that decides if the loop is working (borrowed from the loop-engineering
literature, adapted): **fraction of promoted changes that survive the reversal
monitor.** If most promoted changes get auto-reverted within a few cycles, the
gate is too loose or the sample bar too low — fix the gate, don't ship more
changes. A loop whose changes mostly stick is learning; one whose changes mostly
revert is overfitting on a slight delay.

---

## 9. What this loop explicitly does NOT do

- It does not trade live, size live, or flip the live gate. Paper only.
- It does not "fix" losses it can't diagnose (noise → recorded, not learned).
- It does not write software bugs into the rulebook (bugs → code/tests).
- It does not grade its own homework (maker ≠ checker; train ≠ holdout).
- It does not manufacture edge or hunt for signal. The weather-forecast edge
  thesis is established-dead (postmortem §5); this loop closes mechanical leaks,
  it does not predict the weather better. A loop with memory refines *use* of
  information — it cannot add information that consensus NOAA doesn't contain.
- It does not iterate forever on a floored strategy. Per §0.5, once it has closed
  every leak it can find and the net-of-cost floor is still <= 0 after KILL_N
  held-out settlements, it declares FLOORED and stops. That verdict — "the leaks
  are gone and the residual is still negative" — is the *successful* outcome of
  this experiment, delivered cheaply in paper with the leaks actually removed
  rather than assumed. If the floor comes back positive, that is an earned result
  and the live decision is yours alone.
```
