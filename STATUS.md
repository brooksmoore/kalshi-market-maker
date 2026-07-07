# kalshi_bot_2.0 — STATUS

> Standardized header. Keep these fields at the very top, always current.
> Detailed history lives in `LEDGER.md`; v1 lessons in `../kalshi_bot_1.0/POSTMORTEM_V1.md`. This file is the at-a-glance map.

- **One-liner:** From-scratch rewrite of the Kalshi weather-bracket trader — GEFS 31-member ensemble for probability, isotonic calibration fit from v1 history, Claude demoted to veto-only (never touches the probability number).
- **Stage:** paper-validating (Kalshi demo mode) — **stopped / FLOORED** (`killed=true` in umbrella snapshot)
- **Live gate:** OFF (demo only; `live_gate=disarmed`)
- **Tests:** present (math / execution / strategy suites) + 3 umbrella snapshot gate tests.
- **Intelligence type:** Statistical (ensemble + calibration); LLM is veto-only, fails open.
- **Single most important next thing:** Loop is FLOORED (n_oos=154, net_ev=-$0.023/contract). Decide: (a) declare done and close, or (b) try a fundamentally different premise (see SKILL_alpha.md graveyard for what NOT to retry). The loop infrastructure (prescan→checker→maker) is fully wired and ready if (b).
- **Honest odds this makes money:** Very low. FLOORED verdict: n=154 first-per-ticker paper trades, net-of-cost EV −$0.023/contract. Taker fees exceed the thin gross edge. Strategy needs a fundamentally different entry premise to escape FLOORED.
- **Last updated:** 2026-07-07

---

## Stage vocabulary
`idea → skeleton → core-done → runner-wiring → paper-validating → live-gated → live`

## Recent movement
- 2026-07-07: Wired onto umbrella `snapshot_emit.py` + `decision_emit.py` (read-only). Snapshot honestly reports `killed=true`, `health=down`, FLOORED loop verdict in `extra`/`warnings`; no trading-logic changes. Gate test `tests/test_audit_snapshot_emit_gate.py` green. `data/state.json` emitted for fleet dashboard.
- 2026-06-24: Built LOOP_SPEC steps 0+2+3. score_paper_trades.py adds load_settled_from_main_db() (trades.db canonical source, 154 first-per-ticker) + score_new_settlements(). loop_prescan.py: 4 synthetic tests pass; real run → FLOORED (n_oos=154, net_ev=−$0.023/contract, taker fees exceed gross edge). STATE_LOOP.md, SKILL_alpha.md, data/change_log.jsonl created. Loop infrastructure complete — FLOORED is the honest verdict of this experiment.
- 2026-06-24: Built leak-closing loop foundation (LOOP_SPEC.md steps 0+2). score_paper_trades.py now reports gross vs net-of-cost vs maker-hypothetical EV. loop_checker.py is the pure-Python walk-forward gate: 4 synthetic tests pass (PRESCAN_EXIT, REJECT too-few, REJECT bad-t-stat, PROMOTE good-change). Real-data prescan: 7 settled trades, need 53 more to split. Reframe confirmed: weather-edge thesis dead; this loop closes mechanical leaks and declares FLOORED when it can't find more.
- 2026-06-07: Portfolio review baseline. Classified as paused-in-demo (the 5th tracked folder), not fully retired. Strong proof you can re-architect from a postmortem.
