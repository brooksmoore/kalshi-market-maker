"""
cross_venue.py — detect arbitrage between Kalshi and Polymarket on
canonically-identical weather markets.

What it does:
  1. For each Polymarket market, find Kalshi markets with the same
     canonical resolution rule + target date.
  2. For each match, fetch live books on both venues.
  3. For each direction (BUY YES on K + BUY NO on P, or vice versa),
     compute fee-inclusive cost-per-unit. If < $1.00, the spread is
     locked-in arb.
  4. Return opportunities sorted by edge.

What it does NOT do (phase 2):
  - Execute. Polymarket execution is not built; opportunities are surfaced
    to the dashboard for the user to inspect.
  - Match across resolution rules. We require EXACT canonical equality —
    a Kalshi `>= 75` and a Polymarket `>= 76` are different bets, not
    a fuzzy arb. The 1°F-bin trap from v1 postmortem §4.2 applies across
    venues too.
  - Use Polymarket's last-trade prices. We refetch the live CLOB book on
    each pair to get the actual best ask. Cheaper than fetching all 37
    Polymarket books per cycle, more honest than trusting last-trade.
"""

from __future__ import annotations

import logging
from typing import Any

from config import MIN_EDGE_ARB, kalshi_trade_fee
from polymarket_client import polymarket_taker_fee
from resolution_rules import canonicalize_kalshi_market, parse_resolution_date


def _target_date(m: dict[str, Any]) -> str | None:
    """Resolution date as 'YYYY-MM-DD' parsed from the market title/question.

    Earlier versions used `close_time[:10]`. That broke for west-coast
    markets whose end-of-local-day close rolls into the next UTC day —
    a Kalshi market resolving on May 3 LA-local closed at 2026-05-04T08:00Z,
    matching a Polymarket market that actually settles on May 4 (one
    day later, independent event). Title parsing is the only reliable
    source — both venues encode the date plainly in their text.

    Returns None if no date is parseable; the market is then dropped
    rather than risk a phantom canonical match.
    """
    text = (
        m.get("title")
        or m.get("question")
        or ""
    )
    d = parse_resolution_date(text)
    return d.isoformat() if d is not None else None


def _canonical_key(m: dict[str, Any]) -> tuple | None:
    """Tuple used to index canonical-equivalent markets across venues.

    Two markets share this key iff they resolve on the same observable, with
    the same comparator, the same threshold(s), on the same date. Mismatches
    on any field mean these are independent bets even if they look related.
    """
    src = m.get("resolution_source")
    cmp_ = m.get("comparator")
    if not src or not cmp_:
        return None
    date = _target_date(m)
    if not date:
        return None
    return (
        src,
        cmp_,
        m.get("threshold"),
        m.get("range_low"),
        m.get("range_high"),
        date,
    )


def detect_cross_venue_arbs(
    kalshi_markets: list[dict[str, Any]],
    polymarket_markets: list[dict[str, Any]],
    polymarket_venue: Any,
) -> list[dict[str, Any]]:
    """Find Kalshi+Polymarket pairs whose combined cost < $1.00 after fees.

    Args:
        kalshi_markets: raw Kalshi market dicts (will be canonicalized here
            so this module doesn't depend on the cycle order).
        polymarket_markets: already-canonicalized Polymarket markets (as
            produced by PolymarketVenue.list_markets).
        polymarket_venue: a PolymarketVenue instance used to fetch books on
            paired markets only (cheap — typically <5 fetches per cycle).

    Returns: list of opportunity dicts, sorted by edge descending. Each
    opportunity describes BOTH legs of the arb — the user / executor must
    place them as a pair.
    """
    # Index Kalshi markets by canonical key. Multiple Kalshi markets can
    # share a key if Kalshi lists duplicate series (e.g. KXHIGHCHI + HIGHCHI
    # for Chicago). We keep them all and pick the best price per side.
    kalshi_by_key: dict[tuple, list[dict[str, Any]]] = {}
    for raw in kalshi_markets:
        canon = canonicalize_kalshi_market(raw)
        if canon is None:
            continue
        key = _canonical_key(canon)
        if key is None:
            continue
        kalshi_by_key.setdefault(key, []).append(canon)

    opportunities: list[dict[str, Any]] = []
    pairs_examined = 0

    for pm in polymarket_markets:
        key = _canonical_key(pm)
        if key is None:
            continue
        kalshi_candidates = kalshi_by_key.get(key)
        if not kalshi_candidates:
            continue

        pairs_examined += 1
        # Fetch the Polymarket book live for this paired market only.
        try:
            pm_book = polymarket_venue.get_book(pm["market_id"])
        except Exception as e:
            logging.debug("[CROSS] polymarket book fetch failed for %s: %s",
                          pm.get("market_id"), e)
            continue

        pm_yes_ask = pm_book.get("best_yes_ask")
        pm_no_ask = pm_book.get("best_no_ask")
        if pm_yes_ask is None and pm_no_ask is None:
            continue

        # Pick the best Kalshi quote per side across duplicate series.
        k_yes_quotes = [
            (k, float(k.get("yes_ask_dollars") or 0.0)) for k in kalshi_candidates
            if k.get("yes_ask_dollars") and 0 < float(k["yes_ask_dollars"]) < 1
        ]
        k_no_quotes = [
            (k, float(k.get("no_ask_dollars") or 0.0)) for k in kalshi_candidates
            if k.get("no_ask_dollars") and 0 < float(k["no_ask_dollars"]) < 1
        ]
        best_k_yes = min(k_yes_quotes, key=lambda x: x[1]) if k_yes_quotes else None
        best_k_no = min(k_no_quotes, key=lambda x: x[1]) if k_no_quotes else None

        # Direction A: BUY YES on Kalshi + BUY NO on Polymarket.
        if best_k_yes and pm_no_ask is not None and 0 < pm_no_ask < 1:
            kalshi_leg, k_yes = best_k_yes
            # Per-contract fees on BOTH legs (conservative taker on
            # Polymarket — see polymarket_client.POLYMARKET_TAKER_FEE_RATE).
            cost = k_yes + pm_no_ask
            fee = kalshi_trade_fee(1, k_yes) + polymarket_taker_fee(1, pm_no_ask)
            cost_with_fee = cost + fee
            edge = 1.0 - cost_with_fee
            if edge >= MIN_EDGE_ARB:
                opportunities.append(_make_opp(
                    direction="K_YES + P_NO",
                    kalshi_leg=kalshi_leg, k_yes=k_yes,
                    polymarket_leg=pm, p_no=pm_no_ask,
                    cost=cost, cost_with_fee=cost_with_fee, edge=edge,
                ))

        # Direction B: BUY NO on Kalshi + BUY YES on Polymarket.
        if best_k_no and pm_yes_ask is not None and 0 < pm_yes_ask < 1:
            kalshi_leg, k_no = best_k_no
            cost = k_no + pm_yes_ask
            fee = kalshi_trade_fee(1, k_no) + polymarket_taker_fee(1, pm_yes_ask)
            cost_with_fee = cost + fee
            edge = 1.0 - cost_with_fee
            if edge >= MIN_EDGE_ARB:
                opportunities.append(_make_opp(
                    direction="K_NO + P_YES",
                    kalshi_leg=kalshi_leg, k_no=k_no,
                    polymarket_leg=pm, p_yes=pm_yes_ask,
                    cost=cost, cost_with_fee=cost_with_fee, edge=edge,
                ))

    opportunities.sort(key=lambda o: o["edge"], reverse=True)
    logging.info(
        "[CROSS] examined %d canonical pairs; found %d arb opportunities",
        pairs_examined, len(opportunities),
    )
    return opportunities


def _make_opp(
    direction: str,
    kalshi_leg: dict[str, Any],
    polymarket_leg: dict[str, Any],
    cost: float,
    cost_with_fee: float,
    edge: float,
    k_yes: float | None = None,
    k_no: float | None = None,
    p_yes: float | None = None,
    p_no: float | None = None,
) -> dict[str, Any]:
    return {
        "direction": direction,
        "edge": round(edge, 4),
        "cost": round(cost, 4),
        "cost_with_fees": round(cost_with_fee, 4),
        "resolution_source": kalshi_leg.get("resolution_source"),
        "comparator": kalshi_leg.get("comparator"),
        "threshold": kalshi_leg.get("threshold"),
        "range_low": kalshi_leg.get("range_low"),
        "range_high": kalshi_leg.get("range_high"),
        "target_date": _target_date(kalshi_leg),
        "kalshi": {
            "ticker": kalshi_leg.get("ticker"),
            "title": kalshi_leg.get("title"),
            "yes_ask": k_yes,
            "no_ask": k_no,
        },
        "polymarket": {
            "market_id": polymarket_leg.get("market_id"),
            "question": polymarket_leg.get("question") or polymarket_leg.get("title"),
            "yes_ask": p_yes,
            "no_ask": p_no,
        },
    }
