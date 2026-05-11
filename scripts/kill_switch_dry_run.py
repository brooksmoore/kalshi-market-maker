"""
kill_switch_dry_run.py — verify each halt and per-trade cap actually fires.

This is the load-bearing safety verification before flipping the bot live
on prod. For every halt condition the bot relies on, this script:
  1. Sets up the trigger state in an isolated temp environment
  2. Calls the real gating function (risk.can_trade, risk.kelly_size, etc.)
  3. Asserts the expected halt/cap behaviour fires

Touches nothing in data/. Uses a tempdir for performance.json + monkeypatched
helpers for the trades-DB-derived signals. Safe to run while the live bot
is also running — no shared state.

Usage:
    venv/bin/python scripts/kill_switch_dry_run.py

Exits 0 on all-pass, 1 on any failure.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


# ─── Tiny test harness ────────────────────────────────────────────────────────
_passes = 0
_fails: list[str] = []


def check(name: str, predicate: bool, detail: str = "") -> None:
    global _passes
    if predicate:
        _passes += 1
        print(f"  ✓ {name}")
    else:
        _fails.append(f"{name} — {detail}")
        print(f"  ✗ {name}: {detail}")


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# ─── Test helpers ─────────────────────────────────────────────────────────────
def reset_risk_state(tmp_perf_path: str) -> None:
    """Point risk module at a fresh perf file and reset all module-level state
    that could leak between tests."""
    import config
    import risk
    config.PERF_FILE = tmp_perf_path
    risk.PERF_FILE = tmp_perf_path
    # Clear exposure cache so each test starts from a known state
    risk._exposure_cache = {
        "total": 0.0,
        "by_city": {},
        "by_cluster": {},
        "count": 0,
        "positions": [],
        "_error": False,
        "_source": "dry-run",
    }


def write_perf(path: str, **fields) -> None:
    """Write a minimal performance.json with the given overrides. Sensible
    defaults so tests only need to set what they're exercising."""
    base = {
        "peak_pnl": 0.0,
        "starting_bankroll": 200.0,
        "bankroll": 200.0,
        "cash": 200.0,
        "updated_at": datetime.now().isoformat(),
    }
    base.update(fields)
    Path(path).write_text(json.dumps(base, indent=2))


# ─── Tests ────────────────────────────────────────────────────────────────────
def test_per_trade_cap() -> None:
    """MAX_SINGLE_BET_PCT (2.5%) should clamp kelly_size output."""
    section("MAX_SINGLE_BET_PCT — per-trade cap")
    import risk
    import config
    # On $200 bankroll, 2.5% = $5 max per trade
    # kelly_size returns dollars; should never exceed $5 regardless of edge
    size = risk.kelly_size(
        p=0.95, price=0.50, bankroll=200.0, action="BUY YES"
    )
    cap_dollars = 200.0 * config.MAX_SINGLE_BET_PCT
    check(
        f"kelly_size on huge edge clamps to ${cap_dollars:.2f} ({config.MAX_SINGLE_BET_PCT:.1%})",
        size <= cap_dollars + 0.01,
        f"got ${size:.2f} (expected ≤ ${cap_dollars:.2f})",
    )
    # Verify MIN_POSITION floor still applies
    size_tiny = risk.kelly_size(
        p=0.51, price=0.50, bankroll=200.0, action="BUY YES"
    )
    check(
        "kelly_size respects MIN_POSITION floor on tiny edges",
        size_tiny >= config.MIN_POSITION,
        f"got ${size_tiny:.2f} below MIN_POSITION ${config.MIN_POSITION:.2f}",
    )


def test_drawdown_halt(tmp: str) -> None:
    """MAX_DRAWDOWN_PCT (25%) should fire when realized loss vs peak exceeds it."""
    section("MAX_DRAWDOWN_PCT — drawdown halt")
    import risk

    # Peak +$60 means peak_bankroll = $200 starting + $60 = $260
    # Need realized < $260 * 0.75 = $195 to fire 25% drawdown
    # $200 starting + realized_pnl < $195 → realized_pnl < -$5
    # So set peak_pnl = $60, monkey realized_pnl_total to return -$70 (drawdown 35%)
    reset_risk_state(tmp)
    write_perf(tmp, peak_pnl=60.0, starting_bankroll=200.0)
    orig = risk.realized_pnl_total
    risk.realized_pnl_total = lambda: -70.0
    try:
        dd = risk.drawdown_pct()
        ok, reasons = risk.can_trade()
        check(
            "drawdown_pct calculation correct",
            abs(dd - 0.50) < 0.01,
            f"expected ~50%, got {dd:.1%}",
        )
        check(
            "can_trade() refuses on heavy drawdown",
            not ok and any("DRAWDOWN" in r for r in reasons),
            f"ok={ok} reasons={reasons}",
        )
    finally:
        risk.realized_pnl_total = orig

    # And the OK case: realized at peak, drawdown 0%
    reset_risk_state(tmp)
    write_perf(tmp, peak_pnl=60.0, starting_bankroll=200.0)
    orig = risk.realized_pnl_total
    risk.realized_pnl_total = lambda: 60.0
    try:
        ok, reasons = risk.can_trade()
        check(
            "can_trade() allows when at peak (no drawdown)",
            not any("DRAWDOWN" in r for r in reasons),
            f"unexpected drawdown reason in {reasons}",
        )
    finally:
        risk.realized_pnl_total = orig


def test_daily_loss_halt(tmp: str) -> None:
    """DAILY_LOSS_LIMIT_PCT (15%) should fire when today's realized loss exceeds it."""
    section("DAILY_LOSS_LIMIT_PCT — daily loss halt")
    import risk

    reset_risk_state(tmp)
    write_perf(tmp, bankroll=200.0)
    # $200 × 15% = $30 daily loss limit; trigger with -$40
    orig = risk._todays_pnl
    risk._todays_pnl = lambda: -40.0
    try:
        ok, reasons = risk.can_trade()
        check(
            "can_trade() refuses when today's loss exceeds daily limit",
            not ok and any("DAILY_LOSS" in r for r in reasons),
            f"ok={ok} reasons={reasons}",
        )
    finally:
        risk._todays_pnl = orig

    # OK case
    reset_risk_state(tmp)
    write_perf(tmp, bankroll=200.0)
    orig = risk._todays_pnl
    risk._todays_pnl = lambda: -5.0  # mild loss, well within limit
    try:
        ok, reasons = risk.can_trade()
        check(
            "can_trade() allows when daily loss is within limit",
            not any("DAILY_LOSS" in r for r in reasons),
            f"unexpected daily-loss reason in {reasons}",
        )
    finally:
        risk._todays_pnl = orig


def test_stale_bankroll_halt(tmp: str) -> None:
    """BANKROLL_STALE_SECONDS (120) should fail-closed when bankroll data is old."""
    section("BANKROLL_STALE_SECONDS — fail-closed on stale bankroll")
    import risk

    reset_risk_state(tmp)
    # Set updated_at far in the past
    old_ts = (datetime.fromtimestamp(time.time() - 300)).isoformat()
    write_perf(tmp, bankroll=200.0, updated_at=old_ts)
    # Also need to backdate the file mtime since get_active_bankroll falls
    # back to mtime when updated_at parsing fails
    old_mtime = time.time() - 300
    os.utime(tmp, (old_mtime, old_mtime))

    ok, reasons = risk.can_trade()
    check(
        "can_trade() refuses on stale bankroll",
        not ok and any("STALE_BANKROLL" in r for r in reasons),
        f"ok={ok} reasons={reasons}",
    )


def test_zero_bankroll_halt(tmp: str) -> None:
    """ZERO_BANKROLL should halt — defends against startup race / API failure."""
    section("ZERO_BANKROLL — refuse to trade with no bankroll")
    import risk

    reset_risk_state(tmp)
    write_perf(tmp, bankroll=0.0)
    ok, reasons = risk.can_trade()
    check(
        "can_trade() refuses on zero bankroll",
        not ok and any("ZERO_BANKROLL" in r for r in reasons),
        f"ok={ok} reasons={reasons}",
    )


def test_portfolio_kelly_cap(tmp: str) -> None:
    """PORTFOLIO_KELLY_CAP (90%) should reject proposed bets that would push
    total exposure over the cap."""
    section("PORTFOLIO_KELLY_CAP — per-bet portfolio-Kelly check")
    import risk
    import config

    reset_risk_state(tmp)
    write_perf(tmp, bankroll=200.0)

    # Simulate $170 already deployed (85%). Proposing $30 (15%) would push
    # total to 100%, well above the 90% cap.
    risk._exposure_cache = {
        "total": 170.0,
        "by_city": {},
        "by_cluster": {},
        "count": 1,
        "positions": [],
        "_error": False,
        "_source": "dry-run",
    }
    ok, reason = risk.portfolio_kelly_ok(30.0, "BUY YES")
    check(
        "portfolio_kelly_ok rejects bet that pushes exposure over cap",
        not ok and "portfolio-Kelly" in (reason or "").lower() or "portfolio" in (reason or "").lower(),
        f"ok={ok} reason={reason}",
    )

    # And the inverse: small proposed bet should pass
    ok2, reason2 = risk.portfolio_kelly_ok(5.0, "BUY YES")
    check(
        "portfolio_kelly_ok allows bet under cap",
        ok2,
        f"ok={ok2} reason={reason2}",
    )


def test_correlation_bucket_cap(tmp: str) -> None:
    """CORRELATION_BUCKET_CAP (50%) should reject proposed bet that would
    push same-settlement-cluster exposure over the cap."""
    section("CORRELATION_BUCKET_CAP — settlement-cluster correlation guard")
    import risk
    import config

    reset_risk_state(tmp)
    write_perf(tmp, bankroll=200.0)

    # $200 × 50% = $100 cluster cap. Simulate $90 already in cluster X,
    # propose $20 in cluster X → total $110 > cap.
    # The cluster key is derived by _cluster_key() from target_settlement.
    # We compute it the same way the code does so our seeded exposure lines up.
    settle = "2026-05-11T22:00:00+00:00"
    cluster_key = risk._cluster_key(settle)
    risk._exposure_cache = {
        "total": 90.0,
        "by_city": {},
        "by_cluster": {cluster_key: 90.0},
        "count": 1,
        "positions": [],
        "_error": False,
        "_source": "dry-run",
    }
    fake_opp = {
        "ticker": "TEST",
        "target_settlement": settle,
    }
    ok, reason = risk.settlement_cluster_ok(fake_opp, 20.0)
    check(
        "settlement_cluster_ok rejects bet that exceeds cluster cap",
        not ok and "cluster" in (reason or "").lower(),
        f"ok={ok} reason={reason}",
    )


def test_venue_signature_mismatch(tmp: str) -> None:
    """check_venue_signature() should raise on stored ≠ current signature."""
    section("VenueSignatureMismatch — fail-closed on credential swap")
    import risk

    reset_risk_state(tmp)
    write_perf(tmp, venue_signature="deadbeefcafe")  # wrong signature
    try:
        risk.check_venue_signature()
        check("check_venue_signature raises on mismatch", False,
              "expected VenueSignatureMismatch, function returned silently")
    except risk.VenueSignatureMismatch as e:
        msg = str(e)
        current = risk._current_venue_signature()
        check(
            "check_venue_signature raises VenueSignatureMismatch",
            True,
        )
        check(
            "error message names both signatures",
            "deadbeefcafe" in msg and current in msg,
            f"missing one in: {msg[:200]}",
        )
        check(
            "error message points to reset script",
            "reset_performance.py" in msg,
            f"missing reset instruction in: {msg[:200]}",
        )

    # Verify the OK case: matching signature passes silently
    reset_risk_state(tmp)
    current = risk._current_venue_signature()
    write_perf(tmp, venue_signature=current)
    try:
        risk.check_venue_signature()
        check("check_venue_signature passes when sig matches", True)
    except risk.VenueSignatureMismatch:
        check("check_venue_signature passes when sig matches", False,
              "unexpected mismatch raised on identical signatures")


def test_can_trade_clean(tmp: str) -> None:
    """Sanity: with all conditions healthy, can_trade() returns (True, [])."""
    section("Clean baseline — all signals healthy")
    import risk

    reset_risk_state(tmp)
    write_perf(tmp, bankroll=200.0, peak_pnl=10.0)
    orig_real = risk.realized_pnl_total
    orig_daily = risk._todays_pnl
    risk.realized_pnl_total = lambda: 10.0
    risk._todays_pnl = lambda: 0.0
    try:
        ok, reasons = risk.can_trade()
        check(
            "can_trade() returns (True, []) when all signals healthy",
            ok and not reasons,
            f"ok={ok} reasons={reasons}",
        )
    finally:
        risk.realized_pnl_total = orig_real
        risk._todays_pnl = orig_daily


# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    print("=" * 72)
    print("KILL-SWITCH DRY RUN")
    print("=" * 72)
    print("Verifying each pre-flip safety gate fires as expected.")
    print("Uses tempdir for performance.json; touches no real state.")

    with tempfile.TemporaryDirectory() as td:
        tmp_perf = os.path.join(td, "performance.json")

        test_per_trade_cap()
        test_can_trade_clean(tmp_perf)
        test_drawdown_halt(tmp_perf)
        test_daily_loss_halt(tmp_perf)
        test_stale_bankroll_halt(tmp_perf)
        test_zero_bankroll_halt(tmp_perf)
        test_portfolio_kelly_cap(tmp_perf)
        test_correlation_bucket_cap(tmp_perf)
        test_venue_signature_mismatch(tmp_perf)

    print()
    print("=" * 72)
    print(f"RESULT: {_passes} passed, {len(_fails)} failed")
    print("=" * 72)
    if _fails:
        print()
        print("FAILURES:")
        for f in _fails:
            print(f"  • {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
