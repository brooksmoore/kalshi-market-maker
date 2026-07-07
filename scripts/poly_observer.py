"""
poly_observer.py — passive Polymarket weather-market book scraper + Phase 2
shadow logger, combined into a single 5-min cycle.

Companion to scripts/prod_observer.py and scripts/phase2_shadow_logger.py.
Cleanly segmented from the Kalshi data: writes to data/poly_observer.db
(separate DB file), never touches data/prod_observer.db or data/trades.db.
Use scripts/analyze_observer.py-style queries against the new DB to compare
Polymarket vs Kalshi market microstructure side-by-side.

Why combined script (vs two scripts like Kalshi):
  - Polymarket has only ~40 weather markets vs Kalshi's ~340. Doing the
    shadow signal in the same loop is essentially free.
  - One process to monitor, one log file to tail.

Discipline (same as Kalshi shadow logger):
  - shadow_signal stores (ts, market_id, calibrated_p, prod_yes_mid).
  - NO synthetic P&L column. Ever. v1 postmortem §3.2.

Usage:
    venv/bin/python scripts/poly_observer.py
"""

from __future__ import annotations

import json
import logging
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polymarket_client import PolymarketVenue  # noqa: E402
from strategy import compute_market_cal_p  # noqa: E402

DB_PATH = ROOT / "data" / "poly_observer.db"
CYCLE_INTERVAL_SEC = 300
LOG_FILE = ROOT / "data" / "poly_observer.log"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("poly_observer")


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS observer_run (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    stopped_at REAL,
    snapshots_total INTEGER NOT NULL DEFAULT 0,
    cycles_total INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);
-- NB: Polymarket exposes only ASK sides for each independent YES/NO token.
-- There are no real "bids" in the Kalshi sense. We store best yes_ask and
-- no_ask as observed, plus a derived implied_yes_bid = 1 - no_ask (the
-- price someone would effectively receive for selling YES via buying NO).
-- spread_c = (yes_ask - (1 - no_ask)) * 100 is the cross-token "spread" —
-- the gap between buying YES outright and the implied yes valuation from
-- the NO token. This is the Polymarket analog to Kalshi's yes_ask-yes_bid
-- spread but reflects the dual-token CLOB structure honestly.
CREATE TABLE IF NOT EXISTS book_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    market_id TEXT NOT NULL,
    city TEXT,
    question TEXT,
    comparator TEXT,
    threshold REAL,
    range_low REAL,
    range_high REAL,
    close_time TEXT,
    yes_ask REAL,
    no_ask REAL,
    yes_ask_size_usd REAL,
    no_ask_size_usd REAL,
    implied_yes_bid REAL,  -- = 1 - no_ask
    spread_c REAL,         -- cross-token spread in cents
    book_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_book_mid ON book_snapshot(market_id);
CREATE INDEX IF NOT EXISTS idx_book_ts ON book_snapshot(ts);

CREATE TABLE IF NOT EXISTS shadow_signal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    market_id TEXT NOT NULL,
    city TEXT,
    calibrated_p REAL,
    prod_yes_mid REAL,
    prod_no_mid REAL,
    book_ts REAL,
    cal_reason TEXT,
    lead_hours REAL,
    gefs_run_ts REAL,
    UNIQUE(ts, market_id)
);
CREATE INDEX IF NOT EXISTS idx_shadow_mid ON shadow_signal(market_id);
CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_signal(ts);
"""


ADDITIVE_COLUMNS = [
    ("lead_hours", "REAL"),
    ("gefs_run_ts", "REAL"),
]


def _apply_additive_columns(conn: sqlite3.Connection) -> None:
    for name, decl in ADDITIVE_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE shadow_signal ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError:
            pass


def _current_gefs_run_ts(now_epoch: float | None = None) -> float:
    if now_epoch is None:
        now_epoch = time.time()
    effective = datetime.fromtimestamp(now_epoch - 4 * 3600, tz=timezone.utc)
    floored = effective.replace(
        hour=(effective.hour // 6) * 6, minute=0, second=0, microsecond=0
    )
    return floored.timestamp()


_running = True


def _shutdown(signum, frame):
    global _running
    log.info("signal %d — finishing current cycle then stopping", signum)
    _running = False


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


def _best_ask(asks_list) -> tuple[float | None, float | None]:
    """Polymarket's book stores asks as [(price, dollar_size), ...] sorted
    by price ascending. Return (best_price, best_dollar_size) or (None, None)."""
    if not asks_list:
        return None, None
    price, dollars = asks_list[0]
    return float(price), float(dollars)


def cycle(conn: sqlite3.Connection, run_id: int, venue: PolymarketVenue) -> int:
    """One observer+shadow cycle. Returns number of snapshots written."""
    ts = time.time()
    try:
        markets = venue.list_markets()
    except Exception as e:
        log.warning("list_markets failed: %s", e)
        return 0

    n = 0
    for m in markets:
        market_id = m.get("market_id") or m.get("ticker")
        yes_token = m.get("_yes_token_id")
        no_token = m.get("_no_token_id")
        if not market_id or not yes_token or not no_token:
            continue

        try:
            book = venue.get_book(market_id)
        except Exception as e:
            log.warning("get_book %s failed: %s", market_id[:20], e)
            continue

        # Polymarket-shaped book (TypedDict, not attribute access).
        # yes_bids/no_bids actually hold the ASK side of the respective token —
        # quirk documented in polymarket_client.get_book.
        yes_ask, yes_ask_size = _best_ask(book.get("yes_bids", []))
        no_ask, no_ask_size = _best_ask(book.get("no_bids", []))

        implied_yes_bid = (1.0 - no_ask) if no_ask is not None else None
        spread_c = None
        if yes_ask is not None and implied_yes_bid is not None:
            spread_c = (yes_ask - implied_yes_bid) * 100.0

        conn.execute(
            "INSERT INTO book_snapshot (run_id, ts, market_id, city, question, "
            "comparator, threshold, range_low, range_high, close_time, "
            "yes_ask, no_ask, yes_ask_size_usd, no_ask_size_usd, "
            "implied_yes_bid, spread_c, book_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, ts, market_id, m.get("city"),
                m.get("question") or m.get("title"),
                m.get("comparator"), m.get("threshold"),
                m.get("range_low"), m.get("range_high"),
                m.get("close_time"),
                yes_ask, no_ask, yes_ask_size, no_ask_size,
                implied_yes_bid, spread_c,
                json.dumps({"levels_truncated": True}),
            ),
        )

        # Mid = average of buy-YES price (yes_ask) and implied yes value
        # from the NO token (1 - no_ask). On a coherent two-token book these
        # converge; the spread captures their disagreement.
        yes_mid = None
        if yes_ask is not None and implied_yes_bid is not None:
            yes_mid = (yes_ask + implied_yes_bid) / 2.0
        elif yes_ask is not None:
            yes_mid = yes_ask
        elif implied_yes_bid is not None:
            yes_mid = implied_yes_bid

        cal_reason = None
        try:
            cal_p = compute_market_cal_p(m, venue="polymarket")
        except Exception as e:
            cal_p = None
            cal_reason = f"exception:{type(e).__name__}"
        if cal_p is None and cal_reason is None:
            cal_reason = "no_forecast_or_bracket"

        lead_hours = None
        if m.get("close_time"):
            try:
                ct_epoch = datetime.fromisoformat(
                    m["close_time"].replace("Z", "+00:00")
                ).timestamp()
                lead_hours = (ct_epoch - ts) / 3600.0
            except Exception:
                pass

        conn.execute(
            "INSERT OR IGNORE INTO shadow_signal "
            "(ts, market_id, city, calibrated_p, prod_yes_mid, prod_no_mid, "
            " book_ts, cal_reason, lead_hours, gefs_run_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, market_id, m.get("city"), cal_p, yes_mid,
             (1.0 - yes_mid) if yes_mid is not None else None,
             ts, cal_reason, lead_hours, _current_gefs_run_ts(ts)),
        )
        n += 1

    conn.execute(
        "UPDATE observer_run SET snapshots_total = snapshots_total + ?, "
        "cycles_total = cycles_total + 1 WHERE run_id = ?",
        (n, run_id),
    )
    conn.commit()
    return n


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # See phase2_shadow_logger.py for the locking rationale: autocommit
    # mode + 30s timeout prevents cross-process write contention.
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30)
    conn.executescript(SCHEMA)
    _apply_additive_columns(conn)
    cur = conn.execute(
        "INSERT INTO observer_run (started_at, notes) VALUES (?, ?)",
        (time.time(), "polymarket weather observer + shadow"),
    )
    run_id = cur.lastrowid
    conn.commit()
    log.info("starting run %d → %s", run_id, DB_PATH)

    venue = PolymarketVenue()
    while _running:
        t0 = time.time()
        n = cycle(conn, run_id, venue)
        log.info("cycle %d: %d snapshots in %.1fs", run_id, n, time.time() - t0)
        # Sleep remainder of interval, but wake up to check _running every 5s.
        elapsed = time.time() - t0
        remaining = max(0.0, CYCLE_INTERVAL_SEC - elapsed)
        end = time.time() + remaining
        while _running and time.time() < end:
            time.sleep(min(5.0, end - time.time()))

    conn.execute("UPDATE observer_run SET stopped_at=? WHERE run_id=?",
                 (time.time(), run_id))
    conn.commit()
    conn.close()
    log.info("clean shutdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
