"""Critical-math tests for kalshi_bot_2.0.

Covers: Kelly sizing, fee deduction, ensemble fractions, isotonic calibration
round-trip, arb fee-inclusive edge, and a parameterized-SQL smoke test (audit B1).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ─── Kelly ────────────────────────────────────────────────────────────────────
def test_kelly_buy_yes_positive_edge(monkeypatch):
    import calibration
    import risk

    # Ensure shrinkage is a known constant.
    monkeypatch.setattr(calibration, "shrinkage_factor", lambda: 0.7)
    # No stored bankroll file — identity return.
    size = risk.kelly_size(0.70, 0.55, 100.0, action="BUY YES")
    assert 0.0 < size <= 5.0  # capped at 5% of $100


def test_kelly_buy_no_symmetry(monkeypatch):
    import calibration
    import risk

    monkeypatch.setattr(calibration, "shrinkage_factor", lambda: 0.7)
    size_yes = risk.kelly_size(0.70, 0.55, 100.0, action="BUY YES")
    size_no = risk.kelly_size(0.30, 0.45, 100.0, action="BUY NO")
    # Same economic bet from the NO side; sizes should match within 1c.
    assert abs(size_yes - size_no) < 0.02, f"yes={size_yes} no={size_no}"


def test_kelly_negative_edge_floor(monkeypatch):
    import calibration
    import risk

    monkeypatch.setattr(calibration, "shrinkage_factor", lambda: 0.7)
    size = risk.kelly_size(0.40, 0.60, 100.0, action="BUY YES")
    # Negative kelly should floor at MIN_POSITION.
    assert size == pytest.approx(1.0, abs=0.01)


# ─── Fees ─────────────────────────────────────────────────────────────────────
def test_fee_at_midprice():
    from config import kalshi_trade_fee

    fee = kalshi_trade_fee(1, 0.5)
    # rate=0.07, 0.5*0.5=0.25, 0.07*0.25=0.0175, ceil cent = 0.02
    assert fee == pytest.approx(0.02, abs=1e-6)


def test_fee_boundaries_zero():
    from config import kalshi_trade_fee

    assert kalshi_trade_fee(0, 0.5) == 0.0
    assert kalshi_trade_fee(1, 0.0) == 0.0
    assert kalshi_trade_fee(1, 1.0) == 0.0


def test_fee_reduces_edge():
    from config import kalshi_trade_fee

    gross_edge = 0.10
    entry_price = 0.40
    fee = kalshi_trade_fee(1, entry_price)
    net = gross_edge - fee / entry_price
    assert net < gross_edge
    assert net > 0  # still positive at 10% gross edge


# ─── Ensemble fractions ───────────────────────────────────────────────────────
# Probabilities are Laplace-smoothed: (k + α) / (n + 2α) with α=3, matching
# the cross-bin settlement noise floor measured over 779 production markets
# (forecast.py:43). Tests anchor against the smoothed value so a future
# alpha tweak surfaces as a single failure here, not a silent drift.
def test_probability_above_exact():
    from forecast import _LAPLACE_ALPHA, probability_above

    members = [70, 71, 72, 73, 74, 75, 76, 77, 78, 79] * 3 + [80]
    assert len(members) == 31
    # >= 75 means 75, 76, 77, 78, 79 (3 times each = 15) + 80 (once) = 16.
    expected = (16 + _LAPLACE_ALPHA) / (31 + 2 * _LAPLACE_ALPHA)
    assert probability_above(members, 75) == pytest.approx(expected, rel=1e-9)


def test_probability_below_complement():
    from forecast import probability_above, probability_below

    members = [float(v) for v in range(31)]  # 0..30
    # probability_above(10) counts 10..30 = 21; below(10) counts 0..9 = 10.
    # Laplace smoothing pulls each toward 0.5 by the same offset, so the sum
    # remains exactly 1.0 — the complement property is invariant.
    assert probability_above(members, 10) + probability_below(members, 10) == pytest.approx(1.0)


def test_probability_between_basic():
    from forecast import _LAPLACE_ALPHA, probability_between

    members = [float(v) for v in range(31)]
    # between(5, 10) counts 5,6,7,8,9,10 = 6
    expected = (6 + _LAPLACE_ALPHA) / (31 + 2 * _LAPLACE_ALPHA)
    assert probability_between(members, 5, 10) == pytest.approx(expected, rel=1e-9)


# ─── Isotonic round-trip ──────────────────────────────────────────────────────
def test_isotonic_monotone_fit(tmp_path, monkeypatch):
    import json

    import calibration

    # Build a synthetic v1-like db with a simple monotone-but-biased mapping.
    db = tmp_path / "v1.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT, our_probability REAL, paper_trade INTEGER, notes TEXT
        );
        CREATE TABLE results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER, outcome TEXT, profit_loss REAL, resolved_at TEXT
        );
        """
    )
    # Model says 0.7 but true win rate is 0.45 (classic overconfidence).
    for _ in range(100):
        cur = conn.execute(
            "INSERT INTO trades (action, our_probability, paper_trade, notes) VALUES (?, ?, 0, NULL)",
            ("BUY YES", 0.7),
        )
        conn.execute(
            "INSERT INTO results (trade_id, outcome, profit_loss, resolved_at) VALUES (?, ?, 0.0, ?)",
            (cur.lastrowid, "yes" if _ < 45 else "no", "2026-04-23"),
        )
    # Also add some lower-confidence observations so isotonic has range.
    for _ in range(50):
        cur = conn.execute(
            "INSERT INTO trades (action, our_probability, paper_trade, notes) VALUES (?, ?, 0, NULL)",
            ("BUY YES", 0.3),
        )
        conn.execute(
            "INSERT INTO results (trade_id, outcome, profit_loss, resolved_at) VALUES (?, ?, 0.0, ?)",
            (cur.lastrowid, "yes" if _ < 10 else "no", "2026-04-23"),
        )
    conn.commit()
    conn.close()

    out = tmp_path / "cal.pkl"
    stats = calibration.fit_from_v1_history(str(db), str(out))
    assert stats["n_samples"] == 150
    assert stats["brier_after"] <= stats["brier_before"] + 1e-9

    # The fit itself works — load it directly and verify monotonicity. The
    # higher-level `calibrate(p, venue=...)` is currently a deliberate
    # identity passthrough (see calibration.py:214 — the v1-trained pickle
    # collapsed everything to ~0.10, which was worse than no calibration).
    # Restoring the wired calibration is tracked as audit item C1; when
    # that lands, replace the assertions below with `calibration.calibrate(0.7) < 0.7`.
    import pickle
    with open(out, "rb") as f:
        iso = pickle.load(f)
    raw_adjusted = float(iso.predict([0.7])[0])
    # Overconfident 0.7 should get pulled down toward 0.45 by the fitted curve.
    assert raw_adjusted < 0.7
    assert 0.0 <= raw_adjusted <= 1.0

    # Document that the wired calibrate() is identity right now. If/when C1
    # is fixed, this assertion will start failing — that's the signal to
    # update this test to the line in the comment above.
    monkeypatch.setattr(calibration, "CALIBRATION_PKL", str(out))
    monkeypatch.setattr(calibration, "CALIBRATION_META", str(tmp_path / "cal.meta.json"))
    calibration._MODELS.clear()
    calibration._METAS.clear()
    assert calibration.calibrate(0.7) == pytest.approx(0.7), (
        "calibrate() is intentionally identity — see calibration.py:214 + audit C1"
    )


# ─── Arb fee-inclusive ────────────────────────────────────────────────────────
def test_arb_fees_inflate_sum():
    from config import kalshi_trade_fee

    legs = [
        {"yes_price": 0.30},
        {"yes_price": 0.35},
        {"yes_price": 0.28},
    ]
    n = 10
    sum_yes = sum(leg["yes_price"] for leg in legs)
    sum_with_fees = 0.0
    for leg in legs:
        fee = kalshi_trade_fee(n, leg["yes_price"])
        sum_with_fees += leg["yes_price"] + (fee / n)
    assert sum_with_fees > sum_yes
    # But shouldn't be more than a handful of cents above sum_yes.
    assert sum_with_fees - sum_yes < 0.10


# ─── SQL parameterization smoke test ──────────────────────────────────────────
def test_log_trade_handles_dict_notes(tmp_path, monkeypatch):
    import config as cfg
    import storage

    db = tmp_path / "trades.db"
    monkeypatch.setattr(cfg, "DB_FILE", str(db))
    monkeypatch.setattr(storage, "DB_FILE", str(db))
    storage.init_db()

    opp = {
        "ticker": "KX-T'\"; DROP TABLE trades; --",
        "city": "NYC",
        "market_type": "high_temp",
        "action": "BUY YES",
        "entry_price": 0.55,
        "recommended_size": 3.0,
        "raw_probability": 0.6,
        "calibrated_p": 0.52,
        "edge": 0.05,
        "target_settlement": "2026-04-24T00:00:00",
        "notes": "{\"a\":1,\"b\":2}",
    }
    fill = {"mode": "taker", "filled": True, "fill_price": 0.55, "fill_count": 5}
    # Should not raise and the row should exist.
    trade_id = storage.log_trade(opp, fill)
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute("SELECT COUNT(*) FROM trades").fetchone()
    assert row[0] == 1
    assert trade_id > 0
