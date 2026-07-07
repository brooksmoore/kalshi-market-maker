"""
phase2_shadow_logger.py — log model forecasts alongside prod market state.

The plan-doc-specified Phase 2 add-on to the prod observer. One-shot script
(intended to run on a cron, ~hourly): for every currently-open ticker in
data/prod_observer.db, compute the bot's calibrated YES probability against
the current GEFS forecast and write one row per ticker per run into a
shadow_signal table. Joined to settlement outcomes later, this gives us
real model Brier on prod markets — the deferred legitimate replacement
for the invalidated 5/9 demo-Brier finding.

DISCIPLINE (from prod_transition_plan_20260510.md):
  - Logs forecast-quality only: (ts, ticker, calibrated_p, prod_yes_mid,
    prod_no_mid). Joined to actual weather outcomes when markets settle.
  - NO synthetic P&L column. Not now, not ever. v1 postmortem §3.2.
  - No fill modeling, no edge gate column either — those mix execution
    questions with forecast questions. Brier first, edge logic later.

What this logger does NOT do:
  - Run find_opportunities (which couples sizing + edge gates +
    held-position dedup with the forecast — we want the cleanest possible
    forecast signal).
  - Touch trades.db. Output goes only to prod_observer.db (new table).
  - Hit any authenticated endpoint. Uses public /markets/ same as observer.

Usage (run hourly via cron, or manually):
    venv/bin/python scripts/phase2_shadow_logger.py
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

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from resolution_rules import canonicalize_kalshi_market  # noqa: E402

DB_PATH = ROOT / "data" / "prod_observer.db"
PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Network hardening (added 2026-05-12 after the 01:33z silent-hang incident):
# (connect_timeout, read_timeout) tuple is more precise than a single int.
# requests' single-int timeout has been observed to miss-fire when a socket
# establishes but the remote stops sending mid-read — common during wifi
# handoffs and hotspot disconnects. The 2-tuple bounds each phase separately.
REQUEST_TIMEOUT = (5.0, 10.0)
RATE_LIMIT_SLEEP = 0.05

# Wall-clock hard cap on the entire run. If anything still slips past the
# per-request timeouts (e.g. an OS-level socket stuck), the alarm fires and
# the process exits with a non-zero status. The outer `while true; sleep
# 3600` shell loop then retries. Better to lose one hour of shadow data than
# silently lose all future hours to a wedged subprocess.
MAX_WALL_CLOCK_SEC = 600

log = logging.getLogger("phase2")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")


def _wall_clock_timeout(signum, frame):
    log.error("WALL-CLOCK TIMEOUT (%ds) — exiting; outer loop will retry",
              MAX_WALL_CLOCK_SEC)
    sys.exit(2)


SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_signal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    city TEXT,
    calibrated_p REAL,
    prod_yes_mid REAL,
    prod_no_mid REAL,
    book_ts REAL,
    cal_reason TEXT,
    UNIQUE(ts, ticker)
);
CREATE INDEX IF NOT EXISTS idx_shadow_ticker ON shadow_signal(ticker);
CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_signal(ts);
"""

# Additive migrations. ALTER TABLE ... ADD COLUMN is idempotent only if we
# try/except — SQLite raises if the column exists. Use a small helper that
# tolerates "already exists" so the schema stays self-healing.
ADDITIVE_COLUMNS = [
    ("lead_hours", "REAL"),       # close_time_epoch - run_ts / 3600
    ("gefs_run_ts", "REAL"),      # estimated synoptic hour of forecast (epoch)
    ("ensemble_mean", "REAL"),    # GEFS ensemble mean (°F), pre-calibration
    ("ensemble_sd", "REAL"),      # GEFS ensemble SD (°F) — under-dispersion proxy
    ("ensemble_n", "INTEGER"),    # number of members in this forecast
    ("raw_p", "REAL"),            # uncalibrated probability (pre-isotonic)
]


def _apply_additive_columns(conn: sqlite3.Connection) -> None:
    for name, decl in ADDITIVE_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE shadow_signal ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError:
            pass  # column already exists


def _current_gefs_run_ts(now_epoch: float | None = None) -> float:
    """Estimate the synoptic hour of the most recently published GEFS run.
    GEFS initializes at 00z/06z/12z/18z UTC and Open-Meteo typically
    publishes ~3-4h after init. This is approximate but consistent
    enough to cluster shadow signals by which forecast cycle they used."""
    if now_epoch is None:
        now_epoch = time.time()
    # Account for ~4h publish lag, then floor to nearest 6h boundary.
    # Use timezone-aware datetimes — naive datetime.timestamp() interprets
    # the value as LOCAL time, which silently shifts by tz offset.
    effective = datetime.fromtimestamp(now_epoch - 4 * 3600, tz=timezone.utc)
    floored = effective.replace(
        hour=(effective.hour // 6) * 6, minute=0, second=0, microsecond=0
    )
    return floored.timestamp()


# Persistent session reuses TCP connections — kinder on flaky wifi and
# faster overall (avoids handshake per call).
_session = requests.Session()


def _prod_get(path: str, retries: int = 1) -> dict | None:
    """Fail-soft GET. Catches every plausible network error, retries once
    on transient failures, returns None on permanent ones. NEVER raises."""
    url = f"{PROD_BASE}{path}"
    for attempt in range(retries + 1):
        try:
            r = _session.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                try:
                    return r.json()
                except (ValueError, json.JSONDecodeError):
                    return None
            # 429/5xx: brief backoff and retry once
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            return None
        except (requests.RequestException, OSError) as e:
            # RequestException covers ConnectionError, Timeout, SSLError,
            # ChunkedEncodingError, ContentDecodingError, etc. OSError covers
            # raw socket failures that occasionally leak past requests.
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            log.debug("prod_get %s gave up: %s", path[:40], type(e).__name__)
            return None
    return None


def _yes_mid(yb, ya, nb, na) -> float | None:
    if yb is not None and ya is not None and yb > 0 and ya > 0:
        return (yb + ya) / 2.0
    if nb is not None and na is not None and nb > 0 and na > 0:
        return 1.0 - (nb + na) / 2.0
    return None


def main() -> int:
    # Arm the wall-clock alarm before anything else can hang us.
    signal.signal(signal.SIGALRM, _wall_clock_timeout)
    signal.alarm(MAX_WALL_CLOCK_SEC)

    if not DB_PATH.exists():
        log.error("no observer DB at %s — observer must be running first", DB_PATH)
        return 1

    # Lazy imports — these load model state (GEFS, calibration)
    from strategy import compute_market_cal_p_full

    # isolation_level=None puts the connection in autocommit mode so each
    # INSERT releases the write lock immediately, instead of holding it for
    # the full 1-2 minute fetch loop. Without this, prod_observer (which
    # writes every 5 min) collides with shadow and errors out with
    # "database is locked" — observed 2026-05-12 16:04 incident.
    # timeout=30 gives any remaining contention 30s to resolve before erroring.
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30)
    conn.executescript(SCHEMA)
    _apply_additive_columns(conn)

    gefs_run_ts = _current_gefs_run_ts()

    # Get the most-recent snapshot per currently-open ticker from observer DB.
    now = time.time()
    rows = conn.execute("""
        SELECT b.ticker, b.city, b.subtitle, b.kind, b.threshold, b.close_time,
               b.yes_bid, b.yes_ask, b.no_bid, b.no_ask, b.ts
        FROM book_snapshot b
        INNER JOIN (
            SELECT ticker, MAX(ts) AS max_ts
            FROM book_snapshot
            GROUP BY ticker
        ) latest ON b.ticker = latest.ticker AND b.ts = latest.max_ts
    """).fetchall()
    log.info("found %d latest snapshots in observer DB", len(rows))

    run_ts = now
    n_logged = 0
    n_skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        n_skipped[reason] = n_skipped.get(reason, 0) + 1

    for r in rows:
        (ticker, city, subtitle, kind, threshold, close_time,
         yb, ya, nb, na, book_ts) = r

        # Compute lead_hours and skip already-closed markets.
        lead_hours: float | None = None
        if close_time:
            try:
                ct_epoch = datetime.fromisoformat(close_time.replace("Z", "+00:00")).timestamp()
                if ct_epoch < now:
                    skip("already_closed")
                    continue
                lead_hours = (ct_epoch - run_ts) / 3600.0
            except Exception:
                pass

        yes_mid = _yes_mid(yb, ya, nb, na)
        no_mid = 1.0 - yes_mid if yes_mid is not None else None

        # Fetch raw market from public prod (needed for title, canonicalization).
        raw_resp = _prod_get(f"/markets/{ticker}")
        time.sleep(RATE_LIMIT_SLEEP)
        if not raw_resp:
            skip("fetch_fail")
            continue
        raw_market = raw_resp.get("market", {})
        if not raw_market:
            skip("no_market")
            continue

        # Enrich with observer-known fields (city is not always in raw)
        raw_market.setdefault("city", city)
        canonical = canonicalize_kalshi_market(raw_market)
        if not canonical:
            skip("canonicalize_fail")
            continue
        # canonicalize_kalshi_market returns a dict with canonical fields;
        # ensure city/title are present
        canonical.setdefault("city", city)
        canonical.setdefault("title", raw_market.get("title") or raw_market.get("subtitle") or subtitle)

        cal_reason = None
        full = None
        try:
            full = compute_market_cal_p_full(canonical, venue="kalshi")
        except Exception as e:
            cal_reason = f"exception:{type(e).__name__}"
        cal_p = full["cal_p"] if full else None
        ens_mean = full["ensemble_mean"] if full else None
        ens_sd = full["ensemble_sd"] if full else None
        ens_n = full["ensemble_n"] if full else None
        raw_p = full["raw_p"] if full else None
        if cal_p is None and cal_reason is None:
            cal_reason = "no_forecast_or_bracket"

        conn.execute(
            "INSERT OR IGNORE INTO shadow_signal "
            "(ts, ticker, city, calibrated_p, prod_yes_mid, prod_no_mid, "
            " book_ts, cal_reason, lead_hours, gefs_run_ts, "
            " ensemble_mean, ensemble_sd, ensemble_n, raw_p) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_ts, ticker, city, cal_p, yes_mid, no_mid, book_ts, cal_reason,
             lead_hours, gefs_run_ts,
             ens_mean, ens_sd, ens_n, raw_p),
        )
        n_logged += 1

    # No explicit commit needed — autocommit mode commits each statement.
    conn.close()

    log.info("logged %d shadow signals; skipped %s", n_logged, dict(n_skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
