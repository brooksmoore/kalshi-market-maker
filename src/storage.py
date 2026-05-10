"""
storage.py — SQLite persistence.

Tables: trades, results, performance_snapshots, scan_log.
Every INSERT/SELECT uses ? placeholders — zero f-string SQL (audit B1).

Addresses audit items:
  B1 — parameterized SQL throughout
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from config import DB_FILE

# ─── SQL filter clauses (centralized — see audit 2026-05-09) ─────────────────
# These predicates appear in 15+ queries across storage / risk / dashboard /
# reconcile / calibration. They were previously duplicated literally; the
# duplication risk was that a change to the exclusion set (e.g. adding a new
# notes prefix to ignore) would update only some counters and silently diverge
# others. All callers now reference these strings.
#
# Use as f-string fragments in queries that already join `trades t`. The `t.`
# prefix is part of the predicate by design — callers must alias `trades` as
# `t`. Keeping the prefix in the constant prevents shadowing surprises and
# matches the pattern that was already in storage.py's local copies.
#
# Why two variants:
#   - NOTES_VALID_SQL excludes the auto-tagged exclusions (invalid/void/ghost)
#     that should NEVER count anywhere — accounting, reconcile, calibration.
#   - NOTES_VALID_LIVE_SQL adds the legacy `t.notes != 'dry-run'` gate. Newer
#     code sets `mode='dry-run'` (covered separately by NON_DRYRUN_SQL), but
#     historical rows used the notes string. Belt-and-suspenders for live-only
#     surfaces (KPIs, today P&L, halt math).
NOTES_VALID_SQL = (
    "(t.notes IS NULL OR ("
    "t.notes NOT LIKE 'invalid:%' "
    "AND t.notes NOT LIKE 'void:%' "
    "AND t.notes NOT LIKE 'ghost-%'))"
)
NOTES_VALID_LIVE_SQL = (
    "(t.notes IS NULL OR ("
    "t.notes NOT LIKE 'invalid:%' "
    "AND t.notes NOT LIKE 'void:%' "
    "AND t.notes NOT LIKE 'ghost-%' "
    "AND t.notes != 'dry-run'))"
)
NON_DRYRUN_SQL = "(t.mode IS NULL OR t.mode != 'dry-run')"
NON_ARB_SQL = "(t.market_type IS NULL OR t.market_type != 'arbitrage')"


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT,
        city TEXT,
        market_type TEXT,
        action TEXT,
        entry_price REAL,
        contracts INTEGER,
        size_usd REAL,
        ensemble_p REAL,
        calibrated_p REAL,
        edge_at_entry REAL,
        mode TEXT,
        opened_at TEXT,
        target_settlement TEXT,
        notes TEXT,
        paper_trade INTEGER DEFAULT 0,
        order_id TEXT,
        venue TEXT NOT NULL DEFAULT 'kalshi'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id INTEGER,
        outcome TEXT,
        exit_price REAL,
        profit_loss REAL,
        resolved_at TEXT,
        venue TEXT NOT NULL DEFAULT 'kalshi',
        FOREIGN KEY(trade_id) REFERENCES trades(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS performance_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_at TEXT,
        bankroll REAL,
        exposure REAL,
        peak_pnl REAL,
        realized_pnl_today REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scan_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_at TEXT,
        markets_scanned INTEGER,
        opportunities INTEGER,
        trades_placed INTEGER,
        claude_cost_usd REAL,
        breakdown_json TEXT
    )
    """,
    # Phase 3b: virtual maker orders awaiting fill in the paper sim.
    # An entry is created by paper_executor when an opp is routed to maker
    # mode. maker_sim.resolve_pending_orders polls each cycle and either:
    #   - fills it (creates a trades row, sets status='filled')
    #   - expires it (sets status='expired') after expires_at passes
    #   - leaves as pending if neither condition met yet
    """
    CREATE TABLE IF NOT EXISTS paper_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        venue TEXT NOT NULL,
        market_id TEXT NOT NULL,
        action TEXT NOT NULL,
        side TEXT NOT NULL,
        limit_price REAL NOT NULL,
        target_contracts INTEGER NOT NULL,
        calibrated_p REAL,
        edge_at_post REAL,
        posted_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        resolved_at TEXT,
        fill_price REAL,
        fill_count INTEGER,
        fill_trade_id INTEGER,
        opp_json TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trades_open ON trades(id)",
    "CREATE INDEX IF NOT EXISTS idx_results_trade ON results(trade_id)",
    "CREATE INDEX IF NOT EXISTS idx_results_resolved ON results(resolved_at)",
    "CREATE INDEX IF NOT EXISTS idx_paper_orders_status ON paper_orders(status)",
    "CREATE INDEX IF NOT EXISTS idx_paper_orders_venue ON paper_orders(venue)",
]


# Idempotent migrations applied after CREATE TABLE IF NOT EXISTS. Each is
# wrapped in its own try/except so a single column-already-exists doesn't
# block the rest. Safe to re-run on every boot.
#
# Order matters: ALTER TABLE statements that add a column MUST run before any
# CREATE INDEX that references that column, because on an existing DB the
# column doesn't exist yet.
_MIGRATIONS: list[str] = [
    "ALTER TABLE trades  ADD COLUMN order_id TEXT",
    "ALTER TABLE trades  ADD COLUMN venue TEXT NOT NULL DEFAULT 'kalshi'",
    "ALTER TABLE results ADD COLUMN venue TEXT NOT NULL DEFAULT 'kalshi'",
    "CREATE INDEX IF NOT EXISTS idx_trades_venue ON trades(venue)",
    "CREATE INDEX IF NOT EXISTS idx_results_venue ON results(venue)",
]


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        for stmt in SCHEMA:
            conn.execute(stmt)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()


def log_trade(opp: dict, fill_result: dict) -> int:
    """Insert a new trade row. Returns the trade_id.

    Venue is read from `opp['venue']`, defaulting to 'kalshi' for backward
    compatibility with any caller that hasn't been venue-aware'd yet.
    """
    opened_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (
                ticker, city, market_type, action,
                entry_price, contracts, size_usd,
                ensemble_p, calibrated_p, edge_at_entry,
                mode, opened_at, target_settlement, notes, paper_trade, order_id,
                venue
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                opp.get("ticker"),
                opp.get("city"),
                opp.get("market_type"),
                opp.get("action"),
                float(fill_result.get("fill_price") or opp.get("entry_price") or 0.0),
                int(fill_result.get("fill_count") or opp.get("contracts") or 0),
                float(opp.get("recommended_size") or 0.0),
                float(opp.get("raw_probability") or 0.0),
                float(opp.get("calibrated_p") or 0.0),
                float(opp.get("edge") or 0.0),
                str(fill_result.get("mode") or "dry-run"),
                opened_at,
                str(opp.get("target_settlement") or ""),
                str(opp.get("notes") or fill_result.get("notes") or ""),
                int(opp.get("paper_trade", 0)),
                fill_result.get("order_id"),
                str(opp.get("venue") or "kalshi"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def split_trade_on_partial_exit(trade_id: int, sold_contracts: int) -> int | None:
    """Handle a partial exit by splitting the trade row.

    Why: load_open_positions defines "open" as "no result row exists."
    On a partial exit we want to log a result for the SOLD portion AND
    keep the unsold remainder visible to future cycles. We achieve that
    by:
        1. Reducing the original trade's contracts to `sold_contracts`
           (so the result row about to be written cleanly closes it).
        2. Inserting a new trade row for the remainder, inheriting all
           model context (ticker, action, entry_price, calibrated_p,
           edge_at_entry, opened_at, etc) so the next cycle's exit
           logic sees correct entry data.

    Returns the new trade_id for the remainder, or None if the split
    wasn't needed (sold == original) or the original wasn't found.

    Caller writes the result row AFTER this returns (against the
    original trade_id, with the now-correct sold contracts).
    """
    sold = int(sold_contracts)
    if sold < 1:
        return None
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM trades WHERE id = ?", (int(trade_id),),
        ).fetchone()
        if not row:
            return None
        original_contracts = int(row["contracts"] or 0)
        if sold >= original_contracts:
            return None  # full exit; no split needed
        remaining = original_contracts - sold

        # Shrink original to the sold portion + scale size_usd.
        if original_contracts > 0:
            new_size_orig = round(
                float(row["size_usd"] or 0) * (sold / original_contracts), 4
            )
        else:
            new_size_orig = 0.0
        conn.execute(
            "UPDATE trades SET contracts = ?, size_usd = ? WHERE id = ?",
            (sold, new_size_orig, int(trade_id)),
        )

        # Mirror the original trade with `remaining` contracts. Inherits
        # all model context so exit logic on the next cycle has the
        # right calibrated_p, edge_at_entry, opened_at (preserves the
        # MIN_HOLD age of the position — splitting shouldn't reset the
        # clock).
        new_size_remain = round(
            float(row["size_usd"] or 0) * (remaining / original_contracts), 4
        ) if original_contracts > 0 else 0.0
        cur = conn.execute(
            """
            INSERT INTO trades (
                ticker, city, market_type, action,
                entry_price, contracts, size_usd,
                ensemble_p, calibrated_p, edge_at_entry,
                mode, opened_at, target_settlement, notes,
                paper_trade, order_id, venue
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["ticker"], row["city"], row["market_type"], row["action"],
                row["entry_price"], remaining, new_size_remain,
                row["ensemble_p"], row["calibrated_p"], row["edge_at_entry"],
                row["mode"], row["opened_at"], row["target_settlement"],
                f"split_from_trade_{trade_id}",
                row["paper_trade"], row["order_id"], row["venue"],
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def log_result(trade_id: int, outcome: str, exit_price: float, pnl: float,
               venue: str = "kalshi") -> None:
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute(
            """
            INSERT INTO results (trade_id, outcome, exit_price, profit_loss,
                                 resolved_at, venue)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (int(trade_id), str(outcome), float(exit_price), float(pnl),
             datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z',
             str(venue)),
        )
        conn.commit()


def write_snapshot(bankroll: float, exposure: float, peak_pnl: float,
                   today_pnl: float) -> None:
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute(
            """
            INSERT INTO performance_snapshots
                (snapshot_at, bankroll, exposure, peak_pnl, realized_pnl_today)
            VALUES (?, ?, ?, ?, ?)
            """,
            (datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z', float(bankroll), float(exposure),
             float(peak_pnl), float(today_pnl)),
        )
        conn.commit()


def log_scan(cycle_summary: dict[str, Any]) -> None:
    breakdown = json.dumps(cycle_summary.get("breakdown", {}))
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute(
            """
            INSERT INTO scan_log
                (cycle_at, markets_scanned, opportunities, trades_placed,
                 claude_cost_usd, breakdown_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z',
                int(cycle_summary.get("markets_scanned", 0)),
                int(cycle_summary.get("opportunities", 0)),
                int(cycle_summary.get("trades_placed", 0)),
                float(cycle_summary.get("claude_cost_usd", 0.0)),
                breakdown,
            ),
        )
        conn.commit()


def load_open_positions() -> list[dict]:
    """Open trades = rows in trades with no matching results row."""
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT t.*
            FROM trades t
            LEFT JOIN results r ON t.id = r.trade_id
            WHERE r.id IS NULL
              AND {NOTES_VALID_SQL}
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_cycle_stats() -> dict:
    """Aggregate stats for the cycle log block. Returns empty dict on error.

    Arb legs are EXCLUDED from total_yes / total_no / total_wins /
    total_resolved / today_wins / today_resolved (these are leg-misleading
    for arbs — by construction every clean arb shows 1 win + (N-1) losses
    if you count legs). Arb groups are bundled separately and added to
    `bundled_*` counters that the dashboard / cycle banner show as the
    "real" total.

    today_pnl includes all P&L (arb and non-arb) since dollar P&L is
    correctly leg-additive — only the WIN-RATE counters get distorted.
    """
    NON_DRYRUN = NON_DRYRUN_SQL
    NOTES_OK = NOTES_VALID_LIVE_SQL
    NON_ARB = NON_ARB_SQL
    try:
        with sqlite3.connect(DB_FILE, timeout=5) as conn:
            # Today's P&L includes everything (legs sum correctly). Wins/
            # resolved are NON-ARB only — arb groups added below.
            today = conn.execute(f"""
                SELECT COALESCE(SUM(r.profit_loss), 0.0)
                FROM results r JOIN trades t ON r.trade_id = t.id
                WHERE DATE(r.resolved_at) = DATE('now')
                  AND {NON_DRYRUN}
            """).fetchone()
            # All-time realized P&L — sourced from results, NOT
            # `bankroll - starting`. The two diverge whenever the user
            # adds/removes funds from the venue (manual demo top-ups,
            # withdrawals to bank), and this number must reflect actual
            # trade outcomes, not deposit history.
            total_realized = conn.execute(f"""
                SELECT COALESCE(SUM(r.profit_loss), 0.0)
                FROM results r JOIN trades t ON r.trade_id = t.id
                WHERE {NON_DRYRUN} AND {NOTES_OK}
            """).fetchone()
            today_nonarb = conn.execute(f"""
                SELECT COUNT(*),
                       SUM(CASE WHEN r.profit_loss > 0 THEN 1 ELSE 0 END)
                FROM results r JOIN trades t ON r.trade_id = t.id
                WHERE DATE(r.resolved_at) = DATE('now')
                  AND {NON_DRYRUN} AND {NON_ARB}
            """).fetchone()

            totals = conn.execute(f"""
                SELECT COUNT(*),
                       SUM(CASE WHEN t.action='BUY YES' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN t.action='BUY NO'  THEN 1 ELSE 0 END),
                       AVG(t.edge_at_entry),
                       SUM(CASE WHEN r.profit_loss > 0 THEN 1 ELSE 0 END),
                       COUNT(r.id)
                FROM trades t LEFT JOIN results r ON r.trade_id = t.id
                WHERE {NON_DRYRUN} AND {NOTES_OK} AND {NON_ARB}
            """).fetchone()

            open_pos = conn.execute(f"""
                SELECT COUNT(*)
                FROM trades t LEFT JOIN results r ON r.trade_id = t.id
                WHERE r.id IS NULL
                  AND {NON_DRYRUN} AND {NOTES_OK} AND {NON_ARB}
            """).fetchone()
    except Exception as e:
        logging.warning("[STATS] db query failed: %s", e)
        return {}

    arb = get_arb_group_stats()

    today_resolved_nonarb = int(today_nonarb[0] or 0)
    today_wins_nonarb = int(today_nonarb[1] or 0)
    total_resolved_nonarb = int(totals[5] or 0)
    total_wins_nonarb = int(totals[4] or 0)

    # "Bundled" counters treat each arb group as one trade (today's only
    # if its resolved_at is today — approximated by checking arb open_groups
    # not changing; for cycle-banner simplicity we count groups_resolved /
    # groups_won lifetime here and let the dashboard surface today vs total).
    bundled_total = total_resolved_nonarb + arb["groups_resolved"]
    bundled_wins = total_wins_nonarb + arb["groups_won"]

    return {
        "today_resolved":  today_resolved_nonarb,        # non-arb legs
        "today_pnl":       float(today[0] or 0.0),       # all-inclusive
        "total_pnl":       float(total_realized[0] or 0.0),  # realized only
        "today_wins":      today_wins_nonarb,            # non-arb legs
        "total_logged":    int(totals[0] or 0),
        "total_yes":       int(totals[1] or 0),
        "total_no":        int(totals[2] or 0),
        "avg_edge":        float(totals[3] or 0.0),
        "total_wins":      total_wins_nonarb,            # non-arb only
        "total_resolved":  total_resolved_nonarb,        # non-arb only
        "open_positions":  int(open_pos[0] or 0),        # non-arb only
        # Arb stats — surfaced separately so the cycle banner / dashboard
        # don't conflate.
        "arb_groups_total":    arb["groups_total"],
        "arb_groups_open":     arb["groups_open"],
        "arb_groups_resolved": arb["groups_resolved"],
        "arb_groups_won":      arb["groups_won"],
        "arb_groups_lost":     arb["groups_lost"],
        "arb_win_rate":        arb["win_rate"],
        "arb_realized_pnl":    arb["realized_pnl"],
        "arb_open_cost":       arb["open_cost"],
        "arb_stranded_legs":   arb["stranded_legs"],
        # Bundled view: arb group counts as one trade for the headline
        # win-rate counter (the user explicitly asked for this).
        "bundled_total":   bundled_total,
        "bundled_wins":    bundled_wins,
    }


def get_resolved_trades(since_date: str | None = None) -> list[dict]:
    query = """
        SELECT t.*, r.outcome, r.exit_price, r.profit_loss, r.resolved_at
        FROM trades t JOIN results r ON t.id = r.trade_id
    """
    params: tuple = ()
    if since_date:
        query += " WHERE DATE(r.resolved_at) >= DATE(?)"
        params = (since_date,)
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def insert_paper_order(
    venue: str, market_id: str, action: str, side: str,
    limit_price: float, target_contracts: int,
    calibrated_p: float, edge_at_post: float,
    expires_at: str, opp_json: str,
) -> int:
    """Insert a pending paper maker order. Returns its id."""
    posted_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        cur = conn.execute(
            """
            INSERT INTO paper_orders (
                venue, market_id, action, side, limit_price, target_contracts,
                calibrated_p, edge_at_post, posted_at, expires_at,
                status, opp_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (str(venue), str(market_id), str(action), str(side),
             float(limit_price), int(target_contracts),
             float(calibrated_p), float(edge_at_post),
             posted_at, str(expires_at), str(opp_json)),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_pending_paper_orders(venue: str | None = None) -> list[dict]:
    """Return all pending paper orders, optionally filtered by venue."""
    sql = "SELECT * FROM paper_orders WHERE status = 'pending'"
    params: tuple = ()
    if venue is not None:
        sql += " AND venue = ?"
        params = (venue,)
    sql += " ORDER BY posted_at ASC"
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def mark_paper_order_filled(order_id: int, fill_price: float,
                            fill_count: int, fill_trade_id: int) -> None:
    resolved_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute(
            """
            UPDATE paper_orders
            SET status='filled', resolved_at=?, fill_price=?, fill_count=?,
                fill_trade_id=?
            WHERE id=?
            """,
            (resolved_at, float(fill_price), int(fill_count),
             int(fill_trade_id), int(order_id)),
        )
        conn.commit()


def mark_paper_order_expired(order_id: int) -> None:
    resolved_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.execute(
            "UPDATE paper_orders SET status='expired', resolved_at=? WHERE id=?",
            (resolved_at, int(order_id)),
        )
        conn.commit()


def paper_order_stats(venue: str) -> dict:
    """Counts of paper orders by status for one venue."""
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM paper_orders WHERE venue=? GROUP BY status",
            (venue,),
        ).fetchall()
    out = {"pending": 0, "filled": 0, "expired": 0}
    for status, n in rows:
        out[str(status)] = int(n)
    return out


def get_arb_group_stats() -> dict:
    """Bundle arb legs into one synthetic trade per arb_id and report.

    The leg-level view is misleading because by construction exactly one
    leg in an arb group resolves YES and the rest resolve NO — every clean
    arb settles as 1 win + (N-1) losses if you count legs, despite the
    GROUP being a guaranteed-positive-EV trade. The user explicitly wants
    arbs treated as ONE cumulative trade, win iff group P&L > 0 (which
    should be true 100% of the time for clean groups; deviations are
    investigation triggers — see notes below).

    Returns:
      groups_total / groups_resolved / groups_open / groups_won /
      groups_lost / win_rate / realized_pnl / open_cost / open_groups (list)
      stranded_legs / stranded_legs_pnl   ← legs from rollback-failed arbs
                                            that don't belong to a clean
                                            group; their notes start with
                                            'arb_stranded:' and the original
                                            arb_id is unfortunately lost.
                                            Counted separately because they
                                            break the no-loss guarantee.
    """
    NON_DRYRUN = NON_DRYRUN_SQL
    NOTES_OK = NOTES_VALID_SQL
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            # Clean arb groups (notes is the arb_id starting with 'arb:')
            rows = conn.execute(f"""
                SELECT t.notes AS arb_id,
                       COUNT(t.id) AS legs,
                       SUM(CASE WHEN r.id IS NULL THEN 1 ELSE 0 END) AS unresolved_legs,
                       COALESCE(SUM(r.profit_loss), 0) AS group_pnl,
                       COALESCE(SUM(t.size_usd), 0) AS group_cost,
                       MIN(t.opened_at) AS opened_at,
                       MAX(r.resolved_at) AS resolved_at
                FROM trades t LEFT JOIN results r ON r.trade_id = t.id
                WHERE t.market_type = 'arbitrage'
                  AND t.notes LIKE 'arb:%'
                  AND {NON_DRYRUN}
                GROUP BY t.notes
                ORDER BY MIN(t.opened_at) DESC
            """).fetchall()

            # Stranded legs (rollback failed; arb_id lost in notes)
            stranded = conn.execute(f"""
                SELECT COUNT(*),
                       COALESCE(SUM(r.profit_loss), 0)
                FROM trades t LEFT JOIN results r ON r.trade_id = t.id
                WHERE t.notes LIKE 'arb_stranded:%'
                  AND {NON_DRYRUN}
            """).fetchone()
    except Exception as e:
        logging.warning("[STATS] get_arb_group_stats failed: %s", e)
        return {
            "groups_total": 0, "groups_resolved": 0, "groups_open": 0,
            "groups_won": 0, "groups_lost": 0,
            "win_rate": None, "realized_pnl": 0.0, "open_cost": 0.0,
            "open_groups": [],
            "stranded_legs": 0, "stranded_legs_pnl": 0.0,
        }

    groups_total = len(rows)
    open_rows = [r for r in rows if (r["unresolved_legs"] or 0) > 0]
    resolved_rows = [r for r in rows if (r["unresolved_legs"] or 0) == 0]
    won = sum(1 for r in resolved_rows if (r["group_pnl"] or 0) > 0)
    realized = sum(float(r["group_pnl"] or 0.0) for r in resolved_rows)
    open_cost = sum(float(r["group_cost"] or 0.0) for r in open_rows)

    open_groups = [
        {
            "arb_id": r["arb_id"],
            "legs": int(r["legs"] or 0),
            "unresolved_legs": int(r["unresolved_legs"] or 0),
            "group_cost": round(float(r["group_cost"] or 0.0), 2),
            "opened_at": r["opened_at"],
        }
        for r in open_rows
    ]

    return {
        "groups_total": groups_total,
        "groups_resolved": len(resolved_rows),
        "groups_open": len(open_rows),
        "groups_won": won,
        "groups_lost": len(resolved_rows) - won,
        "win_rate": (won / len(resolved_rows)) if resolved_rows else None,
        "realized_pnl": round(realized, 2),
        "open_cost": round(open_cost, 2),
        "open_groups": open_groups,
        "stranded_legs": int(stranded[0] or 0),
        "stranded_legs_pnl": round(float(stranded[1] or 0.0), 2),
    }


def get_resolved_arb_groups(limit: int = 20) -> list[dict]:
    """List recent resolved arb groups (most-recent first) for the
    dashboard arb-tracker history table."""
    NON_DRYRUN = "(t.mode IS NULL OR t.mode != 'dry-run')"
    try:
        with sqlite3.connect(DB_FILE, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"""
                SELECT t.notes AS arb_id,
                       COUNT(t.id) AS legs,
                       COALESCE(SUM(r.profit_loss), 0) AS group_pnl,
                       COALESCE(SUM(t.size_usd), 0) AS group_cost,
                       MAX(r.resolved_at) AS resolved_at,
                       MIN(t.city) AS city
                FROM trades t LEFT JOIN results r ON r.trade_id = t.id
                WHERE t.market_type = 'arbitrage'
                  AND t.notes LIKE 'arb:%'
                  AND {NON_DRYRUN}
                GROUP BY t.notes
                HAVING SUM(CASE WHEN r.id IS NULL THEN 1 ELSE 0 END) = 0
                ORDER BY MAX(r.resolved_at) DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return [
            {
                "arb_id": r["arb_id"],
                "legs": int(r["legs"] or 0),
                "group_pnl": round(float(r["group_pnl"] or 0.0), 2),
                "group_cost": round(float(r["group_cost"] or 0.0), 2),
                "resolved_at": r["resolved_at"],
                "city": r["city"],
                "won": (r["group_pnl"] or 0) > 0,
            }
            for r in rows
        ]
    except Exception as e:
        logging.warning("[STATS] get_resolved_arb_groups failed: %s", e)
        return []


def get_venue_pnl(venue: str, paper_only: bool = False) -> dict:
    """Aggregate P&L stats for one venue. Used by the dashboard's
    per-venue panels.

    Returns: {trades, resolved, wins, losses, win_rate, realized_pnl,
              open_positions}.
    paper_only=True restricts to paper_trade=1 rows (Polymarket case).

    Arb legs are EXCLUDED from the win-rate counters (`wins`, `losses`,
    `win_rate`, `resolved`) because per-leg counting misrepresents arb
    performance. They ARE included in `realized_pnl` and `trades` because
    those are dollar/position aggregates that sum correctly leg-by-leg.
    Use `get_arb_group_stats()` for the bundled-arb view.
    """
    paper_filter = " AND COALESCE(t.paper_trade, 0) = 1" if paper_only else ""
    try:
        with sqlite3.connect(DB_FILE, timeout=5) as conn:
            # All-trades aggregates (count + realized P&L include arb legs)
            all_row = conn.execute(f"""
                SELECT
                    COUNT(t.id),
                    COALESCE(SUM(r.profit_loss), 0.0)
                FROM trades t
                LEFT JOIN results r ON r.trade_id = t.id
                WHERE COALESCE(t.venue, 'kalshi') = ?
                  {paper_filter}
                  AND {NOTES_VALID_SQL}
            """, (venue,)).fetchone()
            # Win-rate aggregates EXCLUDE arb legs (group-level view in
            # get_arb_group_stats covers arbs honestly).
            wr_row = conn.execute(f"""
                SELECT
                    COUNT(r.id),
                    SUM(CASE WHEN r.profit_loss > 0 THEN 1 ELSE 0 END)
                FROM trades t
                LEFT JOIN results r ON r.trade_id = t.id
                WHERE COALESCE(t.venue, 'kalshi') = ?
                  {paper_filter} AND {NON_ARB_SQL}
                  AND {NOTES_VALID_SQL}
            """, (venue,)).fetchone()
            open_row = conn.execute(f"""
                SELECT COUNT(t.id)
                FROM trades t LEFT JOIN results r ON r.trade_id = t.id
                WHERE COALESCE(t.venue, 'kalshi') = ?
                  {paper_filter}
                  AND r.id IS NULL
            """, (venue,)).fetchone()
        trades = int(all_row[0] or 0)
        resolved = int(wr_row[0] or 0)
        wins = int(wr_row[1] or 0)
        losses = resolved - wins
        return {
            "trades": trades,
            "resolved": resolved,            # non-arb only
            "wins": wins,                    # non-arb only
            "losses": losses,                # non-arb only
            "win_rate": (wins / resolved) if resolved else None,
            "realized_pnl": round(float(all_row[1] or 0.0), 2),  # all
            "open_positions": int(open_row[0] or 0),
        }
    except Exception as e:
        logging.warning("[STATS] get_venue_pnl(%s) failed: %s", venue, e)
        return {"trades": 0, "resolved": 0, "wins": 0, "losses": 0,
                "win_rate": None, "realized_pnl": 0.0, "open_positions": 0}


def get_last_resolved(n: int = 3) -> list[dict]:
    """Return the N most recently resolved trades, newest first."""
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT t.city, t.market_type, t.action,
                   r.outcome, r.profit_loss, r.resolved_at
            FROM results r JOIN trades t ON r.trade_id = t.id
            ORDER BY r.resolved_at DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
    return [dict(r) for r in rows]
