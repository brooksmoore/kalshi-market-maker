"""
loop_prescan.py — Step 1 of the cheap-exit chain (LOOP_SPEC.md §7.1).

Pure Python. Zero model calls. Costs ~$0 per run.

Responsibilities:
  1. Call score_new_settlements() to persist any newly-finalized tickers.
  2. Count held-out settlements (settled after last_change_ts in STATE_LOOP.md).
  3. Declare FLOORED if n_oos >= KILL_N and net-of-cost EV <= 0.
  4. Write STATE_LOOP.md.
  5. Exit before any model invocation if n_oos < MIN_OOS_SAMPLES.

Exit codes:
  0  GATE_OPEN    — enough holdout data, EV still alive; checker may run
  1  PRESCAN_EXIT — insufficient holdout data; stop here (~$0 cost)
  2  FLOORED      — >= KILL_N holdout settlements with net EV <= 0; permanent stop

Usage:
    venv/bin/python scripts/loop_prescan.py          # real run
    venv/bin/python scripts/loop_prescan.py --test   # synthetic tests
"""

from __future__ import annotations

import argparse
import re
import statistics as _stats
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config import kalshi_trade_fee  # noqa: E402
from score_paper_trades import (  # noqa: E402
    load_settled_from_main_db,
    score_new_settlements,
)

# ── Constants (mirror loop_checker.py — keep in sync) ────────────────────────
MIN_OOS_SAMPLES: int = 30
KILL_N: int = 100

STATE_PATH = ROOT / "STATE_LOOP.md"
CHANGE_LOG = ROOT / "data" / "change_log.jsonl"


# ── Core logic ────────────────────────────────────────────────────────────────

@dataclass
class PrescanResult:
    verdict: str          # "GATE_OPEN" | "PRESCAN_EXIT" | "FLOORED"
    n_settled: int = 0
    n_oos: int = 0
    net_ev_oos: float = 0.0
    last_change_ts: str = "0"
    reason: str = ""


def _net_ev(trade: dict) -> float:
    ep = float(trade.get("entry_price") or 0.0)
    gross = float(trade.get("pnl_per_contract") or 0.0)
    if ep <= 0 or ep >= 1:
        return gross
    return gross - kalshi_trade_fee(1, ep)


def _read_last_change_ts() -> str:
    """Parse last_change_ts from STATE_LOOP.md. Returns '0' if not found."""
    if not STATE_PATH.exists():
        return "0"
    text = STATE_PATH.read_text()
    m = re.search(r"^\*\*last_change_ts\*\*:\s*(.+)$", text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return "0"


def _ts_to_epoch(ts: str) -> float:
    """Convert ISO timestamp or '0'/'epoch' sentinel to Unix epoch seconds."""
    ts = ts.strip()
    if ts in ("0", "epoch", "none", ""):
        return 0.0
    try:
        return float(ts)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def run_prescan(settled_trades: list[dict] | None = None) -> PrescanResult:
    """Core prescan logic. Accepts pre-loaded trades for synthetic testing."""
    last_change_ts = _read_last_change_ts()
    cutoff = _ts_to_epoch(last_change_ts)

    if settled_trades is None:
        settled_trades = load_settled_from_main_db()

    n_settled = len(settled_trades)

    # Holdout: trades settled after the last proposed change
    oos_trades = [
        t for t in settled_trades
        if _ts_to_epoch(t.get("settled_at") or "0") > cutoff
    ]
    n_oos = len(oos_trades)

    if n_oos < MIN_OOS_SAMPLES:
        return PrescanResult(
            verdict="PRESCAN_EXIT",
            n_settled=n_settled,
            n_oos=n_oos,
            last_change_ts=last_change_ts,
            reason=f"only {n_oos} holdout settlements; need {MIN_OOS_SAMPLES}",
        )

    net_evs = [_net_ev(t) for t in oos_trades]
    net_ev_oos = _stats.fmean(net_evs)

    if n_oos >= KILL_N and net_ev_oos <= 0:
        return PrescanResult(
            verdict="FLOORED",
            n_settled=n_settled,
            n_oos=n_oos,
            net_ev_oos=net_ev_oos,
            last_change_ts=last_change_ts,
            reason=(
                f"n_oos={n_oos} >= KILL_N={KILL_N} "
                f"and net_ev_oos={net_ev_oos:+.4f} <= 0"
            ),
        )

    return PrescanResult(
        verdict="GATE_OPEN",
        n_settled=n_settled,
        n_oos=n_oos,
        net_ev_oos=net_ev_oos,
        last_change_ts=last_change_ts,
        reason=f"n_oos={n_oos} >= {MIN_OOS_SAMPLES}; net_ev={net_ev_oos:+.4f}",
    )


def _write_state(result: PrescanResult) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = f"""# STATE_LOOP.md — Loop state (machine-written, do not edit by hand)

**last_run**: {now}
**last_change_ts**: {result.last_change_ts}
**n_settled**: {result.n_settled}
**n_oos**: {result.n_oos}
**net_ev_oos**: {result.net_ev_oos:+.4f}
**kill_n**: {KILL_N}
**gate_status**: {result.verdict}
**reason**: {result.reason}

---

## Interpretation

| Field | Value | Meaning |
|-------|-------|---------|
| gate_status | {result.verdict} | {"Enough OOS data + EV alive; checker may run" if result.verdict == "GATE_OPEN" else "Stopped before model call — see reason" if result.verdict == "PRESCAN_EXIT" else "PERMANENT STOP: strategy is net-negative at n>=" + str(KILL_N)} |
| n_oos | {result.n_oos} | Holdout settlements since last proposed change |
| net_ev_oos | {result.net_ev_oos:+.4f} | Mean net-of-cost EV per contract on holdout |
| MIN_OOS_SAMPLES | {MIN_OOS_SAMPLES} | Minimum holdout trades to open the gate |
| KILL_N | {KILL_N} | Holdout threshold for FLOORED declaration |

## Active rulebook

See `SKILL_alpha.md` for the maker's current rulebook and graveyard.

## Recent changes

See `data/change_log.jsonl` for the machine-readable proposal history.
"""
    STATE_PATH.write_text(content)


# ── Synthetic tests ───────────────────────────────────────────────────────────

def _make_trade(cal_p: float, ep: float, outcome: str,
                settled_at: str = "2026-05-01T00:00:00Z") -> dict:
    action = "BUY NO"
    won = outcome == "no"
    pnl = (1.0 - ep) if won else (-ep)
    return {
        "ticker": f"TEST-{cal_p}-{ep}-{outcome}",
        "venue": "kalshi",
        "action": action,
        "cal_p": cal_p,
        "entry_price": ep,
        "contracts": 1,
        "outcome": outcome,
        "pnl_per_contract": pnl,
        "pnl_total": pnl,
        "settled_at": settled_at,
        "net_pnl_per_contract": pnl - kalshi_trade_fee(1, ep),
    }


def run_synthetic_tests() -> bool:
    """4 synthetic tests; returns True if all pass."""
    PASS = "\033[32mPASS\033[0m"
    FAIL = "\033[31mFAIL\033[0m"
    all_ok = True

    # Trades used across tests
    # Positive-EV trades: BUY NO at 0.20 when outcome=no → pnl=+0.80, fee≈0.01
    pos_trade = lambda ts: _make_trade(0.20, 0.20, "no", ts)
    # Negative-EV trades: BUY NO at 0.40 when outcome=yes → pnl=-0.40, fee≈0.02
    neg_trade = lambda ts: _make_trade(0.35, 0.40, "yes", ts)

    def _run(name: str, trades: list[dict], cutoff_ts: str) -> PrescanResult:
        # Patch STATE_PATH read via monkey-patching isn't clean in prescan,
        # so we call run_prescan with a mock: override last_change_ts by
        # temporarily writing STATE_LOOP.md and restoring.
        old_text = STATE_PATH.read_text() if STATE_PATH.exists() else None
        STATE_PATH.write_text(
            f"**last_change_ts**: {cutoff_ts}\n"
            "**gate_status**: (test)\n"
        )
        try:
            result = run_prescan(settled_trades=trades)
        finally:
            if old_text is not None:
                STATE_PATH.write_text(old_text)
            elif STATE_PATH.exists():
                STATE_PATH.unlink()
        return result

    # ── Test 1: 0 OOS trades (all pre-cutoff) → PRESCAN_EXIT ─────────────────
    old_trades = [pos_trade("2026-05-01T00:00:00Z") for _ in range(50)]
    cutoff = "2026-06-01T00:00:00Z"
    r = _run("Test 1 — zero OOS", old_trades, cutoff)
    ok1 = r.verdict == "PRESCAN_EXIT" and r.n_oos == 0
    print(f"  {PASS if ok1 else FAIL} Test 1 — zero OOS (n_oos={r.n_oos}): "
          f"verdict={r.verdict}")
    all_ok = all_ok and ok1

    # ── Test 2: 10 OOS trades (< MIN_OOS_SAMPLES=30) → PRESCAN_EXIT ──────────
    new_trades = [pos_trade("2026-06-15T00:00:00Z") for _ in range(10)]
    r = _run("Test 2 — 10 OOS", old_trades + new_trades, cutoff)
    ok2 = r.verdict == "PRESCAN_EXIT" and r.n_oos == 10
    print(f"  {PASS if ok2 else FAIL} Test 2 — 10 OOS (< {MIN_OOS_SAMPLES}): "
          f"verdict={r.verdict} n_oos={r.n_oos}")
    all_ok = all_ok and ok2

    # ── Test 3: 30 OOS, positive EV, < KILL_N → GATE_OPEN ────────────────────
    new_trades_30 = [pos_trade("2026-06-15T00:00:00Z") for _ in range(30)]
    r = _run("Test 3 — 30 OOS pos-EV", old_trades + new_trades_30, cutoff)
    ok3 = r.verdict == "GATE_OPEN" and r.n_oos == 30 and r.net_ev_oos > 0
    print(f"  {PASS if ok3 else FAIL} Test 3 — 30 OOS pos-EV: "
          f"verdict={r.verdict} n_oos={r.n_oos} net_ev={r.net_ev_oos:+.4f}")
    all_ok = all_ok and ok3

    # ── Test 4: 100 OOS, negative EV → FLOORED ────────────────────────────────
    neg_trades_100 = [neg_trade("2026-06-15T00:00:00Z") for _ in range(100)]
    r = _run("Test 4 — 100 OOS neg-EV", old_trades + neg_trades_100, cutoff)
    ok4 = r.verdict == "FLOORED" and r.n_oos == 100 and r.net_ev_oos < 0
    print(f"  {PASS if ok4 else FAIL} Test 4 — 100 OOS neg-EV (FLOORED): "
          f"verdict={r.verdict} n_oos={r.n_oos} net_ev={r.net_ev_oos:+.4f}")
    all_ok = all_ok and ok4

    return all_ok


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="run synthetic tests")
    ap.add_argument(
        "--skip-score", action="store_true",
        help="skip score_new_settlements() API call (faster for local testing)",
    )
    args = ap.parse_args()

    if args.test:
        print("loop_prescan synthetic tests")
        print("-" * 40)
        ok = run_synthetic_tests()
        print("-" * 40)
        print(f"{'ALL PASS' if ok else 'SOME FAILED'}")
        return 0 if ok else 1

    # Real run
    if not args.skip_score:
        n_scored, n_pending = score_new_settlements()
        if n_scored or n_pending:
            print(f"[prescan] settlement scoring: +{n_scored} new, {n_pending} pending")

    result = run_prescan()
    _write_state(result)

    verdict_line = {
        "GATE_OPEN":    f"[prescan] GATE_OPEN — {result.reason}",
        "PRESCAN_EXIT": f"[prescan] PRESCAN_EXIT — {result.reason}",
        "FLOORED":      f"[prescan] *** FLOORED *** — {result.reason}",
    }.get(result.verdict, f"[prescan] {result.verdict} — {result.reason}")
    print(verdict_line)

    return {
        "GATE_OPEN":    0,
        "PRESCAN_EXIT": 1,
        "FLOORED":      2,
    }.get(result.verdict, 1)


if __name__ == "__main__":
    sys.exit(main())
