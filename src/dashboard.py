"""
dashboard.py — single-file Flask dashboard for kalshi_bot_2.0.

Serves localhost:8082 with:
  - KPIs (bankroll, cash, exposure, today P&L, peak, drawdown, halt status,
          total win rate, API cost/day, monthly cost projection)
  - All open positions table
  - 10 most recently resolved trades
  - Edge vs actual P&L scatterplot
  - Daily P&L bar chart (last 30 days)
  - Per-city win rate bar chart
  - Calibration reliability chart
  - Scan history (last 20 cycles)

Auto-started from main.py as a daemon thread. Read-only against
data/trades.db and data/performance.json — no shared state with the bot,
so a dashboard bug cannot take the bot down.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any

from flask import Flask, jsonify, render_template_string

from config import (
    CALIBRATION_META,
    CITIES,
    DATA_DIR,
    DB_FILE,
    FORECAST_HEALTH_FILE,
    LIVE_TRADING_ENABLED,
    PERF_FILE,
    STARTING_BANKROLL,
)
from storage import (
    NON_ARB_SQL,
    NON_DRYRUN_SQL,
    NOTES_VALID_LIVE_SQL,
)

_POLYMARKET_SNAPSHOT_FILE = os.path.join(DATA_DIR, "polymarket_markets.json")
_CROSS_VENUE_SNAPSHOT_FILE = os.path.join(DATA_DIR, "cross_venue_arb.json")

app = Flask(__name__)

logging.getLogger("werkzeug").setLevel(logging.WARNING)
# Suppress Flask's "* Serving Flask app / Debug mode" startup banner,
# which bypasses logging and goes through click.echo().
try:
    import flask.cli as _flask_cli
    _flask_cli.show_server_banner = lambda *_a, **_kw: None
except Exception:
    pass


# ─── Data helpers (read-only) ───────────────────────────────────────────────
def _read_perf() -> dict[str, Any]:
    if not os.path.exists(PERF_FILE):
        return {}
    try:
        with open(PERF_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


_kalshi_balance_cache: dict = {}
_kalshi_balance_lock = threading.Lock()

def _kalshi_balance() -> tuple[float, float, float]:
    """Return (cash, position_value, total_balance) from Kalshi. Cached for 30s.
    Falls back to (0, 0, 0) on API failure."""
    import time
    with _kalshi_balance_lock:
        if _kalshi_balance_cache.get("ts", 0) > time.time() - 30:
            return _kalshi_balance_cache["v"]
        try:
            import kalshi_client
            data = kalshi_client.get_portfolio_balance()
            cash = float(data.get("cash_cents", 0)) / 100
            pos_val = float(data.get("portfolio_value_cents", 0)) / 100
            total = float(data.get("balance_cents", 0)) / 100
            result = (cash, pos_val, total)
        except Exception:
            result = (0.0, 0.0, 0.0)
        _kalshi_balance_cache["ts"] = time.time()
        _kalshi_balance_cache["v"] = result
        return result


def _latest_scan_halt_state() -> tuple[bool, list[str], bool]:
    """Read halt state from the most recent scan_log row.

    Returns (halted, reasons, is_fresh). The dashboard runs in its own
    process — calling risk.can_trade() from here uses the dashboard's
    local in-memory bankroll cache, which nothing refreshes (the bot
    process has its own cache). The truthful halt state is what the
    *bot* wrote to scan_log on its last cycle (src/main.py:582), so we
    read that instead.

    `is_fresh` indicates the bot has cycled within the last 10 minutes;
    if not, halt state is unknown and the UI should treat it as such
    rather than showing a stale halt indefinitely.
    """
    rows = _db_query(
        "SELECT cycle_at, breakdown_json FROM scan_log "
        "ORDER BY id DESC LIMIT 1"
    )
    if not rows:
        return False, [], False
    cycle_at, bd_json = rows[0]
    try:
        bd = json.loads(bd_json) if bd_json else {}
    except Exception:
        bd = {}
    halted = bool(bd.get("halted"))
    reasons = list(bd.get("reasons") or [])

    # Freshness: bot writes a scan_log row every cycle (~5 min cadence).
    # If we haven't seen one in 10 min, assume bot is not running.
    is_fresh = False
    try:
        from datetime import datetime, timezone
        ca = datetime.fromisoformat(str(cycle_at).replace("Z", "+00:00"))
        if ca.tzinfo is None:
            ca = ca.replace(tzinfo=timezone.utc)
        age_s = (datetime.now(timezone.utc) - ca).total_seconds()
        is_fresh = age_s < 600
    except Exception:
        pass
    return halted, reasons, is_fresh


def _risk_halted_now() -> bool:
    halted, _, fresh = _latest_scan_halt_state()
    return bool(halted and fresh)


def _halt_reasons_now() -> list[str]:
    halted, reasons, fresh = _latest_scan_halt_state()
    if halted and fresh:
        return reasons
    if not fresh:
        # Surface this as a "reason" so the user knows the dashboard is
        # showing stale info rather than a real halt.
        return ["BOT_NOT_RUNNING (no scan in 10+ min)"] if not _bot_recently_active() else []
    return []


def _bot_recently_active() -> bool:
    rows = _db_query("SELECT cycle_at FROM scan_log ORDER BY id DESC LIMIT 1")
    if not rows:
        return False
    try:
        from datetime import datetime, timezone
        ca = datetime.fromisoformat(str(rows[0][0]).replace("Z", "+00:00"))
        if ca.tzinfo is None:
            ca = ca.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ca).total_seconds() < 600
    except Exception:
        return False


def _read_cal_meta() -> dict[str, Any]:
    if not os.path.exists(CALIBRATION_META):
        return {}
    try:
        with open(CALIBRATION_META) as f:
            return json.load(f)
    except Exception:
        return {}


def _db_query(sql: str, params: tuple = ()) -> list[tuple]:
    if not os.path.exists(DB_FILE):
        return []
    try:
        with sqlite3.connect(DB_FILE, timeout=5) as c:
            return c.execute(sql, params).fetchall()
    except Exception as e:
        logging.warning("[DASH] db query failed: %s", e)
        return []


# ─── JSON endpoints ─────────────────────────────────────────────────────────
@app.route("/api/kpis")
def api_kpis():
    perf = _read_perf()
    live_bankroll = float(perf.get("bankroll", STARTING_BANKROLL))
    peak = float(perf.get("peak_pnl", 0.0))
    starting = float(perf.get("starting_bankroll", STARTING_BANKROLL))
    realized_rows = _db_query(f"""
        SELECT COALESCE(SUM(r.profit_loss), 0.0)
        FROM results r JOIN trades t ON r.trade_id = t.id
        WHERE {NON_DRYRUN_SQL}
          AND {NOTES_VALID_LIVE_SQL}
    """)
    realized_pnl = float(realized_rows[0][0]) if realized_rows else 0.0
    # Headline bankroll = the live Kalshi snapshot, so the dashboard agrees
    # with what the venue actually shows. Manual top-ups and withdrawals
    # are part of bankroll but explicitly NOT part of total_pnl, which is
    # sourced only from resolved trades.
    bankroll = live_bankroll
    total_pnl = realized_pnl
    db_bankroll = starting + realized_pnl  # what bankroll would be sans deposits
    peak_bankroll = max(bankroll, starting + peak)
    unrealized_pnl = live_bankroll - db_bankroll

    # Count unique held positions, not raw fill rows. The same ticker+side
    # can have many trade rows (e.g. retries during demo lag, or the bot
    # firing the same opp across cycles), but the user thinks of each
    # ticker+side combination as one position — same as Kalshi's UI.
    open_rows = _db_query(f"""
        SELECT COALESCE(SUM(t.size_usd), 0.0),
               COUNT(DISTINCT t.ticker || '|' || COALESCE(t.action, ''))
        FROM trades t LEFT JOIN results r ON r.trade_id = t.id
        WHERE r.id IS NULL
          AND {NON_DRYRUN_SQL}
          AND {NOTES_VALID_LIVE_SQL}
    """)
    open_count = int(open_rows[0][1]) if open_rows else 0
    kalshi_cash, position_value, total_balance = _kalshi_balance()
    cash = kalshi_cash if total_balance > 0 else max(0.0, bankroll)
    deployed_pct = position_value / total_balance if total_balance > 0 else 0.0
    drawdown_pct = (peak_bankroll - bankroll) / peak_bankroll if peak_bankroll > 0 else 0.0

    # Today P&L = real trading activity that resolved today.
    # Excludes 'dry-run' (never real) and 'backfill' (historical
    # reconciliation stamps resolved_at = the date the backfill ran,
    # not the actual settlement date — would pollute "today" bucket
    # even for legitimate historical trades; see §6.3).
    today_rows = _db_query("""
        SELECT COALESCE(SUM(r.profit_loss), 0.0)
        FROM results r JOIN trades t ON r.trade_id = t.id
        WHERE DATE(r.resolved_at) = DATE('now', 'localtime')
          AND (t.mode IS NULL OR t.mode NOT IN ('dry-run', 'backfill'))
    """)
    today_pnl = float(today_rows[0][0]) if today_rows else 0.0

    # Win rate (overall + per side) — EXCLUDES arb legs. Arb legs settle
    # 1 win + (N-1) losses by construction and would distort BUY YES /
    # BUY NO / maker / taker counters. The arb KPI below treats each
    # group as one trade.
    wr_rows = _db_query(f"""
        SELECT t.action,
               COUNT(*),
               SUM(CASE WHEN r.profit_loss > 0 THEN 1 ELSE 0 END),
               COALESCE(SUM(r.profit_loss), 0.0)
        FROM trades t JOIN results r ON r.trade_id = t.id
        WHERE {NON_DRYRUN_SQL}
          AND {NON_ARB_SQL}
          AND {NOTES_VALID_LIVE_SQL}
        GROUP BY t.action
    """)
    yes_total = yes_wins = no_total = no_wins = 0
    yes_pnl = no_pnl = 0.0
    for action, n, w, pnl in wr_rows:
        if action == "BUY YES":
            yes_total, yes_wins, yes_pnl = int(n), int(w or 0), float(pnl or 0.0)
        elif action == "BUY NO":
            no_total, no_wins, no_pnl = int(n), int(w or 0), float(pnl or 0.0)
    nonarb_resolved = yes_total + no_total
    nonarb_wins = yes_wins + no_wins
    yes_win_rate = round(yes_wins / yes_total * 100, 1) if yes_total > 0 else None
    no_win_rate = round(no_wins / no_total * 100, 1) if no_total > 0 else None

    # Win rate / P&L by execution mode (maker vs taker) — also excludes arbs.
    mode_rows = _db_query(f"""
        SELECT t.mode,
               COUNT(*),
               SUM(CASE WHEN r.profit_loss > 0 THEN 1 ELSE 0 END),
               COALESCE(SUM(r.profit_loss), 0.0)
        FROM trades t JOIN results r ON r.trade_id = t.id
        WHERE {NON_DRYRUN_SQL}
          AND {NON_ARB_SQL}
          AND {NOTES_VALID_LIVE_SQL}
        GROUP BY t.mode
    """)
    maker_total = maker_wins = taker_total = taker_wins = 0
    maker_pnl = taker_pnl = 0.0
    for mode, n, w, pnl in mode_rows:
        if mode == "maker":
            maker_total, maker_wins, maker_pnl = int(n), int(w or 0), float(pnl or 0.0)
        elif mode == "taker":
            taker_total, taker_wins, taker_pnl = int(n), int(w or 0), float(pnl or 0.0)
    maker_win_rate = round(maker_wins / maker_total * 100, 1) if maker_total > 0 else None
    taker_win_rate = round(taker_wins / taker_total * 100, 1) if taker_total > 0 else None

    # Arb stats (group-bundled). One arb_id = one trade, win iff group P&L > 0.
    import storage
    arb = storage.get_arb_group_stats()
    arb_win_rate = (
        round(arb["win_rate"] * 100, 1) if arb["win_rate"] is not None else None
    )


    # "Bundled" headline win rate: non-arb legs + arb groups (one each).
    bundled_total = nonarb_resolved + arb["groups_resolved"]
    bundled_wins = nonarb_wins + arb["groups_won"]
    win_rate = round(bundled_wins / bundled_total * 100, 1) if bundled_total > 0 else None
    total_trades = bundled_total
    wins = bundled_wins

    # API cost today + monthly projection
    cost_today_rows = _db_query("""
        SELECT COALESCE(SUM(claude_cost_usd), 0.0)
        FROM scan_log
        WHERE DATE(cycle_at) = DATE('now', 'localtime')
    """)
    cost_today = float(cost_today_rows[0][0]) if cost_today_rows else 0.0

    avg_cost_rows = _db_query("""
        SELECT AVG(daily_cost)
        FROM (
            SELECT DATE(cycle_at) as day, SUM(claude_cost_usd) as daily_cost
            FROM scan_log
            WHERE DATE(cycle_at) >= DATE('now', '-30 days', 'localtime')
            GROUP BY day
        )
    """)
    avg_daily_cost = float(avg_cost_rows[0][0] or 0.0) if avg_cost_rows else 0.0
    monthly_projection = round(avg_daily_cost * 30, 2)

    return jsonify({
        "bankroll": round(bankroll, 2),
        "live_bankroll": round(live_bankroll, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "starting_bankroll": round(starting, 2),
        "total_pnl": round(total_pnl, 2),
        "cash": round(cash, 2),
        "position_value": round(position_value, 2),
        "deployed_pct": round(deployed_pct * 100, 1),
        "open_positions": open_count,
        "today_pnl": round(today_pnl, 2),
        "peak_pnl": round(peak, 2),
        "peak_bankroll": round(peak_bankroll, 2),
        "drawdown_pct": round(drawdown_pct * 100, 2),
        # `live_trading` is the env config flag — does the user *want* the
        # bot to trade live? `risk_halted` is the runtime state — is the
        # bot actually being blocked by risk checks right now? Dashboard
        # surfaces both so the pill at top can render LIVE / HALTED /
        # DRY-RUN correctly. Prior behavior (2026-05-09 audit): pill was
        # wired only to `live_trading`, so a 33%-drawdown halt showed
        # "LIVE" in the UI while the bot was actually skipping every cycle.
        "live_trading": bool(LIVE_TRADING_ENABLED),
        "risk_halted": _risk_halted_now(),
        "halt_reasons": _halt_reasons_now(),
        "updated_at": perf.get("updated_at", ""),
        "win_rate": win_rate,
        "total_trades": total_trades,
        "wins": wins,
        "yes_win_rate": yes_win_rate,
        "yes_total": yes_total,
        "yes_wins": yes_wins,
        "yes_pnl": round(yes_pnl, 2),
        "no_win_rate": no_win_rate,
        "no_total": no_total,
        "no_wins": no_wins,
        "no_pnl": round(no_pnl, 2),
        "maker_win_rate": maker_win_rate,
        "maker_total": maker_total,
        "maker_wins": maker_wins,
        "maker_pnl": round(maker_pnl, 2),
        "taker_win_rate": taker_win_rate,
        "taker_total": taker_total,
        "taker_wins": taker_wins,
        "taker_pnl": round(taker_pnl, 2),
        # Arb stats (group-bundled). win_rate should be 100% by construction
        # for clean arbs; <100% means investigate (stranded legs, fee math,
        # partial fills).
        "arb_win_rate": arb_win_rate,
        "arb_groups_total": arb["groups_total"],
        "arb_groups_open": arb["groups_open"],
        "arb_groups_resolved": arb["groups_resolved"],
        "arb_groups_won": arb["groups_won"],
        "arb_groups_lost": arb["groups_lost"],
        "arb_realized_pnl": arb["realized_pnl"],
        "arb_open_cost": arb["open_cost"],
        "arb_stranded_legs": arb["stranded_legs"],
        "cost_today": round(cost_today, 4),
        "monthly_projection": monthly_projection,
    })


@app.route("/api/positions")
def api_positions():
    rows = _db_query(f"""
        SELECT t.id, t.ticker, t.city, t.action, t.entry_price, t.contracts,
               t.size_usd, t.ensemble_p, t.calibrated_p, t.edge_at_entry,
               t.mode, t.opened_at, t.target_settlement,
               t.market_type, t.notes
        FROM trades t LEFT JOIN results r ON r.trade_id = t.id
        WHERE r.id IS NULL
          AND {NON_DRYRUN_SQL}
          AND {NOTES_VALID_LIVE_SQL}
        ORDER BY t.opened_at DESC
    """)
    _MONTHS = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
               'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}
    import re
    from datetime import timezone

    def _secs_to_settle(settle_str: str, ticker: str) -> float | None:
        now = datetime.now(timezone.utc)
        # Try explicit target_settlement first
        if settle_str:
            try:
                dt = datetime.fromisoformat(str(settle_str).replace("Z", "+00:00"))
                return (dt - now).total_seconds()
            except Exception:
                pass
        # Fall back to parsing date from ticker e.g. KXHIGHMIA-26APR25-B83.5
        # Format: {YY}{MON}{DD} — close_time is always measurement_date +1d 04:59 UTC
        m = re.search(r'-(\d{2})([A-Z]{3})(\d{2})-', ticker or '')
        if m:
            try:
                from datetime import timedelta
                mon = _MONTHS.get(m.group(2))
                if mon:
                    meas = datetime(2000 + int(m.group(1)), mon, int(m.group(3)),
                                    tzinfo=timezone.utc)
                    dt = meas + timedelta(days=1, hours=4, minutes=59)
                    return (dt - now).total_seconds()
            except Exception:
                pass
        return None

    # Aggregate non-arb fills by (ticker, action). Arb legs stay 1-row-per-leg
    # because the rollback/grouping logic keys off arb_id.
    grouped: dict[tuple[str, str], dict] = {}
    arb_legs: list[dict] = []

    for r in rows:
        (rid, ticker, city, action, entry_price, contracts, size_usd,
         ensemble_p, calibrated_p, edge_at_entry, mode, opened_at,
         target_settle, market_type, notes_raw) = r
        notes = notes_raw or ""
        is_arb = (market_type == "arbitrage")
        secs = _secs_to_settle(target_settle, ticker)

        if is_arb:
            arb_id = None
            if notes.startswith("arb:"):
                arb_id = notes
            elif notes.startswith("arb_stranded:"):
                arb_id = "stranded"
            arb_legs.append({
                "id": rid, "ticker": ticker, "city": city, "action": action,
                "entry_price": entry_price, "contracts": contracts,
                "size_usd": size_usd, "ensemble_p": ensemble_p,
                "calibrated_p": calibrated_p, "edge_at_entry": edge_at_entry,
                "mode": mode, "opened_at": opened_at,
                "market_type": market_type, "is_arb": True, "arb_id": arb_id,
                "secs_to_settle": round(secs) if secs is not None else None,
                "fill_count": 1,
            })
            continue

        key = (ticker or "", action or "")
        cur = grouped.get(key)
        if cur is None:
            grouped[key] = {
                "id": rid, "ticker": ticker, "city": city, "action": action,
                # entry_price and the calibration/edge fields are accumulated
                # as contract-weighted sums; finalized below.
                "_px_qty": float(entry_price or 0.0) * int(contracts or 0),
                "_p_qty": float(calibrated_p or 0.0) * int(contracts or 0),
                "_e_qty": float(ensemble_p or 0.0) * int(contracts or 0),
                "_edge_qty": float(edge_at_entry or 0.0) * int(contracts or 0),
                "contracts": int(contracts or 0),
                "size_usd": float(size_usd or 0.0),
                "mode": mode,
                "opened_at": opened_at,  # min, set below
                "market_type": market_type,
                "is_arb": False, "arb_id": None,
                "secs_to_settle": round(secs) if secs is not None else None,
                "fill_count": 1,
            }
        else:
            qty = int(contracts or 0)
            cur["_px_qty"] += float(entry_price or 0.0) * qty
            cur["_p_qty"] += float(calibrated_p or 0.0) * qty
            cur["_e_qty"] += float(ensemble_p or 0.0) * qty
            cur["_edge_qty"] += float(edge_at_entry or 0.0) * qty
            cur["contracts"] += qty
            cur["size_usd"] += float(size_usd or 0.0)
            cur["fill_count"] += 1
            # Keep the earliest opened_at — that's when the position started.
            if opened_at and (not cur["opened_at"] or opened_at < cur["opened_at"]):
                cur["opened_at"] = opened_at
            # `id` from the first-seen (most recent because ORDER BY DESC) row
            # is fine for frontend linking.

    out = list(arb_legs)
    for g in grouped.values():
        qty = g["contracts"] or 1
        g["entry_price"] = g.pop("_px_qty") / qty
        g["calibrated_p"] = g.pop("_p_qty") / qty
        g["ensemble_p"] = g.pop("_e_qty") / qty
        g["edge_at_entry"] = g.pop("_edge_qty") / qty
        out.append(g)

    out.sort(key=lambda x: x.get("opened_at") or "", reverse=True)
    return jsonify(out)


@app.route("/api/arbs")
def api_arbs():
    """Phase-2-followup: arb tracker. Open groups + recent resolved
    history. Bundled by arb_id (clean groups) plus a separate stranded
    bucket for rollback-failed legs."""
    import storage
    stats = storage.get_arb_group_stats()
    history = storage.get_resolved_arb_groups(limit=20)
    return jsonify({
        "summary": {
            "groups_total": stats["groups_total"],
            "groups_open": stats["groups_open"],
            "groups_resolved": stats["groups_resolved"],
            "groups_won": stats["groups_won"],
            "groups_lost": stats["groups_lost"],
            "win_rate": stats["win_rate"],
            "realized_pnl": stats["realized_pnl"],
            "open_cost": stats["open_cost"],
            "stranded_legs": stats["stranded_legs"],
            "stranded_legs_pnl": stats["stranded_legs_pnl"],
        },
        "open_groups": stats["open_groups"],
        "history": history,
    })


@app.route("/api/trades")
def api_trades():
    # Arb legs excluded — they have their own bundled history in
    # /api/arbs and would clutter the recent-trades feed with 1 win +
    # (N-1) "losses" per group.
    #
    # Partial-fill splits collapsed into one logical position. When an
    # exit partially fills, storage.split_trade_on_partial_exit forks the
    # unsold remainder into a new trade row whose `notes` field is set to
    # `split_from_trade_<parent_id>`. Repeated partial exits chain these
    # rows. Walking the chain to its root lets us aggregate slices back
    # into a single row for display — otherwise one Kalshi position with
    # N partial exits looks like N separately resolved trades.
    parent_rows = _db_query(
        "SELECT id, notes FROM trades WHERE notes LIKE 'split_from_trade_%'"
    )
    parent_of: dict[int, int] = {}
    for tid, notes in parent_rows:
        try:
            parent_of[int(tid)] = int(str(notes).split("_")[-1])
        except (ValueError, AttributeError):
            continue

    def root_of(tid: int) -> int:
        seen: set[int] = set()
        cur = tid
        while cur in parent_of and cur not in seen:
            seen.add(cur)
            cur = parent_of[cur]
        return cur

    rows = _db_query("""
        SELECT t.id, t.ticker, t.city, t.action, t.entry_price, t.size_usd,
               t.edge_at_entry, t.mode, t.opened_at,
               r.outcome, r.exit_price, r.profit_loss, r.resolved_at
        FROM trades t JOIN results r ON r.trade_id = t.id
        WHERE (t.mode IS NULL OR t.mode != 'dry-run')
          AND (t.market_type IS NULL OR t.market_type != 'arbitrage')
        ORDER BY r.resolved_at DESC
        LIMIT 200
    """)

    # Aggregate by root trade id. The root row's metadata (ticker, city,
    # action, entry_price, edge, opened_at) represents the logical
    # position; size_usd and profit_loss sum across slices; resolved_at
    # is the most recent slice's exit. `status` distinguishes a manual
    # exit (sold pre-settlement) from an actual market settlement —
    # outcome ∈ {yes, no} means the market settled; 'exited' means the
    # bot sold out before settlement.
    grouped: dict[int, dict[str, Any]] = {}
    for r in rows:
        tid = int(r[0])
        rid = root_of(tid)
        outcome = r[9]
        resolved_at = r[12]
        size = float(r[5] or 0)
        pnl = float(r[11] or 0) if r[11] is not None else 0.0
        g = grouped.get(rid)
        if g is None:
            grouped[rid] = {
                "id": rid,
                "ticker": r[1], "city": r[2], "action": r[3],
                "entry_price": r[4],
                "size_usd": size,
                "edge_at_entry": r[6],
                "mode": r[7], "opened_at": r[8],
                "outcome": outcome, "exit_price": r[10],
                "profit_loss": pnl,
                "resolved_at": resolved_at,
                "slice_count": 1,
            }
        else:
            g["size_usd"] += size
            g["profit_loss"] += pnl
            g["slice_count"] += 1
            # Latest slice wins for outcome / exit_price / resolved_at;
            # rows arrive newest-first so only overwrite if we somehow
            # see a newer one (defensive).
            if (resolved_at or "") > (g["resolved_at"] or ""):
                g["outcome"] = outcome
                g["exit_price"] = r[10]
                g["resolved_at"] = resolved_at

    out = sorted(grouped.values(),
                 key=lambda g: g["resolved_at"] or "", reverse=True)[:10]
    for g in out:
        oc = str(g.get("outcome") or "").lower()
        g["status"] = "exited" if oc == "exited" else "settled"
        g["profit_loss"] = round(g["profit_loss"], 4)
        g["size_usd"] = round(g["size_usd"], 4)
    return jsonify(out)


@app.route("/api/scans")
def api_scans():
    rows = _db_query("""
        SELECT cycle_at, markets_scanned, opportunities, trades_placed,
               claude_cost_usd, breakdown_json
        FROM scan_log
        ORDER BY id DESC
        LIMIT 5
    """)
    out = []
    for r in rows:
        try:
            breakdown = json.loads(r[5]) if r[5] else {}
        except Exception:
            breakdown = {}
        out.append({
            "cycle_at": r[0], "markets_scanned": r[1], "opportunities": r[2],
            "trades_placed": r[3], "claude_cost_usd": r[4], "breakdown": breakdown,
        })
    return jsonify(out)


@app.route("/api/calibration")
def api_calibration():
    meta = _read_cal_meta()
    # Arb legs excluded — strategy_arb sets calibrated_p == yes_price by
    # construction (the arb bypasses the model), so including them in the
    # reliability chart inflates "calibration" with degenerate datapoints
    # where prediction always equals the realized market price.
    rows = _db_query("""
        SELECT t.calibrated_p, t.action, r.outcome, r.profit_loss
        FROM trades t JOIN results r ON r.trade_id = t.id
        WHERE t.calibrated_p IS NOT NULL
          AND r.outcome IS NOT NULL
          AND (t.mode IS NULL OR t.mode != 'dry-run')
          AND (t.market_type IS NULL OR t.market_type != 'arbitrage')
    """)
    buckets: dict[int, list[int]] = {i: [] for i in range(10)}
    for cal_p, action, outcome, pnl in rows:
        if cal_p is None or outcome is None:
            continue
        if action == "BUY NO":
            p_win = 1.0 - float(cal_p)
            won = 1 if str(outcome).lower() == "no" else 0
        else:
            p_win = float(cal_p)
            won = 1 if str(outcome).lower() == "yes" else 0
        if not (0.0 <= p_win <= 1.0):
            continue
        idx = min(9, int(p_win * 10))
        buckets[idx].append(won)

    points = []
    for i in range(10):
        bucket = buckets[i]
        n = len(bucket)
        if n == 0:
            continue
        mid = (i + 0.5) / 10.0
        actual = sum(bucket) / n
        points.append({"predicted": round(mid, 3), "actual": round(actual, 3), "n": n})

    return jsonify({"meta": meta, "points": points, "n_resolved": len(rows)})


@app.route("/api/forecast_health")
def api_forecast_health():
    if not os.path.exists(FORECAST_HEALTH_FILE):
        return jsonify({"computed_at": None, "cities": {}, "global_alerts": [],
                        "status": "not_computed_yet"})
    try:
        with open(FORECAST_HEALTH_FILE) as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e), "cities": {}}), 500


@app.route("/api/analytics")
def api_analytics():
    # Edge vs actual P&L scatter — exclude arb legs (their edge is
    # mechanical, not a model prediction; including them creates a fake
    # "high edge always wins" cluster).
    edge_rows = _db_query("""
        SELECT t.edge_at_entry, r.profit_loss, t.city
        FROM trades t JOIN results r ON r.trade_id = t.id
        WHERE t.edge_at_entry IS NOT NULL
          AND r.profit_loss IS NOT NULL
          AND (t.mode IS NULL OR t.mode != 'dry-run')
          AND (t.market_type IS NULL OR t.market_type != 'arbitrage')
        ORDER BY r.resolved_at DESC
        LIMIT 500
    """)
    edge_scatter = [
        {"edge": round(float(r[0]) * 100, 2), "pnl": round(float(r[1]), 2), "city": r[2] or ""}
        for r in edge_rows
    ]

    # Daily P&L last 30 days
    daily_rows = _db_query("""
        SELECT DATE(r.resolved_at, 'localtime') as day,
               ROUND(SUM(r.profit_loss), 2)
        FROM results r JOIN trades t ON r.trade_id = t.id
        WHERE (t.mode IS NULL OR t.mode != 'dry-run')
          AND DATE(r.resolved_at) >= DATE('now', '-30 days')
        GROUP BY day
        ORDER BY day ASC
    """)
    daily_pnl = [{"date": r[0], "pnl": round(float(r[1]), 2)} for r in daily_rows]

    # Per-city win rate. Show ALL configured cities — cities with no resolved
    # binary trade get n=0 and are rendered with a neutral bar (so the gap is
    # visible rather than silently omitted).
    # Per-city: exclude arb legs from win/total (each clean arb in a city
    # adds 1 win + (N-1) losses to that city's tally — distorting). P&L is
    # still surfaced via the inclusive daily P&L chart.
    city_rows = _db_query("""
        SELECT t.city,
               COUNT(*) as total,
               SUM(CASE WHEN r.profit_loss > 0 THEN 1 ELSE 0 END) as wins,
               COALESCE(SUM(r.profit_loss), 0.0) as pnl
        FROM trades t JOIN results r ON r.trade_id = t.id
        WHERE t.city IS NOT NULL AND t.city != ''
          AND (t.mode IS NULL OR t.mode != 'dry-run')
          AND (t.market_type IS NULL OR t.market_type != 'arbitrage')
        GROUP BY t.city
    """)
    by_city = {
        r[0]: (int(r[1]), int(r[2] or 0), float(r[3] or 0.0))
        for r in city_rows
    }
    city_winrate = []
    for city in CITIES.keys():
        total, wins, pnl = by_city.get(city, (0, 0, 0.0))
        city_winrate.append({
            "city": city,
            "total": total,
            "wins": wins,
            "pnl": round(pnl, 2),
            "win_rate": round(wins / total * 100, 1) if total else None,
        })
    # Sort: cities with data first, by win rate desc; then untraded cities last.
    city_winrate.sort(key=lambda d: (d["win_rate"] is None, -(d["win_rate"] or 0)))

    # API cost per day (last 30 days)
    cost_rows = _db_query("""
        SELECT DATE(cycle_at, 'localtime') as day,
               ROUND(SUM(claude_cost_usd), 4)
        FROM scan_log
        WHERE DATE(cycle_at) >= DATE('now', '-30 days')
        GROUP BY day
        ORDER BY day ASC
    """)
    daily_cost = [{"date": r[0], "cost": float(r[1])} for r in cost_rows]

    # Edge calibration: bucket by edge_at_entry in 5% increments, compute
    # realized win rate per bucket. If our edge estimate is well-calibrated,
    # win rate should rise monotonically with the bucket midpoint.
    edge_cal_rows = _db_query("""
        SELECT t.edge_at_entry, t.action, r.outcome
        FROM trades t JOIN results r ON r.trade_id = t.id
        WHERE t.edge_at_entry IS NOT NULL
          AND r.outcome IN ('yes','no')
          AND (t.mode IS NULL OR t.mode != 'dry-run')
          AND (t.market_type IS NULL OR t.market_type != 'arbitrage')
    """)
    BUCKET_W = 0.05  # 5% wide
    N_BUCKETS = 8    # 0–5, 5–10, ..., 35–40+
    ec_buckets: dict[int, list[int]] = {i: [] for i in range(N_BUCKETS)}
    for edge, action, outcome in edge_cal_rows:
        if edge is None:
            continue
        e = float(edge)
        if e < 0:
            continue
        idx = min(N_BUCKETS - 1, int(e / BUCKET_W))
        won = 1 if (
            (action == "BUY YES" and str(outcome).lower() == "yes")
            or (action == "BUY NO" and str(outcome).lower() == "no")
        ) else 0
        ec_buckets[idx].append(won)
    edge_calibration = []
    for i in range(N_BUCKETS):
        b = ec_buckets[i]
        lo_pct = round(i * BUCKET_W * 100, 1)
        hi_pct = round((i + 1) * BUCKET_W * 100, 1)
        label = f"{int(lo_pct)}–{int(hi_pct)}%" if i < N_BUCKETS - 1 else f"{int(lo_pct)}%+"
        edge_calibration.append({
            "label": label,
            "lo_pct": lo_pct,
            "hi_pct": hi_pct,
            "mid_pct": round((i + 0.5) * BUCKET_W * 100, 1),
            "win_rate": round(sum(b) / len(b) * 100, 1) if b else None,
            "n": len(b),
        })

    # Daily Brier score — squared error between predicted P(our side wins)
    # and the realized win indicator, averaged within each day. Lower is
    # better; postmortem §9.2 calls Brier > 0.25 a halt threshold.
    brier_rows = _db_query("""
        SELECT DATE(r.resolved_at, 'localtime') as day,
               t.action, t.calibrated_p, r.outcome
        FROM results r JOIN trades t ON r.trade_id = t.id
        WHERE r.outcome IN ('yes','no')
          AND t.calibrated_p IS NOT NULL
          AND (t.mode IS NULL OR t.mode != 'dry-run')
          AND (t.market_type IS NULL OR t.market_type != 'arbitrage')
          AND DATE(r.resolved_at) >= DATE('now', '-30 days')
    """)
    by_day: dict[str, list[float]] = {}
    for day, action, cp, outcome in brier_rows:
        try:
            cp_f = float(cp)
        except (TypeError, ValueError):
            continue
        # P(our side wins) = cp for BUY YES, (1 - cp) for BUY NO.
        p_win = cp_f if action == "BUY YES" else 1.0 - cp_f
        won = 1.0 if (
            (action == "BUY YES" and str(outcome).lower() == "yes")
            or (action == "BUY NO" and str(outcome).lower() == "no")
        ) else 0.0
        by_day.setdefault(day, []).append((p_win - won) ** 2)
    daily_brier = [
        {
            "date": day,
            "brier": round(sum(errs) / len(errs), 4),
            "n": len(errs),
        }
        for day, errs in sorted(by_day.items())
    ]

    # ─── Segment P&L (audit 2026-05-09) ──────────────────────────────────────
    # Cross-tab BUY-side P&L by ticker_kind × entry_band × edge_band so we can
    # detect regime-level segment shifts before they aggregate into a session
    # drawdown. The 2026-05-09 post-mortem revealed pre-reset's apparent
    # +$257 was almost entirely T-tickers (+$24.64 model-driven, with the
    # rest being outage-recovery backfill); B-ticker (1°F bin) BUY NO had
    # been roughly breakeven the whole time. Without this surface, a regime
    # where the bot drifts toward more B-tickers shows up only as a
    # whole-session bleed — exactly the failure mode that triggered the
    # audit. Same exclusions as edge_scatter (no arb, no dry-run, no paper).
    seg_rows = _db_query("""
        SELECT t.ticker, t.action, t.entry_price, t.edge_at_entry,
               r.outcome, r.profit_loss
        FROM trades t JOIN results r ON r.trade_id = t.id
        WHERE r.outcome IN ('yes','no','exited')
          AND t.entry_price IS NOT NULL
          AND t.edge_at_entry IS NOT NULL
          AND r.profit_loss IS NOT NULL
          AND (t.mode IS NULL OR t.mode NOT IN ('dry-run','backfill'))
          AND (t.market_type IS NULL OR t.market_type != 'arbitrage')
          AND (t.notes IS NULL OR t.notes NOT LIKE 'arb%')
          AND (t.paper_trade IS NULL OR t.paper_trade = 0)
    """)

    def _entry_band(p: float) -> str:
        if p < 0.40: return "<0.40"
        if p < 0.60: return "0.40-0.60"
        if p < 0.75: return "0.60-0.75"
        return ">=0.75"

    def _edge_band(e: float) -> str:
        if e < 0.20: return "<0.20"
        if e < 0.35: return "0.20-0.35"
        if e < 0.50: return "0.35-0.50"
        return ">=0.50"

    def _kind(ticker: str) -> str:
        if ticker is None: return "?"
        if "-T" in ticker: return "T (threshold)"
        if "-B" in ticker: return "B (1°F bin)"
        return "other"

    # Bucket key = (kind, action, entry_band, edge_band). Win = directional
    # win on the side we bought; "exited" is counted as a loss for hit-rate
    # purposes (a forced exit is not a model success) but P&L is the actual
    # realized number.
    seg: dict[tuple[str, str, str, str], dict[str, float]] = {}
    for ticker, action, ep, edge, outcome, pnl in seg_rows:
        try:
            ep_f = float(ep); edge_f = float(edge); pnl_f = float(pnl)
        except (TypeError, ValueError):
            continue
        key = (_kind(ticker), action or "?", _entry_band(ep_f), _edge_band(edge_f))
        s = seg.setdefault(key, {"n": 0, "wins": 0, "losses": 0, "exited": 0, "pnl": 0.0})
        s["n"] += 1
        oc = str(outcome).lower()
        if oc == "exited":
            s["exited"] += 1
        elif (action == "BUY YES" and oc == "yes") or (action == "BUY NO" and oc == "no"):
            s["wins"] += 1
        else:
            s["losses"] += 1
        s["pnl"] += pnl_f

    segment_pnl = []
    for (kind, action, eb, edb), s in seg.items():
        resolved = s["wins"] + s["losses"]  # excludes exited from hit-rate denom
        segment_pnl.append({
            "kind": kind, "action": action,
            "entry_band": eb, "edge_band": edb,
            "n": int(s["n"]),
            "wins": int(s["wins"]), "losses": int(s["losses"]),
            "exited": int(s["exited"]),
            "hit_rate": round(s["wins"] / resolved * 100, 1) if resolved else None,
            "pnl": round(s["pnl"], 2),
        })
    # Sort by P&L descending so eyes land on best/worst segments first.
    segment_pnl.sort(key=lambda d: -d["pnl"])

    return jsonify({
        "edge_scatter": edge_scatter,
        "daily_pnl": daily_pnl,
        "city_winrate": city_winrate,
        "daily_cost": daily_cost,
        "edge_calibration": edge_calibration,
        "daily_brier": daily_brier,
        "segment_pnl": segment_pnl,
    })


# ─── HTML page ──────────────────────────────────────────────────────────────
_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>kalshi_bot_2.0</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background:#0b0d12; color:#e6e8ee; margin:0; padding:20px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  h2 { font-size: 14px; text-transform: uppercase; letter-spacing:.08em;
       color:#8891a3; margin: 28px 0 8px; font-weight: 600; }
  .sub { color:#8891a3; font-size: 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
          gap: 12px; margin-top: 14px; }
  .kpi { background: #151924; border-radius: 8px; padding: 14px;
         border: 1px solid #242a3a; }
  .kpi .label { color:#8891a3; font-size:11px; text-transform:uppercase;
                letter-spacing:.06em; margin-bottom:6px; }
  .kpi .value { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .kpi .delta { font-size: 12px; margin-top:4px; font-variant-numeric: tabular-nums; }
  .green  { color:#7ee787; }
  .red    { color:#ff7b72; }
  .amber  { color:#d29922; }
  .muted  { color:#8891a3; }
  .blue   { color:#79c0ff; }
  table { width: 100%; border-collapse: collapse; font-size: 13px;
          background: #151924; border-radius: 8px; overflow: hidden;
          border: 1px solid #242a3a; font-variant-numeric: tabular-nums; }
  th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #242a3a; }
  th { background: #1a1f2e; color:#8891a3; font-weight: 600;
       font-size:11px; text-transform: uppercase; letter-spacing:.06em; }
  tr:last-child td { border-bottom: none; }
  .pill { display:inline-block; padding:2px 8px; border-radius: 4px;
          font-size: 11px; font-weight: 600; }
  .pill-yes   { background:#1f3a2e; color:#7ee787; }
  .pill-no    { background:#3a1f1f; color:#ff7b72; }
  .pill-live  { background:#1f3a2e; color:#7ee787; }
  .pill-halt  { background:#3a1f1f; color:#ff7b72; }
  .pill-maker { background:#1f2e3a; color:#79c0ff; }
  .pill-taker { background:#3a2e1f; color:#d29922; }
  .pill-arb   { background:#2e1f3a; color:#c792ea; }
  .chart-wrap { background:#151924; border-radius:8px; padding:16px;
                border:1px solid #242a3a; }
  .chart-wrap canvas { max-height: 300px; }
  .chart-wrap.city-wr-wrap { height: 460px; }
  .chart-wrap.city-wr-wrap canvas { max-height: 100%; height: 100% !important; }
  .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .row3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }
  @media (max-width: 1100px) { .row3 { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 900px)  { .row2, .row3 { grid-template-columns: 1fr; } }
  .meta { color:#8891a3; font-size:12px; margin-top:6px; }
  .section { margin-top: 28px; }
</style>
</head>
<body>

<h1>kalshi_bot_2.0 <span id="live-pill" class="pill pill-halt">loading</span></h1>
<div class="sub" id="sub">connecting to data...</div>

<h2>Performance</h2>
<div class="grid" id="kpis"></div>

<div class="section row2">
  <div>
    <h2>All open positions (<span id="open-count">-</span>)</h2>
    <div id="positions-wrap"></div>
  </div>
  <div>
    <h2>10 most recently resolved trades</h2>
    <div id="trades-wrap"></div>
  </div>
</div>

<h2>Arb tracker
  <span class="muted" id="arb-summary" style="font-size:0.6em; font-weight:normal;"></span>
</h2>
<div class="section row2">
  <div>
    <h3 style="font-size:0.95em; color:#aaa; margin:6px 0;">Open arb groups (<span id="arb-open-count">0</span>)</h3>
    <div id="arbs-open-wrap"><div class="meta">none yet</div></div>
  </div>
  <div>
    <h3 style="font-size:0.95em; color:#aaa; margin:6px 0;">Recently resolved arb groups</h3>
    <div id="arbs-history-wrap"><div class="meta">none yet</div></div>
  </div>
</div>


<h2>Daily P&amp;L — last 30 days</h2>
<div class="chart-wrap"><canvas id="daily-pnl-chart"></canvas></div>

<h2>Daily Brier score <span class="muted">(lower is better; 0.25 = postmortem halt threshold)</span></h2>
<div class="chart-wrap"><canvas id="daily-brier-chart"></canvas></div>

<div class="section row2" style="margin-top:28px;">
  <div>
    <h2>Edge vs. actual P&amp;L</h2>
    <div class="chart-wrap"><canvas id="edge-scatter-chart"></canvas></div>
  </div>
  <div>
    <h2>Calibration reliability <span class="muted" id="cal-meta"></span></h2>
    <div class="chart-wrap"><canvas id="cal-chart"></canvas></div>
  </div>
</div>

<div class="section row3" style="margin-top:28px;">
  <div>
    <h2>Edge calibration <span class="muted">(win rate vs edge)</span></h2>
    <div class="chart-wrap"><canvas id="edge-cal-chart"></canvas></div>
  </div>
  <div>
    <h2>Win rate by city</h2>
    <div class="chart-wrap city-wr-wrap"><canvas id="city-winrate-chart"></canvas></div>
  </div>
  <div>
    <h2>API cost per day</h2>
    <div class="chart-wrap"><canvas id="daily-cost-chart"></canvas></div>
  </div>
</div>

<h2>Segment P&amp;L <span class="muted">(ticker × entry × edge — see strategy.py 2026-05-09 audit)</span></h2>
<div id="segment-wrap"></div>

<h2>Forecast health — 14-day GFS vs ASOS <span class="muted" id="fhealth-meta"></span></h2>
<div id="fhealth-wrap"></div>

<h2>Last 5 scan cycles</h2>
<div id="scans-wrap"></div>

<script>
const CHART_OPTS = {
  responsive: true,
  maintainAspectRatio: true,
  plugins: { legend: { labels: { color: '#8891a3', boxWidth: 12 } } },
};
const GRID_COLOR  = '#242a3a';
const TICK_COLOR  = '#8891a3';
const AXIS_STYLE  = { ticks: { color: TICK_COLOR }, grid: { color: GRID_COLOR } };

let charts = {};

function destroyChart(id) {
  if (charts[id]) { charts[id].destroy(); delete charts[id]; }
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + ' -> ' + r.status);
  return r.json();
}

function fmtUsd(n) {
  if (n === null || n === undefined || isNaN(n)) return '-';
  return (n >= 0 ? '$' : '-$') + Math.abs(Number(n)).toFixed(2);
}
function fmtPct(n) {
  if (n === null || n === undefined || isNaN(n)) return '-';
  return Number(n).toFixed(1) + '%';
}
function fmtTime(iso) {
  if (!iso) return '-';
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

// ── KPIs ────────────────────────────────────────────────────────────────────
async function refreshKPIs() {
  const k = await getJSON('/api/kpis');
  const pnlClass  = k.total_pnl > 0 ? 'green' : (k.total_pnl < 0 ? 'red' : 'muted');
  const todayClass= k.today_pnl > 0 ? 'green' : (k.today_pnl < 0 ? 'red' : 'muted');
  const ddClass   = k.drawdown_pct > 20 ? 'red' : (k.drawdown_pct > 10 ? 'amber' : 'green');
  const wrClassFor = v => v === null || v === undefined ? 'muted'
                  : v >= 55 ? 'green' : v >= 45 ? 'amber' : 'red';
  const wrClass   = wrClassFor(k.win_rate);

  const pill = document.getElementById('live-pill');
  // Three states: HALTED (runtime risk-halt active, regardless of env flag) /
  // LIVE (env says live AND not halted) / DRY-RUN (env says dry-run).
  // The 2026-05-09 audit caught this: prior pill was env-only, so the user
  // saw "LIVE" while the bot was being blocked every cycle by drawdown.
  let pillState, pillText;
  if (k.risk_halted) {
    pillState = 'pill-halt';
    const reasons = (k.halt_reasons || []).join(', ');
    pillText = reasons ? 'HALTED — ' + reasons : 'HALTED';
  } else if (k.live_trading) {
    pillState = 'pill-live';
    pillText = 'LIVE';
  } else {
    pillState = 'pill-halt';
    pillText = 'DRY-RUN';
  }
  pill.className = 'pill ' + pillState;
  pill.textContent = pillText;

  document.getElementById('sub').textContent =
    'last update ' + fmtTime(k.updated_at) + ' · starting bankroll ' + fmtUsd(k.starting_bankroll);

  const wrStr = k.win_rate !== null
    ? k.win_rate.toFixed(1) + '%'
    : '—';

  document.getElementById('kpis').innerHTML = `
    <div class="kpi"><div class="label">Bankroll</div>
      <div class="value">${fmtUsd(k.bankroll)}</div>
      <div class="delta ${pnlClass}">${k.total_pnl >= 0 ? '+' : ''}${fmtUsd(k.total_pnl)} all-time</div>
    </div>
    <div class="kpi"><div class="label">Today's P&L</div>
      <div class="value ${todayClass}">${k.today_pnl >= 0 ? '+' : ''}${fmtUsd(k.today_pnl)}</div>
    </div>
    <div class="kpi"><div class="label">Cash available</div>
      <div class="value">${fmtUsd(k.cash)}</div>
      <div class="delta muted">${fmtUsd(k.position_value)} in positions (${fmtPct(k.deployed_pct)} deployed)</div>
    </div>
    <div class="kpi"><div class="label">Total win rate <span class="muted" style="font-size:0.7em;">(arb groups bundled as 1)</span></div>
      <div class="value ${wrClass}">${wrStr}</div>
      <div class="delta muted">${k.wins} / ${k.total_trades} resolved</div>
    </div>
    <div class="kpi"><div class="label">BUY YES win rate</div>
      <div class="value ${wrClassFor(k.yes_win_rate)}">${k.yes_win_rate !== null ? k.yes_win_rate.toFixed(1) + '%' : '—'}</div>
      <div class="delta muted">${k.yes_wins} / ${k.yes_total} resolved</div>
      <div class="delta ${k.yes_pnl >= 0 ? 'green' : 'red'}">P&L ${k.yes_pnl >= 0 ? '+' : ''}${fmtUsd(k.yes_pnl)}</div>
    </div>
    <div class="kpi"><div class="label">BUY NO win rate</div>
      <div class="value ${wrClassFor(k.no_win_rate)}">${k.no_win_rate !== null ? k.no_win_rate.toFixed(1) + '%' : '—'}</div>
      <div class="delta muted">${k.no_wins} / ${k.no_total} resolved</div>
      <div class="delta ${k.no_pnl >= 0 ? 'green' : 'red'}">P&L ${k.no_pnl >= 0 ? '+' : ''}${fmtUsd(k.no_pnl)}</div>
    </div>
    <div class="kpi"><div class="label">Maker win rate</div>
      <div class="value ${wrClassFor(k.maker_win_rate)}">${k.maker_win_rate !== null ? k.maker_win_rate.toFixed(1) + '%' : '—'}</div>
      <div class="delta muted">${k.maker_wins} / ${k.maker_total} resolved</div>
      <div class="delta ${k.maker_pnl >= 0 ? 'green' : 'red'}">P&L ${k.maker_pnl >= 0 ? '+' : ''}${fmtUsd(k.maker_pnl)}</div>
    </div>
    <div class="kpi"><div class="label">Taker win rate</div>
      <div class="value ${wrClassFor(k.taker_win_rate)}">${k.taker_win_rate !== null ? k.taker_win_rate.toFixed(1) + '%' : '—'}</div>
      <div class="delta muted">${k.taker_wins} / ${k.taker_total} resolved</div>
      <div class="delta ${k.taker_pnl >= 0 ? 'green' : 'red'}">P&L ${k.taker_pnl >= 0 ? '+' : ''}${fmtUsd(k.taker_pnl)}</div>
    </div>
    <div class="kpi"><div class="label">Arb win rate <span class="muted" style="font-size:0.7em;">(group-bundled)</span></div>
      <div class="value ${wrClassFor(k.arb_win_rate)}">${k.arb_win_rate !== null ? k.arb_win_rate.toFixed(1) + '%' : '—'}</div>
      <div class="delta muted">${k.arb_groups_won} / ${k.arb_groups_resolved} groups (${k.arb_groups_open} open${k.arb_stranded_legs > 0 ? ', ' + k.arb_stranded_legs + ' stranded leg' + (k.arb_stranded_legs === 1 ? '' : 's') : ''})</div>
      <div class="delta ${k.arb_realized_pnl >= 0 ? 'green' : 'red'}">P&L ${k.arb_realized_pnl >= 0 ? '+' : ''}${fmtUsd(k.arb_realized_pnl)}</div>
    </div>
    <div class="kpi"><div class="label">Peak bankroll · drawdown</div>
      <div class="value">${fmtUsd(k.peak_bankroll)}</div>
      <div class="delta muted">peak P&L ${k.peak_pnl >= 0 ? '+' : ''}${fmtUsd(k.peak_pnl)}</div>
      <div class="delta ${ddClass}">drawdown ${fmtPct(k.drawdown_pct)} (halt 33%)</div>
    </div>
    <div class="kpi"><div class="label">API cost today</div>
      <div class="value blue">$${Number(k.cost_today).toFixed(4)}</div>
      <div class="delta muted">$${Number(k.monthly_projection).toFixed(2)} / mo projected</div>
    </div>
  `;
}

// ── Tables ───────────────────────────────────────────────────────────────────
function mkTable(headers, rows) {
  if (!rows.length) return '<div class="meta">none yet</div>';
  const h = headers.map(x => '<th>' + x + '</th>').join('');
  const b = rows.map(r => '<tr>' + r.map(c => '<td>' + c + '</td>').join('') + '</tr>').join('');
  return '<table><thead><tr>' + h + '</tr></thead><tbody>' + b + '</tbody></table>';
}

async function refreshPositions() {
  const pos = await getJSON('/api/positions');
  document.getElementById('open-count').textContent = pos.length;
  const rows = pos.map(p => {
    const actionPill = p.action === 'BUY YES'
      ? '<span class="pill pill-yes">YES</span>'
      : '<span class="pill pill-no">NO</span>';
    const modePill = p.is_arb
      ? '<span class="pill pill-arb" title="' + (p.arb_id || 'arb leg') + '">arb</span>'
      : p.mode === 'maker'
      ? '<span class="pill pill-maker">maker</span>'
      : p.mode === 'taker' ? '<span class="pill pill-taker">taker</span>'
      : '<span class="pill muted">' + (p.mode || '-') + '</span>';
    const edge = p.edge_at_entry !== null ? (p.edge_at_entry * 100).toFixed(1) + '%' : '-';
    let timeLeft = '-';
    if (p.secs_to_settle !== null && p.secs_to_settle !== undefined) {
      const s = p.secs_to_settle;
      if (s < 0) { timeLeft = 'pending'; }
      else if (s < 3600) { timeLeft = Math.floor(s / 60) + 'm'; }
      else if (s < 86400) {
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
        timeLeft = m > 0 ? h + 'h ' + m + 'm' : h + 'h';
      } else {
        const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
        timeLeft = h > 0 ? d + 'd ' + h + 'h' : d + 'd';
      }
    }
    let placedAt = '-';
    if (p.opened_at) {
      const d = new Date(p.opened_at.endsWith('Z') ? p.opened_at : p.opened_at + 'Z');
      placedAt = d.toLocaleString(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
    }
    return [placedAt, p.city || p.ticker, actionPill, modePill, fmtUsd(p.size_usd),
            p.entry_price !== null ? '$' + Number(p.entry_price).toFixed(2) : '-', edge, timeLeft];
  });
  document.getElementById('positions-wrap').innerHTML = mkTable(
    ['Placed', 'Market', 'Side', 'Mode', 'Size', 'Entry', 'Edge', 'Settles'], rows
  );
}

async function refreshTrades() {
  const tr = await getJSON('/api/trades');
  const rows = tr.map(t => {
    const outcome  = String(t.outcome || '').toLowerCase();
    const pnlClass = t.profit_loss > 0 ? 'green' : (t.profit_loss < 0 ? 'red' : 'muted');
    const actionPill = t.action === 'BUY YES'
      ? '<span class="pill pill-yes">YES</span>'
      : '<span class="pill pill-no">NO</span>';
    const slices = t.slice_count > 1 ? ` <span class="meta">(${t.slice_count} slices)</span>` : '';
    const statusLabel = t.status === 'exited' ? 'EXITED' : outcome.toUpperCase();
    return [
      (t.city || t.ticker) + slices, actionPill, fmtUsd(t.size_usd),
      statusLabel,
      '<span class="' + pnlClass + '">' + (t.profit_loss >= 0 ? '+' : '') + fmtUsd(t.profit_loss) + '</span>',
      fmtTime(t.resolved_at),
    ];
  });
  document.getElementById('trades-wrap').innerHTML = mkTable(
    ['Market', 'Side', 'Size', 'Status', 'P&L', 'Resolved'], rows
  );
}

async function refreshScans() {
  const scans = await getJSON('/api/scans');
  const rows = scans.map(s => {
    const bd = s.breakdown || {};
    const keys = Object.keys(bd);
    const bdStr = keys.length ? keys.map(k => k + ':' + bd[k]).join(', ') : '-';
    return [
      fmtTime(s.cycle_at), s.markets_scanned, s.opportunities, s.trades_placed,
      '$' + Number(s.claude_cost_usd || 0).toFixed(4),
      '<span class="meta">' + bdStr + '</span>',
    ];
  });
  document.getElementById('scans-wrap').innerHTML = mkTable(
    ['When', 'Markets', 'Opps', 'Placed', 'Claude $', 'Breakdown'], rows
  );
}

// ── Charts ───────────────────────────────────────────────────────────────────
async function refreshCalibration() {
  const cal = await getJSON('/api/calibration');
  const meta = cal.meta || {};
  const parts = [];
  if (cal.n_resolved) parts.push(cal.n_resolved + ' resolved');
  if (meta.brier_before !== undefined) parts.push('brier before ' + meta.brier_before);
  if (meta.brier_after  !== undefined) parts.push('brier after '  + meta.brier_after);
  if (meta.shrinkage_factor !== undefined) parts.push('shrinkage ' + meta.shrinkage_factor);
  document.getElementById('cal-meta').textContent =
    parts.length ? '(' + parts.join(' · ') + ')' : '(identity — no fit yet)';

  const dataPoints = cal.points.map(p => ({
    x: p.predicted, y: p.actual,
    r: Math.max(4, Math.min(20, Math.sqrt(p.n) * 2))
  }));
  destroyChart('cal');
  charts['cal'] = new Chart(document.getElementById('cal-chart').getContext('2d'), {
    type: 'bubble',
    data: {
      datasets: [
        { label: 'Actual win rate (size = n)', data: dataPoints,
          backgroundColor: 'rgba(126,231,135,0.55)', borderColor: '#7ee787' },
        { label: 'Perfect calibration', type: 'line',
          data: [{x:0,y:0},{x:1,y:1}], showLine:true, pointRadius:0,
          borderColor:'#8891a3', borderDash:[4,4], borderWidth:1, fill:false },
      ],
    },
    options: {
      ...CHART_OPTS,
      scales: {
        x: { ...AXIS_STYLE, min:0, max:1, title:{ display:true, text:'Predicted P(win)', color:TICK_COLOR } },
        y: { ...AXIS_STYLE, min:0, max:1, title:{ display:true, text:'Actual win rate',  color:TICK_COLOR } },
      },
    },
  });
}

async function refreshAnalytics() {
  const data = await getJSON('/api/analytics');

  // ── Segment P&L table ──────────────────────────────────────────────────
  // Cross-tab BUY-side P&L by ticker_kind × entry_band × edge_band — see
  // strategy.py 2026-05-09 audit. Renders nothing if empty (fresh DB).
  const seg = data.segment_pnl || [];
  if (seg.length) {
    const segRows = seg.map(s => {
      const pnlClass = s.pnl >= 0 ? 'good' : 'bad';
      const pnlStr = (s.pnl >= 0 ? '+' : '') + fmtUsd(s.pnl);
      const hr = s.hit_rate === null ? '-' : s.hit_rate.toFixed(0) + '%';
      return [
        s.kind, s.action, s.entry_band, s.edge_band,
        s.n, s.wins, s.losses, s.exited, hr,
        '<span class="' + pnlClass + '">' + pnlStr + '</span>',
      ];
    });
    document.getElementById('segment-wrap').innerHTML = mkTable(
      ['Ticker', 'Side', 'Entry', 'Edge', 'n', 'W', 'L', 'Exited', 'Hit %', 'P&L'],
      segRows,
    );
  } else {
    document.getElementById('segment-wrap').innerHTML =
      '<p class="muted">No resolved trades yet.</p>';
  }

  // ── Daily P&L bar chart ────────────────────────────────────────────────
  const dpDates = data.daily_pnl.map(d => d.date);
  const dpVals  = data.daily_pnl.map(d => d.pnl);
  const dpColors = dpVals.map(v => v >= 0 ? 'rgba(126,231,135,0.7)' : 'rgba(255,123,114,0.7)');
  destroyChart('dailyPnl');
  charts['dailyPnl'] = new Chart(
    document.getElementById('daily-pnl-chart').getContext('2d'), {
    type: 'bar',
    data: {
      labels: dpDates,
      datasets: [{ label: 'P&L', data: dpVals, backgroundColor: dpColors, borderRadius: 3 }],
    },
    options: {
      ...CHART_OPTS,
      plugins: { ...CHART_OPTS.plugins, legend: { display: false } },
      scales: {
        x: { ...AXIS_STYLE, title:{ display:true, text:'Date', color:TICK_COLOR } },
        y: { ...AXIS_STYLE, title:{ display:true, text:'P&L ($)', color:TICK_COLOR } },
      },
    },
  });

  // ── Daily Brier score bar chart ────────────────────────────────────────
  const db = data.daily_brier || [];
  const dbDates  = db.map(d => d.date);
  const dbVals   = db.map(d => d.brier);
  const dbNs     = db.map(d => d.n);
  // §9.2: Brier > 0.25 is the postmortem's halt threshold. Color bars red
  // when over the line, amber 0.20–0.25, green under.
  const dbColors = dbVals.map(v =>
        v >= 0.25 ? 'rgba(255,123,114,0.85)'
      : v >= 0.20 ? 'rgba(210,153,34,0.75)'
                  : 'rgba(126,231,135,0.7)');
  destroyChart('dailyBrier');
  charts['dailyBrier'] = new Chart(
    document.getElementById('daily-brier-chart').getContext('2d'), {
    type: 'bar',
    data: {
      labels: dbDates,
      datasets: [
        { label: 'Brier', data: dbVals, backgroundColor: dbColors,
          borderRadius: 3, order: 2 },
        { label: 'Halt threshold (0.25)', type: 'line',
          data: dbDates.map(() => 0.25),
          borderColor: '#ff7b72', borderDash: [4,4], borderWidth: 1,
          pointRadius: 0, fill: false, order: 1 },
      ],
    },
    options: {
      ...CHART_OPTS,
      plugins: {
        ...CHART_OPTS.plugins,
        tooltip: {
          callbacks: {
            label: (ctx) => {
              if (ctx.datasetIndex !== 0) return ` halt: 0.25`;
              const i = ctx.dataIndex;
              return ` brier ${dbVals[i].toFixed(3)}  (n=${dbNs[i]})`;
            },
          },
        },
      },
      scales: {
        x: { ...AXIS_STYLE, title:{ display:true, text:'Date', color:TICK_COLOR } },
        y: { ...AXIS_STYLE, min: 0,
             suggestedMax: 0.35,
             title:{ display:true, text:'Brier score', color:TICK_COLOR } },
      },
    },
  });

  // ── Edge vs P&L scatter ────────────────────────────────────────────────
  const scatterPts = data.edge_scatter.map(d => ({ x: d.edge, y: d.pnl }));
  destroyChart('edgeScatter');
  charts['edgeScatter'] = new Chart(
    document.getElementById('edge-scatter-chart').getContext('2d'), {
    type: 'scatter',
    data: {
      datasets: [{
        label: 'Trade (edge % → P&L $)',
        data: scatterPts,
        backgroundColor: 'rgba(121,192,255,0.5)',
        borderColor: '#79c0ff',
        pointRadius: 4,
      }],
    },
    options: {
      ...CHART_OPTS,
      scales: {
        x: { ...AXIS_STYLE, title:{ display:true, text:'Edge at entry (%)', color:TICK_COLOR } },
        y: { ...AXIS_STYLE, title:{ display:true, text:'Actual P&L ($)',    color:TICK_COLOR } },
      },
    },
  });

  // ── Per-city win rate bar ──────────────────────────────────────────────
  // Cities with no resolved binary trades render at 0 with a neutral grey bar
  // so the gap is visible (rather than silently omitting the city).
  const cities = data.city_winrate.map(d => d.city);
  const wrVals  = data.city_winrate.map(d => d.win_rate === null ? 0 : d.win_rate);
  const totals  = data.city_winrate.map(d => d.total);
  const wrColors = data.city_winrate.map(d =>
        d.win_rate === null ? 'rgba(136,145,163,0.35)'
      : d.win_rate >= 55    ? 'rgba(126,231,135,0.7)'
      : d.win_rate >= 45    ? 'rgba(210,153,34,0.7)'
                            : 'rgba(255,123,114,0.7)');
  // Inline plugin: draw "n=N · ±$P.PP" to the right of each horizontal bar.
  const cityRowLabels = {
    id: 'cityRowLabels',
    afterDatasetsDraw(chart) {
      const ctx = chart.ctx;
      const meta = chart.getDatasetMeta(0);
      ctx.save();
      ctx.font = '11px ui-sans-serif, system-ui, sans-serif';
      ctx.textBaseline = 'middle';
      ctx.textAlign = 'left';
      meta.data.forEach((bar, i) => {
        const row = data.city_winrate[i];
        if (!row) return;
        let txt;
        if (row.total === 0) {
          txt = 'n=0';
        } else {
          const sign = row.pnl >= 0 ? '+$' : '-$';
          txt = `n=${row.total} · ${sign}${Math.abs(row.pnl).toFixed(2)}`;
        }
        ctx.fillStyle = row.total === 0 ? '#8891a3'
                      : row.pnl >  0    ? '#7ee787'
                      : row.pnl <  0    ? '#ff7b72'
                                        : '#c9d1d9';
        // bar.x is the right edge of the bar in horizontal mode.
        ctx.fillText(txt, bar.x + 6, bar.y);
      });
      ctx.restore();
    },
  };
  destroyChart('cityWinrate');
  charts['cityWinrate'] = new Chart(
    document.getElementById('city-winrate-chart').getContext('2d'), {
    type: 'bar',
    plugins: [cityRowLabels],
    data: {
      labels: cities,
      datasets: [{
        label: 'Win rate %',
        data: wrVals,
        backgroundColor: wrColors,
        borderRadius: 3,
      }],
    },
    options: {
      ...CHART_OPTS,
      maintainAspectRatio: false,
      indexAxis: 'y',
      layout: { padding: { right: 110 } },
      plugins: {
        ...CHART_OPTS.plugins,
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const i = ctx.dataIndex;
              const wr = data.city_winrate[i].win_rate;
              if (wr === null) return ` no resolved trades`;
              return ` ${wr.toFixed(1)}%  (n=${totals[i]})`;
            },
          },
        },
      },
      scales: {
        x: { ...AXIS_STYLE, min:0, max:100, title:{ display:true, text:'Win rate (%)', color:TICK_COLOR } },
        y: { ...AXIS_STYLE, ticks: { ...AXIS_STYLE.ticks, autoSkip: false } },
      },
    },
  });

  // ── Edge calibration: realized win rate by 5% edge bucket ──────────────
  const ec = data.edge_calibration || [];
  const ecLabels = ec.map(d => d.label);
  const ecVals   = ec.map(d => d.win_rate === null ? 0 : d.win_rate);
  const ecNs     = ec.map(d => d.n);
  const ecMids   = ec.map(d => d.mid_pct);
  // Perfect calibration baseline: a true edge of E% on a 50¢ market yields
  // win rate (50 + E)%. Plotted at each bucket midpoint.
  const refData  = ec.map(d => Math.min(100, 50 + d.mid_pct));
  const ecColors = ec.map(d =>
        d.win_rate === null            ? 'rgba(136,145,163,0.35)'
      : d.win_rate >= (50 + d.mid_pct) ? 'rgba(126,231,135,0.7)'
                                       : 'rgba(255,123,114,0.7)');
  destroyChart('edgeCal');
  charts['edgeCal'] = new Chart(
    document.getElementById('edge-cal-chart').getContext('2d'), {
    type: 'bar',
    data: {
      labels: ecLabels,
      datasets: [
        { label: 'Realized win rate', data: ecVals,
          backgroundColor: ecColors, borderRadius: 3, order: 2 },
        { label: 'Perfect calibration (50% + edge)', type: 'line',
          data: refData, borderColor: '#8891a3', borderDash: [4,4],
          borderWidth: 1, pointRadius: 0, fill: false, order: 1 },
      ],
    },
    options: {
      ...CHART_OPTS,
      plugins: {
        ...CHART_OPTS.plugins,
        tooltip: {
          callbacks: {
            label: (ctx) => {
              if (ctx.datasetIndex !== 0) {
                return ` perfect: ${ctx.parsed.y.toFixed(1)}%`;
              }
              const i = ctx.dataIndex;
              const wr = ec[i].win_rate;
              if (wr === null) return ` no trades in ${ec[i].label}`;
              return ` ${ec[i].label}: ${wr.toFixed(1)}%  (n=${ecNs[i]})`;
            },
          },
        },
      },
      scales: {
        x: { ...AXIS_STYLE,
             title:{ display:true, text:'Predicted edge bucket', color:TICK_COLOR } },
        y: { ...AXIS_STYLE, min: 0, max: 100,
             title:{ display:true, text:'Realized win rate (%)', color:TICK_COLOR } },
      },
    },
  });

  // ── Daily API cost bar ─────────────────────────────────────────────────
  const costDates = data.daily_cost.map(d => d.date);
  const costVals  = data.daily_cost.map(d => d.cost);
  destroyChart('dailyCost');
  charts['dailyCost'] = new Chart(
    document.getElementById('daily-cost-chart').getContext('2d'), {
    type: 'bar',
    data: {
      labels: costDates,
      datasets: [{
        label: 'Claude API cost ($)',
        data: costVals,
        backgroundColor: 'rgba(121,192,255,0.6)',
        borderRadius: 3,
      }],
    },
    options: {
      ...CHART_OPTS,
      plugins: { ...CHART_OPTS.plugins, legend: { display: false } },
      scales: {
        x: { ...AXIS_STYLE, title:{ display:true, text:'Date', color:TICK_COLOR } },
        y: { ...AXIS_STYLE, title:{ display:true, text:'Cost ($)', color:TICK_COLOR } },
      },
    },
  });
}

// ── Forecast health table ─────────────────────────────────────────────────────
async function refreshForecastHealth() {
  const data = await getJSON('/api/forecast_health');
  const cities = data.cities || {};
  const meta = data.computed_at
    ? `(computed ${data.computed_at} · window ${data.window_days}d · `
      + `alert if MAE>${data.mae_threshold}°F | |bias|>${data.bias_threshold}°F | RMSE>${data.rmse_threshold}°F)`
    : '(not yet computed — starts on next bot cycle)';
  document.getElementById('fhealth-meta').textContent = meta;

  const cityKeys = Object.keys(cities).sort();
  if (!cityKeys.length) {
    document.getElementById('fhealth-wrap').innerHTML =
      '<div class="meta">No data yet — health file will be written on first bot cycle.</div>';
    return;
  }

  const headers = ['City', 'n', 'MAE (°F)', 'Bias (°F)', 'RMSE (°F)', 'Status'];
  const rows = cityKeys.map(city => {
    const c = cities[city];
    if (c.error) return [city, '—', '—', '—', '—',
      '<span class="pill pill-halt">ERR</span>'];
    const fmt = v => v !== null && v !== undefined ? Number(v).toFixed(2) : '—';
    const maeClass = c.mae > data.mae_threshold ? 'red'
                   : c.mae > data.mae_threshold * 0.75 ? 'amber' : 'green';
    const biasClass = Math.abs(c.bias) > data.bias_threshold ? 'red'
                    : Math.abs(c.bias) > data.bias_threshold * 0.75 ? 'amber' : 'muted';
    const rmseClass = c.rmse > data.rmse_threshold ? 'red'
                    : c.rmse > data.rmse_threshold * 0.75 ? 'amber' : 'green';
    const pill = c.alert
      ? `<span class="pill pill-halt" title="${(c.alert_reasons||[]).join(', ')}">ALERT — skipped</span>`
      : '<span class="pill pill-live">OK</span>';
    return [
      city,
      c.n,
      `<span class="${maeClass}">${fmt(c.mae)}</span>`,
      `<span class="${biasClass}">${c.bias >= 0 ? '+' : ''}${fmt(c.bias)}</span>`,
      `<span class="${rmseClass}">${fmt(c.rmse)}</span>`,
      pill,
    ];
  });
  document.getElementById('fhealth-wrap').innerHTML = mkTable(headers, rows);
}

async function refreshArbs() {
  const j = await getJSON('/api/arbs');
  const s = j.summary || {};

  // Header summary line
  const wr = (s.win_rate == null) ? '—' : (s.win_rate * 100).toFixed(1) + '%';
  const stranded = s.stranded_legs > 0
    ? ` · <span class="red">${s.stranded_legs} stranded leg${s.stranded_legs === 1 ? '' : 's'}</span>`
    : '';
  document.getElementById('arb-summary').innerHTML =
    `${s.groups_resolved} resolved (win rate ${wr}) · ${s.groups_open} open ` +
    `· realized ${s.realized_pnl >= 0 ? '+' : ''}${fmtUsd(s.realized_pnl)}` +
    stranded;
  document.getElementById('arb-open-count').textContent = s.groups_open || 0;

  // Open arb groups
  const openWrap = document.getElementById('arbs-open-wrap');
  if (!j.open_groups || j.open_groups.length === 0) {
    openWrap.innerHTML = '<div class="meta">none open</div>';
  } else {
    const headers = ['Arb id', 'Legs', 'Unresolved', 'Cost', 'Opened'];
    const rows = j.open_groups.map(g => [
      '<code style="font-size:0.8em">' + (g.arb_id || '').slice(0, 36) + '</code>',
      g.legs,
      g.unresolved_legs,
      fmtUsd(g.group_cost),
      g.opened_at ? fmtTime(g.opened_at) : '-',
    ]);
    openWrap.innerHTML = mkTable(headers, rows);
  }

  // Resolved history
  const histWrap = document.getElementById('arbs-history-wrap');
  if (!j.history || j.history.length === 0) {
    histWrap.innerHTML = '<div class="meta">none yet</div>';
  } else {
    const headers = ['Arb id', 'City', 'Legs', 'Cost', 'P&L', 'Result', 'Resolved'];
    const rows = j.history.map(h => {
      const pnlClass = h.group_pnl > 0 ? 'green' : (h.group_pnl < 0 ? 'red' : 'muted');
      const resultPill = h.won
        ? '<span class="pill pill-yes">WIN</span>'
        : '<span class="pill pill-no">LOSS</span>';
      return [
        '<code style="font-size:0.8em">' + (h.arb_id || '').slice(0, 30) + '</code>',
        h.city || '-',
        h.legs,
        fmtUsd(h.group_cost),
        '<span class="' + pnlClass + '">' + (h.group_pnl >= 0 ? '+' : '') + fmtUsd(h.group_pnl) + '</span>',
        resultPill,
        fmtTime(h.resolved_at),
      ];
    });
    histWrap.innerHTML = mkTable(headers, rows);
  }
}

// ── Orchestration ─────────────────────────────────────────────────────────────
async function refreshAll() {
  try {
    await Promise.all([
      refreshKPIs(), refreshPositions(), refreshTrades(),
      refreshScans(), refreshCalibration(), refreshAnalytics(),
      refreshForecastHealth(), refreshArbs(),
    ]);
  } catch (e) { console.error('refresh failed', e); }
}

refreshAll();
setInterval(refreshAll, 15000);
</script>
</body>
</html>
"""


@app.route("/api/polymarket/markets")
def api_polymarket_markets():
    """Phase-1 read-only ingest snapshot. Written by main._ingest_polymarket()."""
    if not os.path.exists(_POLYMARKET_SNAPSHOT_FILE):
        return jsonify({"fetched_at": None, "count": 0, "markets": []})
    try:
        with open(_POLYMARKET_SNAPSHOT_FILE) as f:
            return jsonify(json.load(f))
    except Exception as e:
        logging.warning("[DASH] polymarket snapshot read failed: %s", e)
        return jsonify({"fetched_at": None, "count": 0, "markets": [],
                        "error": str(e)})


@app.route("/api/polymarket/pnl")
def api_polymarket_pnl():
    """Phase-3 paper-trading P&L for Polymarket. Aggregated from the
    trades+results tables filtered to venue='polymarket' AND paper_trade=1."""
    import storage
    pnl = storage.get_venue_pnl("polymarket", paper_only=True)
    pnl["paper_orders"] = storage.paper_order_stats("polymarket")
    return jsonify(pnl)


@app.route("/api/polymarket/pending")
def api_polymarket_pending():
    """Phase-3b: pending paper maker orders awaiting fill. Limits opp_json
    payload to keep response small."""
    import storage
    rows = storage.get_pending_paper_orders(venue="polymarket")
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "market_id": r["market_id"],
            "action": r["action"],
            "side": r["side"],
            "limit_price": r["limit_price"],
            "target_contracts": r["target_contracts"],
            "edge_at_post": r["edge_at_post"],
            "posted_at": r["posted_at"],
            "expires_at": r["expires_at"],
        })
    return jsonify({"count": len(out), "orders": out})


_POLYMARKET_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8">
<title>Polymarket — phase-1 read-only ingest</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; margin: 24px;
         background: #0e1117; color: #e6e6e6; }
  h1 { font-size: 1.4em; margin-bottom: 4px; }
  .meta { color: #888; font-size: 0.85em; margin-bottom: 16px; }
  .banner { background: #1f2937; border-left: 3px solid #fbbf24;
            padding: 12px 16px; margin-bottom: 20px; font-size: 0.9em; }
  table { border-collapse: collapse; width: 100%; font-size: 0.85em; }
  th, td { padding: 6px 10px; border-bottom: 1px solid #222;
           text-align: left; vertical-align: top; }
  th { background: #1a1d24; color: #aaa; font-weight: 600; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  a { color: #6db7ff; }
</style></head>
<body>
<h1>Polymarket — read-only ingest</h1>
<div class="meta">
  <a href="/">&larr; Kalshi dashboard</a>
  <a href="/cross" style="margin-left:16px;">Cross-venue arb</a>
</div>
<div class="banner">
  <strong>Phase 3b — paper maker active.</strong> Strategy scores Polymarket
  markets, paper executor posts virtual maker orders 1¢ inside the spread,
  and maker_sim resolves them on subsequent cycles when the book actually
  crosses the limit. Conservative: requires best_ask STRICTLY BELOW limit
  for fill (queue priority assumed worst-case). Filled trades persist with
  <code>paper_trade=1, mode='paper:maker'</code>; reconcile grades them when
  Polymarket's UMA oracle resolves. <strong>No real Polymarket orders are
  placed</strong> — execution requires a Polygon wallet.
</div>
<div id="pnl-panel" style="display:flex;gap:24px;margin-bottom:12px;
     padding:14px 18px;background:#1a1d24;border-radius:6px;font-size:0.9em;
     flex-wrap:wrap;">
  <div>Paper trades: <strong id="p-trades">…</strong></div>
  <div>Resolved: <strong id="p-resolved">…</strong></div>
  <div>Win rate: <strong id="p-wr">…</strong></div>
  <div>Realized P&L: <strong id="p-pnl">…</strong></div>
  <div>Open positions: <strong id="p-open">…</strong></div>
  <div>|</div>
  <div>Pending makers: <strong id="p-pending">…</strong></div>
  <div>Filled lifetime: <strong id="p-filled">…</strong></div>
  <div>Expired lifetime: <strong id="p-expired">…</strong></div>
</div>
<details style="margin-bottom:20px;background:#1a1d24;border-radius:6px;
         padding:10px 16px;font-size:0.85em;">
  <summary style="cursor:pointer;color:#aaa;">Pending paper maker orders
   (<span id="pending-n">0</span>)</summary>
  <table style="margin-top:10px;width:100%;"><thead><tr>
    <th>Order</th><th>Market</th><th>Side</th><th class="num">Limit</th>
    <th class="num">Size</th><th class="num">Edge</th>
    <th>Posted</th><th>Expires</th>
  </tr></thead><tbody id="pending-tbody"></tbody></table>
</details>
<div class="meta" id="meta">Loading…</div>
<table id="markets"><thead><tr>
  <th>City</th><th>Question</th>
  <th>Resolution source</th>
  <th>Rule</th>
  <th class="num">YES</th><th class="num">NO</th>
  <th>Closes</th>
</tr></thead><tbody></tbody></table>
<script>
function fmtRule(m) {
  if (m.comparator === 'in_range') return `${m.range_low}–${m.range_high}`;
  if (m.comparator && m.threshold != null) return `${m.comparator} ${m.threshold}`;
  return '—';
}
function fmtPrice(p) {
  return (p == null) ? '—' : (p < 1 ? p.toFixed(2) : p.toFixed(0));
}
async function loadPnl() {
  const r = await fetch('/api/polymarket/pnl');
  const j = await r.json();
  document.getElementById('p-trades').textContent = j.trades;
  document.getElementById('p-resolved').textContent = j.resolved;
  document.getElementById('p-wr').textContent =
    j.win_rate == null ? '—' : (j.win_rate * 100).toFixed(1) + '%';
  const pnl = j.realized_pnl;
  const el = document.getElementById('p-pnl');
  el.textContent = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2);
  el.style.color = pnl >= 0 ? '#48bb78' : '#fc8181';
  document.getElementById('p-open').textContent = j.open_positions;
  const po = j.paper_orders || {};
  document.getElementById('p-pending').textContent = po.pending || 0;
  document.getElementById('p-filled').textContent = po.filled || 0;
  document.getElementById('p-expired').textContent = po.expired || 0;
}
async function loadPending() {
  const r = await fetch('/api/polymarket/pending');
  const j = await r.json();
  document.getElementById('pending-n').textContent = j.count;
  const tbody = document.getElementById('pending-tbody');
  tbody.innerHTML = '';
  for (const o of (j.orders || [])) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>#${o.id}</td>
      <td><code style="font-size:0.8em">${(o.market_id || '').slice(0, 16)}…</code></td>
      <td>${o.action}</td>
      <td class="num">$${o.limit_price.toFixed(4)}</td>
      <td class="num">${o.target_contracts}</td>
      <td class="num">${(o.edge_at_post * 100).toFixed(1)}¢</td>
      <td>${o.posted_at}</td>
      <td>${o.expires_at}</td>`;
    tbody.appendChild(tr);
  }
}
async function load() {
  loadPnl();
  loadPending();
  const r = await fetch('/api/polymarket/markets');
  const j = await r.json();
  document.getElementById('meta').textContent =
    j.fetched_at
      ? `${j.count} markets · last fetched ${j.fetched_at}`
      : 'No snapshot yet — bot has not completed a cycle.';
  const tbody = document.querySelector('#markets tbody');
  tbody.innerHTML = '';
  for (const m of (j.markets || [])) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${m.city || ''}</td>
      <td>${m.question || ''}</td>
      <td><code>${m.resolution_source || ''}</code></td>
      <td>${fmtRule(m)}</td>
      <td class="num">${fmtPrice(m.yes_price)}</td>
      <td class="num">${fmtPrice(m.no_price)}</td>
      <td>${m.close_time || ''}</td>`;
    tbody.appendChild(tr);
  }
}
load();
setInterval(load, 30000);
</script>
</body></html>
"""


@app.route("/polymarket")
def polymarket_page():
    return render_template_string(_POLYMARKET_HTML)


@app.route("/api/cross/arbs")
def api_cross_arbs():
    """Phase-2 cross-venue arb detections. Detection-only, not executed."""
    if not os.path.exists(_CROSS_VENUE_SNAPSHOT_FILE):
        return jsonify({"fetched_at": None, "count": 0, "opportunities": []})
    try:
        with open(_CROSS_VENUE_SNAPSHOT_FILE) as f:
            return jsonify(json.load(f))
    except Exception as e:
        logging.warning("[DASH] cross-venue snapshot read failed: %s", e)
        return jsonify({"fetched_at": None, "count": 0, "opportunities": [],
                        "error": str(e)})


_CROSS_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8">
<title>Cross-venue arb — phase-2 detection</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; margin: 24px;
         background: #0e1117; color: #e6e6e6; }
  h1 { font-size: 1.4em; margin-bottom: 4px; }
  .meta { color: #888; font-size: 0.85em; margin-bottom: 16px; }
  .banner { background: #1f2937; border-left: 3px solid #fbbf24;
            padding: 12px 16px; margin-bottom: 20px; font-size: 0.9em; }
  .nav a { margin-right: 16px; color: #6db7ff; }
  table { border-collapse: collapse; width: 100%; font-size: 0.85em;
          margin-top: 16px; }
  th, td { padding: 8px 10px; border-bottom: 1px solid #222;
           text-align: left; vertical-align: top; }
  th { background: #1a1d24; color: #aaa; font-weight: 600; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .edge-positive { color: #48bb78; font-weight: 600; }
  .empty { padding: 40px; text-align: center; color: #666;
           background: #1a1d24; border-radius: 6px; }
  code { font-size: 0.85em; color: #d4a373; }
  .leg { font-size: 0.8em; color: #aaa; }
</style></head>
<body>
<h1>Cross-venue arbitrage</h1>
<div class="nav meta">
  <a href="/">&larr; Kalshi dashboard</a>
  <a href="/polymarket">Polymarket markets</a>
</div>
<div class="banner">
  <strong>Phase 2: detection-only.</strong> Polymarket execution is not built;
  any opportunity here would have to be acted on manually. Detection is exact:
  both legs must have identical canonical resolution rules (source, comparator,
  threshold, target date). Edge is fee-inclusive on the Kalshi side; Polymarket
  fee = 0 today. This data is fresh from <code>data/cross_venue_arb.json</code>.
</div>
<div class="meta" id="meta">Loading…</div>
<div id="container"></div>
<script>
function fmtRule(o) {
  if (o.comparator === 'in_range') return `${o.range_low}–${o.range_high}°F`;
  if (o.comparator && o.threshold != null) return `${o.comparator} ${o.threshold}°F`;
  return '—';
}
function fmtPrice(p) { return (p == null) ? '—' : p.toFixed(3); }
async function load() {
  const r = await fetch('/api/cross/arbs');
  const j = await r.json();
  document.getElementById('meta').textContent =
    j.fetched_at
      ? `${j.count} arb opportunit${j.count === 1 ? 'y' : 'ies'} · last fetched ${j.fetched_at}`
      : 'No snapshot yet — bot has not completed a cycle.';
  const c = document.getElementById('container');
  if (!j.opportunities || j.opportunities.length === 0) {
    c.innerHTML = '<div class="empty">No cross-venue arb opportunities detected this cycle. ' +
                  'Markets must have identical canonical resolution rules to pair.</div>';
    return;
  }
  let html = '<table><thead><tr><th>Edge</th><th>Resolution</th><th>Rule</th>' +
             '<th>Date</th><th>Direction</th><th>Kalshi</th><th>Polymarket</th>' +
             '<th class="num">Cost+fee</th></tr></thead><tbody>';
  for (const o of j.opportunities) {
    html += `<tr>
      <td class="num edge-positive">${(o.edge * 100).toFixed(2)}¢</td>
      <td><code>${o.resolution_source}</code></td>
      <td>${fmtRule(o)}</td>
      <td>${o.target_date || ''}</td>
      <td>${o.direction}</td>
      <td>
        <div>${o.kalshi.ticker}</div>
        <div class="leg">YES ${fmtPrice(o.kalshi.yes_ask)} · NO ${fmtPrice(o.kalshi.no_ask)}</div>
      </td>
      <td>
        <div class="leg">${(o.polymarket.question || '').slice(0, 60)}</div>
        <div class="leg">YES ${fmtPrice(o.polymarket.yes_ask)} · NO ${fmtPrice(o.polymarket.no_ask)}</div>
      </td>
      <td class="num">$${o.cost_with_fees.toFixed(3)}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  c.innerHTML = html;
}
load();
setInterval(load, 30000);
</script>
</body></html>
"""


@app.route("/cross")
def cross_page():
    return render_template_string(_CROSS_HTML)


@app.route("/")
def index():
    return render_template_string(_HTML)


# ─── Bootstrapping ──────────────────────────────────────────────────────────
def start_in_thread(host: str = "127.0.0.1", port: int = 8082) -> threading.Thread:
    """Start the Flask dev server as a daemon thread."""
    def _run():
        try:
            app.run(host=host, port=port, debug=False, use_reloader=False,
                    threaded=True)
        except Exception as e:
            logging.warning("[DASH] server crashed (non-fatal): %s", e)

    t = threading.Thread(target=_run, name="dashboard", daemon=True)
    t.start()
    logging.info("[DASH] serving on http://%s:%d", host, port)
    return t


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="127.0.0.1", port=8082, debug=False)
