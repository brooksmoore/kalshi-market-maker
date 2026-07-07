"""
metar_logger.py — passive collector of NWS METAR observations.

Pulls hourly observed temperatures from NWS for each Kalshi-tracked city's
airport station, stores in data/metar_observations.db. Daemon: cycles
every 15 minutes (METARs publish ~hourly but with variable lag; 15min
poll catches fresh data within minutes of publication).

THIS LOGGER IS NOT USED BY THE BOT. It's preparation for Checkpoint 1
(METAR observed-so-far floor): when the v3 forecaster ships, the
historical METAR table gives us clean contemporaneous data to re-score
the existing shadow cohort against, instead of scrambling for METAR
data after the fact.

Self-contained: writes to its own DB (data/metar_observations.db).
No interaction with prod_observer.db, poly_observer.db, or trades.db.

Usage:
    venv/bin/python scripts/metar_logger.py
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

from resolution_rules import KALSHI_CITY_STATION  # noqa: E402

DB_PATH = ROOT / "data" / "metar_observations.db"
LOG_FILE = ROOT / "data" / "metar_logger.log"
CYCLE_INTERVAL_SEC = 900  # 15 min — METARs publish ~hourly with variable lag
REQUEST_TIMEOUT = (5.0, 15.0)
NWS_BASE = "https://api.weather.gov"
USER_AGENT = "kalshi-bot-shadow-audit/2.0 (contact: bcm3000@gmail.com)"

# Wall-clock safety per cycle. NWS API is generally reliable but socket
# stalls on a flaky network are real; bound the cycle.
MAX_CYCLE_SEC = 600

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("metar")


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS metar_observation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at REAL NOT NULL,             -- epoch when we pulled the record
    station_id TEXT NOT NULL,             -- e.g. "KLAX"
    city TEXT NOT NULL,                   -- e.g. "LA"
    observation_ts REAL NOT NULL,         -- timestamp of the observation itself
    observation_iso TEXT NOT NULL,        -- ISO-8601 form (UTC) for human inspection
    temperature_f REAL,                   -- Fahrenheit, null if METAR missing
    raw_json TEXT,                        -- full feature.properties for forensics
    UNIQUE(station_id, observation_ts)
);
CREATE INDEX IF NOT EXISTS idx_metar_station_ts ON metar_observation(station_id, observation_ts);
CREATE INDEX IF NOT EXISTS idx_metar_city ON metar_observation(city);
CREATE INDEX IF NOT EXISTS idx_metar_fetched ON metar_observation(fetched_at);
"""

_running = True
_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/geo+json"})


def _shutdown(signum, frame):
    global _running
    log.info("signal %d — finishing current cycle then stopping", signum)
    _running = False


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


def _cycle_wall_clock_timeout(signum, frame):
    raise TimeoutError(f"cycle wall-clock {MAX_CYCLE_SEC}s exceeded")


def _c_to_f(celsius: float | None) -> float | None:
    if celsius is None:
        return None
    return celsius * 9.0 / 5.0 + 32.0


def _fetch_station_observations(station: str, hours: int = 25) -> list[dict] | None:
    """Pull recent METAR observations for a station. Returns list of feature
    properties dicts, or None on failure. 25h window catches all hourly
    observations from the past day plus a buffer."""
    start = datetime.now(timezone.utc).replace(microsecond=0)
    start = start.replace(hour=start.hour - (start.hour % 1))  # already hourly
    # NWS API: ?start=ISO returns observations >= that time
    from datetime import timedelta
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    url = f"{NWS_BASE}/stations/{station}/observations"
    params = {"start": start.isoformat()}
    try:
        r = _session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            log.warning("station %s → HTTP %d", station, r.status_code)
            return None
        payload = r.json()
        return [f.get("properties", {}) for f in payload.get("features", [])]
    except (requests.RequestException, OSError, ValueError) as e:
        log.warning("station %s fetch failed: %s", station, type(e).__name__)
        return None


def _persist_observation(conn: sqlite3.Connection, station: str, city: str,
                         props: dict) -> bool:
    """Insert one observation. Returns True if new row written, False if
    duplicate (UNIQUE constraint) or invalid payload."""
    iso = props.get("timestamp")
    if not iso:
        return False
    try:
        obs_dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return False
    obs_ts = obs_dt.timestamp()

    temp_block = props.get("temperature") or {}
    temp_c = temp_block.get("value")
    temp_f = _c_to_f(temp_c)

    # Strip the big related-station block to keep raw_json compact.
    raw_compact = {k: v for k, v in props.items() if k != "@id"}

    cur = conn.execute(
        "INSERT OR IGNORE INTO metar_observation "
        "(fetched_at, station_id, city, observation_ts, observation_iso, "
        " temperature_f, raw_json) VALUES (?,?,?,?,?,?,?)",
        (time.time(), station, city, obs_ts, iso, temp_f,
         json.dumps(raw_compact)),
    )
    return cur.rowcount > 0


def cycle(conn: sqlite3.Connection) -> dict:
    """One pass over all stations. Returns stats dict."""
    stats = {"fetched": 0, "new_rows": 0, "stations_ok": 0, "stations_fail": 0}
    for city, station in KALSHI_CITY_STATION.items():
        if not _running:
            break
        observations = _fetch_station_observations(station)
        if observations is None:
            stats["stations_fail"] += 1
            continue
        stats["stations_ok"] += 1
        stats["fetched"] += len(observations)
        for props in observations:
            if _persist_observation(conn, station, city, props):
                stats["new_rows"] += 1
        time.sleep(0.2)  # rate-limit politeness to NWS
    return stats


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30)
    conn.executescript(SCHEMA)
    log.info("starting METAR logger → %s (%d stations)", DB_PATH, len(KALSHI_CITY_STATION))

    while _running:
        t0 = time.time()
        signal.signal(signal.SIGALRM, _cycle_wall_clock_timeout)
        signal.alarm(MAX_CYCLE_SEC)
        try:
            stats = cycle(conn)
            log.info("cycle: %d new rows, %d/%d stations ok, %d obs fetched, %.1fs",
                     stats["new_rows"],
                     stats["stations_ok"],
                     stats["stations_ok"] + stats["stations_fail"],
                     stats["fetched"], time.time() - t0)
        except TimeoutError as e:
            log.error("cycle wall-clock alarm: %s", e)
        finally:
            signal.alarm(0)

        # Sleep remainder, wakeable every 5s for clean Ctrl-C.
        elapsed = time.time() - t0
        end = time.time() + max(0.0, CYCLE_INTERVAL_SEC - elapsed)
        while _running and time.time() < end:
            time.sleep(min(5.0, end - time.time()))

    conn.close()
    log.info("clean shutdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
