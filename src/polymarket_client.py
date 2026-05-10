"""
polymarket_client.py — PolymarketVenue: read-only ingest (phase 1).

What works in phase 1:
  - list_markets()          via Polymarket Gamma API (public, no auth)
  - get_book(market_id)     via Polymarket CLOB API (public, no auth)
  - get_market(market_id)   via Gamma API
  - get_resolution(...)     reads from Gamma (resolved + outcome fields)
  - verify_connection()     pings Gamma /markets

What raises NotImplementedError until phase 3+:
  - place_limit_order, cancel_order, get_order_status, sell_position
  - get_portfolio_balance, get_open_positions
  - fee_for_trade returns 0.0 (Polymarket has no trading fees today)

Why no execution code: Polymarket is non-custodial — every order is an
EIP-712 signature from the wallet that holds the USDC. The bot has no
wallet yet. Stubs raise loudly so we can't accidentally route a live
order through this venue.

Endpoints used:
  - https://gamma-api.polymarket.com/markets   (market metadata)
  - https://clob.polymarket.com/book           (order book by token_id)

Polymarket exposes two outcome tokens per market (YES and NO), each with
its own CLOB book. We map those to the (yes_bids, no_bids) shape that
KalshiVenue.get_book returns so downstream code is uniform.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from resolution_rules import canonicalize_polymarket_market
from venue import OrderBook, Side

# Polymarket weather-market fee schedule (from live /markets payload's
# feeSchedule: {rate: 0.05, takerOnly: True, rebateRate: 0.25}). We
# apply the taker rate to notional and treat makers as zero — the 25%
# rebate would benefit us, but we don't bake a credit into paper PnL
# we can't yet verify against a real fill. Tighten only after live
# verification; never loosen.
POLYMARKET_TAKER_FEE_RATE: float = 0.05
POLYMARKET_MAKER_FEE_RATE: float = 0.0


def polymarket_taker_fee(contracts: int, price: float) -> float:
    """Per-trade taker fee in dollars. Mirrors the shape of
    config.kalshi_trade_fee so upstream gates (strategy.py, cross_venue.py)
    can call either without branching on dimension."""
    return POLYMARKET_TAKER_FEE_RATE * price * contracts

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Tag IDs / slugs used by Polymarket for weather markets. Their tag taxonomy
# changes; we filter post-hoc by the canonicalizer rather than relying on a
# single tag, but the request narrows the universe to "Climate & Weather"
# style buckets if the slug exists. Empty list = fetch all active markets
# and let canonicalize_polymarket_market drop non-weather ones.
WEATHER_TAG_SLUGS: list[str] = ["weather", "climate", "temperature"]


def _request(method: str, url: str, **kwargs: Any) -> requests.Response | None:
    """HTTP with 429-aware exponential backoff. Mirrors kalshi_client._request
    discipline. Returns None on persistent failure (caller handles)."""
    for attempt in range(4):
        try:
            resp = requests.request(method, url, timeout=15, **kwargs)
            if resp.status_code == 429:
                retry = float(resp.headers.get("Retry-After", 0)) or (0.5 * (2 ** attempt))
                time.sleep(min(retry, 8.0))
                continue
            return resp
        except Exception as e:
            logging.warning("[POLYMARKET] %s %s failed: %s", method, url, e)
            if attempt == 3:
                return None
            time.sleep(0.5 * (2 ** attempt))
    return None


def _parse_outcome_prices(market: dict[str, Any]) -> tuple[float | None, float | None]:
    """Polymarket markets carry a JSON-encoded `outcomePrices` string like
    '["0.42","0.58"]' aligned with `outcomes` (typically ["Yes","No"]).

    Returns (yes_price, no_price) as floats, or (None, None) on any parse
    failure. These are *last trade* prices, not asks — we only use them as
    a sanity hint; the order book from CLOB is authoritative."""
    try:
        outcomes_raw = market.get("outcomes")
        prices_raw = market.get("outcomePrices")
        if isinstance(outcomes_raw, str):
            outcomes = json.loads(outcomes_raw)
        else:
            outcomes = outcomes_raw or []
        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw)
        else:
            prices = prices_raw or []
        outcomes = [str(o).lower() for o in outcomes]
        prices = [float(p) for p in prices]
        yes_p = None
        no_p = None
        for o, p in zip(outcomes, prices):
            if o == "yes":
                yes_p = p
            elif o == "no":
                no_p = p
        return yes_p, no_p
    except Exception:
        return None, None


def _token_ids(market: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract YES and NO CLOB token IDs from a Polymarket market payload."""
    try:
        tokens_raw = market.get("clobTokenIds") or market.get("tokens") or "[]"
        if isinstance(tokens_raw, str):
            tokens = json.loads(tokens_raw)
        else:
            tokens = tokens_raw
        # Polymarket convention: tokens align with outcomes order. Outcomes
        # are usually ["Yes","No"] so tokens[0] is YES, tokens[1] is NO.
        # Guard against payloads that include the outcome label inline.
        flat: list[str] = []
        for t in tokens:
            if isinstance(t, dict):
                flat.append(str(t.get("token_id") or t.get("id") or ""))
            else:
                flat.append(str(t))
        flat = [t for t in flat if t]
        yes_id = flat[0] if len(flat) > 0 else None
        no_id = flat[1] if len(flat) > 1 else None
        return yes_id, no_id
    except Exception:
        return None, None


def _book_to_bids(side_book: dict[str, Any]) -> list[tuple[float, float]]:
    """Polymarket /book returns {asks: [...], bids: [...]} per token. Each
    entry is {price, size}. We convert to v2's (price_dollars, dollar_depth)
    shape — depth in *dollars*, not contracts, mirroring Kalshi's payload."""
    out: list[tuple[float, float]] = []
    for entry in side_book or []:
        try:
            price = float(entry.get("price"))
            size = float(entry.get("size"))   # contracts
            if price <= 0 or size <= 0:
                continue
            out.append((price, price * size))
        except Exception:
            continue
    return out


class PolymarketVenue:
    name: str = "polymarket"

    # ─── Discovery ─────────────────────────────────────────────────────────
    def list_markets(self) -> list[dict[str, Any]]:
        """Fetch active weather-ish markets and canonicalize. Returns the
        same list[dict] shape that KalshiVenue produces so strategy code
        sees a uniform payload."""
        # Phase 1: pull a generous slice of active markets and let the
        # canonicalizer (which inspects the question text) filter to actual
        # weather markets. This is more robust than relying on tag slugs
        # whose IDs we'd need to hardcode.
        # Paginate through Gamma. Weather markets are not always in the first
        # page even ordered by volume, because Polymarket lists thousands of
        # active sports/politics markets. We pull a few pages and let the
        # canonicalizer drop the non-weather ones. Ordering by volume biases
        # toward markets with depth — exactly what we want to trade against
        # eventually.
        url = f"{GAMMA_BASE}/markets"
        page_size = 500
        max_pages = 4   # 2,000 markets total; weather slice is small
        all_markets: list[dict[str, Any]] = []
        for page in range(max_pages):
            params = {
                "active": "true",
                "closed": "false",
                "limit": page_size,
                "offset": page * page_size,
                "order": "volume",
                "ascending": "false",
            }
            resp = _request("GET", url, params=params)
            if resp is None or resp.status_code != 200:
                logging.warning(
                    "[POLYMARKET] list_markets page %d failed: %s",
                    page, resp.status_code if resp else "no response",
                )
                break
            try:
                payload = resp.json()
            except Exception as e:
                logging.warning("[POLYMARKET] list_markets JSON parse failed: %s", e)
                break
            if isinstance(payload, dict):
                page_markets = payload.get("data") or payload.get("markets") or []
            else:
                page_markets = payload
            if not page_markets:
                break
            all_markets.extend(page_markets)
            if len(page_markets) < page_size:
                break

        markets = all_markets

        out: list[dict[str, Any]] = []
        for m in markets:
            canon = canonicalize_polymarket_market(m)
            if canon is None:
                continue
            yes_last, no_last = _parse_outcome_prices(m)
            yes_id, no_id = _token_ids(m)
            canon["venue"] = self.name
            canon["market_id"] = (
                m.get("conditionId") or m.get("condition_id") or m.get("id") or ""
            )
            canon["ticker"] = canon["market_id"]
            # Phase 1 leaves yes_ask / no_ask unpopulated; populating them
            # would require a per-market /book round trip. The cycle's
            # strategy code asks for the book separately when it actually
            # wants to act, which doesn't happen for Polymarket yet.
            canon["yes_ask_dollars"] = yes_last
            canon["no_ask_dollars"] = no_last
            canon["close_time"] = m.get("endDate") or m.get("end_date_iso")
            canon["expected_expiration_time"] = canon["close_time"]
            canon["target_settlement"] = canon["close_time"] or ""
            canon["_yes_token_id"] = yes_id
            canon["_no_token_id"] = no_id
            canon["raw"] = m
            out.append(canon)

        logging.info(
            "[POLYMARKET] list_markets: %d total -> %d weather after canonicalize",
            len(markets), len(out),
        )
        return out

    def get_book(self, market_id: str) -> OrderBook:
        """Fetch books for both YES and NO outcome tokens of a Polymarket
        market and merge into Kalshi-shaped OrderBook.

        market_id here must be the conditionId. Caller is expected to have
        come from list_markets() output where _yes_token_id / _no_token_id
        were already extracted; we re-lookup the market here so the API is
        usable standalone too.
        """
        empty: OrderBook = {
            "yes_bids": [],
            "no_bids": [],
            "best_yes_ask": None,
            "best_no_ask": None,
            "yes_depth_at_price": lambda p: 0,
            "no_depth_at_price": lambda p: 0,
        }

        market = self.get_market(market_id)
        if market is None:
            return empty
        yes_id, no_id = _token_ids(market)
        if not (yes_id or no_id):
            return empty

        def _fetch(token_id: str) -> dict[str, Any] | None:
            r = _request("GET", f"{CLOB_BASE}/book", params={"token_id": token_id})
            if r is None or r.status_code != 200:
                return None
            try:
                return r.json()
            except Exception:
                return None

        yes_book = _fetch(yes_id) if yes_id else None
        no_book = _fetch(no_id) if no_id else None

        # Polymarket /book returns asks (sells) and bids (buys) for one token.
        # In the YES token, an "ask" is someone selling YES, which is a YES-side
        # offer that someone wanting to BUY YES would lift. In Kalshi's payload
        # we model this as no_bids (because BUY YES at price p == BUY a NO
        # contract at 1-p in Kalshi's complementary token model). Polymarket
        # doesn't have that complementarity in the payload — YES and NO are
        # independent CTF tokens — so we keep them separate here.
        yes_asks = _book_to_bids(yes_book.get("asks", [])) if yes_book else []
        no_asks = _book_to_bids(no_book.get("asks", [])) if no_book else []

        best_yes_ask = min((p for p, _ in yes_asks), default=None)
        best_no_ask = min((p for p, _ in no_asks), default=None)

        # Depth functions: contracts available at-or-better than limit_price.
        def _depth(asks: list[tuple[float, float]], limit_price: float) -> int:
            total = 0.0
            for price, dollars in asks:
                if price <= limit_price and price > 0:
                    total += dollars / price
            return int(total)

        return {
            # NB: we expose YES asks under yes_bids slot to keep the field
            # name compatible with v2's strategy/executor code, which reads
            # depth via the depth_at_price callable (and best_yes_ask) and
            # ignores the raw lists' interpretation. This is a known quirk
            # to revisit in phase 2 when strategy.py becomes venue-aware.
            "yes_bids": yes_asks,
            "no_bids": no_asks,
            "best_yes_ask": best_yes_ask,
            "best_no_ask": best_no_ask,
            "yes_depth_at_price": lambda p: _depth(yes_asks, p),
            "no_depth_at_price": lambda p: _depth(no_asks, p),
        }

    def get_market(self, market_id: str) -> dict[str, Any] | None:
        # Polymarket Gamma uses conditionId as the canonical lookup key.
        # Gamma's /markets defaults to active-only — once a market closes
        # (settlement complete) it disappears from the default query, so
        # we must retry with closed=true to look up settled trades for
        # resolution. Try the active path first (cheaper for live books),
        # fall back to closed.
        url = f"{GAMMA_BASE}/markets"
        for params in (
            {"condition_ids": market_id},
            {"condition_ids": market_id, "closed": "true"},
        ):
            resp = _request("GET", url, params=params)
            if resp is None or resp.status_code != 200:
                continue
            try:
                payload = resp.json()
            except Exception:
                continue
            markets = payload.get("data") if isinstance(payload, dict) else payload
            if markets:
                return markets[0] if isinstance(markets, list) else markets
        return None

    def get_resolution(self, market_id: str) -> dict[str, Any] | None:
        """Outcome from Polymarket / UMA. Returns None if not yet resolved.

        CRITICAL (v1 postmortem §3.1): the outcome MUST come from
        Polymarket's own settlement record (which is UMA-arbitrated), never
        from our forecast or from Open-Meteo / NOAA archive. A trade that
        Polymarket settles NO is a losing trade for us even if NWS data
        says it should have been YES.
        """
        m = self.get_market(market_id)
        if m is None:
            return None
        # A Gamma market is fully resolved when umaResolutionStatus
        # (singular) is "resolved". The plural umaResolutionStatuses can
        # linger on "proposed" even post-finality — don't trust it.
        # closed: True alone isn't enough (it flips at endDate, before UMA
        # finalizes), so require both.
        if not m.get("closed"):
            return None
        if str(m.get("umaResolutionStatus", "")).lower() != "resolved":
            return None
        # Winner is encoded in outcomePrices: a JSON-string list of "1"/"0"
        # paired by index with `outcomes` (e.g. ["Yes","No"]). The winning
        # index has price 1.
        outcomes_raw = m.get("outcomes")
        prices_raw = m.get("outcomePrices")
        outcome: str | None = None
        try:
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            if isinstance(outcomes, list) and isinstance(prices, list) and len(outcomes) == len(prices):
                for label, price in zip(outcomes, prices):
                    try:
                        if float(price) >= 0.5:
                            outcome = str(label)
                            break
                    except (TypeError, ValueError):
                        continue
        except (TypeError, ValueError):
            outcome = None
        if outcome is None:
            return None
        return {
            "outcome": outcome,
            "settled_at": m.get("closedTime") or m.get("resolvedTime") or m.get("endDate"),
            "raw": m,
        }

    # ─── Fees ──────────────────────────────────────────────────────────────
    def fee_for_trade(
        self, price: float, contracts: int, side: Side, mode: str = "taker"
    ) -> float:
        """Conservative fee model for Polymarket weather markets.

        Polymarket exposes a per-market `feeSchedule` like
        ``{rate: 0.05, takerOnly: True, rebateRate: 0.25}`` for weather
        markets (feeType='weather_fees'). We model:

          taker: 5% of notional (price × contracts)
          maker: 0% — rebate ignored, worst-case for paper PnL

        Constants live at module scope so they're easy to retune once
        we've verified them against a real fill. Loosening these requires
        proof from a live execution, never the other way around.
        """
        rate = (
            POLYMARKET_TAKER_FEE_RATE if mode == "taker"
            else POLYMARKET_MAKER_FEE_RATE
        )
        return rate * price * contracts

    # ─── Execution: phase 1 stubs ──────────────────────────────────────────
    def place_limit_order(self, market_id: str, side: Side,
                          contracts: int, limit_price_cents: int) -> str | None:
        raise NotImplementedError(
            "PolymarketVenue execution is not enabled in phase 1. "
            "A separate Polygon wallet + EIP-712 signing flow is required."
        )

    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError("PolymarketVenue: phase-1 read-only.")

    def get_order_status(self, order_id: str) -> dict[str, Any] | None:
        raise NotImplementedError("PolymarketVenue: phase-1 read-only.")

    def sell_position(self, market_id: str, side: Side,
                      contracts: int) -> str | None:
        raise NotImplementedError("PolymarketVenue: phase-1 read-only.")

    # ─── Portfolio: phase 1 stubs ──────────────────────────────────────────
    def get_portfolio_balance(self) -> dict[str, Any] | None:
        # Phase 1 has no wallet — there's no balance to report. Returning
        # None is the same shape KalshiVenue uses on a connection failure,
        # so risk.py treats it correctly (fails-closed on stale bankroll).
        return None

    def get_open_positions(self) -> list[dict[str, Any]]:
        return []

    def verify_connection(self) -> bool:
        resp = _request("GET", f"{GAMMA_BASE}/markets",
                        params={"limit": 1, "active": "true"})
        return resp is not None and resp.status_code == 200
