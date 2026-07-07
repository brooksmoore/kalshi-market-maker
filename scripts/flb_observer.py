"""
flb_observer.py — passive non-sports prediction-market price+settlement logger.

Purpose (per direction_investigation_20260529 memory): the weather forecast
thesis is dead; the surviving thesis is the favorite-longshot bias (FLB) in
LESS-efficient, non-sports markets. Before writing any model code we measure,
exactly like the shadow audit that killed the weather thesis. This daemon
collects the two things an FLB study needs:

  1. price-at-lead    — periodic top-of-book snapshots of every non-sports
                         market closing within HORIZON_DAYS, on both Kalshi
                         (public unauth API) and Polymarket (public Gamma API).
  2. settlement       — the realized yes/no outcome, captured when a tracked
                         market closes.

Joining (1)+(2) later (scripts/flb_analyze.py) yields a realized-return-by-
entry-price curve net of fees — the go/no-go for the FLB strategy.

What this is NOT:
  - NOT a strategy runner / paper trader. No synthetic P&L, ever.
  - NOT authenticated. Public read-only endpoints only; cannot place orders.
  - NOT weather. Sports is excluded (most efficient, ~80% of volume); weather
    is kept (it's the calibrated control cohort) but is not the point.

Run:
    venv/bin/python scripts/flb_observer.py
    (Ctrl-C to stop cleanly. Safe to restart anytime — no in-memory state.)

Output:
    data/flb_observer.db    sqlite: observer_run, market_snapshot, settlement
                            (+ pm_snapshot, pm_settlement for Polymarket)
    logs/flb_observer.log   rotating log, INFO+
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]

# ─── Constants ────────────────────────────────────────────────────────────────
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_BASE = "https://gamma-api.polymarket.com"
SNAPSHOT_INTERVAL_SEC = 3600       # hourly — these markets move far slower than
                                   # weather/crypto; hourly is ample to pick a
                                   # price-at-fixed-lead later, and keeps API load low.
HORIZON_DAYS = 45                  # track markets closing within 45d (covers the
                                   # 31d trade window + markets rolling into it).
EXCLUDE_CATEGORIES = {"Sports", "Crypto"}
                                   # Sports: ~80% of volume, most efficient.
                                   # Crypto: BTC hourly/15-min micro-markets are
                                   #   near-martingale efficient (measured 5/29:
                                   #   0-10c longshots priced 0.054 / realized
                                   #   0.053, gap -0.001 — zero FLB edge) AND they
                                   #   settle constantly, flooding the settlement
                                   #   table + driving most of the per-cycle
                                   #   settlement-fetch API load. Not a profit
                                   #   source; excluded to keep collection lean.
SETTLE_FETCH_CAP = 300             # max individual settlement fetches per cycle.
REQUEST_TIMEOUT_SEC = 20
DB_PATH = ROOT / "data" / "flb_observer.db"
LOG_PATH = ROOT / "logs" / "flb_observer.log"
HEADERS = {"User-Agent": "flb-research/1.0", "Accept": "application/json"}


# ─── Logging ──────────────────────────────────────────────────────────────────
def _setup_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("flb_observer")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


log = _setup_logging()


# ─── HTTP ─────────────────────────────────────────────────────────────────────
def _get(url: str, params: dict | None = None, retries: int = 4) -> dict | list | None:
    """GET with backoff on 429/5xx. Returns parsed JSON or None."""
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT_SEC)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            log.warning("GET %s → %d %s", url, r.status_code, r.text[:120])
            return None
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            log.warning("GET %s raised: %s", url, e)
            return None
    return None


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_iso(t: str | None) -> datetime | None:
    if not t:
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None


# ─── Kalshi collection ──────────────────────────────────────────────────────────
def kalshi_open_markets(horizon: datetime) -> list[dict]:
    """Page all open events with nested markets; return flat list of non-sports
    active markets closing before `horizon`, each annotated with its event's
    category / mutually_exclusive / title."""
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    cursor = ""
    for _ in range(80):  # safety cap (~35 pages observed)
        j = _get(f"{KALSHI_BASE}/events",
                 params={"limit": 200, "status": "open",
                         "with_nested_markets": "true", "cursor": cursor})
        if not isinstance(j, dict):
            break
        for e in j.get("events", []):
            cat = e.get("category")
            if cat in EXCLUDE_CATEGORIES:
                continue
            for m in (e.get("markets") or []):
                if m.get("status") != "active":
                    continue
                ct = _parse_iso(m.get("close_time"))
                if ct is None or ct > horizon or ct < now:
                    continue
                m["_category"] = cat
                m["_event_ticker"] = e.get("event_ticker")
                m["_series_ticker"] = e.get("series_ticker")
                m["_mutually_exclusive"] = 1 if e.get("mutually_exclusive") else 0
                m["_event_title"] = e.get("title")
                out.append(m)
        cursor = j.get("cursor") or ""
        if not cursor:
            break
        time.sleep(0.3)
    return out


def kalshi_snapshot_row(m: dict, ts: float) -> tuple:
    yb = _f(m.get("yes_bid_dollars"))
    ya = _f(m.get("yes_ask_dollars"))
    return (
        ts, m.get("ticker"), m.get("_event_ticker"), m.get("_series_ticker"),
        m.get("_category"), m.get("_mutually_exclusive"),
        (m.get("_event_title") or "")[:200], m.get("close_time"), m.get("status"),
        yb, ya, _f(m.get("no_bid_dollars")), _f(m.get("no_ask_dollars")),
        _f(m.get("volume_24h_fp")), _f(m.get("volume_fp")),
        _f(m.get("open_interest_fp")), _f(m.get("liquidity_dollars")),
        _f(m.get("last_price_dollars")),
    )


def kalshi_fetch_settlement(ticker: str) -> str | None:
    """Return 'yes'/'no' if the market is settled/finalized, else None."""
    j = _get(f"{KALSHI_BASE}/markets/{ticker}")
    if not isinstance(j, dict):
        return None
    m = j.get("market") or {}
    if (m.get("status") or "").lower() in ("finalized", "settled"):
        res = (m.get("result") or "").lower()
        if res in ("yes", "no"):
            return res
    return None


# ─── Polymarket collection ──────────────────────────────────────────────────────
def pm_open_markets(horizon: datetime) -> list[dict]:
    """Pull active, non-closed Polymarket markets ending before `horizon`,
    ordered by 24h volume (most liquid first). Resilient: returns [] on failure."""
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    offset = 0
    for _ in range(20):
        page = _get(f"{GAMMA_BASE}/markets",
                    params={"closed": "false", "active": "true", "limit": 500,
                            "offset": offset, "order": "volume24hr", "ascending": "false"})
        if not isinstance(page, list) or not page:
            break
        for m in page:
            end = _parse_iso(m.get("endDate"))
            if end is None or end > horizon or end < now:
                continue
            out.append(m)
        if len(page) < 500:
            break
        offset += len(page)
        time.sleep(0.3)
    return out


def pm_snapshot_row(m: dict, ts: float) -> tuple:
    try:
        op = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
    except (ValueError, TypeError):
        op = []
    yes_price = op[0] if op else None
    return (
        ts, str(m.get("id")), m.get("conditionId"), m.get("slug"),
        (m.get("question") or "")[:200], m.get("endDate"),
        yes_price, _f(m.get("volume24hr")), _f(m.get("liquidityNum") or m.get("liquidity")),
        1 if m.get("closed") else 0,
    )


def pm_fetch_settlement(market_id: str) -> str | None:
    """Return 'yes'/'no' if a Polymarket market has resolved, else None.
    Resolution signal: closed==true and outcomePrices collapsed to {1,0}."""
    j = _get(f"{GAMMA_BASE}/markets/{market_id}")
    if not isinstance(j, dict):
        return None
    if not j.get("closed"):
        return None
    try:
        op = [float(x) for x in json.loads(j.get("outcomePrices") or "[]")]
    except (ValueError, TypeError):
        return None
    if not op:
        return None
    if op[0] >= 0.99:
        return "yes"
    if op[0] <= 0.01:
        return "no"
    return None  # closed but ambiguous (rare); leave for a later pass


# ─── Storage ──────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS observer_run (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL, stopped_at REAL,
    snapshots_total INTEGER NOT NULL DEFAULT 0,
    settlements_total INTEGER NOT NULL DEFAULT 0,
    cycles_total INTEGER NOT NULL DEFAULT 0, notes TEXT
);
CREATE TABLE IF NOT EXISTS market_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, ts REAL NOT NULL,
    ticker TEXT NOT NULL, event_ticker TEXT, series_ticker TEXT, category TEXT,
    mutually_exclusive INTEGER, title TEXT, close_time TEXT, status TEXT,
    yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL,
    volume_24h REAL, volume REAL, open_interest REAL, liquidity REAL, last_price REAL
);
CREATE INDEX IF NOT EXISTS idx_ms_ticker_ts ON market_snapshot(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_ms_ts ON market_snapshot(ts);
CREATE TABLE IF NOT EXISTS settlement (
    ticker TEXT PRIMARY KEY, result TEXT NOT NULL, close_time TEXT,
    category TEXT, settled_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pm_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, ts REAL NOT NULL,
    market_id TEXT NOT NULL, condition_id TEXT, slug TEXT, question TEXT,
    end_date TEXT, yes_price REAL, volume_24h REAL, liquidity REAL, closed INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pm_mid_ts ON pm_snapshot(market_id, ts);
CREATE TABLE IF NOT EXISTS pm_settlement (
    market_id TEXT PRIMARY KEY, result TEXT NOT NULL, end_date TEXT,
    question TEXT, settled_at REAL NOT NULL
);
"""


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


# ─── Main loop ──────────────────────────────────────────────────────────────────
_STOP = False


def _handle_sig(signum, frame) -> None:
    global _STOP
    if _STOP:
        log.warning("second signal — exiting hard")
        os._exit(130)
    _STOP = True
    log.info("signal %d — finishing cycle then stopping (again to force)", signum)


def settle_due(conn: sqlite3.Connection, run_id: int) -> int:
    """Fetch outcomes for tracked Kalshi/Polymarket markets that have closed
    and aren't settled yet. Bounded by SETTLE_FETCH_CAP per venue per cycle."""
    now_iso = datetime.now(timezone.utc).isoformat()
    settled = 0
    # Kalshi: distinct tracked tickers whose close_time has passed, no settlement row
    # Exclude crypto: leftover crypto tickers from pre-exclusion runs are still
    # in market_snapshot; without this filter settle_due keeps firing individual
    # settlement API calls for thousands of closed BTC micro-markets we don't care
    # about (the bulk of per-cycle load). We never want their outcomes.
    due = conn.execute(
        """SELECT DISTINCT ms.ticker, ms.close_time, ms.category
           FROM market_snapshot ms
           LEFT JOIN settlement s ON s.ticker = ms.ticker
           WHERE s.ticker IS NULL AND ms.close_time IS NOT NULL AND ms.close_time < ?
             AND (ms.category IS NULL OR ms.category != 'Crypto')
           LIMIT ?""", (now_iso, SETTLE_FETCH_CAP)).fetchall()
    for ticker, close_time, cat in due:
        if _STOP:
            break
        res = kalshi_fetch_settlement(ticker)
        if res:
            conn.execute(
                "INSERT OR IGNORE INTO settlement (ticker, result, close_time, category, settled_at) "
                "VALUES (?,?,?,?,?)", (ticker, res, close_time, cat, time.time()))
            settled += 1
        time.sleep(0.05)
    # Polymarket
    due_pm = conn.execute(
        """SELECT DISTINCT ps.market_id, ps.end_date, ps.question
           FROM pm_snapshot ps
           LEFT JOIN pm_settlement s ON s.market_id = ps.market_id
           WHERE s.market_id IS NULL AND ps.end_date IS NOT NULL AND ps.end_date < ?
           LIMIT ?""", (now_iso, SETTLE_FETCH_CAP)).fetchall()
    for mid, end_date, q in due_pm:
        if _STOP:
            break
        res = pm_fetch_settlement(mid)
        if res:
            conn.execute(
                "INSERT OR IGNORE INTO pm_settlement (market_id, result, end_date, question, settled_at) "
                "VALUES (?,?,?,?,?)", (mid, res, end_date, q, time.time()))
            settled += 1
        time.sleep(0.05)
    return settled


def run_cycle(conn: sqlite3.Connection, run_id: int) -> tuple[int, int]:
    horizon = datetime.now(timezone.utc) + timedelta(days=HORIZON_DAYS)
    ts = time.time()

    k_mkts = kalshi_open_markets(horizon)
    if k_mkts:
        conn.executemany(
            """INSERT INTO market_snapshot (
                run_id, ts, ticker, event_ticker, series_ticker, category,
                mutually_exclusive, title, close_time, status,
                yes_bid, yes_ask, no_bid, no_ask, volume_24h, volume,
                open_interest, liquidity, last_price)
               VALUES (?,""" + ",".join("?" * 18) + ")",
            [(run_id,) + kalshi_snapshot_row(m, ts) for m in k_mkts])

    pm_mkts = pm_open_markets(horizon)
    if pm_mkts:
        conn.executemany(
            """INSERT INTO pm_snapshot (
                run_id, ts, market_id, condition_id, slug, question, end_date,
                yes_price, volume_24h, liquidity, closed)
               VALUES (?,""" + ",".join("?" * 10) + ")",
            [(run_id,) + pm_snapshot_row(m, ts) for m in pm_mkts])

    settled = settle_due(conn, run_id)
    log.info("cycle ok: kalshi=%d pm=%d snapshots, %d settlements recorded",
             len(k_mkts), len(pm_mkts), settled)
    return len(k_mkts) + len(pm_mkts), settled


def main() -> None:
    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)
    conn = open_db()
    cur = conn.execute("INSERT INTO observer_run (started_at, notes) VALUES (?, ?)",
                       (time.time(), "flb non-sports price+settlement logger"))
    run_id = int(cur.lastrowid)
    log.info("flb_observer started: run_id=%d db=%s horizon=%dd interval=%ds",
             run_id, DB_PATH, HORIZON_DAYS, SNAPSHOT_INTERVAL_SEC)

    snaps_total = settle_total = cycles = 0
    try:
        while not _STOP:
            t0 = time.time()
            try:
                n, s = run_cycle(conn, run_id)
                snaps_total += n
                settle_total += s
                cycles += 1
            except Exception as e:
                log.exception("cycle raised, continuing: %s", e)
            elapsed = time.time() - t0
            sleep_for = max(0.0, SNAPSHOT_INTERVAL_SEC - elapsed)
            log.info("cycle %d done in %.1fs; next in %.0fs", cycles, elapsed, sleep_for)
            end_at = time.time() + sleep_for
            while not _STOP and time.time() < end_at:
                time.sleep(min(5.0, end_at - time.time()))
    finally:
        conn.execute(
            "UPDATE observer_run SET stopped_at=?, snapshots_total=?, settlements_total=?, "
            "cycles_total=? WHERE run_id=?",
            (time.time(), snaps_total, settle_total, cycles, run_id))
        conn.close()
        log.info("flb_observer stopped: run_id=%d snapshots=%d settlements=%d cycles=%d",
                 run_id, snaps_total, settle_total, cycles)


if __name__ == "__main__":
    main()
