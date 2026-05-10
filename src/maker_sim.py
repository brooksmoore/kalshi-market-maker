"""
maker_sim.py — resolve pending paper maker orders against live book activity.

Phase 3b. Each cycle, walk all paper_orders WHERE status='pending':
  1. Re-fetch the book for that market.
  2. If best_ask on the side we want to BUY is STRICTLY BELOW our limit
     price, the spread crossed through us → mark filled at our limit.
     (Equality is a no-go because of unknown queue priority — see notes.)
  3. If now > expires_at, mark expired.
  4. Otherwise, leave pending; will check again next cycle.

Why STRICT inequality:
  When we see best_ask == our limit in a snapshot, we don't know whether
  the ask was there before or after we posted. If before, our bid sat
  behind it in the queue and likely didn't fill. To stay honest under
  cycle-grained polling, we require the ask to drop *below* our price —
  unambiguous evidence someone wanted to sell at a worse price than ours.

  Real continuous polling (sub-second) would catch fills at equality more
  reliably; cycle-grained sim trades that for conservatism. Phase 3c
  could add a higher-frequency monitoring thread for tighter accounting.

Adverse selection (postmortem §3.4) is NOT modeled in 3b — we'd need
sub-cycle book history to see whether mid moved through our limit and
back. The `paper_orders.fill_price = limit_price` assumption is correct
(we got our limit) but ignores the "got picked off" cost. Phase 3c
work.

When a maker fills, we write a normal `trades` row (paper_trade=1, mode
'paper:maker') so reconcile.py settles it like any other paper trade
when the venue's oracle resolves the market.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import storage


def _venue_for(name: str) -> Any | None:
    """Lazy-import venue clients (avoids cycles + lets tests stub)."""
    if name == "kalshi":
        import kalshi_venue
        return kalshi_venue.KalshiVenue()
    if name == "polymarket":
        import polymarket_client
        return polymarket_client.PolymarketVenue()
    return None


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'


def _is_expired(expires_at: str) -> bool:
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except Exception:
        return False
    return datetime.now(timezone.utc) >= exp


def resolve_pending_orders(sleep_between: float = 0.0) -> dict:
    """Resolve all pending paper maker orders against live books.

    Returns: {checked, filled, expired, still_pending, errors,
              by_venue: {...}}
    """
    summary: dict[str, Any] = {
        "checked": 0, "filled": 0, "expired": 0,
        "still_pending": 0, "errors": 0, "by_venue": {},
    }

    pending = storage.get_pending_paper_orders()
    if not pending:
        return summary

    venues: dict[str, Any] = {}

    # Cache books per (venue, market_id) — multiple orders can rest on
    # the same market.
    book_cache: dict[tuple[str, str], dict | None] = {}

    for order in pending:
        venue_name = order["venue"]
        market_id = order["market_id"]
        side = order["side"]
        limit_price = float(order["limit_price"])
        target_contracts = int(order["target_contracts"])

        summary["checked"] += 1
        v_stats = summary["by_venue"].setdefault(
            venue_name,
            {"checked": 0, "filled": 0, "expired": 0,
             "still_pending": 0, "errors": 0},
        )
        v_stats["checked"] += 1

        # Expiry check first — cheap and common.
        if _is_expired(order["expires_at"]):
            storage.mark_paper_order_expired(int(order["id"]))
            summary["expired"] += 1
            v_stats["expired"] += 1
            logging.debug(
                "[MAKER_SIM] order#%d EXPIRED %s %s @%.4f",
                order["id"], venue_name, market_id, limit_price,
            )
            continue

        if venue_name not in venues:
            venues[venue_name] = _venue_for(venue_name)
        venue = venues[venue_name]
        if venue is None:
            summary["errors"] += 1
            v_stats["errors"] += 1
            continue

        cache_key = (venue_name, market_id)
        if cache_key not in book_cache:
            try:
                book_cache[cache_key] = venue.get_book(market_id)
            except Exception as e:
                logging.warning(
                    "[MAKER_SIM] book fetch failed for %s/%s: %s",
                    venue_name, market_id, e,
                )
                book_cache[cache_key] = None
                summary["errors"] += 1
                v_stats["errors"] += 1
                continue

        book = book_cache[cache_key]
        if book is None:
            summary["still_pending"] += 1
            v_stats["still_pending"] += 1
            continue

        best_ask = book.get(f"best_{side}_ask")
        if best_ask is None or not (0 < best_ask < 1):
            summary["still_pending"] += 1
            v_stats["still_pending"] += 1
            continue

        # Strict-below check (queue priority assumed worst-case).
        if best_ask >= limit_price:
            summary["still_pending"] += 1
            v_stats["still_pending"] += 1
            continue

        # Fill triggered — write a paper trade row, then mark order filled.
        try:
            opp = json.loads(order.get("opp_json") or "{}")
        except Exception:
            opp = {}
        # Reconstruct the minimum opp shape storage.log_trade needs.
        opp.setdefault("ticker", market_id)
        opp.setdefault("market_id", market_id)
        opp.setdefault("venue", venue_name)
        opp.setdefault("city", opp.get("city", ""))
        opp.setdefault("market_type", opp.get("market_type", "high_temp"))
        opp.setdefault("action", order["action"])
        opp.setdefault("recommended_size", limit_price * target_contracts)
        opp.setdefault("raw_probability", float(order.get("calibrated_p") or 0.5))
        opp.setdefault("calibrated_p", float(order.get("calibrated_p") or 0.5))
        opp.setdefault("edge", float(order.get("edge_at_post") or 0.0))
        opp["paper_trade"] = 1
        opp["entry_price"] = limit_price

        fill = {
            "fill_price": limit_price,
            "fill_count": target_contracts,
            "mode": "paper:maker",
            "order_id": f"paper:{order['id']}",
            "notes": (
                f"maker_filled: book_crossed_below_{limit_price:.4f} "
                f"(best_ask={best_ask:.4f})"
            ),
        }
        trade_id = storage.log_trade(opp, fill)
        storage.mark_paper_order_filled(
            int(order["id"]), limit_price, target_contracts, int(trade_id),
        )
        summary["filled"] += 1
        v_stats["filled"] += 1
        logging.info(
            "[MAKER_SIM] order#%d FILLED %s %s @%.4f x%d (best_ask now %.4f) → trade#%d",
            order["id"], venue_name, market_id, limit_price,
            target_contracts, best_ask, trade_id,
        )

    return summary
