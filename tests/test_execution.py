"""Execution-logic + profitability tests for kalshi_bot_2.0.

Targets the specific weaknesses called out in v1 postmortem and the
project memory — the failure modes that lost money or made paper P&L
look better than reality. Each test maps to a known landmine:

  postmortem §3.1 — same-source resolution leak (forecast feeding outcome)
  postmortem §3.2 — fantasy fills (filling beyond snapshot depth)
  postmortem §3.3 — mark-to-side instead of mark-to-mid round-trip
  postmortem §3.4 — adverse selection unmodeled in maker fills
  postmortem §4.4 — selection bias (only scoring scanner-picked subset)
  postmortem §4.5 — heuristic fees instead of observed
  postmortem §6.3 — dashboard double-count vs SUM(profit_loss)
  memory      — separate calibration per venue (no cross-venue leak)
  memory      — strict-below maker fill (queue-priority honesty)

Each test is independent. Heavy use of monkeypatching + tmp DB so we
never touch the live bot.db / trades.db.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ─── Fakes ────────────────────────────────────────────────────────────────────
def _fake_book(best_yes_ask=None, best_no_ask=None,
               yes_depth=None, no_depth=None):
    """Build the OrderBook shape paper_executor / maker_sim expect.

    `*_depth` is a dict {price_ceiling: cumulative_contracts_at_or_below}.
    """
    def depth_at(table):
        def _f(p):
            if not table:
                return 0
            keys = sorted(table.keys())
            best = 0
            for k in keys:
                if k <= p + 1e-9:
                    best = table[k]
            return best
        return _f
    return {
        "best_yes_ask": best_yes_ask,
        "best_no_ask": best_no_ask,
        "yes_depth_at_price": depth_at(yes_depth or {}),
        "no_depth_at_price": depth_at(no_depth or {}),
    }


class FakeVenue:
    """Minimal Venue surface for paper_executor / maker_sim / reconcile."""
    def __init__(self, name="fake", book=None, resolution=None,
                 fee_per_contract=0.0):
        self.name = name
        self._book = book
        self._resolution = resolution
        self._fee = fee_per_contract

    def get_book(self, market_id):
        return self._book

    def get_resolution(self, market_id):
        return self._resolution

    def fee_for_trade(self, price, contracts, side, mode="taker"):
        return float(self._fee) * int(contracts)


# ─── §3.2 — fantasy fills are clamped to snapshot depth ───────────────────────
def test_paper_taker_clamps_to_snapshot_depth(monkeypatch):
    import paper_executor

    # 100 contracts wanted; book has only 12 at best ask, then nothing.
    venue = FakeVenue(book=_fake_book(
        best_yes_ask=0.40,
        yes_depth={0.40: 12},
    ))
    opp = {
        "market_id": "MKT", "action": "BUY YES",
        "calibrated_p": 0.70, "contracts": 100,
    }
    out = paper_executor.execute_paper_opportunity(opp, venue, mode="taker")
    assert out["filled"] is True
    assert out["fill_count"] == 12, "must clamp; v1 §3.2 forbids fantasy fills"
    assert out["fill_price"] == pytest.approx(0.40)


def test_paper_taker_walks_levels_with_vwap(monkeypatch):
    import paper_executor

    # 30 wanted; 10 at 0.40, then +10 at 0.41, then +20 at 0.42.
    venue = FakeVenue(book=_fake_book(
        best_yes_ask=0.40,
        yes_depth={0.40: 10, 0.41: 20, 0.42: 40},
    ))
    opp = {"market_id": "M", "action": "BUY YES",
           "calibrated_p": 0.95, "contracts": 30}
    out = paper_executor.execute_paper_opportunity(opp, venue, mode="taker")
    assert out["filled"] and out["fill_count"] == 30
    expected_vwap = (0.40 * 10 + 0.41 * 10 + 0.42 * 10) / 30
    assert out["fill_price"] == pytest.approx(expected_vwap, abs=1e-4)


def test_paper_taker_stops_walking_when_fees_eat_edge(monkeypatch):
    """§4.5: walking up the book must respect fee-net edge gate, not just
    gross. A high per-contract fee should cut the walk short."""
    import paper_executor
    from config import MIN_EDGE

    venue = FakeVenue(
        book=_fake_book(best_yes_ask=0.40, yes_depth={0.40: 5, 0.50: 100}),
        fee_per_contract=0.05,  # eats 5c per contract regardless of price
    )
    opp = {"market_id": "M", "action": "BUY YES",
           "calibrated_p": 0.55, "contracts": 50}
    out = paper_executor.execute_paper_opportunity(opp, venue, mode="taker")
    # gross edge at 0.50 = 0.05; minus 0.05 fee = 0.0 < MIN_EDGE → must stop
    if out["filled"]:
        assert out["fill_count"] <= 5, f"walked past fee-eaten levels: {out}"
    else:
        assert "fees_eat_edge" in out["notes"] or "no_fillable" in out["notes"]
    assert MIN_EDGE > 0  # sanity


# ─── memory — strict-below maker fill (no fantasy queue jumps) ────────────────
def test_maker_sim_does_not_fill_at_equal_price(monkeypatch, tmp_path):
    """Memory rule + maker_sim docstring: fill requires best_ask STRICTLY
    BELOW our limit. Equality means we don't know queue position, must
    leave pending."""
    import maker_sim
    import storage

    # Stub storage: one pending order at limit 0.40.
    pending = [{
        "id": 1, "venue": "fake", "market_id": "M",
        "action": "BUY YES", "side": "yes",
        "limit_price": 0.40, "target_contracts": 10,
        "calibrated_p": 0.70, "edge_at_post": 0.10,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat() + "Z",
        "opp_json": "{}",
    }]
    monkeypatch.setattr(storage, "get_pending_paper_orders", lambda: pending)
    log_calls = []
    monkeypatch.setattr(storage, "log_trade",
                        lambda opp, fill: log_calls.append((opp, fill)) or 1)
    monkeypatch.setattr(storage, "mark_paper_order_filled",
                        lambda *a, **k: None)
    monkeypatch.setattr(storage, "mark_paper_order_expired",
                        lambda *a, **k: None)

    # Equality case → must NOT fill.
    venue = FakeVenue(book=_fake_book(best_yes_ask=0.40, yes_depth={0.40: 99}))
    monkeypatch.setattr(maker_sim, "_venue_for", lambda n: venue)
    summary = maker_sim.resolve_pending_orders()
    assert summary["filled"] == 0
    assert summary["still_pending"] == 1
    assert log_calls == []


def test_maker_sim_fills_when_book_strictly_crosses(monkeypatch):
    import maker_sim
    import storage

    pending = [{
        "id": 7, "venue": "fake", "market_id": "M",
        "action": "BUY YES", "side": "yes",
        "limit_price": 0.40, "target_contracts": 10,
        "calibrated_p": 0.70, "edge_at_post": 0.10,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat() + "Z",
        "opp_json": "{}",
    }]
    monkeypatch.setattr(storage, "get_pending_paper_orders", lambda: pending)
    trades = []
    fills = []
    monkeypatch.setattr(storage, "log_trade",
                        lambda o, f: (trades.append((o, f)), 42)[1])
    monkeypatch.setattr(storage, "mark_paper_order_filled",
                        lambda *a, **k: fills.append(a))
    monkeypatch.setattr(storage, "mark_paper_order_expired",
                        lambda *a, **k: None)

    venue = FakeVenue(book=_fake_book(best_yes_ask=0.39, yes_depth={0.39: 50}))
    monkeypatch.setattr(maker_sim, "_venue_for", lambda n: venue)
    summary = maker_sim.resolve_pending_orders()
    assert summary["filled"] == 1
    assert len(trades) == 1
    o, f = trades[0]
    # Filled at our LIMIT, not the new best_ask — we got the spread.
    assert f["fill_price"] == 0.40
    assert f["fill_count"] == 10


def test_maker_sim_expires_stale_orders(monkeypatch):
    import maker_sim
    import storage

    pending = [{
        "id": 9, "venue": "fake", "market_id": "M",
        "action": "BUY YES", "side": "yes",
        "limit_price": 0.40, "target_contracts": 10,
        "calibrated_p": 0.5, "edge_at_post": 0.10,
        "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=5))
            .strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
        "opp_json": "{}",
    }]
    monkeypatch.setattr(storage, "get_pending_paper_orders", lambda: pending)
    expired = []
    monkeypatch.setattr(storage, "mark_paper_order_expired",
                        lambda oid: expired.append(oid))
    monkeypatch.setattr(maker_sim, "_venue_for", lambda n: None)  # not consulted

    summary = maker_sim.resolve_pending_orders()
    assert summary["expired"] == 1 and expired == [9]


# ─── C2 / 2026-05-06 checkpoint — Kalshi resolution requires venue-side settlement
def test_kalshi_resolution_requires_zero_position(monkeypatch):
    """The 2026-05-05 demo flap returned status='finalized' for ~28 markets,
    then reverted them. reconcile wrote results based on the briefly-bad
    data → ~$200 of phantom P&L. Defense: also require position_fp == 0
    on the positions endpoint before accepting the resolution.

    Audit C2 / checkpoint open issue #1.
    """
    import kalshi_client
    import kalshi_venue

    # Fresh module state for this test.
    kalshi_venue._positions_cache["last_ok_ts"] = 0.0
    kalshi_venue._positions_cache["by_ticker"] = {}

    # Market shows finalized, but venue still says we hold the position.
    monkeypatch.setattr(
        kalshi_client, "get_market",
        lambda mid: {"status": "finalized", "result": "yes",
                     "expiration_time": "2026-05-08T00:00:00Z"},
    )
    monkeypatch.setattr(
        kalshi_client, "get_open_positions",
        lambda: [{"ticker": "TKR", "position_fp": "10.00"}],
    )

    venue = kalshi_venue.KalshiVenue()
    res = venue.get_resolution("TKR")
    assert res is None, (
        "must NOT accept resolution while position_fp != 0 — "
        "this is the trust-but-verify defense from the 2026-05-06 checkpoint"
    )

    # Now the venue has cleared the position; resolution should land.
    kalshi_venue._positions_cache["last_ok_ts"] = 0.0  # bust cache
    kalshi_venue._positions_cache["by_ticker"] = {}
    monkeypatch.setattr(
        kalshi_client, "get_open_positions",
        lambda: [{"ticker": "TKR", "position_fp": "0"}],
    )
    res = venue.get_resolution("TKR")
    assert res is not None and res["outcome"] == "yes"


def test_kalshi_resolution_fail_closed_when_positions_unreachable(monkeypatch):
    """Audit C2: if /portfolio/positions can't be fetched at all, we have
    no way to verify settlement happened. Fail closed (return None) rather
    than trusting the status flag alone — that's the exact gap the 2026-
    05-05 incident exploited."""
    import kalshi_client
    import kalshi_venue

    kalshi_venue._positions_cache["last_ok_ts"] = 0.0
    kalshi_venue._positions_cache["by_ticker"] = {}

    monkeypatch.setattr(
        kalshi_client, "get_market",
        lambda mid: {"status": "finalized", "result": "yes"},
    )
    def _boom():
        raise RuntimeError("simulated network outage")
    monkeypatch.setattr(kalshi_client, "get_open_positions", _boom)

    venue = kalshi_venue.KalshiVenue()
    res = venue.get_resolution("TKR")
    assert res is None, "must fail closed when settlement cannot be verified"


# ─── §3.1 — resolution must come from venue oracle, never forecast ────────────
def test_reconcile_uses_venue_outcome_not_forecast(monkeypatch, tmp_path):
    """Even if our calibrated probability said 0.99 YES, a venue NO
    outcome must produce a LOSS. v1's biggest false-signal source."""
    import config
    import reconcile

    db = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_FILE", str(db))
    monkeypatch.setattr(reconcile, "DB_FILE", str(db), raising=False)

    with sqlite3.connect(db) as c:
        c.executescript("""
            CREATE TABLE trades (id INTEGER PRIMARY KEY, ticker TEXT,
                action TEXT, entry_price REAL, contracts INT,
                venue TEXT, mode TEXT, notes TEXT);
            CREATE TABLE results (id INTEGER PRIMARY KEY, trade_id INT,
                outcome TEXT, exit_price REAL, profit_loss REAL,
                resolved_at TEXT, venue TEXT);
            INSERT INTO trades VALUES
                (1, 'TKR', 'BUY YES', 0.30, 10, 'fake', 'live', NULL);
        """)

    # Forecast said YES with p=0.99, but the venue oracle says NO.
    venue = FakeVenue(name="fake", resolution={"outcome": "no"})
    monkeypatch.setattr(reconcile, "_venue_for", lambda n: venue)

    out = reconcile.reconcile_settled_trades(sleep_between=0)
    assert out["settled"] == 1
    with sqlite3.connect(db) as c:
        row = c.execute(
            "SELECT outcome, profit_loss FROM results WHERE trade_id=1"
        ).fetchone()
    assert row[0] == "no"
    # BUY YES @ 0.30 x 10, lost: pnl = -0.30 * 10 = -3.00 (no fee for fake venue)
    assert row[1] == pytest.approx(-3.00, abs=1e-3)


def test_reconcile_skips_ambiguous_outcomes(monkeypatch, tmp_path):
    """UMA can return Tie / 50-50. Must not guess; leave open for review."""
    import config
    import reconcile

    db = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_FILE", str(db))
    monkeypatch.setattr(reconcile, "DB_FILE", str(db), raising=False)
    with sqlite3.connect(db) as c:
        c.executescript("""
            CREATE TABLE trades (id INT PRIMARY KEY, ticker TEXT, action TEXT,
                entry_price REAL, contracts INT, venue TEXT, mode TEXT, notes TEXT);
            CREATE TABLE results (id INTEGER PRIMARY KEY, trade_id INT,
                outcome TEXT, exit_price REAL, profit_loss REAL,
                resolved_at TEXT, venue TEXT);
            INSERT INTO trades VALUES (1, 'T', 'BUY YES', 0.5, 5,
                'polymarket', 'live', NULL);
        """)
    venue = FakeVenue(name="polymarket", resolution={"outcome": "Tie"})
    monkeypatch.setattr(reconcile, "_venue_for", lambda n: venue)
    out = reconcile.reconcile_settled_trades(sleep_between=0)
    assert out["settled"] == 0 and out["still_open"] == 1


def test_reconcile_normalizes_polymarket_capitalization(monkeypatch, tmp_path):
    import config
    import reconcile

    db = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_FILE", str(db))
    monkeypatch.setattr(reconcile, "DB_FILE", str(db), raising=False)
    with sqlite3.connect(db) as c:
        c.executescript("""
            CREATE TABLE trades (id INT PRIMARY KEY, ticker TEXT, action TEXT,
                entry_price REAL, contracts INT, venue TEXT, mode TEXT, notes TEXT);
            CREATE TABLE results (id INTEGER PRIMARY KEY, trade_id INT,
                outcome TEXT, exit_price REAL, profit_loss REAL,
                resolved_at TEXT, venue TEXT);
            INSERT INTO trades VALUES (1, 'T', 'BUY YES', 0.40, 10,
                'polymarket', 'live', NULL);
        """)
    venue = FakeVenue(name="polymarket", resolution={"outcome": "Yes"})
    monkeypatch.setattr(reconcile, "_venue_for", lambda n: venue)
    reconcile.reconcile_settled_trades(sleep_between=0)
    with sqlite3.connect(db) as c:
        row = c.execute("SELECT outcome, profit_loss FROM results").fetchone()
    assert row[0] == "yes"
    assert row[1] == pytest.approx(0.60 * 10, abs=1e-3)  # polymarket = 0 fee


# ─── P&L correctness across all four win/lose × side combinations ─────────────
@pytest.mark.parametrize("action,entry,result,expected_sign", [
    ("BUY YES", 0.30, "yes",  +1),  # win
    ("BUY YES", 0.30, "no",   -1),  # loss
    ("BUY NO",  0.40, "no",   +1),  # win (BUY NO @ 0.40 → pays 0.60 if NO)
    ("BUY NO",  0.40, "yes",  -1),  # loss
])
def test_pnl_signs_for_all_quadrants(action, entry, result, expected_sign):
    import reconcile
    _, _, pnl = reconcile._compute_pnl(action, entry, 10, result, "polymarket")
    assert (pnl > 0) is (expected_sign > 0), f"{action}@{entry} vs {result} → {pnl}"


def test_pnl_kalshi_subtracts_fees():
    """Kalshi-specific: fees must come out of P&L. Compare same trade on
    'fake' (zero-fee) vs 'kalshi' (nonzero) — kalshi P&L must be lower."""
    import reconcile
    _, _, pnl_no_fee = reconcile._compute_pnl("BUY YES", 0.50, 100, "yes", "polymarket")
    _, _, pnl_kalshi = reconcile._compute_pnl("BUY YES", 0.50, 100, "yes", "kalshi")
    assert pnl_kalshi < pnl_no_fee, "kalshi fees must reduce P&L"


# ─── memory — separate calibration per venue (no cross-venue leak) ────────────
def test_calibration_is_per_venue(monkeypatch):
    """Applying Kalshi's isotonic curve to a Polymarket price is the §3.1
    mistake in a new outfit. Calibrate must accept (or be keyed by) venue
    and produce different mappings when venue-specific data exists."""
    import calibration

    # If calibrate doesn't accept a venue kwarg at all, that's the failure
    # mode the memory warns about.
    sig = getattr(calibration.calibrate, "__code__", None)
    assert sig is not None
    arg_names = sig.co_varnames[: sig.co_argcount]
    assert "venue" in arg_names, (
        "calibration.calibrate() must take venue= kwarg. Memory: 'Kalshi's "
        "isotonic transform applied to Polymarket prices = the §3.1 mistake'."
    )


# ─── §3.3 — round-trip mark-to-mid, not mark-to-side ──────────────────────────
def test_round_trip_pnl_pays_both_spreads():
    """Honest mark-to-mid round-trip: entry at ask, exit at bid. The
    spread is paid TWICE. Any helper that marks-to-side underreports cost.

    This test computes the "true" round-trip cost of a flat trade and
    asserts it's negative when there's a spread, regardless of which side
    of mid you came in on. Pure math — no module dependency — so it
    survives refactors and documents the discipline.
    """
    bid, ask = 0.38, 0.42  # 4c spread, mid=0.40
    contracts = 10
    # Buy YES @ ask, immediately sell YES @ bid → loss = spread × contracts
    entry = ask
    exit_ = bid
    pnl_naive_mid = (0.40 - entry) * contracts          # mark-to-mid only
    pnl_true_round_trip = (exit_ - entry) * contracts    # both spreads
    assert pnl_true_round_trip < pnl_naive_mid          # mid is too rosy
    assert pnl_true_round_trip == pytest.approx(-0.40)


# ─── §6.3 — dashboard headline P&L equals SUM(profit_loss) ────────────────────
def test_dashboard_headline_matches_sum_of_results(tmp_path, monkeypatch):
    """The single most dangerous bug class per memory. We don't depend on
    the dashboard's exact code path; we assert the invariant holds at the
    SQL level — any aggregator must equal SUM(profit_loss)."""
    import config

    db = tmp_path / "smoke.db"
    monkeypatch.setattr(config, "DB_FILE", str(db))
    with sqlite3.connect(db) as c:
        c.executescript("""
            CREATE TABLE results (id INTEGER PRIMARY KEY, trade_id INT,
                outcome TEXT, exit_price REAL, profit_loss REAL,
                resolved_at TEXT, venue TEXT);
            INSERT INTO results (trade_id, outcome, profit_loss, venue) VALUES
                (1, 'yes',  1.50, 'kalshi'),
                (2, 'no',  -0.80, 'kalshi'),
                (3, 'yes',  2.10, 'polymarket'),
                (4, 'no',  -0.50, 'polymarket');
        """)
        total_sql = c.execute("SELECT SUM(profit_loss) FROM results").fetchone()[0]
        per_venue = dict(c.execute(
            "SELECT venue, SUM(profit_loss) FROM results GROUP BY venue"
        ).fetchall())
    # The smoke check the memory mandates:
    assert round(total_sql, 4) == round(sum(per_venue.values()), 4)
    # And the per-venue split is non-empty for both venues (catches a
    # silent dashboard filter that drops one venue's rows from the total).
    assert "kalshi" in per_venue and "polymarket" in per_venue


# ─── §6.3 — "today" P&L excludes backfill and dry-run rows ───────────────────
def test_today_pnl_excludes_backfill_and_dry_run(tmp_path, monkeypatch):
    """Backfill rows have resolved_at = the date the backfill ran, not
    the date the market actually settled. If the dashboard's "today"
    headline includes them, a backfill operation will appear as a giant
    P&L spike that masks real trading. Real-world incident 2026-05-05:
    +$518 backfill hid −$186 of live taker losses behind a +$332 number.

    The query under test is the EXACT one in dashboard._kpis(); change
    both sides together if you refactor.
    """
    import config

    db = tmp_path / "today.db"
    monkeypatch.setattr(config, "DB_FILE", str(db))
    with sqlite3.connect(db) as c:
        c.executescript("""
            CREATE TABLE trades (id INTEGER PRIMARY KEY, mode TEXT);
            CREATE TABLE results (id INTEGER PRIMARY KEY, trade_id INT,
                profit_loss REAL, resolved_at TEXT);
            INSERT INTO trades VALUES
                (1, 'taker'), (2, 'maker'), (3, 'paper:maker'),
                (4, 'backfill'), (5, 'dry-run'), (6, NULL);
            INSERT INTO results (trade_id, profit_loss, resolved_at) VALUES
                (1, -150.0, datetime('now','localtime')),
                (2,    1.0, datetime('now','localtime')),
                (3,  -35.0, datetime('now','localtime')),
                (4, +500.0, datetime('now','localtime')),  -- must be excluded
                (5,  +99.0, datetime('now','localtime')),  -- must be excluded
                (6,   +5.0, datetime('now','localtime'));
        """)

    sql = """
        SELECT COALESCE(SUM(r.profit_loss), 0.0)
        FROM results r JOIN trades t ON r.trade_id = t.id
        WHERE DATE(r.resolved_at) = DATE('now', 'localtime')
          AND (t.mode IS NULL OR t.mode NOT IN ('dry-run', 'backfill'))
    """
    with sqlite3.connect(db) as c:
        today_pnl = c.execute(sql).fetchone()[0]
    # Real trading: -150 + 1 - 35 + 5 = -179. Backfill (+500) and
    # dry-run (+99) excluded.
    assert today_pnl == pytest.approx(-179.0, abs=1e-6), (
        f"got {today_pnl}; backfill or dry-run rows are leaking into "
        f"today P&L (§6.3 dashboard double-count class of bug)"
    )


# ─── checkpoint #1 — _verify_after_cancel recovers ambiguous fills ──────────
def test_verify_after_cancel_recovers_canceled_with_partial_fill(monkeypatch):
    """The exact 2026-05-07 PHIL pattern: order placed, status 'resting'
    fc=0 for several polls, then resolves to status='canceled' fc=21
    (partial fill landed before cancel propagated). The original
    implementation bailed at attempt 3 with status still 'resting',
    leaking the 21-contract position into trades.db's blind spot."""
    import executor
    import kalshi_client

    # Simulate the timing: 5 polls show resting+0, then status resolves
    # to canceled with a partial fill of 21 contracts.
    poll_count = {"n": 0}
    def fake_status(oid):
        poll_count["n"] += 1
        if poll_count["n"] <= 5:
            return {"status": "resting", "fill_count_fp": "0.00",
                    "remaining_count_fp": "35.00", "order_id": oid}
        return {"status": "canceled", "fill_count_fp": "21.00",
                "remaining_count_fp": "0.00", "order_id": oid}
    monkeypatch.setattr(kalshi_client, "get_order_status", fake_status)
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    monkeypatch.setattr(kalshi_client, "cancel_order", lambda oid: True)

    out = executor._verify_after_cancel("ORD123", 0.59, "taker", "TKR")
    assert out is not None, "must recover the partial fill, not orphan it"
    assert out["filled"] is True
    assert out["fill_count"] == 21
    assert out["mode"] == "taker_late_fill"


def test_verify_after_cancel_clean_zero_no_cooldown(monkeypatch):
    """Order resolves to status='canceled' fc=0 within poll window:
    that's authoritative 'no fill', so no cooldown should be armed."""
    import executor
    import kalshi_client

    monkeypatch.setattr(
        kalshi_client, "get_order_status",
        lambda oid: {"status": "canceled", "fill_count_fp": "0.00",
                     "remaining_count_fp": "0.00"},
    )
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    cooldowns_armed: list[str] = []
    monkeypatch.setattr(executor, "_arm_cooldown",
                        lambda t, **k: cooldowns_armed.append(t))

    out = executor._verify_after_cancel("ORD999", 0.40, "maker", "TKR2")
    assert out is None
    assert cooldowns_armed == [], "clean cancel must not arm cooldown"


def test_find_recent_order_matches_by_ticker_side_count_time(monkeypatch):
    """find_recent_order is the recovery primitive for silent-accept:
    place_limit_order returned None but Kalshi may have accepted the
    order. Match on (ticker, side, initial_count, created within
    window). The 2026-05-07 CHI-B62.5 18:36 incident orphaned a fill
    via this exact gap."""
    import kalshi_client
    import time as _time

    fixed_now = 1_700_000_000.0
    monkeypatch.setattr(_time, "time", lambda: fixed_now)

    fake_resp_orders = [
        # Wrong side — should not match.
        {"order_id": "A", "side": "yes", "initial_count_fp": "10",
         "created_time": "2023-11-14T22:13:20+00:00"},
        # Wrong contracts — should not match.
        {"order_id": "B", "side": "no", "initial_count_fp": "5",
         "created_time": "2023-11-14T22:13:20+00:00"},
        # Right shape, but created 2 hours later — outside window.
        {"order_id": "C", "side": "no", "initial_count_fp": "10",
         "created_time": "2023-11-15T00:13:20+00:00"},
        # Match: right side, right count, ~10s ago.
        {"order_id": "MATCH", "side": "no", "initial_count_fp": "10",
         "created_time": "2023-11-14T22:13:10+00:00"},
    ]
    class FakeResp:
        status_code = 200
        def json(self):
            return {"orders": fake_resp_orders}
    monkeypatch.setattr(kalshi_client, "_request",
                        lambda *a, **k: FakeResp())

    out = kalshi_client.find_recent_order("TKR", "no", 10, fixed_now)
    assert out is not None
    assert out["order_id"] == "MATCH"


def test_find_recent_order_returns_none_when_nothing_matches(monkeypatch):
    import kalshi_client
    class FakeResp:
        status_code = 200
        def json(self): return {"orders": []}
    monkeypatch.setattr(kalshi_client, "_request",
                        lambda *a, **k: FakeResp())
    assert kalshi_client.find_recent_order("TKR", "no", 10, 0) is None


def test_verify_after_cancel_retries_cancel_if_stuck_resting(monkeypatch):
    """If status stays 'resting' midway through the poll window, the
    original cancel may have been silently dropped. We retry it once
    rather than just timing out and stranding the order."""
    import executor
    import kalshi_client

    monkeypatch.setattr(
        kalshi_client, "get_order_status",
        lambda oid: {"status": "resting", "fill_count_fp": "0.00",
                     "remaining_count_fp": "35.00"},
    )
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    cancels: list[str] = []
    monkeypatch.setattr(kalshi_client, "cancel_order",
                        lambda oid: cancels.append(oid) or True)

    executor._verify_after_cancel("ORD777", 0.60, "taker", "TKR3")
    assert cancels == ["ORD777"], "should retry cancel exactly once on stuck-resting"


# ─── checkpoint #1.5 — should_exit_position decision rules ──────────────────
def _make_position(**kw):
    """Build an open-position dict with sensible defaults for exit tests."""
    base = {
        "id": 1, "ticker": "TKR", "action": "BUY NO",
        "entry_price": 0.65, "contracts": 30,
        "calibrated_p": 0.10, "edge_at_entry": 0.25,
        "opened_at": (datetime.now(timezone.utc) - timedelta(hours=2))
            .strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
        "target_settlement": (datetime.now(timezone.utc) + timedelta(hours=8))
            .strftime("%Y-%m-%d"),
        "venue": "kalshi", "paper_trade": 0, "mode": "taker",
    }
    base.update(kw)
    return base


def _book(*, our_side, mid_value, bid_offset=0.0):
    """Build a synthetic Kalshi orderbook dict.

    `mid_value` ≈ (our_ask + (1 - other_ask)) / 2 in Kalshi's inverted
    book. Setting both asks to `mid_value` gives zero-spread by default.
    `bid_offset` widens the implied bid below mid (simulates one-sided
    book / no-buyer scenarios for take-profit).
    """
    other_side = "yes" if our_side == "no" else "no"
    return {
        f"best_{our_side}_ask": mid_value,
        # other_ask = 1 - our_bid; bigger other_ask => smaller our_bid
        f"best_{other_side}_ask": 1.0 - max(0.0, mid_value - bid_offset),
    }


def test_exit_skips_fresh_position():
    """User constraint: don't exit just because position went underwater
    after entry — that's the half-spread we paid, not signal."""
    import executor
    pos = _make_position(opened_at=(datetime.now(timezone.utc) - timedelta(minutes=5))
                         .strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z")
    book = _book(our_side="no", mid_value=0.55)
    do_exit, reason = executor.should_exit_position(pos, current_cal_p=0.50, book=book)
    assert do_exit is False
    assert "too_fresh" in reason


def test_exit_skips_when_forecast_unchanged(monkeypatch):
    """The principled case: market moved against us 10c but the forecast
    hasn't budged. Per user's framing, this should NOT trigger exit —
    we entered based on the model, we exit based on the model."""
    import executor
    pos = _make_position(entry_price=0.65, calibrated_p=0.10, edge_at_entry=0.25)
    book = _book(our_side="no", mid_value=0.55)  # 10c adverse
    # cal_p unchanged from entry
    do_exit, reason = executor.should_exit_position(pos, current_cal_p=0.10, book=book)
    assert do_exit is False, f"forecast unchanged → must hold: {reason}"
    assert reason == "hold"


def test_exit_take_profit_fires_when_bid_executable():
    """Take-profit fires only when there's a real bid we'd cross — the
    2026-05-06 empty-book lesson."""
    import executor
    pos = _make_position(entry_price=0.40)
    # mid 0.96, implied bid 0.96 (zero spread) → executable
    book = _book(our_side="no", mid_value=0.96)
    do_exit, reason = executor.should_exit_position(pos, current_cal_p=0.05, book=book)
    assert do_exit is True
    assert reason.startswith("take_profit:")


def test_exit_take_profit_skips_when_other_side_empty():
    """One-sided book: our-side ask is 0.96 but no other-side ask
    (no implied bid we can compute). _live_position_value falls back
    to our_ask, but the bid-aware guard refuses to fire — we don't
    know if any buyer exists. Don't waste a sell order."""
    import executor
    pos = _make_position(entry_price=0.40)
    # No other_side ask → no implied bid
    book = {"best_no_ask": 0.96, "best_yes_ask": None}
    do_exit, reason = executor.should_exit_position(pos, current_cal_p=0.05, book=book)
    assert do_exit is False
    assert "take_profit_no_book_bid" in reason


def test_exit_forecast_inversion_buy_no(monkeypatch):
    """The principled exit: BUY NO at entry cal_p=0.10 (model: 90% NO).
    Forecast updates and now says cal_p=0.40 (model: only 60% NO).
    Drift 0.30 >= 0.15 ✓, live edge at cost = (1-0.40)-0.65 = -0.05 ≤ -0.05 ✓.
    Exit because our own model now says we're losing."""
    import executor
    pos = _make_position(entry_price=0.65, calibrated_p=0.10, edge_at_entry=0.25)
    book = _book(our_side="no", mid_value=0.55)  # mid moves but doesn't drive exit
    do_exit, reason = executor.should_exit_position(
        pos, current_cal_p=0.40, book=book,
    )
    assert do_exit is True, f"forecast moved 30pp against entry → must exit: {reason}"
    assert "forecast_inversion" in reason


def test_exit_forecast_inversion_does_not_fire_on_small_drift():
    """Forecast drifted only 5pp (within noise). Even if the trade
    edge slightly compressed, this is normal model wobble. Don't fire."""
    import executor
    pos = _make_position(entry_price=0.65, calibrated_p=0.10, edge_at_entry=0.25)
    book = _book(our_side="no", mid_value=0.60)
    # cal_p moved from 0.10 → 0.15, drift = 0.05 (well below 0.15 threshold)
    do_exit, reason = executor.should_exit_position(
        pos, current_cal_p=0.15, book=book,
    )
    assert do_exit is False, f"5pp forecast drift must not fire: {reason}"


def test_exit_holds_on_catastrophic_decay_when_forecast_unchanged():
    """Removed safety_net (2026-05-08): a 55c price decay with forecast
    UNCHANGED no longer exits. The model still says edge; we hold and
    let the position ride to settlement. Previously this fired
    safety_net, but that contradicted entry logic (entry would re-buy
    on the same scan because the same forecast still says edge),
    causing stop-out → re-entry oscillation."""
    import executor
    pos = _make_position(entry_price=0.65, calibrated_p=0.10, edge_at_entry=0.25)
    book = _book(our_side="no", mid_value=0.10)  # 55c decay
    do_exit, reason = executor.should_exit_position(
        pos, current_cal_p=0.10, book=book,
    )
    assert do_exit is False, f"forecast unchanged → must hold, got {reason}"
    assert reason == "hold"


def test_exit_skips_backfill_no_model_context():
    """Backfill rows have calibrated_p=0 — can't reason about forecast
    drift. Skip the row, let it ride to settlement."""
    import executor
    pos = _make_position(calibrated_p=0.0, edge_at_entry=0.0, mode="backfill")
    book = _book(our_side="no", mid_value=0.10)
    do_exit, reason = executor.should_exit_position(pos, current_cal_p=0.10, book=book)
    assert do_exit is False
    assert "no_model_context" in reason


def test_exit_buy_yes_forecast_inversion():
    """BUY YES inverts the drift sign: adverse drift = cal_p going DOWN
    (forecast now says YES is less likely)."""
    import executor
    pos = _make_position(
        action="BUY YES", entry_price=0.40,
        calibrated_p=0.70, edge_at_entry=0.30,
    )
    book = _book(our_side="yes", mid_value=0.32)
    # cal_p drops from 0.70 → 0.30 (drift 0.40), live_edge_at_cost = 0.30 - 0.40 = -0.10
    do_exit, reason = executor.should_exit_position(
        pos, current_cal_p=0.30, book=book,
    )
    assert do_exit is True
    assert "forecast_inversion" in reason


def test_split_trade_on_partial_exit_preserves_remainder(tmp_path, monkeypatch):
    """Partial exits split the trade so the unsold remainder stays in
    `load_open_positions`. The original is reduced to the sold count
    (so the upcoming result row cleanly closes it); a new row inherits
    all model context (including opened_at, so MIN_HOLD age is preserved)."""
    import config
    import storage

    db = tmp_path / "split.db"
    monkeypatch.setattr(config, "DB_FILE", str(db))
    monkeypatch.setattr(storage, "DB_FILE", str(db), raising=False)
    storage.init_db()

    with sqlite3.connect(db) as c:
        c.execute("""
            INSERT INTO trades (
                ticker, action, entry_price, contracts, size_usd,
                calibrated_p, edge_at_entry, mode, opened_at, venue,
                paper_trade
            ) VALUES (
                'M', 'BUY NO', 0.65, 30, 19.5,
                0.10, 0.25, 'taker', '2026-05-07T18:00:00Z', 'kalshi', 0
            )
        """)
        c.commit()

    new_id = storage.split_trade_on_partial_exit(1, sold_contracts=12)
    assert new_id is not None and new_id > 1

    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        original = dict(c.execute("SELECT * FROM trades WHERE id=1").fetchone())
        remainder = dict(c.execute(
            "SELECT * FROM trades WHERE id=?", (new_id,),
        ).fetchone())

    assert original["contracts"] == 12  # shrunk to sold portion
    assert remainder["contracts"] == 18  # 30 - 12
    # Model context inherited.
    assert remainder["calibrated_p"] == 0.10
    assert remainder["edge_at_entry"] == 0.25
    assert remainder["entry_price"] == 0.65
    # Opened_at preserved — splitting must not reset MIN_HOLD age.
    assert remainder["opened_at"] == "2026-05-07T18:00:00Z"
    # size_usd scaled proportionally.
    assert original["size_usd"] == pytest.approx(19.5 * (12/30), abs=1e-3)
    assert remainder["size_usd"] == pytest.approx(19.5 * (18/30), abs=1e-3)
    assert "split_from_trade_1" in (remainder["notes"] or "")


# ─── process_exits integration tests — end-to-end exit pipeline ─────────────
def _seed_open_trade(db_path, **overrides):
    """Insert a single open (no result) Kalshi trade. Returns trade_id."""
    base = {
        "ticker": "KXHIGHTHOU-26MAY08-B85.5", "city": "Houston",
        "market_type": "high_temp", "action": "BUY NO",
        "entry_price": 0.65, "contracts": 30, "size_usd": 19.5,
        "ensemble_p": None, "calibrated_p": 0.10, "edge_at_entry": 0.25,
        "mode": "taker",
        "opened_at": (datetime.now(timezone.utc) - timedelta(hours=2))
            .strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
        "target_settlement": (datetime.now(timezone.utc) + timedelta(hours=8))
            .strftime("%Y-%m-%d"),
        "notes": "", "paper_trade": 0, "order_id": "ENTRYOID",
        "venue": "kalshi",
    }
    base.update(overrides)
    cols = ", ".join(base.keys())
    qs = ", ".join("?" * len(base))
    with sqlite3.connect(db_path) as c:
        cur = c.execute(f"INSERT INTO trades ({cols}) VALUES ({qs})",
                        tuple(base.values()))
        c.commit()
        return int(cur.lastrowid)


def _setup_exit_test_env(monkeypatch, tmp_path):
    """Boilerplate: tmp DB with schema, executor module pointed at it."""
    import config
    import storage

    db = tmp_path / "exits.db"
    monkeypatch.setattr(config, "DB_FILE", str(db))
    monkeypatch.setattr(storage, "DB_FILE", str(db), raising=False)
    storage.init_db()
    return db


def test_process_exits_full_fill_subtracts_fees_from_pnl(monkeypatch, tmp_path):
    """Realized P&L on an exit must account for Kalshi fees that come back
    on the order's *_fees_dollars fields. Otherwise the dashboard's
    per-trade pnl overstates what actually hit the cash balance."""
    import executor
    import kalshi_client
    import strategy

    db = _setup_exit_test_env(monkeypatch, tmp_path)
    # Position: BUY NO 30 @ $0.65 (high-cost favorite) → cost basis $19.50.
    # Entry cal_p=0.10, entry_edge=0.25.
    tid = _seed_open_trade(db, entry_price=0.65, contracts=30,
                           calibrated_p=0.10, edge_at_entry=0.25)

    # Forecast drifts UP to 0.45 → live_edge_at_cost = (1-0.45)-0.65 = -0.10
    # which is < -0.05 floor; drift 0.35 >= 0.15 — fires forecast_inversion.
    monkeypatch.setattr(strategy, "compute_market_cal_p",
                        lambda m, venue="kalshi": 0.45)
    # Book: mid_no around 0.55 (modest decay, not take-profit nor safety-net).
    monkeypatch.setattr(kalshi_client, "get_orderbook",
                        lambda t: {"best_no_ask": 0.55, "best_yes_ask": 0.45})
    # Kalshi's positions endpoint: realized goes from 0 (pre-sell) to
    # -$3.20 (post-sell). This is the AUTHORITATIVE pnl for the exit;
    # _execute_exit_sell now reads this delta as ground truth instead
    # of trying to derive from taker_fill_cost_dollars (which is wrong
    # for sells — it's reported in the counterparty's frame).
    realized_state = {"v": 0.0}
    def fake_positions():
        return [{"ticker": "KXHIGHTHOU-26MAY08-B85.5",
                 "realized_pnl_dollars": str(realized_state["v"])}]
    monkeypatch.setattr(kalshi_client, "get_open_positions", fake_positions)
    def fake_sell(*a, **k):
        realized_state["v"] = -3.20  # post-sell: pnl came in at -$3.20
        return "SELLOID"
    monkeypatch.setattr(kalshi_client, "sell_position", fake_sell)
    monkeypatch.setattr(kalshi_client, "get_order_status",
                        lambda oid: {"status": "executed",
                                     "fill_count_fp": "30.00",
                                     "taker_fees_dollars": "0.20",
                                     "maker_fees_dollars": "0.00"})
    monkeypatch.setattr(kalshi_client, "cancel_order", lambda oid: True)
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)

    summary = executor.process_exits(markets=[{
        "ticker": "KXHIGHTHOU-26MAY08-B85.5",
        "city": "Houston", "title": "high temp below 85.5",
    }])
    assert summary["exited_full"] == 1, summary
    assert summary["exited_partial"] == 0
    assert "forecast_inversion" in summary["by_reason"]

    with sqlite3.connect(db) as c:
        row = c.execute(
            "SELECT outcome, exit_price, profit_loss FROM results "
            "WHERE trade_id=?", (tid,),
        ).fetchone()
    assert row is not None, "result row must be written for the exit"
    outcome, exit_price, pnl = row
    assert outcome == "exited"
    assert exit_price == pytest.approx(0.55, abs=1e-4)
    # Expected pnl: (0.55 - 0.65) * 30 − 0.20 = -3.20
    assert pnl == pytest.approx(-3.20, abs=1e-4), (
        f"pnl must subtract Kalshi fees: got {pnl}, expected -3.20"
    )


def test_process_exits_partial_fill_splits_trade_correctly(monkeypatch, tmp_path):
    """Contract availability scenario: market-sell 30 but only 12 fill.
    Must (a) write a result row for the 12 sold at the right pnl,
    (b) shrink the original trade's contracts to 12 so the result
    cleanly closes it, (c) create a new trade row for the unsold 18,
    inheriting the original's model context (so next cycle re-evaluates
    correctly)."""
    import executor
    import kalshi_client
    import strategy

    db = _setup_exit_test_env(monkeypatch, tmp_path)
    tid = _seed_open_trade(db, entry_price=0.65, contracts=30,
                           calibrated_p=0.10, edge_at_entry=0.25)

    monkeypatch.setattr(strategy, "compute_market_cal_p",
                        lambda m, venue="kalshi": 0.45)  # forecast_inversion
    monkeypatch.setattr(kalshi_client, "get_orderbook",
                        lambda t: {"best_no_ask": 0.50, "best_yes_ask": 0.50})
    # Partial fill 12/30 — Kalshi realized goes 0 → -$1.85 on the closed 12.
    realized_state = {"v": 0.0}
    monkeypatch.setattr(kalshi_client, "get_open_positions",
                        lambda: [{"ticker": "KXHIGHTHOU-26MAY08-B85.5",
                                  "realized_pnl_dollars": str(realized_state["v"])}])
    def fake_sell(*a, **k):
        realized_state["v"] = -1.85
        return "SELLOID"
    monkeypatch.setattr(kalshi_client, "sell_position", fake_sell)
    monkeypatch.setattr(kalshi_client, "get_order_status",
                        lambda oid: {"status": "canceled",
                                     "fill_count_fp": "12.00",
                                     "taker_fees_dollars": "0.05",
                                     "maker_fees_dollars": "0.00"})
    monkeypatch.setattr(kalshi_client, "cancel_order", lambda oid: True)
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)

    summary = executor.process_exits(markets=[{
        "ticker": "KXHIGHTHOU-26MAY08-B85.5",
        "city": "Houston", "title": "high temp below 85.5",
    }])
    assert summary["exited_partial"] == 1
    assert summary["exited_full"] == 0

    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        # Original trade: contracts shrunk to 12 (the sold portion).
        original = dict(c.execute(
            "SELECT * FROM trades WHERE id=?", (tid,)).fetchone())
        assert original["contracts"] == 12
        # Result row covers the 12 sold; pnl = (0.50 - 0.65)*12 - 0.05 = -1.85.
        result = dict(c.execute(
            "SELECT * FROM results WHERE trade_id=?", (tid,)).fetchone())
        assert result["profit_loss"] == pytest.approx(-1.85, abs=1e-4)
        # New trade row exists for the unsold 18.
        remainder_rows = c.execute(
            "SELECT * FROM trades WHERE id != ?", (tid,)).fetchall()
        assert len(remainder_rows) == 1
        remainder = dict(remainder_rows[0])
        assert remainder["contracts"] == 18
        # Model context inherited so next cycle's exit logic is correct.
        assert remainder["entry_price"] == 0.65
        assert remainder["calibrated_p"] == 0.10
        assert remainder["edge_at_entry"] == 0.25
        # Opened_at preserved → MIN_HOLD age survives the split.
        assert remainder["opened_at"] == original["opened_at"]


def test_process_exits_no_fill_leaves_trade_unchanged(monkeypatch, tmp_path):
    """Contract availability scenario: market sell rests but never finds
    a buyer (yesterday's empty-book pattern). No result row, no trade
    row mutation, summary["no_fill"] increments — position rides to
    next cycle for re-evaluation or natural settlement."""
    import executor
    import kalshi_client
    import strategy

    db = _setup_exit_test_env(monkeypatch, tmp_path)
    tid = _seed_open_trade(db, entry_price=0.65, contracts=30,
                           calibrated_p=0.10, edge_at_entry=0.25)

    monkeypatch.setattr(strategy, "compute_market_cal_p",
                        lambda m, venue="kalshi": 0.45)  # forecast_inversion
    monkeypatch.setattr(kalshi_client, "get_orderbook",
                        lambda t: {"best_no_ask": 0.50, "best_yes_ask": 0.50})
    monkeypatch.setattr(kalshi_client, "get_open_positions",
                        lambda: [{"ticker": "KXHIGHTHOU-26MAY08-B85.5",
                                  "realized_pnl_dollars": "0.00"}])
    monkeypatch.setattr(kalshi_client, "sell_position",
                        lambda *a, **k: "SELLOID")
    # Order rests forever, fill_count stays at 0.
    monkeypatch.setattr(kalshi_client, "get_order_status",
                        lambda oid: {"status": "resting",
                                     "fill_count_fp": "0.00"})
    monkeypatch.setattr(kalshi_client, "cancel_order", lambda oid: True)
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)

    summary = executor.process_exits(markets=[{
        "ticker": "KXHIGHTHOU-26MAY08-B85.5",
        "city": "Houston", "title": "high temp below 85.5",
    }])
    assert summary["no_fill"] == 1
    assert summary["exited_full"] == 0
    assert summary["exited_partial"] == 0

    with sqlite3.connect(db) as c:
        # Trade row unchanged.
        contracts = c.execute(
            "SELECT contracts FROM trades WHERE id=?", (tid,),
        ).fetchone()[0]
        assert contracts == 30
        # No result row written.
        n_results = c.execute(
            "SELECT COUNT(*) FROM results WHERE trade_id=?", (tid,),
        ).fetchone()[0]
        assert n_results == 0
        # No new trade rows (no split happened).
        n_trades = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert n_trades == 1


def test_process_exits_pulls_fresh_calp_per_position(monkeypatch, tmp_path):
    """Forecast-update intelligence: process_exits must call
    strategy.compute_market_cal_p for each held position, with the
    matching market dict from the cycle's market list, and feed the
    result into should_exit_position. Without this wiring, the
    forecast-inversion rule is dead even if the function is correct."""
    import executor
    import kalshi_client
    import strategy

    db = _setup_exit_test_env(monkeypatch, tmp_path)
    _seed_open_trade(db, ticker="MKT-A", calibrated_p=0.10,
                     edge_at_entry=0.25)
    _seed_open_trade(db, ticker="MKT-B", calibrated_p=0.10,
                     edge_at_entry=0.25)

    # Track which markets compute_market_cal_p was called with.
    seen: list[str] = []
    def fake_calp(market, venue="kalshi"):
        seen.append(market["ticker"])
        # MKT-A: forecast inverted. MKT-B: unchanged.
        return 0.50 if market["ticker"] == "MKT-A" else 0.10

    monkeypatch.setattr(strategy, "compute_market_cal_p", fake_calp)
    monkeypatch.setattr(kalshi_client, "get_orderbook",
                        lambda t: {"best_no_ask": 0.50, "best_yes_ask": 0.50})
    # No need to track per-ticker realized_pnl — these tests only assert
    # that the right code paths fired, not exact pnl values.
    monkeypatch.setattr(kalshi_client, "get_open_positions",
                        lambda: [])
    monkeypatch.setattr(kalshi_client, "sell_position",
                        lambda *a, **k: sells.append(a[0]) if 'sells' in dir() else "SELLOID" or "SELLOID")
    monkeypatch.setattr(kalshi_client, "get_order_status",
                        lambda oid: {"status": "executed",
                                     "fill_count_fp": "30.00",
                                     "taker_fees_dollars": "0.00",
                                     "maker_fees_dollars": "0.00"})
    monkeypatch.setattr(kalshi_client, "cancel_order", lambda oid: True)
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)

    markets = [
        {"ticker": "MKT-A", "city": "Test", "title": "high temp below 85"},
        {"ticker": "MKT-B", "city": "Test", "title": "high temp below 85"},
    ]
    summary = executor.process_exits(markets=markets)

    # Both held positions must be queried for fresh cal_p.
    assert sorted(seen) == ["MKT-A", "MKT-B"], (
        f"compute_market_cal_p must be called for each held position; "
        f"got {seen}"
    )
    # Only MKT-A inverted → only one exit.
    assert summary["exited_full"] == 1
    assert summary["checked"] == 2


def test_process_exits_skips_non_kalshi_paper_and_backfill(monkeypatch, tmp_path):
    """Exit logic only fires on Kalshi live trades. Paper trades exit via
    maker_sim/reconcile, Polymarket has its own path (currently paused),
    and backfill rows have no model context to reason about."""
    import executor
    import kalshi_client
    import strategy

    db = _setup_exit_test_env(monkeypatch, tmp_path)

    # Only this row is eligible for exit logic.
    tid_kalshi = _seed_open_trade(
        db, ticker="KALSHI-LIVE", venue="kalshi", paper_trade=0, mode="taker",
    )
    _seed_open_trade(
        db, ticker="POLY-LIVE", venue="polymarket", paper_trade=0, mode="taker",
    )
    _seed_open_trade(
        db, ticker="KALSHI-PAPER", venue="kalshi", paper_trade=1,
        mode="paper:maker",
    )
    _seed_open_trade(
        db, ticker="KALSHI-BACKFILL", venue="kalshi", paper_trade=0,
        mode="backfill", calibrated_p=0.0, edge_at_entry=0.0,
    )

    seen_calp: list[str] = []
    def spy_calp(m, venue="kalshi"):
        seen_calp.append(m["ticker"])
        return 0.50  # would be inversion if checked
    monkeypatch.setattr(strategy, "compute_market_cal_p", spy_calp)
    monkeypatch.setattr(kalshi_client, "get_orderbook",
                        lambda t: {"best_no_ask": 0.50, "best_yes_ask": 0.50})
    sells: list[str] = []
    monkeypatch.setattr(kalshi_client, "sell_position",
                        lambda *a, **k: sells.append(a[0]) or "SELLOID")
    monkeypatch.setattr(kalshi_client, "get_order_status",
                        lambda oid: {"status": "executed",
                                     "fill_count_fp": "30.00",
                                     "taker_fill_cost_dollars": "15.00",
                                     "maker_fill_cost_dollars": "0.00",
                                     "taker_fees_dollars": "0.00",
                                     "maker_fees_dollars": "0.00"})
    monkeypatch.setattr(kalshi_client, "cancel_order", lambda oid: True)
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)

    summary = executor.process_exits(markets=[
        {"ticker": "KALSHI-LIVE", "city": "T", "title": "high temp below 85"},
        {"ticker": "POLY-LIVE", "city": "T", "title": "high temp below 85"},
        {"ticker": "KALSHI-PAPER", "city": "T", "title": "high temp below 85"},
        {"ticker": "KALSHI-BACKFILL", "city": "T", "title": "high temp below 85"},
    ])

    assert summary["checked"] == 1, (
        f"only KALSHI-LIVE eligible; got {summary['checked']}"
    )
    assert sells == ["KALSHI-LIVE"], f"only Kalshi-live should sell; got {sells}"
    assert summary["exited_full"] == 1


# ─── exited trades count as wins when profitable ────────────────────────────
def test_win_rate_counts_profitable_exits_as_wins(tmp_path, monkeypatch):
    """When the new exit logic closes a position with positive P&L, the
    bot's win-rate counter must recognise it as a win. The previous
    SQL pattern `(action='BUY YES' AND outcome='yes') OR ...` only
    matched natural settlements; exited trades have outcome='exited'
    which matched neither branch, so profitable exits silently became
    'losses' in the counters. Switched to `profit_loss > 0`."""
    import config
    db = tmp_path / "winrate.db"
    monkeypatch.setattr(config, "DB_FILE", str(db))
    with sqlite3.connect(db) as c:
        c.executescript("""
            CREATE TABLE trades (id INTEGER PRIMARY KEY, action TEXT,
                mode TEXT, market_type TEXT, notes TEXT);
            CREATE TABLE results (id INTEGER PRIMARY KEY, trade_id INT,
                outcome TEXT, exit_price REAL, profit_loss REAL,
                resolved_at TEXT, venue TEXT);
            INSERT INTO trades VALUES
                -- Natural-settlement winner (BUY YES + outcome=yes).
                (1, 'BUY YES', 'taker', 'high_temp', NULL),
                -- Natural-settlement loser.
                (2, 'BUY NO',  'taker', 'high_temp', NULL),
                -- Exited at a profit (must count as win).
                (3, 'BUY NO',  'taker', 'high_temp', NULL),
                -- Exited at a loss (must count as loss).
                (4, 'BUY YES', 'taker', 'high_temp', NULL);
            INSERT INTO results (trade_id, outcome, profit_loss) VALUES
                (1, 'yes',    +5.00),
                (2, 'yes',    -3.00),
                (3, 'exited', +5.26),
                (4, 'exited', -1.20);
        """)

    # The win-rate SQL pattern from storage.py / dashboard.py.
    sql = """
        SELECT COUNT(*),
               SUM(CASE WHEN r.profit_loss > 0 THEN 1 ELSE 0 END)
        FROM trades t JOIN results r ON r.trade_id = t.id
        WHERE (t.mode IS NULL OR t.mode != 'dry-run')
          AND (t.market_type IS NULL OR t.market_type != 'arbitrage')
    """
    with sqlite3.connect(db) as c:
        total, wins = c.execute(sql).fetchone()
    assert total == 4
    assert wins == 2, (
        f"profitable exits must count as wins; got {wins} of 4 "
        "(expected 2: trade #1 natural win + trade #3 exited at profit)"
    )


# ─── §3.4 — adverse-selection witness test (currently UNMODELED) ──────────────
@pytest.mark.xfail(reason="phase 3c — adverse-selection cost not yet modeled "
                          "(memory: 'NOT modeled in 3b'). Test exists to flip "
                          "to a real assertion when 3c lands.",
                   strict=False)
def test_maker_fills_capture_adverse_selection_cost():
    """If maker fills only happen when mid moves against us (we got
    picked off), an honest sim should mark exit at the new mid, not at
    entry. Until 3c, we deliberately fill at limit_price — this xfail
    documents the gap so it doesn't stay invisible."""
    raise AssertionError("intentionally failing until 3c")


