"""
prod_observer.py — passive Kalshi PROD orderbook scraper.

Phase 1 of the demo→prod transition observer. Hits Kalshi's public (unauth)
prod endpoints on a fixed cadence, captures top-of-book + top-5 depth for
every daily-high weather market in the universe, and writes snapshots to
`data/prod_observer.db`.

What this is NOT:
  - NOT a paper-trading simulator. Never computes synthetic P&L.
  - NOT a strategy runner. Phase 2 will layer that on top.
  - NOT authenticated. Uses only public prod endpoints; cannot place orders.

Run:
    python scripts/prod_observer.py
    (Ctrl-C to stop cleanly. Safe to restart anytime — no in-memory state.)

Output:
    data/prod_observer.db   sqlite, two tables: observer_run + book_snapshot
    logs/prod_observer.log  rotating log, INFO+
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import signal
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from resolution_rules import (
    derive_city_from_kalshi_series,
    is_kalshi_daily_high_series,
)

# ─── Constants ────────────────────────────────────────────────────────────────
PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SNAPSHOT_INTERVAL_SEC = 300        # 5 min cadence
DISCOVERY_TTL_SEC = 1800           # re-discover series every 30 min
DB_PATH = ROOT / "data" / "prod_observer.db"
LOG_PATH = ROOT / "logs" / "prod_observer.log"
REQUEST_TIMEOUT_SEC = 15
PARALLEL_FETCHES = 4               # for orderbook scrape; stays under ~10 req/s polite limit
TOP_LEVELS = 5                     # capture top-5 each side for depth json

# Ticker → (kind, low, high). Bin tickers like KXHIGH...-B74.5 → ("B", 74, 75).
# Threshold tickers like KXHIGH...-T72 → ("T", 72, None). Used downstream for
# analysis grouping; identical to the parse_bracket logic strategy.py uses but
# inferred from ticker suffix to avoid needing the title.
_TICKER_KIND_RE = re.compile(r"-([BT])(\d+(?:\.\d+)?)$")


# ─── Logging ──────────────────────────────────────────────────────────────────
def _setup_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("prod_observer")
    log.setLevel(logging.INFO)
    log.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=5_000_000, backupCount=3
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


log = _setup_logging()


# ─── Prod public HTTP ─────────────────────────────────────────────────────────
def _prod_get(path: str, retries: int = 2) -> dict | None:
    """Unauth GET against Kalshi prod. Returns parsed JSON or None on failure.

    Retries transient errors (5xx, ConnectionError, Timeout) once with a
    short backoff. Permanent errors (4xx) return None immediately.
    """
    url = f"{PROD_BASE}{path}"
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT_SEC)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            log.warning("prod_get %s → %d %s", path, r.status_code, r.text[:120])
            return None
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            log.warning("prod_get %s raised: %s", path, e)
            return None
    return None


# ─── Discovery ────────────────────────────────────────────────────────────────
_discovery_cache: dict[str, Any] = {"ts": 0.0, "series": []}


def discover_prod_universe() -> list[tuple[str, str]]:
    """Return list of (series_ticker, city_key) for every daily-high Kalshi
    weather series the bot would trade. Cached for DISCOVERY_TTL_SEC.

    This is a prod-side re-discovery, deliberately NOT reading the bot's
    data/kalshi_series.json — that file is populated by the live bot which
    is currently pointed at demo. Series tickers should overlap heavily,
    but we don't want a demo-side artifact silently shaping prod analysis.
    """
    now = time.time()
    if now - _discovery_cache["ts"] < DISCOVERY_TTL_SEC and _discovery_cache["series"]:
        return _discovery_cache["series"]

    cat = urllib.parse.quote("Climate and Weather")
    all_series: list[dict] = []
    cursor = ""
    for _ in range(20):  # safety cap
        path = f"/series?category={cat}&limit=200"
        if cursor:
            path += f"&cursor={cursor}"
        data = _prod_get(path)
        if not data:
            break
        page = data.get("series") or []
        all_series.extend(page)
        cursor = data.get("cursor") or ""
        if not cursor or not page:
            break

    mapped: list[tuple[str, str]] = []
    seen: set[str] = set()
    unmapped = 0
    for s in all_series:
        ticker = s.get("ticker") or ""
        title = s.get("title") or ""
        if not ticker or ticker in seen:
            continue
        if not is_kalshi_daily_high_series(ticker, title):
            continue
        city = derive_city_from_kalshi_series(ticker, title)
        if city is None:
            unmapped += 1
            continue
        mapped.append((ticker, city))
        seen.add(ticker)

    _discovery_cache["ts"] = now
    _discovery_cache["series"] = mapped
    log.info(
        "discovery: %d daily-high series on prod (cities=%d, unmapped=%d)",
        len(mapped), len({c for _, c in mapped}), unmapped,
    )
    return mapped


def list_markets(series_ticker: str) -> list[dict]:
    """Return all open markets in a series."""
    data = _prod_get(f"/markets?series_ticker={series_ticker}&status=open&limit=200")
    if not data:
        return []
    return data.get("markets") or []


def get_orderbook(ticker: str) -> dict | None:
    """Return raw orderbook_fp dict (yes_dollars + no_dollars level arrays)."""
    data = _prod_get(f"/markets/{ticker}/orderbook?depth={TOP_LEVELS}")
    if not data:
        return None
    return data.get("orderbook_fp") or data.get("orderbook")


# ─── Snapshot extraction ──────────────────────────────────────────────────────
def _ticker_kind(ticker: str) -> tuple[str | None, float | None]:
    """('B', 74.5) for KXHIGHNY-26MAY10-B74.5; ('T', 72) for ...-T72;
    (None, None) otherwise. Cheap regex; no title parse needed."""
    m = _TICKER_KIND_RE.search(ticker or "")
    if not m:
        return (None, None)
    try:
        return (m.group(1), float(m.group(2)))
    except ValueError:
        return (m.group(1), None)


def _top_levels(side_levels: list, n: int = TOP_LEVELS) -> list[tuple[float, float]]:
    """Sort + dedupe levels into [(price, size), ...] descending by price.

    Kalshi's orderbook_fp returns yes_dollars / no_dollars as bid arrays
    on each side. Best bid is the highest price.
    """
    out: list[tuple[float, float]] = []
    for e in side_levels or []:
        try:
            p = float(e[0])
            s = float(e[1])
        except (TypeError, ValueError, IndexError):
            continue
        if p <= 0:
            continue
        out.append((p, s))
    out.sort(key=lambda x: -x[0])
    return out[:n]


def snapshot_market(market: dict, series_to_city: dict[str, str]) -> dict | None:
    """Pull orderbook + assemble a snapshot row for one market. Returns None
    on fetch failure so the caller can count and continue."""
    ticker = market.get("ticker") or ""
    if not ticker:
        return None
    book = get_orderbook(ticker)
    if book is None:
        return None

    yes_levels = _top_levels(book.get("yes_dollars") or book.get("yes") or [])
    no_levels = _top_levels(book.get("no_dollars") or book.get("no") or [])

    # Yes bid = highest price on yes side. Yes ask = 1 - highest no bid.
    yes_bid = yes_levels[0][0] if yes_levels else None
    yes_bid_size = yes_levels[0][1] if yes_levels else None
    no_bid = no_levels[0][0] if no_levels else None
    no_bid_size = no_levels[0][1] if no_levels else None
    yes_ask = round(1.0 - no_bid, 4) if no_bid is not None else None
    yes_ask_size = no_bid_size
    no_ask = round(1.0 - yes_bid, 4) if yes_bid is not None else None
    no_ask_size = yes_bid_size

    spread_yes_c = (
        round((yes_ask - yes_bid) * 100, 2)
        if yes_bid is not None and yes_ask is not None
        else None
    )
    spread_no_c = (
        round((no_ask - no_bid) * 100, 2)
        if no_bid is not None and no_ask is not None
        else None
    )

    kind, threshold = _ticker_kind(ticker)
    series_ticker = market.get("series_ticker") or _series_from_ticker(ticker)

    return {
        "ts": time.time(),
        "ticker": ticker,
        "series_ticker": series_ticker,
        "city": series_to_city.get(series_ticker or "", None),
        "subtitle": market.get("subtitle"),
        "kind": kind,
        "threshold": threshold,
        "close_time": market.get("close_time"),
        "status": market.get("status"),
        "volume_24h": _f(market.get("volume_24h_fp") or market.get("volume_24h")),
        "open_interest": _f(market.get("open_interest_fp") or market.get("open_interest")),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "yes_bid_size": yes_bid_size,
        "yes_ask_size": yes_ask_size,
        "no_bid_size": no_bid_size,
        "no_ask_size": no_ask_size,
        "spread_yes_c": spread_yes_c,
        "spread_no_c": spread_no_c,
        "book_json": json.dumps(
            {"yes_levels": yes_levels, "no_levels": no_levels},
            separators=(",", ":"),
        ),
    }


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _series_from_ticker(ticker: str) -> str | None:
    """KXHIGHNY-26MAY10-B74.5 → KXHIGHNY. Cheap fallback when the API
    response doesn't echo series_ticker on the market dict."""
    if not ticker:
        return None
    return ticker.split("-", 1)[0] or None


# ─── Storage ──────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS observer_run (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    stopped_at REAL,
    snapshots_total INTEGER NOT NULL DEFAULT 0,
    cycles_total INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS book_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    series_ticker TEXT,
    city TEXT,
    subtitle TEXT,
    kind TEXT,
    threshold REAL,
    close_time TEXT,
    status TEXT,
    volume_24h REAL,
    open_interest REAL,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    yes_bid_size REAL,
    yes_ask_size REAL,
    no_bid_size REAL,
    no_ask_size REAL,
    spread_yes_c REAL,
    spread_no_c REAL,
    book_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_book_ticker_ts ON book_snapshot(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_book_run ON book_snapshot(run_id);
CREATE INDEX IF NOT EXISTS idx_book_ts ON book_snapshot(ts);
"""


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def start_run(conn: sqlite3.Connection, notes: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO observer_run (started_at, notes) VALUES (?, ?)",
        (time.time(), notes),
    )
    return int(cur.lastrowid)


def end_run(conn: sqlite3.Connection, run_id: int, snapshots: int, cycles: int) -> None:
    conn.execute(
        "UPDATE observer_run SET stopped_at=?, snapshots_total=?, cycles_total=? "
        "WHERE run_id=?",
        (time.time(), snapshots, cycles, run_id),
    )


def insert_snapshots(conn: sqlite3.Connection, run_id: int, rows: list[dict]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO book_snapshot (
            run_id, ts, ticker, series_ticker, city, subtitle, kind, threshold,
            close_time, status, volume_24h, open_interest,
            yes_bid, yes_ask, no_bid, no_ask,
            yes_bid_size, yes_ask_size, no_bid_size, no_ask_size,
            spread_yes_c, spread_no_c, book_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                run_id, r["ts"], r["ticker"], r["series_ticker"], r["city"],
                r["subtitle"], r["kind"], r["threshold"],
                r["close_time"], r["status"], r["volume_24h"], r["open_interest"],
                r["yes_bid"], r["yes_ask"], r["no_bid"], r["no_ask"],
                r["yes_bid_size"], r["yes_ask_size"], r["no_bid_size"], r["no_ask_size"],
                r["spread_yes_c"], r["spread_no_c"], r["book_json"],
            )
            for r in rows
        ],
    )


# ─── Main loop ────────────────────────────────────────────────────────────────
_STOP = False


def _handle_sigint(signum, frame) -> None:
    global _STOP
    if _STOP:
        # second Ctrl-C — force exit, the user means it
        log.warning("second SIGINT — exiting hard")
        os._exit(130)
    _STOP = True
    log.info("SIGINT — finishing current cycle then stopping (Ctrl-C again to force)")


def run_cycle(conn: sqlite3.Connection, run_id: int) -> int:
    """One full scrape pass. Returns count of snapshots written."""
    from concurrent.futures import ThreadPoolExecutor

    series_pairs = discover_prod_universe()
    if not series_pairs:
        log.warning("no series discovered this cycle; sleeping until next")
        return 0
    series_to_city = dict(series_pairs)

    # 1) Discover markets per series in parallel
    def _list(s: str) -> tuple[str, list[dict]]:
        return s, list_markets(s)

    market_universe: list[dict] = []
    with ThreadPoolExecutor(max_workers=PARALLEL_FETCHES) as ex:
        for series_ticker, markets in ex.map(_list, [s for s, _ in series_pairs]):
            for m in markets:
                m["series_ticker"] = series_ticker
                market_universe.append(m)

    if not market_universe:
        log.warning("zero markets returned across %d series", len(series_pairs))
        return 0

    # 2) Snapshot orderbook for each market in parallel
    rows: list[dict] = []
    fail = 0
    with ThreadPoolExecutor(max_workers=PARALLEL_FETCHES) as ex:
        for r in ex.map(lambda m: snapshot_market(m, series_to_city), market_universe):
            if r is None:
                fail += 1
            else:
                rows.append(r)

    insert_snapshots(conn, run_id, rows)
    log.info(
        "cycle ok: %d markets snapshotted, %d failed, %d series",
        len(rows), fail, len(series_pairs),
    )
    return len(rows)


def main() -> None:
    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    conn = open_db()
    run_id = start_run(conn, notes="phase1 scrape-only")
    log.info("observer started: run_id=%d db=%s", run_id, DB_PATH)

    snapshots_total = 0
    cycles_total = 0
    try:
        while not _STOP:
            cycle_start = time.time()
            try:
                n = run_cycle(conn, run_id)
                snapshots_total += n
                cycles_total += 1
            except Exception as e:
                log.exception("cycle raised, continuing: %s", e)

            # Drift-corrected sleep to next 5-min boundary
            elapsed = time.time() - cycle_start
            sleep_for = max(0.0, SNAPSHOT_INTERVAL_SEC - elapsed)
            log.info(
                "cycle %d done in %.1fs; next in %.0fs",
                cycles_total, elapsed, sleep_for,
            )
            # Poll _STOP every 5s so SIGINT is responsive
            end_at = time.time() + sleep_for
            while not _STOP and time.time() < end_at:
                time.sleep(min(5.0, end_at - time.time()))
    finally:
        end_run(conn, run_id, snapshots_total, cycles_total)
        conn.close()
        log.info(
            "observer stopped: run_id=%d snapshots=%d cycles=%d",
            run_id, snapshots_total, cycles_total,
        )


if __name__ == "__main__":
    main()
