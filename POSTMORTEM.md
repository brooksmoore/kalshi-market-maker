# Postmortem — kalshi_bot_2.0

**Born:** 2026-05-10 (v2 rewrite, following v1's live loss)
**Last real work / verdict reached:** 2026-06-24 (loop gate's final holdout run)
**Buried:** 2026-07-08

---

## 1. Thesis

Kalshi's daily weather-bracket markets (temperature above/below a strike) are retail-priced
against a single point forecast; a 31-member GEFS ensemble forecast, properly calibrated,
should price the true probability more accurately than the crowd and capture the difference
as edge.

## 2. Verdict: disproven, by a pre-committed test

**FLOORED.** Not "inconclusive," not "needs more time" — a kill criterion was written in
advance of the evaluation window: after ≥100 held-out settled trades, if net-of-cost EV
(fees + spread + slippage) was still ≤ 0, the strategy would be declared dead. The receipt,
verbatim from `STATE_LOOP.md`:

```
n_oos: 154
net_ev_oos: -0.0234
kill_n: 100
gate_status: FLOORED
reason: n_oos=154 >= KILL_N=100 and net_ev_oos=-0.0234 <= 0
```

154 settled paper trades, net EV = **−$0.0234/contract**. Closing the largest identified cost
leak (switching taker fills to maker where possible) recovered roughly a quarter of the bleed
without flipping the sign — there was no rescuable edge underneath the execution cost, only a
smaller loss. This is the same lesson v1 learned live, losing 44% of a $100 bankroll over 135
resolved trades over six weeks — v2 got the identical answer for under $5 of paper-mode
compute and zero capital.

## 3. What it taught (the part that transfers)

The strategy failed; the infrastructure that measured the failure is the actual asset:

- **The loop-gate harness itself** — a maker/checker split (deterministic prescan → checker →
  maker proposal), a walk-forward out-of-sample gate, net-of-cost scoring (not gross), and an
  effect-size bar rather than a bare sign check. This is now the template pointed at Multi's
  own efficacy test, per the current roadmap — it does not need to be rebuilt from scratch.
- **v1's forensic lessons, already engineered into v2 before this verdict:** blended
  Gaussian+LLM probabilities were overconfident by ~20 points; a single point forecast can't
  beat an informed counterparty; an LLM in the probability path adds miscalibrated noise, not
  signal. v2 replaced all three (real 31-member ensemble, isotonic calibration fit from v1's
  own resolved history, LLM demoted to veto-only). Correct fixes — just not sufficient to
  create edge that survives fees.
- **Paper ≠ live, encoded as mechanism, not just a lesson learned once.** The gate scores
  net-of-cost specifically because v1's live loss happened after paper looked fine.

## 4. Revival condition

**Never, for this exact premise.** The kill criterion was pre-committed and it was answered
definitively at n=154, nearly double the KILL_N threshold — this isn't a small sample that
could tip the other way with more data; fees and spread don't change, so nothing external
"unlocks" a re-test of the same bet on the same venue.

A **structurally different premise** (a different market type, a different venue, a
genuinely new pricing edge) does not get to inherit this verdict either way — it requires its
own fresh `/postmortem test` with its own pre-committed kill criterion, run on the reusable
harness above. This document closes the weather-bracket thesis specifically, not the
technique that measured it.

---

*Full engineering detail: `SKILL_alpha.md` (maker rulebook + graveyard), `data/change_log.jsonl`
(machine-readable proposal history), `README.md` (retirement notice + case-study link).*
