# STATE_LOOP.md — Loop state (machine-written, do not edit by hand)

**last_run**: 2026-06-24T17:06:32Z
**last_change_ts**: 0
**n_settled**: 154
**n_oos**: 154
**net_ev_oos**: -0.0234
**kill_n**: 100
**gate_status**: FLOORED
**reason**: n_oos=154 >= KILL_N=100 and net_ev_oos=-0.0234 <= 0

---

## Interpretation

| Field | Value | Meaning |
|-------|-------|---------|
| gate_status | FLOORED | PERMANENT STOP: strategy is net-negative at n>=100 |
| n_oos | 154 | Holdout settlements since last proposed change |
| net_ev_oos | -0.0234 | Mean net-of-cost EV per contract on holdout |
| MIN_OOS_SAMPLES | 30 | Minimum holdout trades to open the gate |
| KILL_N | 100 | Holdout threshold for FLOORED declaration |

## Active rulebook

See `SKILL_alpha.md` for the maker's current rulebook and graveyard.

## Recent changes

See `data/change_log.jsonl` for the machine-readable proposal history.
