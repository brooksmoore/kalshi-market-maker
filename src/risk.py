"""
risk.py — exposure cache, Kelly math, portfolio caps, fail-closed bankroll.

Addresses audit items:
  M8 — calibration shrinkage multiplied into Kelly size
  M9 — portfolio Kelly cap across all open positions
  M10 — per-settlement-cluster correlation cap
  R1 — atomic monotonic peak_pnl (never regresses)
  R5 — fail-closed on stale bankroll (age > BANKROLL_STALE_SECONDS)
  B8 — peak_pnl tempfile+rename writes, read before write
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta

from config import (
    BANKROLL_STALE_SECONDS,
    CLUSTER_WINDOW_HOURS,
    CORRELATION_BUCKET_CAP,
    DAILY_LOSS_LIMIT_PCT,
    DB_FILE,
    KELLY_FRACTION,
    MAX_DRAWDOWN_PCT,
    MAX_SINGLE_BET_PCT,
    MIN_POSITION,
    PERF_FILE,
    PORTFOLIO_KELLY_CAP,
    STARTING_BANKROLL,
)
from storage import (
    NON_DRYRUN_SQL,
    NOTES_VALID_SQL,
    NOTES_VALID_LIVE_SQL,
)

# ─── Exposure cache ──────────────────────────────────────────────────────────
# Source of truth: Kalshi /portfolio/positions. The local trades DB is consulted
# only to attribute each live position to its city/cluster bucket. This avoids
# false halts when settlement reconciliation fails to mark local rows closed.
_LOCAL_META_SQL = f"""
    SELECT t.ticker, t.city, t.market_type, t.target_settlement, t.id
    FROM trades t
    LEFT JOIN results r ON t.id = r.trade_id
    WHERE r.id IS NULL
      AND {NON_DRYRUN_SQL}
      AND {NOTES_VALID_LIVE_SQL}
"""

_exposure_cache: dict | None = None


def invalidate_exposure_cache() -> None:
    global _exposure_cache
    _exposure_cache = None


def _load_local_meta() -> dict:
    """ticker → {city, market_type, target_settlement, id} from open trades DB rows."""
    meta: dict[str, dict] = {}
    try:
        with sqlite3.connect(DB_FILE, timeout=5) as conn:
            rows = conn.execute(_LOCAL_META_SQL).fetchall()
        for ticker, city, mtype, target_settle, trade_id in rows:
            if ticker and ticker not in meta:
                meta[ticker] = {
                    "city": city,
                    "market_type": mtype,
                    "target_settlement": target_settle,
                    "id": trade_id,
                }
    except sqlite3.OperationalError:
        pass
    return meta


def _ensure_exposure_cache() -> None:
    global _exposure_cache
    if _exposure_cache is not None:
        return
    _exposure_cache = {
        "total": 0.0,
        "by_city": {},
        "by_cluster": {},
        "count": 0,
        "positions": [],
        "_error": False,
        "_source": "kalshi",
    }
    # Import locally to avoid a circular import at module load time.
    from kalshi_client import get_open_positions

    try:
        live = get_open_positions()
    except Exception as e:
        logging.error("[RISK] Kalshi positions fetch failed — blocking new trades: %s", e)
        _exposure_cache["_error"] = True
        return

    meta_by_ticker = _load_local_meta()
    for pos in live:
        try:
            qty = float(pos.get("position_fp") or pos.get("position") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty == 0:
            continue
        ticker = pos.get("ticker") or ""
        # market_exposure_dollars is cost basis paid for the current position.
        try:
            size = float(pos.get("market_exposure_dollars") or 0.0)
        except (TypeError, ValueError):
            size = 0.0
        if size <= 0:
            continue

        meta = meta_by_ticker.get(ticker, {})
        city = meta.get("city")
        mtype = meta.get("market_type")
        target_settle = meta.get("target_settlement")
        trade_id = meta.get("id")

        _exposure_cache["total"] += size
        _exposure_cache["count"] += 1
        if mtype != "arbitrage" and city:
            _exposure_cache["by_city"][city] = (
                _exposure_cache["by_city"].get(city, 0.0) + size
            )
        cluster = _cluster_key(target_settle) or "unknown"
        _exposure_cache["by_cluster"][cluster] = (
            _exposure_cache["by_cluster"].get(cluster, 0.0) + size
        )
        _exposure_cache["positions"].append({
            "id": trade_id,
            "ticker": ticker,
            "city": city,
            "market_type": mtype,
            "size_usd": size,
            "target_settlement": target_settle,
        })


def _cluster_key(target_settlement: str | None) -> str | None:
    """Settlements that fall in the same 6-hour window share a cluster key."""
    if not target_settlement:
        return None
    try:
        dt = datetime.fromisoformat(str(target_settlement).replace("Z", "+00:00"))
    except Exception:
        return None
    epoch_hours = int(dt.timestamp() // 3600)
    bucket = epoch_hours // max(1, CLUSTER_WINDOW_HOURS)
    return f"cluster_{bucket}"


def get_total_exposure() -> float:
    _ensure_exposure_cache()
    return float(_exposure_cache["total"]) if _exposure_cache else 0.0


def get_city_exposure(city: str) -> float:
    _ensure_exposure_cache()
    if not _exposure_cache:
        return 0.0
    return float(_exposure_cache["by_city"].get(city, 0.0))


def get_settlement_cluster_exposure(cluster_date: str | None) -> float:
    _ensure_exposure_cache()
    if not _exposure_cache:
        return 0.0
    key = _cluster_key(cluster_date) or "unknown"
    return float(_exposure_cache["by_cluster"].get(key, 0.0))


# ─── Bankroll (with fail-closed staleness) ───────────────────────────────────
def get_active_bankroll() -> tuple[float, float]:
    """Return (bankroll_usd, age_seconds). age=inf if no performance.json.

    The caller can treat age > BANKROLL_STALE_SECONDS as a halt condition.
    """
    if not os.path.exists(PERF_FILE):
        return STARTING_BANKROLL, float("inf")
    try:
        with open(PERF_FILE, "r") as f:
            data = json.load(f)
        bankroll = float(
            data.get(
                "bankroll",
                float(data.get("starting_bankroll", STARTING_BANKROLL))
                + float(data.get("total_pnl", 0.0)),
            )
        )
        ts = data.get("updated_at")
        if ts:
            try:
                updated = datetime.fromisoformat(ts)
                age = (datetime.now() - updated).total_seconds()
            except Exception:
                age = float("inf")
        else:
            age = float(os.path.getmtime(PERF_FILE))
            age = time.time() - age
        return bankroll, float(age)
    except Exception as e:
        logging.warning("[RISK] Could not read %s: %s", PERF_FILE, e)
        return STARTING_BANKROLL, float("inf")


def bankroll_stale() -> bool:
    _, age = get_active_bankroll()
    return age > BANKROLL_STALE_SECONDS


# ─── Kelly sizing ────────────────────────────────────────────────────────────
def kelly_size(
    p: float, price: float, bankroll: float, action: str = "BUY YES",
    p_for_sizing: float | None = None,
) -> float:
    """Quarter-Kelly sizing with calibration shrinkage and 5% hard cap.

    `p` is always the YES probability (caller never flips it). For BUY NO,
    `price` is the no_ask, and we convert to NO perspective internally.
    Returns a dollar size, floored at MIN_POSITION, capped at 5% of bankroll.
    Multiplied by calibration.shrinkage_factor() (audit M8).

    `p_for_sizing` (audit 2026-05-09): optional override for the probability
    fed into the Kelly fraction math. Callers pass a Wilson-bounded version
    of `p` when the underlying estimate is small-N (e.g. a 1°F bin from a
    37-member ensemble) — see strategy.find_opportunities for the direction
    rule. The entry-decision still uses the point estimate `p`; only sizing
    is shrunk, so we don't gate out trades whose point-estimate edge is
    real, we just bet less when the estimate has wide CI.
    """
    from calibration import shrinkage_factor

    if price <= 0.001 or price >= 0.999:
        return MIN_POSITION

    p_size = float(p) if p_for_sizing is None else float(p_for_sizing)
    if action == "BUY NO":
        win_p = 1.0 - p_size
        lose_p = p_size
    else:
        win_p = p_size
        lose_p = 1.0 - p_size

    b = (1.0 - price) / price
    kelly_frac = (win_p * b - lose_p) / b
    if kelly_frac <= 0:
        return MIN_POSITION

    # NOTE on default shrinkage (audit 2026-05-09): shrinkage_factor() returns
    # 1.0 when no fitted calibration exists, which is the post-reset state.
    # We considered hard-coding a 0.7 default (the M8 design ceiling) at
    # this audit, but rejected it: M8 ties shrinkage to *measured*
    # calibration error, and we have no measurement to apply. Wilson-shrunk
    # `p_for_sizing` (passed by the caller for small-N estimates) is the
    # principled per-trade equivalent; stacking a flat 0.7 on top would be
    # double-counting the same uncertainty. C1 option A's reasoning
    # (calibration.py:241-248) still stands.
    shrink = shrinkage_factor()
    size = kelly_frac * KELLY_FRACTION * shrink * bankroll
    cap = bankroll * MAX_SINGLE_BET_PCT
    return round(max(MIN_POSITION, min(cap, size)), 2)


# ─── Portfolio caps ──────────────────────────────────────────────────────────
def portfolio_kelly_ok(
    proposed_size: float, action: str
) -> tuple[bool, str]:
    """sum of open sizes + proposed must not exceed bankroll * PORTFOLIO_KELLY_CAP."""
    bankroll, _ = get_active_bankroll()
    if bankroll <= 0:
        return False, "zero bankroll"
    total = get_total_exposure() + float(proposed_size)
    cap = bankroll * PORTFOLIO_KELLY_CAP
    if total > cap + 0.005:
        return False, (
            f"PORTFOLIO_KELLY: ${total:.2f} exceeds ${cap:.2f} cap "
            f"({PORTFOLIO_KELLY_CAP:.0%} of bankroll)"
        )
    return True, ""


def settlement_cluster_ok(
    opportunity: dict, proposed_size: float
) -> tuple[bool, str]:
    """Positions settling within CLUSTER_WINDOW_HOURS share a correlation bucket."""
    bankroll, _ = get_active_bankroll()
    if bankroll <= 0:
        return False, "zero bankroll"
    settle = opportunity.get("target_settlement")
    cur = get_settlement_cluster_exposure(settle)
    proposed_total = cur + float(proposed_size)
    cap = bankroll * CORRELATION_BUCKET_CAP
    if proposed_total > cap + 0.005:
        return False, (
            f"CLUSTER_CAP: ${proposed_total:.2f} in settlement cluster {settle} "
            f"exceeds ${cap:.2f} ({CORRELATION_BUCKET_CAP:.0%} of bankroll)"
        )
    return True, ""


# ─── Global halts ────────────────────────────────────────────────────────────
def _todays_pnl() -> float:
    """Realized P&L from trades resolved today. EXCLUDES paper trades —
    they aren't real money and must not contribute to live halt math.
    Without the paper_trade filter, a Polymarket paper drawdown would
    trip DAILY_LOSS on the live Kalshi bankroll (audit C5)."""
    if not os.path.exists(DB_FILE):
        return 0.0
    try:
        with sqlite3.connect(DB_FILE, timeout=5) as conn:
            row = conn.execute(
                f"""
                SELECT COALESCE(SUM(r.profit_loss), 0.0)
                FROM results r JOIN trades t ON r.trade_id = t.id
                WHERE DATE(r.resolved_at) = DATE(?, 'localtime')
                  AND COALESCE(t.paper_trade, 0) = 0
                  AND {NOTES_VALID_SQL}
                """,
                (datetime.now().isoformat(),),
            ).fetchone()
        return float(row[0] if row else 0.0)
    except Exception:
        return 0.0


def _read_perf() -> dict:
    if not os.path.exists(PERF_FILE):
        return {}
    try:
        with open(PERF_FILE, "r") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _atomic_write_perf(data: dict) -> None:
    os.makedirs(os.path.dirname(PERF_FILE) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix="perf.", suffix=".json", dir=os.path.dirname(PERF_FILE) or "."
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, PERF_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def peak_pnl_update(current_pnl: float) -> float:
    """Monotonic peak tracker. Reads current, writes new_peak = max(prior, current).

    NEVER writes lower. Atomic via tempfile+rename (audit R1/B8).
    """
    data = _read_perf()
    prior = float(data.get("peak_pnl", 0.0) or 0.0)
    new_peak = max(prior, float(current_pnl))
    if new_peak > prior:
        data["peak_pnl"] = new_peak
        data["peak_updated_at"] = datetime.now().isoformat()
        _atomic_write_perf(data)
    return new_peak


def realized_pnl_total() -> float:
    """Sum of profit_loss across all resolved, valid LIVE trades. Excludes
    MTM and paper trades. Used by drawdown_pct (which feeds the live halt
    cap MAX_DRAWDOWN_PCT) — paper P&L must never contribute (audit C5)."""
    if not os.path.exists(DB_FILE):
        return 0.0
    try:
        with sqlite3.connect(DB_FILE, timeout=5) as conn:
            row = conn.execute(
                f"""
                SELECT COALESCE(SUM(r.profit_loss), 0.0)
                FROM results r JOIN trades t ON r.trade_id = t.id
                WHERE {NON_DRYRUN_SQL}
                  AND COALESCE(t.paper_trade, 0) = 0
                  AND {NOTES_VALID_LIVE_SQL}
                """
            ).fetchone()
        return float(row[0] if row else 0.0)
    except Exception:
        return 0.0


def drawdown_pct() -> float:
    """Drawdown computed against realized P&L only — open-position MTM swings
    do not affect peak or current. Avoids false halts when unrealized gains
    on open positions ratchet the peak above what's actually been booked."""
    data = _read_perf()
    seed = float(data.get("starting_bankroll", STARTING_BANKROLL))
    realized = realized_pnl_total()
    realized_bankroll = seed + realized
    peak_pnl = float(data.get("peak_pnl", 0.0) or 0.0)
    peak_bankroll = max(realized_bankroll, seed + peak_pnl)
    if peak_bankroll <= 0:
        return 0.0
    return max(0.0, (peak_bankroll - realized_bankroll) / peak_bankroll)


def can_trade() -> tuple[bool, list[str]]:
    """Global pre-trade gate. Reasons=[] means all good."""
    reasons: list[str] = []
    try:
        _ensure_exposure_cache()
        if _exposure_cache and _exposure_cache.get("_error"):
            return False, ["EXPOSURE_CACHE_ERROR — blocking until refresh"]

        bankroll, age = get_active_bankroll()
        if bankroll <= 0:
            reasons.append("ZERO_BANKROLL")
        if age > BANKROLL_STALE_SECONDS:
            reasons.append(
                f"STALE_BANKROLL: age={age:.0f}s > {BANKROLL_STALE_SECONDS}s (fail-closed)"
            )

        total_exp = get_total_exposure()
        if bankroll > 0:
            exp_pct = total_exp / bankroll
            if exp_pct > PORTFOLIO_KELLY_CAP:
                reasons.append(
                    f"EXPOSURE: {exp_pct:.0%} of bankroll exceeds "
                    f"{PORTFOLIO_KELLY_CAP:.0%} portfolio-Kelly cap"
                )

        dd = drawdown_pct()
        if dd > MAX_DRAWDOWN_PCT:
            reasons.append(
                f"DRAWDOWN: {dd:.1%} exceeds {MAX_DRAWDOWN_PCT:.0%} limit"
            )

        today = _todays_pnl()
        if bankroll > 0:
            limit = -abs(DAILY_LOSS_LIMIT_PCT * bankroll)
            if today < limit:
                reasons.append(
                    f"DAILY_LOSS: ${today:+.2f} exceeds ${limit:.2f} limit"
                )
    except Exception as e:
        reasons.append(f"RISK_CHECK_ERROR: {e}")

    return (len(reasons) == 0), reasons


def write_bankroll_snapshot(bankroll: float, cash: float | None = None) -> None:
    """Persist bankroll (total equity), available cash, and updated_at."""
    data = _read_perf()
    data.setdefault("starting_bankroll", STARTING_BANKROLL)
    data["bankroll"] = float(bankroll)
    if cash is not None:
        data["cash"] = float(cash)
    data["updated_at"] = datetime.now().isoformat()
    _atomic_write_perf(data)


def get_available_cash() -> float:
    """Cash available for new orders (from last Kalshi refresh). 0 if unknown."""
    data = _read_perf()
    try:
        return float(data.get("cash", 0.0) or 0.0)
    except (ValueError, TypeError):
        return 0.0


# In-cycle deployment counter. Reset at the top of each cycle by
# reset_cycle_deployment(). Debited by record_deployment() right after
# each successful place_limit_order. Without this, sequential cash_ok()
# calls within a single cycle all see the same starting snapshot — which
# is exactly how 8 orders all "passed" the cash gate yet got rejected by
# Kalshi with insufficient_balance on 2026-05-03.
_cycle_deployed_usd: float = 0.0


def reset_cycle_deployment() -> None:
    """Call once at the top of each cycle, after the bankroll refresh."""
    global _cycle_deployed_usd
    _cycle_deployed_usd = 0.0


def record_deployment(usd: float) -> None:
    """Call after every successful place_limit_order so subsequent cash_ok
    checks within the same cycle see the new available cash."""
    global _cycle_deployed_usd
    if usd > 0:
        _cycle_deployed_usd += float(usd)


def get_cycle_deployment() -> float:
    return _cycle_deployed_usd


def cash_ok(proposed_size: float) -> tuple[bool, str]:
    """Reject orders whose cost exceeds free cash. Guards against the exposure
    tracker lying (e.g. local DB out of sync with Kalshi fills).

    Subtracts in-cycle deployment from the snapshot so multiple orders within
    one cycle don't all over-spend the same dollars.
    """
    snapshot_cash = get_available_cash()
    available = snapshot_cash - _cycle_deployed_usd
    if proposed_size > available + 0.005:
        return False, (
            f"INSUFFICIENT_CASH: order ${float(proposed_size):.2f} exceeds "
            f"available ${available:.2f} "
            f"(snapshot ${snapshot_cash:.2f}, deployed this cycle "
            f"${_cycle_deployed_usd:.2f})"
        )
    return True, ""
