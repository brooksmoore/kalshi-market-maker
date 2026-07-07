"""
loop_checker.py — Walk-forward gate for the leak-closing loop (LOOP_SPEC.md §4 step 3).

Pure Python, zero model calls. This is the CHECKER role in the maker/checker split:
it scores a proposed change on the held-out window and returns a binary verdict.
It never sees training data and cannot be influenced by the maker's reasoning.

The gate enforces the spec's single inviolable rule:
  "The data that justifies a change must never include the data the change was
   derived from." — LOOP_SPEC.md §0

USAGE:
  venv/bin/python scripts/loop_checker.py
    → runs synthetic self-tests (proves it rejects bad / passes good)
    → runs prescan on real paper_trades.db

  from scripts.loop_checker import run_gate, GateResult
  result = run_gate(settled_trades, change_fn)

CHANGE FN CONTRACT:
  change_fn(holdout: list[dict]) -> list[dict]
  Takes the holdout trade list, returns a filtered/transformed version.
  Must NOT see train data — the maker proposes on train only; the checker
  applies the change to holdout only. The caller enforces this separation.
"""

from __future__ import annotations

import math
import statistics as _stats
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config import kalshi_trade_fee  # noqa: E402

# ─── Constants (LOOP_SPEC.md §7) ─────────────────────────────────────────────
MIN_OOS_SAMPLES: int = 30   # minimum holdout settlements required to gate a change
T_STAT_BAR: float = 2.0     # OOS EV improvement must exceed 2× its SE (§5.1)
KILL_N: int = 100           # trades after which net-EV <= 0 → FLOORED (§0.5)


# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class GateResult:
    """Returned by run_gate. verdict is the actionable field; others are audit trail."""
    verdict: str            # "PROMOTE" | "REJECT" | "PRESCAN_EXIT"
    n_train: int = 0
    n_holdout: int = 0
    n_changed: int = 0
    oos_ev_before: float = 0.0   # baseline net-of-cost EV on holdout
    oos_ev_after: float = 0.0    # changed net-of-cost EV on holdout
    ev_delta: float = 0.0
    ev_se: float = 0.0           # SE of changed_ev distribution
    t_stat: float = 0.0
    oos_brier_before: float = 0.0
    oos_brier_after: float = 0.0
    kill_check: str = ""         # "FLOORED" | "ABOVE_ZERO" | "INSUFFICIENT_DATA"
    reason: str = ""


# ─── Core metrics ─────────────────────────────────────────────────────────────

def _net_ev(trade: dict) -> float:
    """Net-of-cost EV per contract for one settled trade. Deducts Kalshi taker fee."""
    ep = float(trade.get("entry_price") or 0.0)
    gross = float(trade.get("pnl_per_contract") or 0.0)
    if ep <= 0 or ep >= 1:
        return gross
    return gross - kalshi_trade_fee(1, ep)


def _brier_score(trades: list[dict]) -> float:
    """Mean Brier score. cal_p is P(YES); outcome is 'yes' or 'no'."""
    if not trades:
        return 0.0
    total, counted = 0.0, 0
    for t in trades:
        cal_p = t.get("cal_p")
        outcome = t.get("outcome")
        if cal_p is None or outcome not in ("yes", "no"):
            continue
        y = 1.0 if outcome == "yes" else 0.0
        total += (float(cal_p) - y) ** 2
        counted += 1
    return total / counted if counted else 0.0


def _kill_check(settled_trades: list[dict], kill_n: int = KILL_N) -> str:
    """Check the FLOORED criterion (LOOP_SPEC.md §0.5).

    If after kill_n held-out settlements the strategy's best achievable
    net-of-cost EV (with all promoted changes active) is still <= 0,
    the loop should declare FLOORED and stop proposing.

    Returns:
      "FLOORED"            — >= kill_n trades, net-EV <= 0
      "ABOVE_ZERO"         — >= kill_n trades, net-EV > 0
      "INSUFFICIENT_DATA"  — < kill_n settled trades
    """
    if len(settled_trades) < kill_n:
        return "INSUFFICIENT_DATA"
    net_evs = [_net_ev(t) for t in settled_trades]
    mean_ev = _stats.fmean(net_evs)
    return "ABOVE_ZERO" if mean_ev > 0 else "FLOORED"


# ─── Walk-forward gate ────────────────────────────────────────────────────────

def run_gate(
    settled_trades: list[dict],
    change_fn: Callable[[list[dict]], list[dict]],
    min_oos_samples: int = MIN_OOS_SAMPLES,
    t_stat_bar: float = T_STAT_BAR,
) -> GateResult:
    """Score a proposed change on the walk-forward held-out window.

    settled_trades must be ordered by settled_at (call
    score_paper_trades.load_settled_trades() which handles this ordering).

    change_fn takes the holdout list and returns a filtered/transformed
    version. It MUST NOT access training data — the caller (maker) proposes
    on the training window only; this function applies to holdout only.

    Gate criteria (all must pass to PROMOTE):
      1. >= min_oos_samples trades in the holdout window
      2. >= min_oos_samples trades remain after change_fn is applied
      3. OOS net-of-cost EV improves (ev_delta > 0)
      4. ev_delta / SE(changed_ev) >= t_stat_bar  (§5.1 multiple-comparisons guard)
      5. Brier score does not worsen on the changed holdout

    Returns PRESCAN_EXIT if there aren't enough trades to even split.
    Returns REJECT if any gate fails.
    Returns PROMOTE if all gates pass.
    """
    n = len(settled_trades)
    if n < min_oos_samples * 2:
        return GateResult(
            verdict="PRESCAN_EXIT",
            kill_check=_kill_check(settled_trades),
            reason=f"{n} settled trades < {min_oos_samples * 2} required for a split",
        )

    cut = n // 2
    train = settled_trades[:cut]
    holdout = settled_trades[cut:]

    if len(holdout) < min_oos_samples:
        return GateResult(
            verdict="PRESCAN_EXIT",
            n_train=len(train),
            n_holdout=len(holdout),
            kill_check=_kill_check(settled_trades),
            reason=f"holdout has {len(holdout)} < {min_oos_samples} required",
        )

    # Baseline: score all holdout trades with current (unchanged) strategy
    baseline_net_evs = [_net_ev(t) for t in holdout]
    baseline_ev = _stats.fmean(baseline_net_evs)
    baseline_brier = _brier_score(holdout)

    # Apply change to holdout — maker never sees this data
    changed = change_fn(holdout)

    if len(changed) < min_oos_samples:
        return GateResult(
            verdict="REJECT",
            n_train=len(train),
            n_holdout=len(holdout),
            n_changed=len(changed),
            oos_ev_before=baseline_ev,
            oos_brier_before=baseline_brier,
            kill_check=_kill_check(settled_trades),
            reason=(
                f"only {len(changed)} holdout trades survive the change "
                f"(need >= {min_oos_samples})"
            ),
        )

    changed_net_evs = [_net_ev(t) for t in changed]
    changed_ev = _stats.fmean(changed_net_evs)
    changed_brier = _brier_score(changed)

    ev_delta = changed_ev - baseline_ev

    if len(changed_net_evs) >= 2:
        ev_se = _stats.stdev(changed_net_evs) / math.sqrt(len(changed_net_evs))
    else:
        ev_se = float("inf")

    t_stat = (
        ev_delta / ev_se
        if ev_se > 0 and math.isfinite(ev_se)
        else 0.0
    )
    brier_ok = changed_brier <= baseline_brier + 1e-6

    if t_stat >= t_stat_bar and brier_ok:
        verdict = "PROMOTE"
        reason = "passed all gates"
    else:
        fails = []
        if t_stat < t_stat_bar:
            fails.append(f"t_stat={t_stat:.2f} < {t_stat_bar}")
        if not brier_ok:
            fails.append(
                f"Brier worsened ({changed_brier:.4f} > {baseline_brier:.4f})"
            )
        verdict = "REJECT"
        reason = " | ".join(fails)

    return GateResult(
        verdict=verdict,
        n_train=len(train),
        n_holdout=len(holdout),
        n_changed=len(changed),
        oos_ev_before=baseline_ev,
        oos_ev_after=changed_ev,
        ev_delta=ev_delta,
        ev_se=ev_se,
        t_stat=t_stat,
        oos_brier_before=baseline_brier,
        oos_brier_after=changed_brier,
        kill_check=_kill_check(settled_trades),
        reason=reason,
    )


# ─── Synthetic self-tests ─────────────────────────────────────────────────────

def _make_trade(cal_p: float, entry_price: float, outcome: str,
                action: str = "BUY NO") -> dict:
    """Build a minimal settled trade dict for testing."""
    won = (
        (action == "BUY NO" and outcome == "no")
        or (action == "BUY YES" and outcome == "yes")
    )
    pnl = (1.0 - entry_price) if won else (-entry_price)
    return {
        "id": 0,
        "action": action,
        "cal_p": cal_p,
        "entry_price": entry_price,
        "contracts": 1,
        "outcome": outcome,
        "pnl_per_contract": pnl,
        "settled_at": 0.0,
    }


def run_synthetic_tests(verbose: bool = True) -> bool:
    """Prove the gate rejects bad changes and promotes good ones.

    Builds 120 synthetic settled trades (60 train + 60 holdout):
      Good trades: BUY NO, cal_p=0.20, entry=$0.40 — high win rate (28/30)
      Bad  trades: BUY NO, cal_p=0.35, entry=$0.40 — low win rate  (10/30)

    Tests:
      1. PRESCAN_EXIT  — fewer than 2 × MIN_OOS_SAMPLES total trades
      2. REJECT (too few) — change that filters out nearly everything
      3. REJECT (bad t-stat) — change that keeps only the losing segment
      4. PROMOTE       — change that removes the losing segment

    Returns True if all four pass, False (with details) if any fail.
    """
    def _print(msg: str) -> None:
        if verbose:
            print(msg)

    all_ok = True

    # 120 trades: 60 train + 60 holdout, each half good/half bad
    good_segment = (
        [_make_trade(0.20, 0.40, "no")] * 28 +
        [_make_trade(0.20, 0.40, "yes")] * 2
    )  # 30 trades, EV ≈ +0.51 net
    bad_segment = (
        [_make_trade(0.35, 0.40, "no")] * 10 +
        [_make_trade(0.35, 0.40, "yes")] * 20
    )  # 30 trades, EV ≈ -0.09 net

    # Build as [good_train | bad_train | good_holdout | bad_holdout]
    # run_gate splits at midpoint (60), so holdout = good_holdout + bad_holdout
    all_trades = good_segment + bad_segment + good_segment + bad_segment

    # ── Test 1: PRESCAN_EXIT — too few trades ──────────────────────────────
    r1 = run_gate(all_trades[:10], lambda t: t)
    if r1.verdict != "PRESCAN_EXIT":
        _print(f"  FAIL test 1 (PRESCAN_EXIT): got {r1.verdict!r} — {r1.reason}")
        all_ok = False
    else:
        _print(f"  PASS test 1 (PRESCAN_EXIT): {r1.reason}")

    # ── Test 2: REJECT — change removes nearly all holdout trades ─────────
    def keep_nothing(trades: list[dict]) -> list[dict]:
        return [t for t in trades if t["entry_price"] > 0.99]  # nothing passes

    r2 = run_gate(all_trades, keep_nothing)
    if r2.verdict not in ("REJECT", "PRESCAN_EXIT"):
        _print(f"  FAIL test 2 (REJECT too-few-after-change): got {r2.verdict!r}")
        all_ok = False
    else:
        _print(f"  PASS test 2 (REJECT too-few-after-change): n_changed={r2.n_changed} — {r2.reason}")

    # ── Test 3: REJECT — bad change keeps only losing segment ─────────────
    # cal_p >= 0.30 selects the bad segment (30 holdout trades, t-stat negative)
    def keep_bad(trades: list[dict]) -> list[dict]:
        return [t for t in trades if (t.get("cal_p") or 0.0) >= 0.30]

    r3 = run_gate(all_trades, keep_bad)
    if r3.verdict not in ("REJECT", "PRESCAN_EXIT"):
        _print(f"  FAIL test 3 (REJECT bad change): got {r3.verdict!r} — {r3.reason}")
        _print(f"       ev_before={r3.oos_ev_before:+.4f}  ev_after={r3.oos_ev_after:+.4f}"
               f"  t={r3.t_stat:.2f}  n_changed={r3.n_changed}")
        all_ok = False
    else:
        _print(f"  PASS test 3 (REJECT bad change): t={r3.t_stat:.2f}  "
               f"ev_delta={r3.ev_delta:+.4f}  n_changed={r3.n_changed} — {r3.reason}")

    # ── Test 4: PROMOTE — good change removes the losing segment ──────────
    # cal_p < 0.30 selects the good segment (30 holdout trades, large positive t-stat)
    def keep_good(trades: list[dict]) -> list[dict]:
        return [t for t in trades if (t.get("cal_p") or 0.0) < 0.30]

    r4 = run_gate(all_trades, keep_good)
    if r4.verdict != "PROMOTE":
        _print(f"  FAIL test 4 (PROMOTE good change): got {r4.verdict!r} — {r4.reason}")
        _print(f"       ev_before={r4.oos_ev_before:+.4f}  ev_after={r4.oos_ev_after:+.4f}"
               f"  t={r4.t_stat:.2f}  n_changed={r4.n_changed}")
        all_ok = False
    else:
        _print(f"  PASS test 4 (PROMOTE good change): t={r4.t_stat:.2f}  "
               f"ev_delta={r4.ev_delta:+.4f}  n_changed={r4.n_changed}")

    return all_ok


# ─── Main ─────────────────────────────────────────────────────────────────────

def _hdr(s: str) -> None:
    print()
    print("=" * 78)
    print(s)
    print("=" * 78)


def main() -> None:
    _hdr("LOOP CHECKER — LOOP_SPEC.md §4 step 3 (pure Python, zero model calls)")

    print()
    print("SYNTHETIC SELF-TESTS")
    print("-" * 40)
    ok = run_synthetic_tests(verbose=True)
    print("-" * 40)
    if ok:
        print("All synthetic tests PASSED — gate is mechanically correct.")
    else:
        print("SYNTHETIC TESTS FAILED — gate is not reliable. Fix before using.")
        sys.exit(1)

    print()
    print("REAL-DATA PRESCAN")
    print("-" * 40)
    # Import load_settled_trades from the sibling scorer script
    scripts_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(scripts_dir))
    try:
        from score_paper_trades import load_settled_trades  # type: ignore
    except ImportError as e:
        print(f"Cannot import load_settled_trades: {e}")
        print("Run score_paper_trades.py first to populate paper_result table.")
        return

    trades = load_settled_trades()
    n = len(trades)
    threshold = MIN_OOS_SAMPLES * 2
    print(f"Settled paper trades in DB: {n}")

    if n == 0:
        print("  No settled trades yet.")
        print("  Run scripts/score_paper_trades.py first to fetch settlements.")
        print(f"  PRESCAN: need >= {threshold} to split a walk-forward window.")
    elif n < threshold:
        print(f"  {n} < {threshold} minimum for a walk-forward split.")
        print(f"  PRESCAN: need {threshold - n} more settlements before a change can be proposed.")
    else:
        print(f"  {n} settled trades — walk-forward split is possible (cut at {n // 2}).")
        # Run the gate with an identity change to get baseline metrics
        r = run_gate(trades, lambda t: t)
        print(f"  Baseline holdout net-of-cost EV/contract: {r.oos_ev_before:+.4f}")
        print(f"  Baseline holdout Brier score:              {r.oos_brier_before:.4f}")
        if r.kill_check == "FLOORED":
            print()
            print("  !! FLOORED (§0.5) — net-of-cost EV <= 0 after >= KILL_N settlements.")
            print("     The strategy's mechanical cost floor has been reached.")
            print("     The loop should stop proposing. See LOOP_SPEC.md §0.5.")
        elif r.kill_check == "ABOVE_ZERO":
            print(f"  KILL_N check: net-EV > 0 — strategy is above cost floor.")
        else:
            print(f"  KILL_N check: {r.kill_check} (need {KILL_N} settlements)")

    print()
    print("=" * 78)


if __name__ == "__main__":
    main()
