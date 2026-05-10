"""Strategy + risk gate tests (audit 2026-05-09 follow-up).

Covers the load-bearing money functions:
  - strategy.find_opportunities — every rejection path that's been tuned in
    response to a real incident, plus the happy path.
  - risk.can_trade — every halt condition that determines whether the bot
    is allowed to take new positions.

These functions were untested before this session. They had been
covered indirectly via integration tests in test_execution.py, but their
specific gating logic (yes_filter, no_filter, bin_gate, wide_spread,
EXPOSURE / DRAWDOWN / DAILY_LOSS) had no direct assertions — meaning
a refactor that flipped one of those checks would not produce a failing
test.

Heavy use of monkeypatching to isolate from kalshi_client / forecast /
storage / config. Each test exercises ONE rejection path.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _market(ticker="KXHIGHTHOU-26MAY10-T80", city="Houston",
            title="Houston high temp on May 10, 2026 above 80",
            comparator=">=", threshold=80.0,
            yes_ask=0.40, no_ask=0.55,
            yes_bid=0.39, no_bid=0.50,
            range_low=None, range_high=None,
            close_time="2026-05-11T04:59:00Z"):
    """Canonical market dict shape that find_opportunities consumes.

    Defaults to a T-ticker (threshold market) with both sides priced
    inside MIN_PRICE/MAX_PRICE and a tight spread. Override individual
    fields per test.
    """
    return {
        "ticker": ticker, "market_id": ticker, "city": city, "title": title,
        "comparator": comparator, "threshold": threshold,
        "range_low": range_low, "range_high": range_high,
        "yes_ask_dollars": yes_ask, "no_ask_dollars": no_ask,
        "yes_bid_dollars": yes_bid, "no_bid_dollars": no_bid,
        "close_time": close_time,
    }


def _setup_strategy_env(monkeypatch, members=None, healthy=True,
                        live_positions=None, db_positions=None,
                        veto=True):
    """Stub every external dep that find_opportunities pokes.

    members: GEFS ensemble forecast (list of floats). Default 31 zeros
             so any "above threshold" returns 0.
    healthy: forecast_health gate result.
    live_positions: list[dict] returned by kalshi_client.get_open_positions.
                    None → raise (forces DB fallback).
    db_positions:   list[dict] returned by storage.load_open_positions.
    veto: True = pass, False = reject.
    """
    import calibration
    import forecast
    import forecast_health
    import kalshi_client
    import storage
    import strategy

    monkeypatch.setattr(forecast_health, "city_is_healthy", lambda c: healthy)
    monkeypatch.setattr(forecast, "get_ensemble_high",
                        lambda city, target: members or [70.0] * 31)
    monkeypatch.setattr(calibration, "calibrate", lambda p, venue="kalshi": p)
    if live_positions is None:
        def _boom(): raise RuntimeError("simulated positions failure")
        monkeypatch.setattr(kalshi_client, "get_open_positions", _boom)
    else:
        monkeypatch.setattr(kalshi_client, "get_open_positions",
                            lambda: live_positions)
    monkeypatch.setattr(storage, "load_open_positions",
                        lambda: db_positions or [])
    monkeypatch.setattr(kalshi_client, "is_blocked_insufficient_balance",
                        lambda t: False)
    monkeypatch.setattr(strategy, "_claude_veto", lambda opp: veto)


# ─── strategy.find_opportunities — happy path + rejections ───────────────────
def test_happy_path_returns_buy_no_opp_with_edge(monkeypatch):
    """Threshold market priced 0.55 NO, all 31 ensemble members below the
    threshold → calibrated_p = 0 → strong BUY NO edge. Should return one
    opportunity sorted by edge."""
    import strategy

    # All 31 members at 70°F; threshold 80°F → 0/31 above → cal_p = 0
    # (Laplace smoothing pulls it slightly above 0 but not by much).
    _setup_strategy_env(monkeypatch, members=[70.0] * 31)
    opps = strategy.find_opportunities(
        [_market(threshold=80.0, yes_ask=0.40, no_ask=0.55,
                 yes_bid=0.39, no_bid=0.50)],
        bankroll=300.0,
    )
    assert len(opps) == 1
    o = opps[0]
    assert o["action"] == "BUY NO"
    assert o["edge"] > 0.30
    assert o["recommended_size"] > 0
    assert o["entry_price"] == 0.55  # we'd cross no_ask
    # Wilson interval should be set; for 0/31 the upper bound is small but >0.
    assert 0 <= o["wilson_lo"] <= o["wilson_hi"] <= 1
    assert o["p_for_sizing"] == o["wilson_hi"]  # BUY NO uses upper bound


def test_already_held_skipped_via_live_positions(monkeypatch):
    """Live Kalshi positions endpoint reports we already hold this ticker.
    Strategy must dedup BEFORE scoring so we don't double up.

    The DB-fallback bug (2026-05-07 CHI-B62.5): if dedup goes through the
    DB only, a stranded fill (Kalshi position with no DB row from a
    cancel-race) lets us re-enter. The fix is to trust the live
    positions endpoint.
    """
    import strategy

    _setup_strategy_env(
        monkeypatch, members=[70.0] * 31,
        live_positions=[{"ticker": "KXHIGHTHOU-26MAY10-T80",
                         "position_fp": "10.00"}],
    )
    opps = strategy.find_opportunities(
        [_market(threshold=80.0, no_ask=0.55, no_bid=0.50)],
        bankroll=300.0,
    )
    assert len(opps) == 0
    venue, rej = strategy.get_last_rejections()
    assert rej.get("already_held") == 1


def test_already_held_falls_back_to_db_on_positions_error(monkeypatch):
    """If kalshi_client.get_open_positions raises, dedup must fall back to
    the DB rather than letting a held ticker through. Better to miss an
    opportunity than to double up."""
    import strategy

    _setup_strategy_env(
        monkeypatch, members=[70.0] * 31,
        live_positions=None,  # force exception
        db_positions=[{"venue": "kalshi", "ticker": "KXHIGHTHOU-26MAY10-T80"}],
    )
    opps = strategy.find_opportunities(
        [_market(threshold=80.0, no_ask=0.55, no_bid=0.50)],
        bankroll=300.0,
    )
    assert len(opps) == 0
    _, rej = strategy.get_last_rejections()
    assert rej.get("already_held") == 1


def test_forecast_unhealthy_city_skipped(monkeypatch):
    """If forecast_health flags a city (large GFS-vs-ASOS bias / RMSE in
    the 14-day window), no opportunities should be generated for it.
    The bot can't trust its own forecast for a city it knows is broken."""
    import strategy
    _setup_strategy_env(monkeypatch, members=[70.0] * 31, healthy=False)
    opps = strategy.find_opportunities(
        [_market(threshold=80.0)], bankroll=300.0,
    )
    assert len(opps) == 0
    _, rej = strategy.get_last_rejections()
    assert rej.get("forecast_unhealthy") == 1


def test_both_sides_dead_market_skipped(monkeypatch):
    """yes_ask=1.0 AND no_ask=1.0 → no real book on either side. Skip
    rather than accept the degenerate edge calc."""
    import strategy
    _setup_strategy_env(monkeypatch, members=[70.0] * 31)
    # yes_ask=1.0 (no YES seller) AND no_ask=1.0 (no NO seller). Hostile book.
    opps = strategy.find_opportunities(
        [_market(yes_ask=1.0, no_ask=1.0)], bankroll=300.0,
    )
    assert len(opps) == 0
    _, rej = strategy.get_last_rejections()
    assert rej.get("both_sides_dead") == 1


def test_one_sided_book_buy_no_still_evaluated(monkeypatch):
    """yes_ask=1.0 (no YES seller) but no_ask=0.55 — the BUY NO side is
    perfectly valid. Should NOT trigger the both_sides_dead skip; we
    should still evaluate the NO side. The 'bail only when BOTH are
    dead' design (strategy.py:487-491) prevents silently killing every
    BUY NO opportunity on a market with a one-sided YES book."""
    import strategy
    _setup_strategy_env(monkeypatch, members=[70.0] * 31)
    opps = strategy.find_opportunities(
        [_market(yes_ask=1.0, yes_bid=0.0,    # YES side dead
                 no_ask=0.55, no_bid=0.50,    # NO side healthy
                 threshold=80.0)],
        bankroll=300.0,
    )
    assert len(opps) == 1
    assert opps[0]["action"] == "BUY NO"


def test_1f_bin_buy_yes_gated(monkeypatch):
    """1°F-wide 'between' bracket: settlement noise (~1°F RMSE) consumes
    the entire bin width, so BUY YES on a 1°F bin is untradeable. The
    gate (strategy.py:512-517) is `is_1f_bin` and zeroes edge_yes."""
    import strategy

    # All 31 members at 75°F; bin is 75-76 → most members satisfy →
    # high cal_p → would normally trigger BUY YES.
    _setup_strategy_env(monkeypatch, members=[75.0] * 31)
    m = _market(comparator="in_range", threshold=None,
                range_low=75.0, range_high=76.0,
                yes_ask=0.30, no_ask=0.75,
                yes_bid=0.25, no_bid=0.70)
    opps = strategy.find_opportunities([m], bankroll=300.0)
    # Either no opp (bin_gate fired) or BUY NO (NO side wasn't gated, but
    # cal_p ~1.0 makes NO edge negative). Either way: not BUY YES.
    for o in opps:
        assert o["action"] != "BUY YES", (
            f"1°F bin BUY YES must be gated; got {o}"
        )
    _, rej = strategy.get_last_rejections()
    # bin_gate fires when the would-be action is BUY YES on a 1°F bin
    # and the resulting min_edge fails. With this setup that should be true.
    # (no_filter may also fire for the NO side; either way edge=-1 prevents YES.)
    assert rej.get("bin_gate", 0) >= 1 or rej.get("no_filter", 0) >= 1


def test_buy_yes_high_calp_filtered(monkeypatch):
    """BUY YES with calibrated_p >= 0.85 was the worst historical bucket
    (1/4, -$6.41 in the 2026-05-03 audit). The yes_filter at 0.85 is
    the tightest of the three BUY YES guards."""
    import strategy

    # 30/31 members hit threshold → cal_p ~0.97 → BUY YES would otherwise fire.
    members = [80.0] * 30 + [60.0]
    _setup_strategy_env(monkeypatch, members=members)
    m = _market(comparator=">=", threshold=70.0,
                yes_ask=0.45, no_ask=0.60, yes_bid=0.40, no_bid=0.55)
    opps = strategy.find_opportunities([m], bankroll=300.0)
    for o in opps:
        assert o["action"] != "BUY YES", f"yes_filter should block: {o}"
    _, rej = strategy.get_last_rejections()
    assert rej.get("yes_filter", 0) >= 1 or rej.get("min_edge", 0) >= 1


def test_buy_no_low_disagreement_blocked(monkeypatch):
    """BUY NO with weak NO-side edge ((1-cal_p)-no_ask < 0.15) must not
    produce an opportunity. From the 2026-05-03 audit: those were 60%
    win-rate but losing in dollars (avg entry $0.77 needed 77% to break
    even). The no_filter zeroes edge_no when this fires.

    Note on attribution: when both yes_filter and no_filter fire, the
    rejection is bucketed as yes_filter (because action selection picks
    BUY YES on the tied edges). We assert blocking, not the attribution
    bucket — the attribution pathway is a known telemetry quirk worth
    flagging separately."""
    import strategy

    # 0/31 → cal_p ≈ 0.08 (Laplace). no_ask 0.85 → edge_no = 0.92 - 0.85
    # = 0.07 < 0.15 → no_filter fires. yes_ask 0.85 also makes
    # cal_p - yes_ask < 0.30 → yes_filter fires.
    _setup_strategy_env(monkeypatch, members=[60.0] * 31)
    m = _market(comparator=">=", threshold=80.0,
                yes_ask=0.85, no_ask=0.85,
                yes_bid=0.80, no_bid=0.80)
    opps = strategy.find_opportunities([m], bankroll=300.0)
    assert len(opps) == 0
    _, rej = strategy.get_last_rejections()
    # Either yes_filter (typical attribution due to tie-breaking) or
    # no_filter (if the comparison ever picks BUY NO) is acceptable —
    # both indicate the design's intent (block the trade) was honored.
    assert (rej.get("yes_filter", 0)
            + rej.get("no_filter", 0)
            + rej.get("min_edge", 0)) >= 1


def test_1f_bin_wide_spread_rejected(monkeypatch):
    """B-ticker (1°F bin) BUY NO with no_ask - no_bid > 0.10: the bot
    was buying against ghost asks (2026-05-09 audit). Spread > $0.10
    on the NO side → wide_spread rejection.

    Inputs need cal_p high enough that no_filter does NOT pre-empt
    (otherwise edge_no zeroes and action picks BUY YES, attributed
    to bin_gate instead of wide_spread)."""
    import strategy

    # 0/31 → cal_p ~0.08 (Laplace). edge_no = 0.92 - 0.65 = 0.27 → NOT
    # filtered. is_1f_bin → yes_filter fires → BUY NO chosen → wide_spread
    # check runs.
    _setup_strategy_env(monkeypatch, members=[70.0] * 31)
    m = _market(comparator="in_range", threshold=None,
                range_low=75.0, range_high=76.0,
                yes_ask=0.30, no_ask=0.65,
                yes_bid=0.10, no_bid=0.40)  # spread = 0.25 > 0.10 → wide
    opps = strategy.find_opportunities([m], bankroll=300.0)
    assert len(opps) == 0
    _, rej = strategy.get_last_rejections()
    assert rej.get("wide_spread", 0) >= 1


def test_1f_bin_no_bid_rejected(monkeypatch):
    """Same gate, separate branch: no_bid <= 0 means there's no real bid
    at all. Reject before we cross."""
    import strategy
    _setup_strategy_env(monkeypatch, members=[70.0] * 31)
    m = _market(comparator="in_range", threshold=None,
                range_low=75.0, range_high=76.0,
                yes_ask=0.30, no_ask=0.55,
                yes_bid=0.20, no_bid=0.0)  # no bid on NO side
    opps = strategy.find_opportunities([m], bankroll=300.0)
    assert len(opps) == 0
    _, rej = strategy.get_last_rejections()
    assert rej.get("wide_spread", 0) >= 1


def test_wilson_sizing_is_more_conservative_than_point(monkeypatch):
    """Wilson-shrunk sizing (audit 2026-05-09): for BUY NO with small-N
    cal_p, we size against the upper Wilson bound (worst-case YES
    probability), not the point estimate. This must produce a smaller
    Kelly than naive point sizing.

    Witness: a market where cal_p = 1/31 (one member hit). Wilson upper
    is ~0.16 — much higher than point 0.032. Sizing against 0.16 is
    smaller (Kelly is concave in p_lose for BUY NO)."""
    import risk
    import strategy

    members = [70.0] * 30 + [85.0]   # 1/31 above 80 → cal_p ~0.06 (Laplace)
    _setup_strategy_env(monkeypatch, members=members)
    opps = strategy.find_opportunities(
        [_market(threshold=80.0, no_ask=0.45, no_bid=0.40)],
        bankroll=1000.0,  # large enough that 5% cap doesn't bind on small p_size
    )
    assert len(opps) == 1
    o = opps[0]

    # Wilson size — what the strategy actually returned.
    wilson_size = o["recommended_size"]

    # Naive size — using cal_p directly (no Wilson shrink).
    naive_size = risk.kelly_size(
        p=o["calibrated_p"], price=o["entry_price"], bankroll=1000.0,
        action="BUY NO",  # do NOT pass p_for_sizing
    )

    # If the cap binds for both we won't see a difference; check that
    # at least one is sub-cap, then assert Wilson <= naive.
    cap = 1000.0 * 0.05  # MAX_SINGLE_BET_PCT
    if naive_size < cap - 0.01:
        assert wilson_size <= naive_size + 1e-3, (
            f"Wilson sizing must shrink (or equal) naive Kelly for BUY NO; "
            f"wilson={wilson_size} naive={naive_size}"
        )


# ─── risk.can_trade — every halt condition ───────────────────────────────────
def _setup_perf(tmp_path, monkeypatch, *, bankroll=300.0, peak_pnl=0.0,
                starting_bankroll=300.0, age_seconds=10.0):
    """Write a fake performance.json with controllable freshness."""
    import config
    import risk

    perf_dir = tmp_path / "data"
    perf_dir.mkdir(exist_ok=True)
    perf_file = perf_dir / "performance.json"
    # Use local-clock naive datetime to match get_active_bankroll's
    # `datetime.now()` (also local). Mixing UTC-stripped and local
    # gave a TZ-offset-sized error and made staleness tests flaky
    # (would fail in PDT, pass in UTC).
    updated = datetime.now() - timedelta(seconds=age_seconds)
    perf_file.write_text(json.dumps({
        "bankroll": bankroll,
        "peak_pnl": peak_pnl,
        "starting_bankroll": starting_bankroll,
        "updated_at": updated.isoformat(),
    }))
    monkeypatch.setattr(config, "PERF_FILE", str(perf_file))
    monkeypatch.setattr(risk, "PERF_FILE", str(perf_file), raising=False)
    return perf_file


def _setup_db(tmp_path, monkeypatch, *, results=None):
    """Empty trades.db with optional pre-seeded results rows.

    `results`: list of dicts with keys profit_loss + resolved_at + paper_trade
    + notes. Used to drive _todays_pnl and realized_pnl_total.
    """
    import config
    import risk
    db = tmp_path / "trades.db"
    monkeypatch.setattr(config, "DB_FILE", str(db))
    monkeypatch.setattr(risk, "DB_FILE", str(db), raising=False)
    with sqlite3.connect(db) as c:
        c.executescript("""
            CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT, mode TEXT, paper_trade INT, notes TEXT,
                market_type TEXT);
            CREATE TABLE results (id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INT, outcome TEXT, exit_price REAL,
                profit_loss REAL, resolved_at TEXT, venue TEXT);
        """)
        for row in (results or []):
            c.execute(
                "INSERT INTO trades (ticker, mode, paper_trade, notes) "
                "VALUES (?, 'taker', ?, ?)",
                ("T", int(row.get("paper_trade", 0)), row.get("notes")),
            )
            tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute(
                "INSERT INTO results (trade_id, profit_loss, resolved_at) "
                "VALUES (?, ?, ?)",
                (tid, row["profit_loss"], row["resolved_at"]),
            )
        c.commit()
    return db


def _stub_kalshi_positions(monkeypatch, positions=None):
    """Default to a successful empty-positions response."""
    import kalshi_client
    monkeypatch.setattr(kalshi_client, "get_open_positions",
                        lambda: positions or [])


def test_can_trade_happy_path(monkeypatch, tmp_path):
    """Fresh bankroll, no exposure, no drawdown, no daily loss → ok=True."""
    import risk
    _setup_perf(tmp_path, monkeypatch, bankroll=300.0, age_seconds=10)
    _setup_db(tmp_path, monkeypatch)
    _stub_kalshi_positions(monkeypatch)
    risk.invalidate_exposure_cache()
    ok, reasons = risk.can_trade()
    assert ok, f"happy path should pass; got reasons={reasons}"
    assert reasons == []


def test_can_trade_zero_bankroll(monkeypatch, tmp_path):
    import risk
    _setup_perf(tmp_path, monkeypatch, bankroll=0.0)
    _setup_db(tmp_path, monkeypatch)
    _stub_kalshi_positions(monkeypatch)
    risk.invalidate_exposure_cache()
    ok, reasons = risk.can_trade()
    assert ok is False
    assert any("ZERO_BANKROLL" in r for r in reasons), reasons


def test_can_trade_stale_bankroll(monkeypatch, tmp_path):
    """BANKROLL_STALE_SECONDS = 120. age 600 must trip STALE_BANKROLL."""
    import risk
    _setup_perf(tmp_path, monkeypatch, bankroll=300.0, age_seconds=600)
    _setup_db(tmp_path, monkeypatch)
    _stub_kalshi_positions(monkeypatch)
    risk.invalidate_exposure_cache()
    ok, reasons = risk.can_trade()
    assert ok is False
    assert any("STALE_BANKROLL" in r for r in reasons), reasons


def test_can_trade_exposure_cache_error_blocks(monkeypatch, tmp_path):
    """Kalshi positions endpoint failure → exposure cache is in error
    state → block all new trades. Better to miss opportunities than to
    trade with unknown exposure."""
    import kalshi_client
    import risk
    _setup_perf(tmp_path, monkeypatch, bankroll=300.0, age_seconds=10)
    _setup_db(tmp_path, monkeypatch)
    def _boom(): raise RuntimeError("simulated outage")
    monkeypatch.setattr(kalshi_client, "get_open_positions", _boom)
    risk.invalidate_exposure_cache()
    ok, reasons = risk.can_trade()
    assert ok is False
    assert any("EXPOSURE_CACHE_ERROR" in r for r in reasons), reasons


def test_can_trade_drawdown_exceeded(monkeypatch, tmp_path):
    """MAX_DRAWDOWN_PCT = 33%. peak +$50, current $100 (= starting $300
    + realized -$200) → bankroll $100, peak_bankroll $350, drawdown 71%
    > 33% → DRAWDOWN halt fires."""
    import risk
    today = datetime.now().strftime("%Y-%m-%d")
    _setup_perf(tmp_path, monkeypatch, bankroll=100.0,
                peak_pnl=50.0, starting_bankroll=300.0)
    _setup_db(tmp_path, monkeypatch, results=[
        {"profit_loss": -200.0, "resolved_at": today},
    ])
    _stub_kalshi_positions(monkeypatch)
    risk.invalidate_exposure_cache()
    ok, reasons = risk.can_trade()
    assert ok is False
    assert any("DRAWDOWN" in r for r in reasons), reasons


def test_can_trade_daily_loss_exceeded(monkeypatch, tmp_path):
    """DAILY_LOSS_LIMIT_PCT = 20%. bankroll $300, today P&L -$70 →
    -23.3% > 20% → DAILY_LOSS halt fires."""
    import risk
    today = datetime.now().strftime("%Y-%m-%d")
    _setup_perf(tmp_path, monkeypatch, bankroll=300.0)
    _setup_db(tmp_path, monkeypatch, results=[
        {"profit_loss": -70.0, "resolved_at": today},
    ])
    _stub_kalshi_positions(monkeypatch)
    risk.invalidate_exposure_cache()
    ok, reasons = risk.can_trade()
    assert ok is False
    assert any("DAILY_LOSS" in r for r in reasons), reasons


def test_can_trade_paper_loss_does_not_trigger_daily_halt(monkeypatch, tmp_path):
    """Audit C5: paper trade losses must NOT contribute to live halt math.
    Today P&L on the live bankroll should be 0 even if there's a -$200
    paper loss. Otherwise re-enabling Polymarket paper would halt live
    Kalshi trading whenever paper had a bad day."""
    import risk
    today = datetime.now().strftime("%Y-%m-%d")
    _setup_perf(tmp_path, monkeypatch, bankroll=300.0)
    _setup_db(tmp_path, monkeypatch, results=[
        # Big paper loss — must be excluded from live today-P&L math.
        {"profit_loss": -200.0, "resolved_at": today, "paper_trade": 1},
    ])
    _stub_kalshi_positions(monkeypatch)
    risk.invalidate_exposure_cache()
    ok, reasons = risk.can_trade()
    assert ok, f"paper loss must not halt live; got reasons={reasons}"


def test_can_trade_invalid_notes_excluded_from_halt_math(monkeypatch, tmp_path):
    """Trades flagged as invalid:* / void:* / ghost-* must be excluded
    from drawdown math — they're auto-tagged as not-real-trades. The
    centralized NOTES_VALID_LIVE_SQL constant is what enforces this;
    a regression that drops the gate would surface as a phantom halt."""
    import risk
    today = datetime.now().strftime("%Y-%m-%d")
    _setup_perf(tmp_path, monkeypatch, bankroll=300.0)
    _setup_db(tmp_path, monkeypatch, results=[
        {"profit_loss": -200.0, "resolved_at": today, "notes": "invalid:demo_flap"},
        {"profit_loss": -200.0, "resolved_at": today, "notes": "ghost-backfill"},
    ])
    _stub_kalshi_positions(monkeypatch)
    risk.invalidate_exposure_cache()
    ok, reasons = risk.can_trade()
    assert ok, f"invalid/ghost trades must not halt live; got reasons={reasons}"
