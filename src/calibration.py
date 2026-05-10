"""
calibration.py — isotonic regression from v1's resolved-trade history.

v1's model was systematically overconfident by ~20 percentage points (audit
M11). v2 fits a one-shot isotonic regression from every resolved live trade
in v1's trades.db, pickles it, and applies it statically before every edge
calculation.

Addresses audit items:
  M8 — shrinkage factor multiplied into Kelly size
  M11 — static isotonic calibration (no online drift in v2)
  E9 — strict paper_trade=0 filter (never mix paper into calibration)
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import sqlite3
from typing import Any

import numpy as np

from config import CALIBRATION_META, CALIBRATION_PKL
from storage import NOTES_VALID_SQL

# Lazy-loaded artifacts, keyed by venue. Phase 2: separate isotonic transform
# per venue. v1's pickle ships at data/calibration.pkl (the original path);
# Polymarket's lives at data/calibration_polymarket.pkl. A venue with no
# pickle yet uses identity (raw probability passes through).
#
# Cross-venue calibration is mathematically wrong: Kalshi and Polymarket have
# different microstructure, oracle, and price formation. Applying Kalshi's
# isotonic transform to Polymarket prices is the v1 §3.1 "score yourself
# against your own input" mistake in a new outfit (see project memory).
_MODELS: dict[str, Any] = {}
_METAS: dict[str, dict[str, Any] | None] = {}


def _venue_pickle_path(venue: str) -> str:
    """Backward-compatible: 'kalshi' uses the existing CALIBRATION_PKL path
    so v1's bootstrapped pickle is still picked up."""
    if venue == "kalshi":
        return CALIBRATION_PKL
    base, ext = os.path.splitext(CALIBRATION_PKL)
    return f"{base}_{venue}{ext}"


def _venue_meta_path(venue: str) -> str:
    if venue == "kalshi":
        return CALIBRATION_META
    base, ext = os.path.splitext(CALIBRATION_META)
    return f"{base.replace('.meta','')}_{venue}.meta{ext}"


def _reliability_calibration_error(probs: np.ndarray, outcomes: np.ndarray,
                                   n_buckets: int = 10) -> float:
    """Mean absolute predicted-vs-actual across probability buckets."""
    if len(probs) == 0:
        return 0.0
    edges = np.linspace(0.0, 1.0, n_buckets + 1)
    errors = []
    weights = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (probs >= lo) & (probs < hi) if hi < 1.0 else (probs >= lo) & (probs <= hi)
        if mask.sum() == 0:
            continue
        pred = probs[mask].mean()
        actual = outcomes[mask].mean()
        errors.append(abs(pred - actual))
        weights.append(int(mask.sum()))
    if not errors:
        return 0.0
    return float(np.average(errors, weights=weights))


def _brier(probs: np.ndarray, outcomes: np.ndarray) -> float:
    if len(probs) == 0:
        return 0.0
    return float(np.mean((probs - outcomes) ** 2))


def fit_from_v1_history(v1_db_path: str, output_pkl_path: str) -> dict:
    """Fit isotonic regression from v1's resolved live-trade outcomes.

    Reads trades JOIN results, filters paper_trade=0 and excludes invalid/void/ghost
    notes, extracts (predicted_p_win, actual_win) pairs, fits
    IsotonicRegression(out_of_bounds='clip'), pickles the model, writes a
    sidecar meta json with stats.

    Returns summary dict with n_samples, brier_before, brier_after,
    calibration_error_factor (= 1 - mean abs error), and shrinkage_factor
    (= calibration_error_factor, floored at 0.3 for prudence).
    """
    from sklearn.isotonic import IsotonicRegression  # lazy

    if not os.path.exists(v1_db_path):
        raise FileNotFoundError(f"v1 db not found: {v1_db_path}")

    conn = sqlite3.connect(v1_db_path, timeout=10)
    try:
        rows = conn.execute(f"""
            SELECT t.action, t.our_probability, r.outcome
            FROM trades t JOIN results r ON t.id = r.trade_id
            WHERE t.paper_trade = 0
              AND t.our_probability IS NOT NULL
              AND r.outcome IS NOT NULL
              AND {NOTES_VALID_SQL}
        """).fetchall()
    finally:
        conn.close()

    # For each trade, compute the probability our side wins:
    #   BUY YES -> p_win = our_probability; win iff outcome=='yes'
    #   BUY NO  -> p_win = 1 - our_probability; win iff outcome=='no'
    preds: list[float] = []
    wins: list[int] = []
    for action, our_p, outcome in rows:
        if our_p is None or outcome is None:
            continue
        our_p = float(our_p)
        outcome = str(outcome).lower()
        if action == "BUY NO":
            p_win = 1.0 - our_p
            won = 1 if outcome == "no" else 0
        else:
            p_win = our_p
            won = 1 if outcome == "yes" else 0
        if not (0.0 <= p_win <= 1.0):
            continue
        preds.append(p_win)
        wins.append(won)

    n = len(preds)
    if n < 10:
        raise ValueError(f"Not enough resolved v1 trades for calibration: n={n}")

    preds_arr = np.array(preds, dtype=float)
    wins_arr = np.array(wins, dtype=float)

    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(preds_arr, wins_arr)
    fitted = iso.predict(preds_arr)

    brier_before = _brier(preds_arr, wins_arr)
    brier_after = _brier(fitted, wins_arr)
    cal_err = _reliability_calibration_error(preds_arr, wins_arr)
    cal_err_factor = max(0.0, 1.0 - cal_err)
    # Shrinkage — never larger than the calibration-error factor, never below 0.3.
    # Audit says don't be generous: floor at 0.3 but also cap at 0.7 even if
    # cal_err_factor is higher, until v2 has its own track record.
    shrinkage_factor = max(0.3, min(0.7, cal_err_factor))

    os.makedirs(os.path.dirname(output_pkl_path) or ".", exist_ok=True)
    with open(output_pkl_path, "wb") as f:
        pickle.dump(iso, f)

    meta = {
        "n_samples": int(n),
        "brier_before": round(brier_before, 4),
        "brier_after": round(brier_after, 4),
        "calibration_error": round(cal_err, 4),
        "calibration_error_factor": round(cal_err_factor, 4),
        "shrinkage_factor": round(shrinkage_factor, 4),
        "source": v1_db_path,
    }
    meta_path = os.path.splitext(output_pkl_path)[0] + ".meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    # Warm the in-process cache (kalshi only — fit_from_v1_history is
    # Kalshi-specific by definition; v1's trades.db has no Polymarket data).
    _MODELS["kalshi"] = iso
    _METAS["kalshi"] = meta

    return meta


def _load_model(venue: str = "kalshi") -> Any:
    if venue in _MODELS:
        return _MODELS[venue]
    path = _venue_pickle_path(venue)
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                _MODELS[venue] = pickle.load(f)
        except Exception as e:
            logging.warning("[CALIB] Failed to load %s: %s", path, e)
            _MODELS[venue] = None
    else:
        _MODELS[venue] = None
    return _MODELS[venue]


def _load_meta(venue: str = "kalshi") -> dict[str, Any] | None:
    if venue in _METAS:
        return _METAS[venue]
    path = _venue_meta_path(venue)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                _METAS[venue] = json.load(f)
        except Exception:
            _METAS[venue] = None
    else:
        _METAS[venue] = None
    return _METAS[venue]


def calibrate(p: float, venue: str = "kalshi") -> float:
    """Map a raw model probability to the calibrated probability for `venue`.

    Currently identity for both venues. The fitted Kalshi artifact in
    data/calibration.pkl was trained on 26 polymarket-bot samples and
    collapses raw_p<~0.85 to a near-constant 0.10, which mechanically forced
    BUY NO on every Kalshi range-bin market regardless of forecast. Until
    v2 has enough resolved trades per venue for a non-degenerate refit,
    calibration is performed manually (offline review of edge buckets,
    scatterplots, win rates).

    For Polymarket: identity until phase-3 paper sim accumulates resolved
    trades. CRITICAL — never apply Kalshi's transform to Polymarket prices
    (different price formation, different oracle, different microstructure).
    """
    # venue parameter accepted but not yet routed through the pickle — the
    # fitted Kalshi pickle is degenerate (above) and we have no Polymarket
    # pickle. Both intentionally identity. The signature is stable so callers
    # can already pass venue=...  and we'll wire the per-venue model in
    # without another API change once we have honest pickles.
    _ = venue
    return max(0.0, min(1.0, float(p)))


def shrinkage_factor(venue: str = "kalshi") -> float:
    """Kelly-sizing shrinkage multiplier for `venue`.

    Default is 1.0 (no shrinkage). Audit C1 option A: previously defaulted
    to 0.7 ("audit: don't be generous"), but `calibrate()` is currently
    identity (see calibration.py:214) — so we have no calibration curve
    whose measured error justifies the shrinkage. Combining identity
    calibration with 0.7 shrinkage was sizing for an overconfidence we
    have no evidence of in v2's universe; the conservative 5% per-bet
    cap (MAX_SINGLE_BET_PCT) binds first anyway, so this is mostly
    cosmetic, but the code-truth alignment matters.

    When isotonic is restored and a venue-specific meta exists, this
    function reads the measured shrinkage from there. The hard floor /
    ceiling [0.3, 0.7] from fit_from_v1_history still applies at fit-time.
    """
    meta = _load_meta(venue)
    if meta is None:
        return 1.0
    try:
        return float(meta.get("shrinkage_factor", 1.0))
    except Exception:
        return 1.0
