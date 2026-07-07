"""
fit_calibration_from_shadow.py — build a real probability calibrator from the
prod_observer shadow_signal table + actual Kalshi settlements.

Pipeline:
  1. Pull distinct shadow signals (one representative point per ticker).
  2. Fetch each ticker's settled outcome from Kalshi (status finalized/settled
     + result yes/no). Cached to data/_calibration_settlements.json so re-runs
     are fast and don't re-hit the API.
  3. Build (model_p, realized_yes) dataset.
  4. Fit isotonic + Platt (logistic) calibrators with k-fold CV.
  5. Report out-of-fold Brier / log-loss vs the identity (current) baseline.
  6. If a calibrator meaningfully beats identity, write data/calibration.pkl
     + data/calibration.meta.json (only when --ship is passed).

Run:
  python3.11 scripts/fit_calibration_from_shadow.py            # analyze only
  python3.11 scripts/fit_calibration_from_shadow.py --ship     # also write pkl
  python3.11 scripts/fit_calibration_from_shadow.py --point latest|earliest|all
"""
from __future__ import annotations

import json
import os
import pickle
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import numpy as np  # noqa: E402

OBS_DB = os.path.join(os.path.dirname(__file__), "..", "data", "prod_observer.db")
CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "_calibration_settlements.json")
PKL_OUT = os.path.join(os.path.dirname(__file__), "..", "data", "calibration.pkl")
META_OUT = os.path.join(os.path.dirname(__file__), "..", "data", "calibration.meta.json")


def load_shadow_points(point: str = "latest") -> dict[str, list[tuple[float, float]]]:
    """Return {ticker: [(ts, model_p), ...]} for tickers with a model_p."""
    import sqlite3
    con = sqlite3.connect(OBS_DB)
    rows = con.execute(
        "SELECT ticker, ts, calibrated_p FROM shadow_signal "
        "WHERE calibrated_p IS NOT NULL"
    ).fetchall()
    by_ticker: dict[str, list[tuple[float, float]]] = {}
    for tk, ts, p in rows:
        by_ticker.setdefault(tk, []).append((float(ts), float(p)))
    return by_ticker


def select_points(by_ticker, point: str) -> list[tuple[str, float]]:
    """Reduce to (ticker, model_p) training rows per the selection policy."""
    out = []
    for tk, pts in by_ticker.items():
        pts.sort()
        if point == "latest":
            out.append((tk, pts[-1][1]))           # closest to settlement
        elif point == "earliest":
            out.append((tk, pts[0][1]))            # max lead time
        elif point == "all":
            # one point per distinct model_p (rounded 0.01) to kill snapshot spam
            seen = set()
            for _ts, p in pts:
                key = round(p, 2)
                if key not in seen:
                    seen.add(key)
                    out.append((tk, p))
        else:
            raise ValueError(point)
    return out


def fetch_settlements(tickers: list[str]) -> dict[str, str]:
    """Return {ticker: 'yes'|'no'}, fetching from Kalshi with disk cache."""
    cache: dict[str, str] = {}
    if os.path.exists(CACHE):
        with open(CACHE) as f:
            cache = json.load(f)

    import kalshi_client
    todo = [t for t in set(tickers) if t not in cache]
    print(f"  settlements cached: {len(cache)}, to fetch: {len(todo)}")
    for i, tk in enumerate(todo, 1):
        try:
            m = kalshi_client.get_market(tk)
            if m and (m.get("status") or "").lower() in ("finalized", "settled"):
                res = (m.get("result") or "").lower()
                if res in ("yes", "no"):
                    cache[tk] = res
                else:
                    cache[tk] = "_ambiguous"
            else:
                cache[tk] = "_unsettled"
        except Exception as e:
            cache[tk] = "_error"
            print(f"    {tk}: {e}")
        if i % 100 == 0:
            print(f"    fetched {i}/{len(todo)} ...")
            with open(CACHE, "w") as f:
                json.dump(cache, f)
        time.sleep(0.08)
    with open(CACHE, "w") as f:
        json.dump(cache, f)
    return cache


def brier(p, y):
    p = np.asarray(p); y = np.asarray(y)
    return float(np.mean((p - y) ** 2))


def logloss(p, y, eps=1e-6):
    p = np.clip(np.asarray(p), eps, 1 - eps); y = np.asarray(y)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def main():
    point = "latest"
    ship = "--ship" in sys.argv
    if "--point" in sys.argv:
        point = sys.argv[sys.argv.index("--point") + 1]

    print(f"=== Calibration fit (point policy: {point}, ship: {ship}) ===\n")
    by_ticker = load_shadow_points()
    print(f"Tickers with model_p in shadow_signal: {len(by_ticker)}")

    rows = select_points(by_ticker, point)
    tickers = [r[0] for r in rows]
    settle = fetch_settlements(tickers)

    # Build labeled dataset
    X, Y, kept_tickers = [], [], []
    drop = {"_unsettled": 0, "_ambiguous": 0, "_error": 0}
    for tk, p in rows:
        res = settle.get(tk, "_unsettled")
        if res in drop:
            drop[res] += 1
            continue
        X.append(p)
        Y.append(1.0 if res == "yes" else 0.0)
        kept_tickers.append(tk)
    X = np.asarray(X); Y = np.asarray(Y)
    print(f"\nLabeled training rows: {len(X)}  (dropped {drop})")
    print(f"Base rate P(yes): {Y.mean():.3f}")

    if len(X) < 50:
        print("Too few labeled points — aborting.")
        return

    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import KFold

    # ── Cross-validated out-of-fold predictions ──
    kf = KFold(n_splits=5, shuffle=True, random_state=42)
    oof_iso = np.zeros_like(X)
    oof_platt = np.zeros_like(X)
    for tr, te in kf.split(X):
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso.fit(X[tr], Y[tr])
        oof_iso[te] = iso.predict(X[te])

        lr = LogisticRegression()
        lr.fit(X[tr].reshape(-1, 1), Y[tr])
        oof_platt[te] = lr.predict_proba(X[te].reshape(-1, 1))[:, 1]

    print("\n=== Out-of-fold performance (5-fold CV) ===")
    print(f"{'model':<22} {'Brier':>8} {'LogLoss':>8}")
    print(f"{'identity (current)':<22} {brier(X, Y):>8.4f} {logloss(X, Y):>8.4f}")
    print(f"{'isotonic':<22} {brier(oof_iso, Y):>8.4f} {logloss(oof_iso, Y):>8.4f}")
    print(f"{'platt (logistic)':<22} {brier(oof_platt, Y):>8.4f} {logloss(oof_platt, Y):>8.4f}")

    # Reliability table (identity) — where is the model mis-placed?
    print("\n=== Reliability (identity model_p vs actual P(yes)) ===")
    print(f"{'bucket':<12} {'n':>5} {'mean_p':>8} {'actual':>8} {'gap':>8}")
    edges = [0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.01]
    for lo, hi in zip(edges, edges[1:]):
        mask = (X >= lo) & (X < hi)
        if mask.sum() == 0:
            continue
        mp = X[mask].mean(); ac = Y[mask].mean()
        print(f"[{lo:.1f},{hi:.1f}){'':<3} {int(mask.sum()):>5} {mp:>8.3f} {ac:>8.3f} {ac-mp:>+8.3f}")

    base_brier = brier(X, Y)
    iso_brier = brier(oof_iso, Y)
    platt_brier = brier(oof_platt, Y)
    best = min(("isotonic", iso_brier), ("platt", platt_brier), key=lambda x: x[1])
    improvement = base_brier - best[1]
    print(f"\nBest calibrator: {best[0]} (Brier {best[1]:.4f}, "
          f"identity {base_brier:.4f}, improvement {improvement:+.4f})")

    if improvement <= 0.002:
        print("Improvement below 0.002 threshold — NOT worth shipping. Keeping identity.")
    elif not ship:
        print("Meaningful improvement. Re-run with --ship to write calibration.pkl.")
    else:
        # Fit final calibrator on ALL data
        if best[0] == "isotonic":
            final = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
            final.fit(X, Y)
        else:
            final = LogisticRegression()
            final.fit(X.reshape(-1, 1), Y)
        with open(PKL_OUT, "wb") as f:
            pickle.dump({"kind": best[0], "model": final}, f)
        meta = {
            "kind": best[0],
            "n_samples": int(len(X)),
            "point_policy": point,
            "brier_identity": round(base_brier, 4),
            "brier_calibrated_oof": round(best[1], 4),
            "improvement": round(improvement, 4),
            "base_rate": round(float(Y.mean()), 4),
            "fit_ts": time.time(),
            "source": "prod_observer.shadow_signal + Kalshi settlements",
        }
        with open(META_OUT, "w") as f:
            json.dump(meta, f, indent=2)
        print(f"\nSHIPPED → {PKL_OUT}\n{json.dumps(meta, indent=2)}")


if __name__ == "__main__":
    main()
