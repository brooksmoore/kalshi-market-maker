"""
reconcile.py — settle local trades against each venue's own outcome.

For each open trade (no matching results row), fetch the market's resolution
status from the venue that owns it. If resolved, compute P&L and write a
results row.

Phase 3a: multi-venue. Kalshi resolves via market.status='finalized' +
market.result; Polymarket via UMA-arbitrated outcome (closed/resolved +
winner). The venue.get_resolution() abstraction hides the API differences.

CRITICAL (v1 postmortem §3.1, project memory): the outcome MUST come from
the venue's own settlement record, never from our forecast or a third-party
weather feed. A trade Polymarket settles NO is a losing trade for us even
if NWS data says it should have been YES.

Called once per scan cycle from main.run_cycle() and standalone via
scripts/reconcile_trades.py.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from config import DB_FILE, kalshi_trade_fee
from storage import NON_DRYRUN_SQL, NOTES_VALID_SQL


_OPEN_TRADES_SQL = f"""
    SELECT t.id, t.ticker, t.action, t.entry_price, t.contracts,
           COALESCE(t.venue, 'kalshi') AS venue
    FROM trades t
    LEFT JOIN results r ON t.id = r.trade_id
    WHERE r.id IS NULL
      AND {NON_DRYRUN_SQL}
      AND {NOTES_VALID_SQL}
"""


def _compute_pnl(action: str, entry_price: float, contracts: int,
                 result: str, venue: str) -> tuple[str, float, float]:
    """Returns (outcome, exit_price, pnl_dollars). outcome is 'yes' or 'no'.

    Fees are venue-specific: Kalshi charges per the audit-M7 formula;
    Polymarket charges zero today.
    """
    won = (action == "BUY YES" and result == "yes") or (action == "BUY NO" and result == "no")
    fee = kalshi_trade_fee(contracts, entry_price) if venue == "kalshi" else 0.0
    if won:
        exit_price = 1.0
        pnl = (1.0 - entry_price) * contracts - fee
    else:
        exit_price = 0.0
        pnl = -entry_price * contracts - fee
    return result, exit_price, round(pnl, 4)


def _normalize_outcome(raw: Any) -> str | None:
    """Resolutions come back in different shapes per venue (Kalshi: 'yes'/'no';
    Polymarket: 'Yes'/'No' or boolean-ish). Normalize to 'yes'/'no' or None."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("yes", "true", "1"):
        return "yes"
    if s in ("no", "false", "0"):
        return "no"
    return None


def _venue_for(name: str) -> Any | None:
    """Lazy-import the venue clients so reconcile is importable in tests
    without a live network."""
    if name == "kalshi":
        import kalshi_venue
        return kalshi_venue.KalshiVenue()
    if name == "polymarket":
        import polymarket_client
        return polymarket_client.PolymarketVenue()
    return None


def reconcile_settled_trades(sleep_between: float = 0.1) -> dict:
    """Scan open trades across all venues, settle any whose markets resolved.

    Returns: {checked, settled, still_open, api_errors, by_venue: {...}}.
    """
    summary = {
        "checked": 0, "settled": 0, "still_open": 0, "api_errors": 0,
        "by_venue": {},
    }

    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        rows = conn.execute(_OPEN_TRADES_SQL).fetchall()
    if not rows:
        return summary

    # Construct each venue once (constructors are cheap; venue clients
    # cache nothing beyond the in-process session).
    venues: dict[str, Any] = {}

    # Cache resolutions per (venue, ticker) within this call — multiple
    # trades commonly share a market.
    resolution_cache: dict[tuple[str, str], dict | None] = {}

    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        for trade_id, ticker, action, entry_price, contracts, venue_name in rows:
            summary["checked"] += 1
            v_stats = summary["by_venue"].setdefault(
                venue_name, {"checked": 0, "settled": 0, "still_open": 0, "api_errors": 0}
            )
            v_stats["checked"] += 1

            if not ticker:
                continue
            if venue_name not in venues:
                venues[venue_name] = _venue_for(venue_name)
            venue = venues[venue_name]
            if venue is None:
                v_stats["api_errors"] += 1
                summary["api_errors"] += 1
                continue

            cache_key = (venue_name, ticker)
            if cache_key not in resolution_cache:
                try:
                    resolution_cache[cache_key] = venue.get_resolution(ticker)
                except Exception as e:
                    logging.warning("[RECONCILE] %s get_resolution(%s) failed: %s",
                                    venue_name, ticker, e)
                    resolution_cache[cache_key] = None
                    v_stats["api_errors"] += 1
                    summary["api_errors"] += 1
                time.sleep(sleep_between)

            resolution = resolution_cache[cache_key]
            if resolution is None:
                v_stats["still_open"] += 1
                summary["still_open"] += 1
                continue

            result = _normalize_outcome(resolution.get("outcome"))
            if result not in ("yes", "no"):
                # Resolved but ambiguous (UMA can return "Tie"/"50-50" etc).
                # Skip rather than guess. A separate alert path could be
                # added in 3b for review.
                v_stats["still_open"] += 1
                summary["still_open"] += 1
                continue

            outcome, exit_price, pnl = _compute_pnl(
                action or "", float(entry_price or 0), int(contracts or 0),
                result, venue_name,
            )
            conn.execute(
                """
                INSERT INTO results (trade_id, outcome, exit_price, profit_loss,
                                     resolved_at, venue)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(trade_id), outcome, exit_price, pnl,
                    datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z',
                    venue_name,
                ),
            )
            v_stats["settled"] += 1
            summary["settled"] += 1
            logging.info(
                "[RECONCILE] [%s] %s trade#%d %s @%.4f x%d → %s, pnl=$%.4f",
                venue_name, ticker, trade_id, action, entry_price, contracts,
                outcome, pnl,
            )
        conn.commit()

    return summary
