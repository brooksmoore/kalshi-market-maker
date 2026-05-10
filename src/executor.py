"""
executor.py — maker-first order execution state machine.

For weather-core opportunities:
  Phase 1 (maker): post a limit at best_ask - 1c, poll for up to 90s.
  Phase 2 (taker): on no-fill, cancel; if live edge still >= MIN_EDGE, cross
                    the spread at best_ask * (1 + SLIPPAGE_BUFFER_PCT).

For arb groups:
  arb_execute_group: fire all legs as takers simultaneously, wait 10s, and
  if any leg came up short, market-sell every filled leg to unwind.

Also exposes process_exits + should_exit_position for closing open
positions (audit E6/E7). 2026-05-07: process_exits is now wired into
main.run_cycle; previously should_exit_position was dead code.

Addresses audit items:
  E3 — require live depth >= 2 * contracts_wanted
  E4 — split across next book level if one level is thin
  E5 — recompute edge from the live book before placing anything
  E6 — Bayesian exit rule with recomputed live edge
  E7 — take-profit when price reaches 95% of max payoff
  B4 — rollback on partial-fill across arb legs
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime

import kalshi_client
from config import (
    MAKER_PRICE_OFFSET_CENTS,
    MAKER_REST_SECONDS,
    MIN_EDGE,
    SLIPPAGE_BUFFER_PCT,
    kalshi_trade_fee,
)

POLL_INTERVAL_SEC = 5

# After any abandonment where we cannot prove fill_count==0, block re-entry on
# the same ticker for this long. Prevents the failure mode that produced
# DC-MAY06-T73 @ 105 contracts: bot retried 5× while Kalshi quietly filled
# every leg. 5 min is long enough for /portfolio/positions to reflect reality
# yet short enough to re-enter within a normal strategy window.
COOLDOWN_AFTER_UNCERTAIN_SEC = 300

# In-process; resets on bot restart. That's fine — restart is a natural moment
# to reconcile via scripts/backfill_kalshi_positions.py before retrading.
_TICKER_COOLDOWN: dict[str, float] = {}


def _arm_cooldown(ticker: str, seconds: int = COOLDOWN_AFTER_UNCERTAIN_SEC) -> None:
    _TICKER_COOLDOWN[ticker] = time.time() + seconds
    logging.warning(
        "[EXEC] cooldown armed for %s (%ds) — will refuse re-entry until "
        "Kalshi state is observable",
        ticker, seconds,
    )


def _in_cooldown(ticker: str) -> float:
    """Return seconds remaining if ticker is in cooldown, else 0."""
    until = _TICKER_COOLDOWN.get(ticker)
    if not until:
        return 0.0
    remaining = until - time.time()
    if remaining <= 0:
        _TICKER_COOLDOWN.pop(ticker, None)
        return 0.0
    return remaining


def _fill_count(status: dict) -> int:
    """Kalshi v2 reports fills as fill_count_fp (a decimal string like '5.00')."""
    fp = status.get("fill_count_fp")
    if fp is not None:
        try:
            return int(float(fp))
        except (ValueError, TypeError):
            pass
    return int(status.get("fill_count") or status.get("filled_count") or 0)


def _verify_after_cancel(
    order_id: str,
    price_dollars: float,
    mode_label: str,
    ticker: str,
) -> dict | None:
    """Re-check Kalshi for fills after we've issued cancel_order.

    The race we're closing: maker/taker poll loop sees filled==0 at deadline,
    cancel_order fires, but the order had already filled on Kalshi (or fills
    in the next tick). Without this check the trade vanishes from trades.db
    while Kalshi-side it's a real position — exactly the failure that left
    ~$275 of stranded fills today.

    Returns a partial fill_result dict (filled=True, fill_count, fill_price,
    order_id, mode, notes) when a late fill is detected, else None. If we
    can't get a clean status read we arm the per-ticker cooldown so the next
    cycle won't re-fire and stack a duplicate position.

    2026-05-07 fix (checkpoint open-issue #1): the previous implementation
    polled 3× / 0.5s = 1.5s and bailed if status was still 'resting'. On
    Kalshi demo the cancel often takes longer to propagate, the order
    then partially-fills, and the bot has no DB record of the position.
    The 2026-05-07 PHIL incident: order ended at status='canceled'
    fill_count_fp=21 (partial fill before cancel propagated) but our
    status polling timed out before that final state was visible.
    We now:
      1. Poll up to 12× / 0.5s = 6s — long enough for Kalshi demo to
         resolve the order to a terminal state.
      2. If still 'resting' midway, retry the cancel once (cancel may
         have been silently dropped).
      3. The /portfolio/orders/{id} endpoint returns the order
         regardless of terminal status, so any non-zero fill_count we
         see at any point is authoritative — no separate fallback
         endpoint is needed (and the executed-only endpoint would miss
         status='canceled' partial fills).
    """
    last_status: dict | None = None
    cancel_retried = False
    for attempt in range(12):
        time.sleep(0.5)
        last_status = kalshi_client.get_order_status(order_id) or {}
        if last_status:
            filled = _fill_count(last_status)
            status = (last_status.get("status") or "").lower()
            # Any non-zero fill is authoritative regardless of terminal
            # state — covers both status='executed' fc>0 and the
            # 2026-05-07 PHIL pattern of status='canceled' fc=21.
            if filled > 0:
                logging.warning(
                    "[EXEC] LATE FILL detected for %s order=%s mode=%s "
                    "filled=%d status=%s — recording as real trade",
                    ticker, order_id, mode_label, filled, status,
                )
                return {
                    "filled": True,
                    "fill_price": price_dollars,
                    "fill_count": filled,
                    "order_id": order_id,
                    "mode": f"{mode_label}_late_fill",
                    "notes": f"{mode_label}_late_fill_after_cancel",
                }
            # Confirmed zero with a terminal status: safe, no cooldown.
            if status in ("canceled", "cancelled", "expired"):
                return None
            # Mid-window retry: if status is still 'resting' after ~3s,
            # the original cancel may have been dropped silently. Retry
            # once and continue polling.
            if status == "resting" and attempt == 6 and not cancel_retried:
                logging.info(
                    "[EXEC] order %s still resting at %.1fs — retrying cancel",
                    order_id, (attempt + 1) * 0.5,
                )
                kalshi_client.cancel_order(order_id)
                cancel_retried = True

    # 6s passed and order is in some non-terminal limbo. Arm cooldown so
    # the next cycle doesn't re-fire on top of a possible silent fill.
    # The next cycle's fresh get_order_status will resolve it.
    logging.warning(
        "[EXEC] cancel verification inconclusive for %s order=%s after 6s — "
        "arming cooldown (last_status=%s)",
        ticker, order_id, last_status,
    )
    _arm_cooldown(ticker)
    return None


def _contracts_for(opp: dict, price: float) -> int:
    if opp.get("contracts"):
        return int(opp["contracts"])
    size_usd = float(opp.get("recommended_size", 0.0))
    if price <= 0:
        return 0
    return max(1, int(size_usd / price))


def _recompute_edge(opp: dict, live_book: dict) -> tuple[float, float, str]:
    """Recompute edge from live book. Returns (net_edge, entry_price, side)."""
    calibrated_p = float(opp.get("calibrated_p", 0.5))
    action = opp.get("action", "BUY YES")
    if action == "BUY NO":
        ask = live_book.get("best_no_ask")
        if ask is None or not (0 < ask < 1):
            return -1.0, 0.0, "no"
        return (1.0 - calibrated_p) - ask, ask, "no"
    ask = live_book.get("best_yes_ask")
    if ask is None or not (0 < ask < 1):
        return -1.0, 0.0, "yes"
    return calibrated_p - ask, ask, "yes"


def execute_opportunity(
    opp: dict,
    stop: threading.Event | None = None,
) -> dict:
    """Maker-first, taker fallback. Returns fill/abort dict.

    stop: threading.Event from main — when set, cancels any open order and
    returns immediately so the process can exit without waiting out the full
    maker window.
    """
    if stop is None:
        stop = threading.Event()

    ticker = opp["ticker"]
    result = {
        "filled": False,
        "fill_price": 0.0,
        "fill_count": 0,
        "order_id": None,
        "mode": "aborted",
        "notes": "",
    }

    if kalshi_client.is_blocked_insufficient_balance(ticker):
        result["notes"] = "skip_insufficient_balance_sticky"
        return result

    cd = _in_cooldown(ticker)
    if cd > 0:
        result["notes"] = f"cooldown_active:{int(cd)}s"
        return result

    live_book = kalshi_client.get_orderbook(ticker)
    net_edge, entry_price, side = _recompute_edge(opp, live_book)
    if net_edge < MIN_EDGE:
        result["notes"] = f"live_edge_below_floor:{net_edge:.3f}"
        return result

    contracts_wanted = _contracts_for(opp, entry_price)
    if contracts_wanted < 1:
        result["notes"] = "zero_contracts"
        return result

    # Depth-adaptive sizing. E3's spirit was "scale depth with size, don't
    # accept slippage that breaks edge"; the prior 2× all-or-nothing rule was
    # rejecting positive-EV partial fills. Instead: clamp size to whatever's
    # available at edge-preserving prices. A smaller filled position is still
    # +EV and Kelly is a cap, not a target — under-filling is safe.
    depth_fn = live_book["yes_depth_at_price"] if side == "yes" else live_book["no_depth_at_price"]
    calibrated_p = float(opp.get("calibrated_p", 0.5))

    def _live_edge_at(price: float) -> float:
        if price <= 0 or price >= 1:
            return -1.0
        return (1.0 - calibrated_p) - price if side == "no" else calibrated_p - price

    depth_inside = int(depth_fn(entry_price))
    fillable = depth_inside
    next_tick = round(entry_price + 0.01, 4)
    # Walk one tick if next level still preserves MIN_EDGE per contract.
    if _live_edge_at(next_tick) >= MIN_EDGE:
        fillable += int(depth_fn(next_tick))

    contracts_to_buy = min(contracts_wanted, fillable)
    if contracts_to_buy < 1:
        result["notes"] = (
            f"no_fillable_depth: inside {depth_inside}@{entry_price:.2f}, "
            f"next-tick edge<{MIN_EDGE}"
        )
        return result

    # Sanity floor: don't bother if the per-contract fee would eat the edge.
    # (Conservative 1-contract bound matches what strategy used to size this.)
    fee_pct = (
        kalshi_trade_fee(contracts_to_buy, entry_price)
        / max(contracts_to_buy * entry_price, 1e-9)
    )
    if (net_edge - fee_pct) < 0:
        result["notes"] = (
            f"fees_eat_edge: net_edge={net_edge:.3f} fee_pct={fee_pct:.3f}"
        )
        return result

    if contracts_to_buy < contracts_wanted:
        logging.debug(
            "[EXEC] %s sized down %d→%d (depth-clamped, +EV preserved)",
            ticker, contracts_wanted, contracts_to_buy,
        )
    contracts_wanted = contracts_to_buy

    # ── Phase 1: maker ────────────────────────────────────────────────────────
    maker_price_cents = int(round(entry_price * 100)) - MAKER_PRICE_OFFSET_CENTS
    maker_price_cents = max(1, min(99, maker_price_cents))
    place_attempt_ts = time.time()
    order_id = kalshi_client.place_limit_order(
        ticker, side, contracts_wanted, maker_price_cents
    )
    if not order_id:
        # If Kalshi flagged the ticker as insufficient_balance, don't bother
        # with the taker leg — same collateral check will reject it too.
        if kalshi_client.is_blocked_insufficient_balance(ticker):
            result["notes"] = "maker_place_failed"
            return result
        # No order_id back from Kalshi but no explicit reject either: the
        # request could have errored on our side OR could have been accepted
        # silently. 2026-05-07: recover the order_id by querying orders-by-
        # ticker before assuming silent-accept and arming cooldown. The
        # 18:36 CHI-B62.5 incident showed that silent-accept fills do
        # happen and orphan into trades.db's blind spot.
        time.sleep(1.0)  # let Kalshi reflect the order
        recovered = kalshi_client.find_recent_order(
            ticker, side, contracts_wanted, place_attempt_ts
        )
        if recovered and recovered.get("order_id"):
            order_id = recovered["order_id"]
            logging.warning(
                "[EXEC] recovered silent-accept maker order for %s "
                "oid=%s — proceeding with normal poll/verify path",
                ticker, order_id,
            )
            # Fall through to normal polling — same as the else: branch.
        else:
            _arm_cooldown(ticker)
            result["notes"] = "maker_place_failed_cooldown_armed"
            return result
    if order_id:
        # Debit the in-cycle cash counter so subsequent cash_ok checks see
        # the reservation. Order may cancel later (maker timeout) but Kalshi
        # holds the cash while it rests. Released when we cancel — accept
        # mild over-conservatism rather than over-deploying.
        import risk
        risk.record_deployment(contracts_wanted * (maker_price_cents / 100.0))
        deadline = time.time() + MAKER_REST_SECONDS
        maker_price_dollars = float(maker_price_cents) / 100.0
        while time.time() < deadline:
            if stop.wait(timeout=POLL_INTERVAL_SEC):
                kalshi_client.cancel_order(order_id)
                late = _verify_after_cancel(
                    order_id, maker_price_dollars, "maker", ticker)
                if late:
                    result.update(**late)
                    return result
                result["notes"] = "shutdown"
                return result
            status = kalshi_client.get_order_status(order_id) or {}
            filled = _fill_count(status)
            if filled >= contracts_wanted:
                result.update(
                    filled=True,
                    fill_price=maker_price_dollars,
                    fill_count=filled,
                    order_id=order_id,
                    mode="maker",
                    notes="maker_fill_full",
                )
                return result
            if filled > 0:
                # Partial maker fill — accept it and stop here.
                kalshi_client.cancel_order(order_id)
                result.update(
                    filled=True,
                    fill_price=maker_price_dollars,
                    fill_count=filled,
                    order_id=order_id,
                    mode="maker",
                    notes="maker_fill_partial",
                )
                return result

        # Maker unfilled at deadline — cancel and verify before moving on.
        # If a fill landed in the gap between our last poll and the cancel,
        # take the fill and stop (don't fall through to taker — that's how
        # we end up with twice the intended position).
        kalshi_client.cancel_order(order_id)
        late = _verify_after_cancel(
            order_id, maker_price_dollars, "maker", ticker)
        if late:
            result.update(**late)
            return result

    if stop.is_set():
        result["notes"] = "shutdown"
        return result

    # ── Phase 2: taker ────────────────────────────────────────────────────────
    live_book2 = kalshi_client.get_orderbook(ticker)
    net_edge2, entry_price2, side2 = _recompute_edge(opp, live_book2)
    if net_edge2 < MIN_EDGE:
        result["notes"] = f"taker_abort_edge:{net_edge2:.3f}"
        return result

    taker_price = entry_price2 * (1.0 + SLIPPAGE_BUFFER_PCT)
    taker_price_cents = int(math.ceil(taker_price * 100))
    taker_price_cents = max(1, min(99, taker_price_cents))
    taker_attempt_ts = time.time()
    order_id2 = kalshi_client.place_limit_order(
        ticker, side2, contracts_wanted, taker_price_cents
    )
    if not order_id2:
        # Same recovery as the maker path: a None order_id may mean
        # Kalshi accepted the order silently. Try to find it before
        # giving up — see CHI-B62.5 18:36 incident, which orphaned a
        # 28-contract fill via this exact path.
        time.sleep(1.0)
        recovered2 = kalshi_client.find_recent_order(
            ticker, side2, contracts_wanted, taker_attempt_ts
        )
        if recovered2 and recovered2.get("order_id"):
            order_id2 = recovered2["order_id"]
            logging.warning(
                "[EXEC] recovered silent-accept taker order for %s "
                "oid=%s — proceeding with normal poll/verify path",
                ticker, order_id2,
            )
        else:
            _arm_cooldown(ticker)
            result["notes"] = "taker_place_failed_cooldown_armed"
            return result
    # Debit the in-cycle counter — same rationale as the maker debit above.
    import risk
    risk.record_deployment(contracts_wanted * (taker_price_cents / 100.0))

    taker_price_dollars = taker_price_cents / 100.0
    deadline = time.time() + 10
    while time.time() < deadline:
        if stop.wait(timeout=2):
            kalshi_client.cancel_order(order_id2)
            late = _verify_after_cancel(
                order_id2, taker_price_dollars, "taker", ticker)
            if late:
                result.update(**late)
                return result
            result["notes"] = "shutdown"
            return result
        status = kalshi_client.get_order_status(order_id2) or {}
        filled = _fill_count(status)
        if filled >= contracts_wanted:
            result.update(
                filled=True,
                fill_price=taker_price_dollars,
                fill_count=filled,
                order_id=order_id2,
                mode="taker",
                notes="taker_fill_full",
            )
            return result
        if filled > 0:
            kalshi_client.cancel_order(order_id2)
            result.update(
                filled=True,
                fill_price=taker_price_dollars,
                fill_count=filled,
                order_id=order_id2,
                mode="taker",
                notes="taker_fill_partial",
            )
            return result

    # Taker timed out — cancel and verify. The original code just dropped any
    # late fill on the floor here; today's reconciliation showed exactly that
    # path stranding ~$275 of real positions in trades.db's blind spot.
    kalshi_client.cancel_order(order_id2)
    late = _verify_after_cancel(
        order_id2, taker_price_dollars, "taker", ticker)
    if late:
        result.update(**late)
        return result
    result["notes"] = "taker_no_fill"
    return result


def arb_execute_group(legs: list[dict]) -> list[dict]:
    """Fire every leg as a taker; if any leg fails, market-sell filled legs.

    Audit B4: rollback on partial-fill across legs.

    Preflight: re-fetches each leg's orderbook immediately before placing so
    the depth check is fresh (strategy_arb's check happens ~5–15s earlier;
    demo books drift in that window). If any leg has insufficient depth at
    its target price, abort the whole group BEFORE placing any orders.
    """
    if not legs:
        return []

    # ── Preflight depth verification ─────────────────────────────────────────
    # Require depth >= contracts_wanted + 1 buffer on every leg AT THE PRICE
    # we'll actually cross at (audit C6). Checking at the unadjusted leg
    # price was optimistic — the book one tick higher is often thinner,
    # so we'd pass preflight then partial-fill in placement and tip into
    # the rollback path.
    PREFLIGHT_BUFFER = 1
    for leg in legs:
        price = float(leg.get("entry_price") or leg.get("yes_price") or 0.0)
        wanted = int(leg.get("contracts") or 0)
        if wanted < 1 or not (0 < price < 1):
            continue  # this leg will be marked aborted in the placement loop
        try:
            ob = kalshi_client.get_orderbook(leg["ticker"])
        except Exception as e:
            logging.warning(
                "[EXEC][ARB] preflight orderbook fetch failed for %s: %s — aborting group",
                leg["ticker"], e,
            )
            return [
                {"ticker": l["ticker"], "filled": False, "fill_count": 0,
                 "order_id": None, "mode": "aborted",
                 "notes": "arb_preflight_orderbook_error"}
                for l in legs
            ]
        depth_fn = ob.get("yes_depth_at_price")
        if not depth_fn:
            return [
                {"ticker": l["ticker"], "filled": False, "fill_count": 0,
                 "order_id": None, "mode": "aborted",
                 "notes": "arb_preflight_no_book"}
                for l in legs
            ]
        # Match the placement-loop's cross price: leg price * (1+slippage),
        # ceiled to a cent and clamped to [1, 99].
        cross_cents = max(1, min(99, int(math.ceil(price * (1.0 + SLIPPAGE_BUFFER_PCT) * 100))))
        cross_price = cross_cents / 100.0
        depth_here = int(depth_fn(cross_price))
        if depth_here < wanted + PREFLIGHT_BUFFER:
            logging.info(
                "[EXEC][ARB] preflight FAIL %s: depth=%d at cross $%.2f "
                "(quoted $%.2f) < %d (wanted %d + buffer %d) — aborting group",
                leg["ticker"], depth_here, cross_price, price,
                wanted + PREFLIGHT_BUFFER, wanted, PREFLIGHT_BUFFER,
            )
            return [
                {"ticker": l["ticker"], "filled": False, "fill_count": 0,
                 "order_id": None, "mode": "aborted",
                 "notes": f"arb_preflight_depth:{leg['ticker']}_had_{depth_here}"}
                for l in legs
            ]

    # ── Place all legs as limit-takers ───────────────────────────────────────
    placed: list[tuple[dict, str | None]] = []
    for leg in legs:
        price = float(leg.get("entry_price") or leg.get("yes_price") or 0.0)
        contracts = int(leg.get("contracts") or 0)
        if contracts < 1 or not (0 < price < 1):
            placed.append((leg, None))
            continue
        taker_price = price * (1.0 + SLIPPAGE_BUFFER_PCT)
        cents = max(1, min(99, int(math.ceil(taker_price * 100))))
        oid = kalshi_client.place_limit_order(leg["ticker"], "yes", contracts, cents)
        placed.append((leg, oid))
        if oid:
            # Arb leg posted — debit cash counter so subsequent legs/opps in
            # this cycle see the reservation.
            import risk
            risk.record_deployment(contracts * (cents / 100.0))

    time.sleep(10)

    results: list[dict] = []
    filled_legs: list[tuple[dict, int]] = []
    any_short = False
    for leg, oid in placed:
        wanted = int(leg.get("contracts") or 0)
        if not oid:
            any_short = True
            results.append({
                "ticker": leg["ticker"],
                "filled": False,
                "fill_count": 0,
                "order_id": None,
                "mode": "aborted",
                "notes": "arb_place_failed",
            })
            continue
        status = kalshi_client.get_order_status(oid) or {}
        filled = _fill_count(status)
        if filled < wanted:
            any_short = True
            kalshi_client.cancel_order(oid)
        if filled > 0:
            filled_legs.append((leg, filled))
        price = float(leg.get("entry_price") or leg.get("yes_price") or 0.0)
        results.append({
            "ticker": leg["ticker"],
            "filled": filled == wanted,
            "fill_count": filled,
            "fill_price": round(price * (1.0 + SLIPPAGE_BUFFER_PCT), 4),
            "order_id": oid,
            "mode": "taker",
            "notes": "arb_leg",
        })

    if any_short and filled_legs:
        logging.warning("[EXEC][ARB] partial — unwinding %d filled leg(s)", len(filled_legs))
        # Track which legs actually unwound so we can flag the stranded ones
        # to the caller and persist them so risk + reconcile see them.
        unwound_tickers: set[str] = set()
        stranded: list[tuple[dict, int]] = []  # (leg, leftover_qty)

        for leg, qty in filled_legs:
            ticker = leg["ticker"]
            try:
                sell_oid = kalshi_client.sell_position(ticker, "yes", qty)
            except Exception as e:
                logging.error("[EXEC][ARB] rollback sell raised for %s: %s", ticker, e)
                stranded.append((leg, qty))
                continue
            if not sell_oid:
                logging.error(
                    "[EXEC][ARB] rollback sell returned no order_id for %s qty=%d "
                    "(likely no opposing bids)", ticker, qty,
                )
                stranded.append((leg, qty))
                continue

            # Poll the sell order — a market sell at 1¢ on a one-sided book
            # rests unfilled silently. Verify before declaring rollback done.
            time.sleep(2)
            sell_status = kalshi_client.get_order_status(sell_oid) or {}
            sell_filled = _fill_count(sell_status)
            if sell_filled >= qty:
                unwound_tickers.add(ticker)
                logging.info(
                    "[EXEC][ARB] rollback ok %s: sold %d/%d", ticker, sell_filled, qty,
                )
            else:
                # Cancel the unfilled remnant to avoid surprise fills later.
                try:
                    kalshi_client.cancel_order(sell_oid)
                except Exception:
                    pass
                leftover = qty - sell_filled
                logging.error(
                    "[EXEC][ARB] STRANDED %s: sold %d/%d, %d contracts still held",
                    ticker, sell_filled, qty, leftover,
                )
                stranded.append((leg, leftover))

        # Mark every result correctly so the caller (main._process_arb_group)
        # can route filled-but-not-rolled-back legs into trades.db rather than
        # silently treating them as failed.
        for r in results:
            if not r["filled"]:
                continue
            tk = r["ticker"]
            if tk in unwound_tickers:
                r["notes"] = "arb_rolled_back"
                r["filled"] = False
                continue
            # Stranded — reduce fill_count to the leftover qty if we partially
            # unwound, mark it so log_trade picks up a real position row.
            leftover_qty = next((q for lg, q in stranded if lg["ticker"] == tk), 0)
            if leftover_qty <= 0:
                # Defensive: shouldn't happen, but if neither unwound nor stranded
                # we can't know the state — flag conservatively as still filled.
                r["notes"] = "arb_rollback_state_unknown"
                continue
            r["fill_count"] = int(leftover_qty)
            r["notes"] = f"arb_stranded:{leftover_qty}_contracts_unsold"

        if stranded:
            logging.error(
                "[EXEC][ARB] %d leg(s) STRANDED — manual cleanup required: %s",
                len(stranded),
                ", ".join(f"{lg['ticker']}({q})" for lg, q in stranded),
            )

    return results


# ─── Exit logic constants (audit E6/E7, 2026-05-07 forecast-driven rewrite) ──
# Design philosophy (per user 2026-05-07): we entered based on the model's
# forecast. We exit based on the model's forecast. Market price movement is
# noisy (whales, MM withdrawals, late-day liquidity collapse) and not by
# itself a reliable signal that our prior was wrong. Three rules:
#
#   1. take_profit (bid-aware, asymmetric) — lock in near-certain wins ONLY
#      when there's a real bid we'd cross. No false-trigger risk.
#   2. forecast_inversion — the latest forecast says our entry is no longer
#      +EV at our cost basis. This is the primary, principled exit signal.
#
# Removed 2026-05-08: far_otm_safety_net (≥30c decay-based exit). It
# created a structural contradiction with entry logic — exit said
# "forecast might be perma-wrong, cut the loss" while entry said
# "forecast sees edge, get back in" on the same scan. Result was
# stop-out → re-entry oscillation on losing trades (DC, LA, CHI on
# 2026-05-07). Both modules now read the same premise: if the model
# still says edge, we hold; if it inverts, we exit via rule 2.

# 30-min cooldown after entry. Suppresses false alarms from the half-spread
# we paid at entry — most positions look "underwater" mark-to-mid from t=0
# by 4-6c. Applies to all rules.
MIN_HOLD_MINUTES_BEFORE_EXIT: int = 30

# Take-profit: asymmetric, only locks in gains. Two thresholds:
#   - VALUE: market value of our position (mid) has reached this level.
#   - BID:   the implied bid we'd actually cross is at least this. Without
#     this guard, take-profit fires when no buyer exists at the price (the
#     2026-05-06 empty-book lesson) and our sell rests unfilled.
TAKE_PROFIT_VALUE_THRESHOLD: float = 0.95
TAKE_PROFIT_BID_FLOOR: float = 0.85

# Forecast inversion: BOTH must hold.
#   1. cal_p has shifted by at least FORECAST_DRIFT_THRESHOLD AGAINST our
#      entry direction. For BUY NO that means cal_p went UP (forecast
#      thinks YES is more likely now); for BUY YES, cal_p went DOWN.
#   2. The trade is no longer +EV at our cost basis under the new
#      forecast — i.e. we'd reject this trade if presented fresh today.
FORECAST_DRIFT_THRESHOLD: float = 0.15
FORECAST_INVERSION_NEGATIVE_FLOOR: float = -0.05

def _minutes_held(position: dict) -> float | None:
    opened = position.get("opened_at")
    if not opened:
        return None
    try:
        from datetime import datetime as _dt, timezone as _tz
        t = _dt.fromisoformat(str(opened).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=_tz.utc)
        delta = _dt.now(_tz.utc) - t
        return delta.total_seconds() / 60.0
    except Exception:
        return None


def _live_position_value(book: dict, our_side: str) -> float | None:
    """Estimate current per-contract market value of our position.

    Uses mid (ask + bid)/2 when both sides are available. Kalshi only
    publishes bids on each side; the OPPOSITE-side bid is what we'd
    receive on a sell, derivable as (1 - other_side_ask). Falls back to
    our-side ask if the book is one-sided.
    """
    other_side = "yes" if our_side == "no" else "no"
    our_ask = book.get(f"best_{our_side}_ask")
    other_ask = book.get(f"best_{other_side}_ask")

    if our_ask is None or not (0 < our_ask < 1):
        return None

    if other_ask is not None and 0 < other_ask < 1:
        our_bid = 1.0 - other_ask
        if 0 < our_bid <= our_ask:
            return (our_ask + our_bid) / 2.0
    return our_ask  # one-sided book — best we can do


def should_exit_position(position: dict, current_cal_p: float | None,
                         book: dict | None = None) -> tuple[bool, str]:
    """Decide whether to close an open position now. Returns (exit, reason).

    `current_cal_p` is the LATEST calibrated probability from a fresh
    forecast pass — caller should compute via strategy.compute_market_cal_p.
    `book` is the live Kalshi orderbook; if None we fetch it.

    Two exit conditions, in priority order:
      1. take_profit: market value near max payoff AND a real bid we'd
         cross exists. Asymmetric — never realises a loss.
      2. forecast_inversion: NEW cal_p has shifted enough against entry
         direction that the trade is no longer +EV at cost basis.
         The principled exit — fires when our model itself updates
         against the position.

    Note: a forecast-independent decay-based "safety net" was removed
    on 2026-05-08 because it contradicted entry logic and caused
    stop-out → re-entry oscillation on the same scan. We now hold
    losing trades until either the model itself flips (rule 2) or the
    market settles. This trades higher per-position drawdown for
    coherent behavior across scans.

    Returns (False, "<skip_reason>") for explicit non-evaluation so the
    caller can log telemetry without a gap.
    """
    ticker = position.get("ticker")
    if not ticker:
        return False, "no_ticker"

    # 0. Fresh-position guard: don't exit on entry-spread cost.
    held = _minutes_held(position)
    if held is not None and held < MIN_HOLD_MINUTES_BEFORE_EXIT:
        return False, f"too_fresh:{held:.0f}m"

    # 0b. Skip trades without model context (backfill rows). Both rules
    # 1 and 2 need either a current cal_p or entry edge; rule 3 (safety
    # net) is OK without those — but we'd rather let backfilled rows
    # ride to settlement than guess.
    calibrated_p_raw = position.get("calibrated_p")
    entry_edge = float(position.get("edge_at_entry") or 0.0)
    if (calibrated_p_raw is None
            or float(calibrated_p_raw or 0) == 0.0
            or entry_edge == 0.0):
        return False, "no_model_context"

    action = position.get("action", "BUY YES")
    side = "no" if action == "BUY NO" else "yes"
    other_side = "yes" if side == "no" else "no"

    entry_price = float(position.get("entry_price") or 0.0)
    cal_p_entry = float(calibrated_p_raw)

    # ── Rule 2 first (book-free) — audit H4 ──────────────────────────────
    # Forecast inversion only needs entry data + the current cal_p; no
    # orderbook fetch required. If it fires, we exit without ever
    # calling get_orderbook, saving N round-trips per cycle (one per
    # held position). Take-profit (rule 1) DOES need the book — we
    # check it second, and only when our model view makes it plausible.
    if current_cal_p is not None:
        if action == "BUY NO":
            # We bet against the threshold being crossed. Adverse drift =
            # cal_p going UP (forecast now says crossing is more likely).
            forecast_drift = float(current_cal_p) - cal_p_entry
            live_edge_at_cost = (1.0 - float(current_cal_p)) - entry_price
        else:  # BUY YES
            forecast_drift = cal_p_entry - float(current_cal_p)
            live_edge_at_cost = float(current_cal_p) - entry_price

        if (forecast_drift >= FORECAST_DRIFT_THRESHOLD
                and live_edge_at_cost < FORECAST_INVERSION_NEGATIVE_FLOOR):
            return True, (
                f"forecast_inversion:cal_p {cal_p_entry:.3f}→"
                f"{float(current_cal_p):.3f} "
                f"drift={forecast_drift:.3f} "
                f"live_edge_at_cost={live_edge_at_cost:.3f}"
            )

    # ── Rule 1: take-profit (bid-aware) — needs the book ─────────────────
    # Cheap-skip heuristic: take-profit fires when market value on our
    # side reaches >= TAKE_PROFIT_VALUE_THRESHOLD (0.95). For the market
    # to be paying near-max, our model's view of P(our side wins) is
    # almost always within ~0.20 of the threshold. If it's not, we'd
    # need a wild market/model divergence — possible but rare, and we
    # re-check every cycle anyway. Skipping the book fetch in the
    # implausible case is a real perf win (saw 16 fetches/cycle on
    # 2026-05-08 logs, most of them futile).
    p_our_side = (
        cal_p_entry if action == "BUY YES" else 1.0 - cal_p_entry
    )
    if current_cal_p is not None:
        p_our_side = (
            float(current_cal_p) if action == "BUY YES"
            else 1.0 - float(current_cal_p)
        )
    if p_our_side < TAKE_PROFIT_VALUE_THRESHOLD - 0.20:
        return False, f"hold:no_takeprofit_signal:p_our={p_our_side:.2f}"

    if book is None:
        book = kalshi_client.get_orderbook(ticker)
    market_value = _live_position_value(book, side)
    if market_value is None:
        return False, "no_book"

    if market_value >= TAKE_PROFIT_VALUE_THRESHOLD:
        # Need a real bid we'd cross. The implied bid on our side is
        # (1 - other_side_ask) per Kalshi book inversion.
        other_ask = book.get(f"best_{other_side}_ask")
        if other_ask is not None and 0 < other_ask < 1:
            implied_our_bid = 1.0 - other_ask
            if implied_our_bid >= TAKE_PROFIT_BID_FLOOR:
                return True, (f"take_profit:value={market_value:.2f} "
                              f"bid={implied_our_bid:.2f}")
            return False, (f"take_profit_no_bid:value={market_value:.2f} "
                           f"bid={implied_our_bid:.2f}")
        return False, f"take_profit_no_book_bid:value={market_value:.2f}"

    return False, "hold"


# ─── Exit execution ──────────────────────────────────────────────────────────
def _kalshi_realized_for_ticker(ticker: str) -> float:
    """Return Kalshi's reported realized_pnl_dollars for `ticker`, or 0.0
    if the ticker has no position record (never traded, or fully flat
    with zero historical activity)."""
    try:
        for p in kalshi_client.get_open_positions() or []:
            if p.get("ticker") == ticker:
                return float(p.get("realized_pnl_dollars") or 0)
    except Exception:
        return 0.0
    return 0.0


def _execute_exit_sell(ticker: str, side: str, contracts: int,
                       entry_price: float,
                       poll_seconds: int = 15) -> dict:
    """Market-sell `contracts` of our `side` position. Polls for fill, then
    cancels any unfilled remainder.

    P&L is read from Kalshi's own realized_pnl_dollars on the position
    endpoint — NOT derived from order_status fill cost fields. The 2026-
    05-08 LAX bug: for a SELL order, taker_fill_cost_dollars is reported
    in the counterparty's frame (gross notional from the buyer's side),
    not what we received. Using it gave wildly wrong exit prices (sold
    LAX at $0.01, recorded as $0.97). Kalshi's realized_pnl_dollars is
    the authoritative number; we capture pre-sell, sell, capture post-
    sell, take the delta.

    Returns:
        {filled_count, fill_price, fees, pnl, sell_oid, notes}
    where `pnl` is Kalshi-authoritative and `fill_price` is implied
    backward from pnl for display purposes.
    """
    out = {"filled_count": 0, "fill_price": 0.0, "fees": 0.0, "pnl": 0.0,
           "sell_oid": None, "notes": ""}

    pre_realized = _kalshi_realized_for_ticker(ticker)

    sell_oid = kalshi_client.sell_position(ticker, side, contracts)
    if not sell_oid:
        out["notes"] = "sell_place_failed"
        return out
    out["sell_oid"] = sell_oid

    deadline = time.time() + poll_seconds
    last_status: dict = {}
    last_fc = 0
    while time.time() < deadline:
        time.sleep(1.0)
        last_status = kalshi_client.get_order_status(sell_oid) or {}
        last_fc = _fill_count(last_status)
        status = (last_status.get("status") or "").lower()
        if last_fc >= contracts:
            break
        if status in ("canceled", "cancelled", "expired", "executed"):
            break

    # Cancel any unfilled remainder so it doesn't linger on the book.
    if last_fc < contracts:
        try:
            kalshi_client.cancel_order(sell_oid)
        except Exception:
            pass

    if last_fc <= 0:
        out["notes"] = f"no_fill:status={last_status.get('status', '?')}"
        return out

    # Brief settle for Kalshi's positions endpoint to reflect the trade.
    time.sleep(1.0)
    post_realized = _kalshi_realized_for_ticker(ticker)
    delta_pnl = post_realized - pre_realized

    out["fees"] = (
        float(last_status.get("taker_fees_dollars") or 0)
        + float(last_status.get("maker_fees_dollars") or 0)
    )
    out["filled_count"] = last_fc
    out["pnl"] = delta_pnl
    # Implied per-contract fill price (for display in the result row):
    #   pnl = (fill_price - entry_price) * fc - sell_fees
    #   fill_price = (pnl + sell_fees) / fc + entry_price
    out["fill_price"] = (delta_pnl + out["fees"]) / last_fc + entry_price
    out["notes"] = (
        f"sold_{last_fc}_of_{contracts} pnl=${delta_pnl:+.2f} "
        f"realized {pre_realized:.2f}→{post_realized:.2f}"
    )
    return out


def process_exits(markets: list[dict] | None = None,
                  stop: threading.Event | None = None) -> dict:
    """Per-cycle pass: check every open Kalshi position against the exit
    rule; sell ones that meet the criteria; write a result row for the
    realised P&L. Partial fills update the trade row's contracts count
    so the unfilled portion stays open for the next cycle to re-check.

    `markets` should be the cycle's freshly-fetched Kalshi market list
    (from kalshi_client.get_all_weather_markets). If provided, we use
    it to compute the latest cal_p per held position — the principled
    forecast-driven exit signal. Without it (None), we fall back to
    take-profit + safety-net only.

    Wired into run_cycle (2026-05-07) — closes the long-standing gap where
    `should_exit_position` was defined but never called.
    """
    import storage  # local import to avoid cycles
    summary: dict = {
        "checked": 0, "exited_full": 0, "exited_partial": 0,
        "no_fill": 0, "errors": 0, "by_reason": {},
    }

    if stop is not None and stop.is_set():
        return summary

    # Build a ticker→market lookup for cal_p computation. None if markets
    # weren't provided — we'll skip the forecast-inversion rule and fire
    # only on take-profit / safety-net.
    market_by_ticker: dict[str, dict] = {}
    if markets:
        for m in markets:
            tk = m.get("ticker") or m.get("market_id") or ""
            if tk:
                market_by_ticker[tk] = m

    open_positions = storage.load_open_positions()
    for pos in open_positions:
        # Only Kalshi live positions; paper / Polymarket exit via their
        # own paths.
        if (pos.get("venue") or "kalshi") != "kalshi":
            continue
        if int(pos.get("paper_trade") or 0) == 1:
            continue
        if pos.get("mode") in ("dry-run", "backfill"):
            # Backfill rows we'll let ride; they have no model context.
            # Dry-run rows shouldn't reach here but skip defensively.
            continue
        if pos.get("market_type") == "arbitrage":
            # Arb legs settle as a group at market settlement; they
            # have no individual exit policy. Directional exit rules
            # (take_profit, forecast_inversion) reference yes_price as
            # cal_p — a different frame than the forecast — and would
            # unilaterally close one leg, breaking the arb's hedge.
            # Roll-back at entry time is the only place a leg gets
            # closed before settlement.
            continue

        summary["checked"] += 1
        ticker = pos.get("ticker") or ""
        # Compute fresh cal_p for this market, if we have it. Lazy import
        # so test code can stub strategy independently of executor.
        current_cal_p: float | None = None
        m = market_by_ticker.get(ticker)
        if m is not None:
            try:
                import strategy as _strategy_mod
                current_cal_p = _strategy_mod.compute_market_cal_p(
                    m, venue=str(pos.get("venue") or "kalshi"),
                )
            except Exception as e:
                logging.warning(
                    "[EXIT] compute_market_cal_p failed for %s: %s",
                    ticker, e,
                )
                current_cal_p = None

        try:
            do_exit, reason = should_exit_position(pos, current_cal_p)
        except Exception as e:
            logging.warning("[EXIT] should_exit failed for trade#%s: %s",
                            pos.get("id"), e)
            summary["errors"] += 1
            continue
        if not do_exit:
            continue
        if stop is not None and stop.is_set():
            return summary

        ticker = pos.get("ticker") or ""
        action = pos.get("action", "BUY YES")
        side = "no" if action == "BUY NO" else "yes"
        contracts = int(pos.get("contracts") or 0)
        entry_price = float(pos.get("entry_price") or 0)
        if contracts < 1 or not ticker:
            continue

        logging.info(
            "[EXIT] trade#%s %s %s x%d @entry=$%.2f — %s",
            pos.get("id"), ticker, action, contracts, entry_price, reason,
        )
        try:
            sell = _execute_exit_sell(ticker, side, contracts, entry_price)
        except Exception as e:
            logging.exception("[EXIT] sell failed for trade#%s: %s",
                              pos.get("id"), e)
            summary["errors"] += 1
            continue

        fc = int(sell.get("filled_count") or 0)
        if fc <= 0:
            logging.warning("[EXIT] trade#%s no fill: %s",
                            pos.get("id"), sell.get("notes"))
            summary["no_fill"] += 1
            continue

        sell_price = float(sell["fill_price"])
        # P&L comes from Kalshi's realized_pnl_dollars delta — authoritative,
        # not derived from order fields. See _execute_exit_sell docstring.
        pnl = float(sell["pnl"])

        # Partial-fill handling: split the trade row BEFORE writing the
        # result so the result cleanly closes only the sold portion and
        # the unsold remainder lives on as its own row for next cycle.
        if fc < contracts:
            try:
                new_tid = storage.split_trade_on_partial_exit(
                    int(pos["id"]), fc
                )
            except Exception as e:
                logging.exception(
                    "[EXIT] split_trade failed for trade#%s: %s",
                    pos.get("id"), e,
                )
                summary["errors"] += 1
                continue
            logging.info(
                "[EXIT] trade#%s PARTIAL %d/%d @$%.4f pnl=$%.2f "
                "(remainder→trade#%s)",
                pos.get("id"), fc, contracts, sell_price, pnl, new_tid,
            )

        try:
            storage.log_result(
                int(pos["id"]), "exited", round(sell_price, 4),
                round(pnl, 4),
                venue=str(pos.get("venue") or "kalshi"),
            )
        except Exception as e:
            logging.exception("[EXIT] log_result failed for trade#%s: %s",
                              pos.get("id"), e)
            summary["errors"] += 1
            continue

        if fc < contracts:
            summary["exited_partial"] += 1
        else:
            logging.info(
                "[EXIT] trade#%s FULL exited %d @$%.4f pnl=$%.2f",
                pos.get("id"), fc, sell_price, pnl,
            )
            summary["exited_full"] += 1

        bucket = reason.split(":")[0]
        summary["by_reason"][bucket] = summary["by_reason"].get(bucket, 0) + 1

    return summary
