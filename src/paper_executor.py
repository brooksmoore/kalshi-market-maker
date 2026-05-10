"""
paper_executor.py — honest paper fill against a snapshot book.

Phase 3a: taker-only VWAP walk against snapshot depth.
Phase 3b: maker mode added. A maker order persists as a `paper_orders`
row with status='pending'; maker_sim resolves it on subsequent cycles
when the book actually crosses our limit.

Implements the discipline from v1 postmortem §3 ("the misleading paper
sessions") and the project memory:
  - Fills only against real book activity. Taker against snapshot depth;
    maker against subsequent book activity that actually reaches our price.
  - Walks levels with VWAP. If a level is thin, the next level pays a
    higher price.
  - Mark-to-mid round-trip is the dashboard's job; this module only handles
    the entry fill.
  - Resolution comes from the venue's oracle, never from our forecast.
    See reconcile.py.

Conservative choices:
  - Taker: depth-clamps to snapshot, no fantasy fills.
  - Maker: requires best_ask STRICTLY BELOW limit_price for fill (queue
    priority assumed worst-case). Conservative — real continuous fills
    might happen at limit; cycle-grained sim won't catch those. Honest
    direction: under-report fills rather than overclaim.
  - Fees come from venue.fee_for_trade. Polymarket = 0 today.

Returns the same fill-result shape as executor.execute_opportunity so
storage.log_trade and downstream code don't need to fork on venue.
For maker mode, returns filled=False with mode='paper:maker:pending' and
order_id=<paper_orders.id>; the cycle treats this as a pending verdict.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import storage
from config import MIN_EDGE

# How long a paper maker order sits before expiring without fill. One
# cycle's worth so the resolution pass on the NEXT cycle gets a fair
# chance to fill it. Set higher (multiple cycles) to be more patient.
MAKER_REST_SECONDS: int = 300  # one 5-min cycle


def _contracts_for(opp: dict[str, Any], price: float) -> int:
    if opp.get("contracts"):
        return int(opp["contracts"])
    size_usd = float(opp.get("recommended_size", 0.0))
    if price <= 0:
        return 0
    return max(1, int(size_usd / price))


def _walk_buy_levels(
    depth_fn: Any, best_ask: float, target_contracts: int,
    edge_at_price_fn: Any | None = None,
    fee_per_contract_fn: Any | None = None,
    min_edge: float = MIN_EDGE,
    increment: float = 0.01,
    max_price: float = 0.99,
) -> list[tuple[float, int]]:
    """Walk the book one tick at a time, returning [(price, contracts), ...].

    Stops when target_contracts is reached, when no more depth exists at
    or below max_price, or when the FEE-ADJUSTED edge at the next price
    level falls below min_edge (avoid chasing into fee-eating territory).

    depth_fn(p) must return total contracts available at price <= p.
    edge_at_price_fn(p), if given, returns the gross edge of buying at p.
    fee_per_contract_fn(p), if given, returns the per-contract fee at p.
    If (edge - fee) at the next tick falls below min_edge we stop walking.
    """
    levels: list[tuple[float, int]] = []
    accumulated = 0
    prev_total = 0
    p = round(best_ask, 4)
    while p <= max_price and accumulated < target_contracts:
        if edge_at_price_fn is not None:
            net = edge_at_price_fn(p)
            if fee_per_contract_fn is not None:
                net = net - fee_per_contract_fn(p)
            if net < min_edge:
                break
        try:
            total_at_p = int(depth_fn(p))
        except Exception:
            break
        new_at_this_level = max(0, total_at_p - prev_total)
        if new_at_this_level > 0:
            take = min(new_at_this_level, target_contracts - accumulated)
            levels.append((p, take))
            accumulated += take
        prev_total = total_at_p
        p = round(p + increment, 4)
    return levels


def _post_paper_maker(opp: dict[str, Any], venue: Any) -> dict[str, Any]:
    """Phase-3b: post a maker order at best_ask - 1 tick. Stores a
    paper_orders row; maker_sim resolves it on subsequent cycles."""
    market_id = opp.get("market_id") or opp.get("ticker") or ""
    action = opp.get("action", "BUY YES")
    side = "yes" if action == "BUY YES" else "no"
    calibrated_p = float(opp.get("calibrated_p", 0.5))

    result: dict[str, Any] = {
        "filled": False, "fill_price": 0.0, "fill_count": 0,
        "order_id": None, "mode": "paper:maker:pending", "notes": "",
        "fees_paid": 0.0,
    }
    try:
        book = venue.get_book(market_id)
    except Exception as e:
        result["notes"] = f"book_fetch_error:{e!s:.50s}"
        return result

    best_ask = book.get(f"best_{side}_ask")
    depth_fn = book.get(f"{side}_depth_at_price")
    if best_ask is None or not (0 < best_ask < 1):
        result["notes"] = f"no_book_{side}"
        return result

    # Depth sanity check — a market with literally zero contracts on the
    # ask side has no incoming flow that could ever cross our maker post.
    # We don't require the book to cover our full size (book can refill),
    # but a fully empty side means this market is dead/illiquid.
    total_depth = 0
    if depth_fn is not None:
        try:
            total_depth = int(depth_fn(0.99))
        except Exception:
            total_depth = 0
    if total_depth <= 0:
        result["notes"] = f"maker_no_depth_{side}"
        return result

    # Maker price = best_ask - 1 cent (1 tick inside). On Polymarket fine-
    # grain ticks (0.001) we still post 1c inside; smaller offsets give
    # essentially zero fill probability across a 5-min window.
    limit_price = round(max(0.01, best_ask - 0.01), 4)

    # Fee at the maker limit. For Polymarket weather, mode='maker' is 0
    # today; we still subtract it so the gate is venue-agnostic and any
    # future maker fee flows through automatically.
    maker_fee_per_contract = float(
        venue.fee_for_trade(limit_price, 1, side, mode="maker")
    )

    gross_edge_at_limit = (
        (1.0 - calibrated_p) - limit_price if side == "no"
        else calibrated_p - limit_price
    )
    net_edge_at_limit = gross_edge_at_limit - maker_fee_per_contract
    if net_edge_at_limit < MIN_EDGE:
        result["notes"] = (
            f"maker_edge_below_floor:gross={gross_edge_at_limit:.3f} "
            f"fee/c={maker_fee_per_contract:.4f} net={net_edge_at_limit:.3f}"
        )
        return result

    target_contracts = _contracts_for(opp, limit_price)
    if target_contracts < 1:
        result["notes"] = "zero_contracts"
        return result

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=MAKER_REST_SECONDS)
    ).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'

    order_id = storage.insert_paper_order(
        venue=opp.get("venue", venue.name),
        market_id=market_id,
        action=action,
        side=side,
        limit_price=limit_price,
        target_contracts=target_contracts,
        calibrated_p=calibrated_p,
        edge_at_post=float(opp.get("edge", 0.0)),
        expires_at=expires_at,
        opp_json=json.dumps({
            k: v for k, v in opp.items()
            if k != "members"  # drop heavy ensemble list
        }, default=str),
    )

    result.update(
        order_id=str(order_id),
        notes=f"posted@{limit_price:.4f}_for_{target_contracts}_contracts",
    )
    return result


def execute_paper_opportunity(
    opp: dict[str, Any], venue: Any, mode: str = "maker"
) -> dict[str, Any]:
    """Paper-fill one opportunity against `venue`'s live book snapshot.

    mode='maker' (default): post a virtual limit order at best_ask - 1c.
        Returns immediately with {filled: False, mode: 'paper:maker:pending',
        order_id: <paper_orders.id>}. maker_sim resolves it on subsequent
        cycles when the book actually crosses our limit.
    mode='taker': existing VWAP walk against snapshot depth. Fills
        immediately at the walked price, paying the spread.

    Maker is preferred for Polymarket — wider spreads, zero fees both ways
    means waiting costs nothing and saves the spread.
    """
    if mode == "maker":
        return _post_paper_maker(opp, venue)
    market_id = opp.get("market_id") or opp.get("ticker")
    action = opp.get("action", "BUY YES")
    side = "yes" if action == "BUY YES" else "no"
    calibrated_p = float(opp.get("calibrated_p", 0.5))

    result: dict[str, Any] = {
        "filled": False, "fill_price": 0.0, "fill_count": 0,
        "order_id": None, "mode": "paper", "notes": "",
        "fees_paid": 0.0,
    }
    if not market_id:
        result["notes"] = "no_market_id"
        return result

    try:
        book = venue.get_book(market_id)
    except Exception as e:
        logging.warning("[PAPER] book fetch failed for %s: %s", market_id, e)
        result["notes"] = f"book_fetch_error:{e!s:.50s}"
        return result

    best_ask = book.get(f"best_{side}_ask")
    depth_fn = book.get(f"{side}_depth_at_price")
    if best_ask is None or depth_fn is None or not (0 < best_ask < 1):
        result["notes"] = f"no_book_{side}"
        return result

    contracts_wanted = _contracts_for(opp, best_ask)
    if contracts_wanted < 1:
        result["notes"] = "zero_contracts"
        return result

    # Edge function for walk-stop, in probability/per-$1-payout units.
    # Stops walking up the book once the next tick's net (after slippage
    # AND fees) would put us below MIN_EDGE — same posture as the live
    # Kalshi fill path.
    def edge_at(price: float) -> float:
        if price <= 0 or price >= 1:
            return -1.0
        return (1.0 - calibrated_p) - price if side == "no" else calibrated_p - price

    def fee_per_contract_at(price: float) -> float:
        # Per-contract fee in dollars (== probability units, since each
        # contract pays $1). venue.fee_for_trade returns a TOTAL for the
        # given contract count, so we ask for 1 contract here.
        return float(venue.fee_for_trade(price, 1, side, mode="taker"))

    levels = _walk_buy_levels(
        depth_fn, best_ask, contracts_wanted,
        edge_at_price_fn=edge_at,
        fee_per_contract_fn=fee_per_contract_at,
        min_edge=MIN_EDGE,
    )
    if not levels:
        result["notes"] = f"no_fillable_depth_at_{best_ask:.3f}"
        return result

    total_filled = sum(qty for _, qty in levels)
    if total_filled <= 0:
        result["notes"] = "zero_filled_after_walk"
        return result

    cost = sum(price * qty for price, qty in levels)
    vwap = cost / total_filled
    fee = float(venue.fee_for_trade(vwap, total_filled, side, mode="taker"))
    fee_per_contract = fee / max(total_filled, 1)
    net_edge = edge_at(vwap) - fee_per_contract
    if net_edge < MIN_EDGE:
        result["notes"] = (
            f"fees_eat_edge:vwap={vwap:.3f} gross_edge={edge_at(vwap):.3f} "
            f"fee/c={fee_per_contract:.4f} net_edge={net_edge:.3f}"
        )
        return result

    if total_filled < contracts_wanted:
        logging.debug(
            "[PAPER] %s sized down %d→%d (depth-clamped, +EV preserved)",
            market_id, contracts_wanted, total_filled,
        )

    result.update(
        filled=True,
        fill_price=round(vwap, 4),
        fill_count=int(total_filled),
        mode="paper",
        notes=(
            f"paper:vwap_{len(levels)}_levels:"
            f"best_ask={best_ask:.4f}:fee=${fee:.4f}"
        ),
        fees_paid=round(fee, 4),
    )
    return result
