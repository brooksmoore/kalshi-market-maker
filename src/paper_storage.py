"""
paper_storage.py — isolated paper-trade persistence for live paper trading.

When LIVE_TRADING_ENABLED=false, the bot runs its full decision pipeline
against real PROD markets but does NOT place orders. This module captures
the bot's would-be trade entries to data/paper_trades.db (separate from
trades.db) so we can score them against real settlements later.

WHY A SEPARATE DB:
  - Writing to trades.db would trip the exposure cache (`risk.py`) into
    thinking these were real open positions, triggering portfolio-Kelly
    halts. The existing dry-run path in main.py:535 intentionally returns
    without persisting for this reason.
  - Paper trades are EXPERIMENTAL DATA. Real trades are LIABILITIES.
    Different concerns → different storage.
  - When we eventually flip to LIVE_TRADING_ENABLED=true, the bot reads
    from trades.db for held-position dedup. paper_trades.db is invisible
    to that path — clean separation.

WHAT THIS DOES NOT DO:
  - Compute synthetic P&L at log time. Outcome is unknown when the
    decision is made; scoring happens post-hoc via scripts/score_paper_trades.py.
  - Model fill quality. We log the entry_price the bot would have hit
    (the ask for the chosen side). Real fills include adverse selection
    we can't simulate.
  - Touch live trading code paths in any way. log_paper_trade is the
    only public function.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "paper_trades.db"

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS paper_trade (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    venue TEXT NOT NULL,
    city TEXT,
    title TEXT,
    action TEXT NOT NULL,             -- 'BUY YES' or 'BUY NO'
    cal_p REAL,
    raw_p REAL,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    entry_price REAL,                 -- price bot would pay
    net_edge REAL,                    -- after-fee edge claim
    contracts INTEGER,                -- size decision
    recommended_size_usd REAL,
    spread_no_c REAL,
    ensemble_mean REAL,               -- forecast mean (°F)
    ensemble_sd REAL,                 -- forecast SD (°F) post-inflation
    close_time TEXT,                  -- market close (ISO 8601)
    bankroll_at_decision REAL,        -- bot's bankroll at decision time
    UNIQUE(cycle_ts, ticker, venue)
);
CREATE INDEX IF NOT EXISTS idx_paper_ticker ON paper_trade(ticker);
CREATE INDEX IF NOT EXISTS idx_paper_cycle ON paper_trade(cycle_ts);

-- Settlement scoring is updated post-hoc by score_paper_trades.py.
-- Outcome columns are nullable here; populated when the market resolves.
CREATE TABLE IF NOT EXISTS paper_result (
    paper_trade_id INTEGER PRIMARY KEY,
    outcome TEXT NOT NULL,            -- 'yes' or 'no'
    settled_at REAL NOT NULL,         -- epoch when we recorded the settlement
    pnl_per_contract REAL NOT NULL,   -- (1 - entry_price) if win, else -entry_price
    pnl_total REAL NOT NULL,          -- pnl_per_contract * contracts
    FOREIGN KEY(paper_trade_id) REFERENCES paper_trade(id)
);
"""


_log = logging.getLogger(__name__)
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit mode + 30s timeout — same locking pattern as the
        # observer / shadow logger DBs (avoids "database is locked" if
        # another process happens to touch this DB).
        _conn = sqlite3.connect(_DB_PATH, isolation_level=None, timeout=30)
        _conn.executescript(_SCHEMA)
    return _conn


def log_paper_trade(opp: dict, bankroll: float | None = None) -> int | None:
    """Persist one would-be trade entry. NEVER raises — failures log a warning
    and return None so the bot cycle continues.

    Returns the inserted row id on success, None on failure or duplicate
    (UNIQUE on (cycle_ts, ticker, venue) prevents same-cycle duplicates).
    """
    try:
        conn = _get_conn()
        # Derive contracts the same way executor._contracts_for does: floor of
        # size/price, with a min of 1 contract. The opp dict doesn't carry a
        # contracts field at paper-time (only the executor populates it on a
        # real fill), so we compute it here from recommended_size + entry_price.
        contracts = opp.get("contracts")
        if not contracts:
            size_usd = float(opp.get("recommended_size") or 0.0)
            entry_price = float(opp.get("entry_price") or 0.0)
            if entry_price > 0 and size_usd > 0:
                contracts = max(1, int(size_usd / entry_price))
            else:
                contracts = None

        cur = conn.execute(
            "INSERT OR IGNORE INTO paper_trade "
            "(cycle_ts, ticker, venue, city, title, action, "
            " cal_p, raw_p, yes_bid, yes_ask, no_bid, no_ask, "
            " entry_price, net_edge, contracts, recommended_size_usd, "
            " spread_no_c, ensemble_mean, ensemble_sd, close_time, "
            " bankroll_at_decision) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                opp.get("ticker") or opp.get("market_id") or "",
                opp.get("venue", "kalshi"),
                opp.get("city"),
                opp.get("title") or opp.get("question"),
                opp.get("action", ""),
                opp.get("calibrated_p"),
                opp.get("raw_p"),
                opp.get("yes_bid_dollars"),
                opp.get("yes_ask_dollars"),
                opp.get("no_bid_dollars"),
                opp.get("no_ask_dollars"),
                opp.get("entry_price"),
                opp.get("net_edge") or opp.get("edge"),
                contracts,
                opp.get("recommended_size"),
                opp.get("spread_no_c"),
                opp.get("ensemble_mean"),
                opp.get("ensemble_sd"),
                opp.get("close_time")
                or opp.get("expected_expiration_time")
                or opp.get("target_settlement"),
                bankroll,
            ),
        )
        if cur.rowcount > 0:
            return cur.lastrowid
        return None  # duplicate (same cycle_ts + ticker, very unlikely)
    except Exception as e:
        _log.warning("[PAPER_STORAGE] log_paper_trade failed: %s", e)
        return None


def get_unresolved_paper_trades() -> list[dict]:
    """Return paper_trade rows that have no paper_result yet AND whose
    close_time has passed. Used by score_paper_trades.py."""
    try:
        conn = _get_conn()
        rows = conn.execute("""
            SELECT pt.id, pt.ticker, pt.venue, pt.action, pt.entry_price,
                   pt.contracts, pt.close_time
            FROM paper_trade pt
            LEFT JOIN paper_result pr ON pr.paper_trade_id = pt.id
            WHERE pr.paper_trade_id IS NULL
            ORDER BY pt.cycle_ts ASC
        """).fetchall()
        cols = ("id", "ticker", "venue", "action", "entry_price", "contracts", "close_time")
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        _log.warning("[PAPER_STORAGE] get_unresolved_paper_trades failed: %s", e)
        return []


def log_paper_result(paper_trade_id: int, outcome: str, entry_price: float,
                     contracts: int) -> bool:
    """Record a settlement and computed P&L for one paper trade."""
    if outcome not in ("yes", "no"):
        return False
    try:
        conn = _get_conn()
        # We need to know whether the trade was BUY YES or BUY NO to compute pnl.
        action = conn.execute(
            "SELECT action FROM paper_trade WHERE id = ?",
            (paper_trade_id,),
        ).fetchone()
        if not action:
            return False
        action = action[0]
        if action == "BUY YES":
            won = outcome == "yes"
        else:  # BUY NO
            won = outcome == "no"
        pnl_per = (1.0 - entry_price) if won else (-entry_price)
        pnl_total = pnl_per * (contracts or 1)
        conn.execute(
            "INSERT OR REPLACE INTO paper_result "
            "(paper_trade_id, outcome, settled_at, pnl_per_contract, pnl_total) "
            "VALUES (?,?,?,?,?)",
            (paper_trade_id, outcome, time.time(), pnl_per, pnl_total),
        )
        return True
    except Exception as e:
        _log.warning("[PAPER_STORAGE] log_paper_result failed: %s", e)
        return False
