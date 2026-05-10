"""
kalshi_venue.py — KalshiVenue: Venue Protocol implementation backed by
the existing kalshi_client.py module.

This is a thin adapter. All HTTP / signing / rate-limit logic lives in
kalshi_client.py and is unchanged. The class exists so main.py can iterate
over a list[Venue] without caring which exchange it's hitting.

Side effects: none beyond what kalshi_client.py already does.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import kalshi_client
from config import kalshi_trade_fee
from venue import OrderBook, Side

# Trust-but-verify resolution (audit C2 / 2026-05-06 checkpoint):
# the 2026-05-05 demo flap had Kalshi briefly return status='finalized'
# for ~28 markets, then revert to 'active'. reconcile.py wrote results
# based on the briefly-bad data and produced ~$200 of phantom P&L.
# Defense: before accepting a resolution, require Kalshi to also show
# position_fp == 0 (or no position record at all) — i.e. the venue has
# actually settled the position out, not just flipped a status flag.
_POSITIONS_CACHE_TTL_SEC = 30.0
# `last_ok_ts` is updated only after a SUCCESSFUL fetch. Without this,
# an API failure that silently returns []/None could be confused with a
# genuine no-positions state and let the caller fail open. A fresh empty
# is fine; an empty derived from failure is not.
_positions_cache: dict[str, Any] = {
    "last_ok_ts": 0.0,
    "by_ticker": {},
}


def _positions_by_ticker() -> dict[str, dict[str, Any]] | None:
    """Cached fetch of /portfolio/positions, indexed by ticker. Returns
    None when we can't get a fresh fetch and don't have a recent
    successful one — the caller MUST treat None as 'cannot verify' and
    fail closed.

    Within a reconcile pass (which calls get_resolution per open trade),
    one successful fetch serves the whole batch.
    """
    now = time.time()
    if now - _positions_cache["last_ok_ts"] < _POSITIONS_CACHE_TTL_SEC:
        return _positions_cache["by_ticker"]
    try:
        live = kalshi_client.get_open_positions()
    except Exception as e:
        logging.warning("[KALSHI_VENUE] positions fetch for verify failed: %s", e)
        live = None
    if live is None:
        return None
    by_ticker: dict[str, dict[str, Any]] = {}
    for p in live:
        tk = p.get("ticker")
        if tk:
            by_ticker[tk] = p
    _positions_cache["last_ok_ts"] = now
    _positions_cache["by_ticker"] = by_ticker
    return by_ticker


class KalshiVenue:
    name: str = "kalshi"

    # ─── Discovery ─────────────────────────────────────────────────────────
    def list_markets(self) -> list[dict[str, Any]]:
        """All open weather markets — canonicalization happens inside
        kalshi_client.get_all_weather_markets() so this is just a passthrough.
        Both call sites share one canonicalization step."""
        return kalshi_client.get_all_weather_markets()

    def get_book(self, market_id: str) -> OrderBook:
        return kalshi_client.get_orderbook(market_id)  # type: ignore[return-value]

    def get_market(self, market_id: str) -> dict[str, Any] | None:
        return kalshi_client.get_market(market_id)

    def get_resolution(self, market_id: str) -> dict[str, Any] | None:
        """Final outcome from Kalshi's settlement record, with trust-but-
        verify defense (audit C2). Two conditions BOTH must hold:

          1. market.status in ('finalized', 'settled')
          2. /portfolio/positions shows either no record for this ticker,
             or position_fp == 0 — i.e. Kalshi has actually settled our
             position out, not just flipped the market's status flag.

        Returns None if either check fails. The 30s positions cache means
        that within one reconcile pass, multiple get_resolution calls
        share a single positions fetch.
        """
        m = kalshi_client.get_market(market_id)
        if m is None:
            return None
        status = (m.get("status") or "").lower()
        if status not in ("finalized", "settled"):
            return None

        # Verify position has actually been settled venue-side. Fail
        # closed if the positions endpoint can't be confirmed fresh.
        positions = _positions_by_ticker()
        if positions is None:
            logging.info(
                "[KALSHI_VENUE] resolution VERIFY-DEFER for %s: status=%s "
                "but positions endpoint unavailable — cannot verify "
                "settlement, holding off",
                market_id, status,
            )
            return None
        pos = positions.get(market_id)
        if pos is not None:
            try:
                fp = float(pos.get("position_fp") or pos.get("position") or 0)
            except (TypeError, ValueError):
                fp = 0.0
            if fp != 0:
                logging.info(
                    "[KALSHI_VENUE] resolution VERIFY-DEFER for %s: "
                    "status=%s but position_fp=%g (still venue-open) — "
                    "treating as not-yet-resolved",
                    market_id, status, fp,
                )
                return None

        return {
            "outcome": m.get("result"),
            "settled_at": m.get("expiration_time") or m.get("close_time"),
            "raw": m,
        }

    # ─── Fees ──────────────────────────────────────────────────────────────
    def fee_for_trade(
        self, price: float, contracts: int, side: Side, mode: str = "taker"
    ) -> float:
        # Kalshi charges the same fee regardless of maker/taker; the kwarg
        # exists for parity with PolymarketVenue.fee_for_trade so callers
        # can pass mode uniformly.
        return kalshi_trade_fee(contracts, price)

    # ─── Execution ─────────────────────────────────────────────────────────
    def place_limit_order(self, market_id: str, side: Side,
                          contracts: int, limit_price_cents: int) -> str | None:
        return kalshi_client.place_limit_order(
            market_id, side, contracts, limit_price_cents
        )

    def cancel_order(self, order_id: str) -> bool:
        return kalshi_client.cancel_order(order_id)

    def get_order_status(self, order_id: str) -> dict[str, Any] | None:
        return kalshi_client.get_order_status(order_id)

    def sell_position(self, market_id: str, side: Side,
                      contracts: int) -> str | None:
        return kalshi_client.sell_position(market_id, side, contracts)

    # ─── Portfolio ─────────────────────────────────────────────────────────
    def get_portfolio_balance(self) -> dict[str, Any] | None:
        return kalshi_client.get_portfolio_balance()

    def get_open_positions(self) -> list[dict[str, Any]]:
        return kalshi_client.get_open_positions()

    def verify_connection(self) -> bool:
        return kalshi_client.verify_api_connection()
