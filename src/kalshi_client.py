"""
kalshi_client.py — Kalshi HTTP client (demo-aware).

Preserves the correct bits from v1 kalshi.py: RSA PSS signing with cached key,
exponential backoff on 429, batch orderbook, and the depth parser. Drops the
auto-applied slippage buffer inside place_order — v2's executor.py decides
exact limit prices itself (maker-first, taker fallback).

Addresses audit items:
  E3 — depth checks live in executor.py, this module just exposes raw depth
  E4 — raw book returned so caller can split across levels
"""

from __future__ import annotations

import base64
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv()

from config import KALSHI_API_URL

KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY = os.getenv("KALSHI_PRIVATE_KEY")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")

_PRIVATE_KEY_CACHE: Any = None


# ─── RSA signing (copied verbatim from v1 kalshi.py:72-106) ───────────────────
def get_private_key():
    global _PRIVATE_KEY_CACHE
    if _PRIVATE_KEY_CACHE is not None:
        return _PRIVATE_KEY_CACHE
    if KALSHI_PRIVATE_KEY:
        key_str = KALSHI_PRIVATE_KEY.replace("\\n", "\n")
        _PRIVATE_KEY_CACHE = serialization.load_pem_private_key(
            key_str.encode(), password=None
        )
    else:
        if not KALSHI_PRIVATE_KEY_PATH:
            raise RuntimeError(
                "Neither KALSHI_PRIVATE_KEY nor KALSHI_PRIVATE_KEY_PATH set in .env"
            )
        with open(KALSHI_PRIVATE_KEY_PATH, "rb") as f:
            _PRIVATE_KEY_CACHE = serialization.load_pem_private_key(
                f.read(), password=None
            )
    return _PRIVATE_KEY_CACHE


def get_headers(method: str, path: str) -> dict[str, str]:
    timestamp = str(int(datetime.now().timestamp() * 1000))
    private_key = get_private_key()
    full_path = f"/trade-api/v2{path.split('?')[0]}"
    message = f"{timestamp}{method}{full_path}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID or "",
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, json_body: dict | None = None, timeout: int = 15) -> requests.Response | None:
    """Signed HTTP with 429 exponential backoff (copied v1 pattern)."""
    url = f"{KALSHI_API_URL}{path}"
    for attempt in range(4):
        try:
            headers = get_headers(method, path)
            resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
            if resp.status_code == 429:
                backoff = float(resp.headers.get("Retry-After", 0)) or (0.5 * (2 ** attempt))
                time.sleep(min(backoff, 8.0))
                continue
            return resp
        except Exception as e:
            logging.warning("[KALSHI] %s %s failed: %s", method, path, e)
            if attempt == 3:
                return None
            time.sleep(0.5 * (2 ** attempt))
    return None


# ─── Market discovery ─────────────────────────────────────────────────────────
def get_markets_for_series(series_ticker: str) -> list[dict]:
    resp = _request("GET", f"/markets?series_ticker={series_ticker}&status=open")
    if resp is None or resp.status_code != 200:
        return []
    return resp.json().get("markets", [])


# Cache of (series_ticker, city) tuples + the unmapped-series list, refreshed
# every DISCOVERY_TTL seconds. Series taxonomy doesn't change every cycle, so
# we don't need to re-paginate /series on every market fetch.
_DISCOVERY_TTL: float = 1800.0  # 30 min
_discovery_cache: dict[str, Any] = {"ts": 0.0, "mapped": [], "unmapped": []}


def discover_weather_series() -> tuple[list[tuple[str, str]], list[dict]]:
    """Page through Kalshi /series?category=Climate and Weather and return:
       - mapped:   list[(series_ticker, city_key)] for daily-high series in
                   cities our forecaster supports
       - unmapped: list[{ticker, title}] for daily-high series whose city
                   isn't yet in resolution_rules._CITY_PATTERNS

    Cached for DISCOVERY_TTL seconds. The cache is intentionally per-process
    so a long-running cycle re-uses it but a restart picks up new series.
    Logs both totals once per refresh.
    """
    import urllib.parse
    from resolution_rules import (
        derive_city_from_kalshi_series,
        is_kalshi_daily_high_series,
    )

    now = time.time()
    if now - _discovery_cache["ts"] < _DISCOVERY_TTL and _discovery_cache["mapped"]:
        return _discovery_cache["mapped"], _discovery_cache["unmapped"]

    cat = urllib.parse.quote("Climate and Weather")
    all_series: list[dict] = []
    cursor = ""
    for _ in range(20):  # safety cap; ~272 weather series, 200/page
        path = f"/series?category={cat}&limit=200"
        if cursor:
            path += f"&cursor={cursor}"
        resp = _request("GET", path)
        if resp is None or resp.status_code != 200:
            break
        data = resp.json()
        page = data.get("series") or []
        all_series.extend(page)
        cursor = data.get("cursor") or ""
        if not cursor or not page:
            break

    mapped: list[tuple[str, str]] = []
    unmapped: list[dict] = []
    seen: set[str] = set()
    for s in all_series:
        ticker = s.get("ticker") or ""
        title = s.get("title") or ""
        if not ticker or ticker in seen:
            continue
        if not is_kalshi_daily_high_series(ticker, title):
            continue
        city = derive_city_from_kalshi_series(ticker, title)
        if city is None:
            unmapped.append({"ticker": ticker, "title": title})
        else:
            mapped.append((ticker, city))
        seen.add(ticker)

    _discovery_cache["ts"] = now
    _discovery_cache["mapped"] = mapped
    _discovery_cache["unmapped"] = unmapped

    logging.info(
        "[DISCOVERY] Kalshi weather series: %d mapped (cities=%d), %d unmapped",
        len(mapped), len({c for _, c in mapped}), len(unmapped),
    )
    if unmapped:
        logging.info(
            "[DISCOVERY] unmapped series (add city pattern to onboard): %s",
            ", ".join(u["ticker"] for u in unmapped[:10]),
        )

    return mapped, unmapped


def get_all_weather_markets() -> list[dict]:
    """Fetch all daily-high markets across the discovered series universe.

    Each market dict is enriched with:
      - city, market_type, series_ticker (legacy fields)
      - venue, market_id, resolution_source, comparator, threshold,
        range_low, range_high (canonical fields for cross-venue logic)
      - ticker (set equal to market_id)

    v2.1: dynamic discovery replaces hardcoded WEATHER_SERIES.
    v3.0: canonicalization applied here (used to be venue-only) so
    strategy.py can rely on canonical fields uniformly across venues.
    v3.1 (audit H3): per-series fetches run in a thread pool. Was a
    serial loop with a 0.3s polite-sleep per series — 29 series × 0.3s
    = 8.7s of pure sleep per cycle. Kalshi handles concurrent reads
    fine (the 429 backoff inside _request is per-request, not global),
    and `cryptography`'s RSA signing is thread-safe.
    """
    from concurrent.futures import ThreadPoolExecutor

    from config import WEATHER_SERIES
    from resolution_rules import canonicalize_kalshi_market

    mapped, _unmapped = discover_weather_series()
    series_to_city: dict[str, str] = {ticker: city for ticker, city in mapped}
    # Guaranteed-include: never silently drop a city we already trade.
    for city, ticker in WEATHER_SERIES.items():
        series_to_city.setdefault(ticker, city)

    items = list(series_to_city.items())

    def _fetch_one(series_city: tuple[str, str]) -> list[dict]:
        series, city = series_city
        markets = get_markets_for_series(series)
        out: list[dict] = []
        for m in markets:
            m["city"] = city
            m["market_type"] = "high_temp"
            m["series_ticker"] = series
            canon = canonicalize_kalshi_market(m)
            if canon is None:
                continue
            canon["venue"] = "kalshi"
            canon["market_id"] = canon.get("ticker", "")
            out.append(canon)
        return out

    # 8 workers is conservative — Kalshi's published rate-limit is
    # ~10 req/s sustained; 8 parallel keeps us inside that even when
    # the per-series response is fast.
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for batch in ex.map(_fetch_one, items):
            out.extend(batch)
    return out


# ─── Order book ───────────────────────────────────────────────────────────────
def get_orderbook(ticker: str) -> dict:
    """Return the parsed order book plus convenience fields.

    Output dict contains:
      yes_bids, no_bids               — raw [(price_dollars, dollar_depth), ...]
      best_yes_ask, best_no_ask       — best available ask prices in dollars
      yes_depth_at_price, no_depth_at_price — callables taking a limit_price and
         returning contracts available at or better than that price.
    """
    resp = _request("GET", f"/markets/{ticker}/orderbook", timeout=10)
    empty = {
        "yes_bids": [],
        "no_bids": [],
        "best_yes_ask": None,
        "best_no_ask": None,
        "yes_depth_at_price": lambda p: 0,
        "no_depth_at_price": lambda p: 0,
    }
    if resp is None or resp.status_code != 200:
        return empty
    ob = resp.json().get("orderbook_fp", {}) or {}
    yes_bids = [
        (float(e[0]), float(e[1]))
        for e in ob.get("yes_dollars", []) or []
        if float(e[0]) > 0
    ]
    no_bids = [
        (float(e[0]), float(e[1]))
        for e in ob.get("no_dollars", []) or []
        if float(e[0]) > 0
    ]

    best_yes_ask = None
    best_no_ask = None
    if no_bids:
        best_no_bid = max(p for p, _ in no_bids)
        best_yes_ask = round(1.0 - best_no_bid, 4)
    if yes_bids:
        best_yes_bid = max(p for p, _ in yes_bids)
        best_no_ask = round(1.0 - best_yes_bid, 4)

    def _depth(side: str, limit_price: float) -> int:
        """Contracts available at or better than limit_price on `side`."""
        threshold = round(1.0 - limit_price, 4)
        entries = yes_bids if side == "no" else no_bids
        total = 0.0
        for price, dollars in entries:
            if price >= threshold and price > 0:
                total += dollars / price
        return int(total)

    return {
        "yes_bids": yes_bids,
        "no_bids": no_bids,
        "best_yes_ask": best_yes_ask,
        "best_no_ask": best_no_ask,
        "yes_depth_at_price": lambda p: _depth("yes", p),
        "no_depth_at_price": lambda p: _depth("no", p),
    }


# ─── Orders ───────────────────────────────────────────────────────────────────
def place_limit_order(
    ticker: str, side: str, count: int, limit_price_cents: int
) -> str | None:
    """Place a BUY limit order at exactly limit_price_cents (no slippage buffer).

    v2: executor.py passes the exact price it wants. Returns the order_id,
    or None on failure.
    """
    # The [1, 99] integer-cent check is what makes this prod-grid compatible
    # (Kalshi prod uses linear_cent — whole cents only). Don't relax it.
    if count < 1 or not (1 <= limit_price_cents <= 99):
        logging.warning("[KALSHI] Invalid order params count=%d price_cents=%d", count, limit_price_cents)
        return None
    price_key = "yes_price" if side == "yes" else "no_price"
    body = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "type": "limit",
        "action": "buy",
        "side": side,
        "count": count,
        price_key: int(limit_price_cents),
    }
    resp = _request("POST", "/portfolio/orders", json_body=body)
    if resp is None:
        return None
    if resp.status_code in (200, 201):
        order = resp.json().get("order", resp.json())
        return order.get("order_id")
    # Surface the order details on rejection so the log tells us *what* we
    # tried to place when Kalshi said no. Without this, we just see the
    # generic "insufficient_balance" without knowing whether it was a $3
    # order or a $30 order.
    cost_dollars = (count * limit_price_cents) / 100.0
    err_text = resp.text[:200]
    is_insuff = "insufficient_balance" in err_text
    # insufficient_balance is already counted in the cycle footer
    # (insufficient_balance_count). Spamming a WARNING per attempt drowns
    # the log when the demo platform locks out; keep full detail at DEBUG.
    logging.log(
        logging.DEBUG if is_insuff else logging.WARNING,
        "[KALSHI] place_limit_order failed %d: %s "
        "(ticker=%s side=%s count=%d price_cents=%d cost=$%.2f)",
        resp.status_code, err_text,
        ticker, side, count, limit_price_cents, cost_dollars,
    )
    if is_insuff:
        _INSUFFICIENT_BALANCE_TICKERS[ticker] = (
            time.time() + _INSUFFICIENT_BALANCE_BLOCK_TTL_SEC
        )
    return None


# Tickers that returned insufficient_balance, with the unix timestamp at which
# the block expires. Sticky per-cycle is good — repeated retries on the same
# bad ticker spam the log and waste API calls — but a permanent block was
# costing real opportunities (audit C4): a ticker that hit insufficient
# balance once in cycle 1 stayed blocked for the lifetime of the process,
# even after settlements freed cash hours later.
_INSUFFICIENT_BALANCE_BLOCK_TTL_SEC = 600  # 10 min — long enough to avoid
                                            # retry storms, short enough that
                                            # the next bankroll refresh after
                                            # any sizable settlement clears it.
_INSUFFICIENT_BALANCE_TICKERS: dict[str, float] = {}


def is_blocked_insufficient_balance(ticker: str) -> bool:
    """True iff `ticker` was rejected for insufficient_balance within the
    block window. Expired entries are pruned lazily on read."""
    expires_at = _INSUFFICIENT_BALANCE_TICKERS.get(ticker)
    if expires_at is None:
        return False
    if time.time() >= expires_at:
        _INSUFFICIENT_BALANCE_TICKERS.pop(ticker, None)
        return False
    return True


def reset_insufficient_balance_tickers() -> None:
    """Clear the entire block dict. Useful from operational scripts after a
    manual top-up; the in-cycle TTL handles the normal case."""
    _INSUFFICIENT_BALANCE_TICKERS.clear()


def cancel_order(order_id: str) -> bool:
    resp = _request("DELETE", f"/portfolio/orders/{order_id}")
    return resp is not None and resp.status_code in (200, 204)


def get_order_status(order_id: str) -> dict | None:
    resp = _request("GET", f"/portfolio/orders/{order_id}")
    if resp is None or resp.status_code != 200:
        return None
    return resp.json().get("order", resp.json())


def find_recent_order(ticker: str, side: str, contracts: int,
                      since_ts: float, window_seconds: int = 30) -> dict | None:
    """Look up an order Kalshi may have silently accepted.

    Used by executor when place_limit_order returns None — the request
    may have errored on our side OR been accepted silently. Querying
    /portfolio/orders?ticker=X recovers the order_id if Kalshi accepted
    it. Match criteria: same ticker, same side, same initial contracts,
    created within `window_seconds` of `since_ts` (typically time.time()
    immediately before our place attempt).

    Returns the order dict on match, else None.
    """
    resp = _request("GET", f"/portfolio/orders?ticker={ticker}&limit=20")
    if resp is None or resp.status_code != 200:
        return None
    orders = resp.json().get("orders") or []
    side_lower = (side or "").lower()
    for o in orders:
        if (o.get("side") or "").lower() != side_lower:
            continue
        try:
            init_count = int(float(
                o.get("initial_count_fp") or o.get("count") or 0
            ))
        except (TypeError, ValueError):
            continue
        if init_count != int(contracts):
            continue
        created_str = o.get("created_time") or ""
        try:
            from datetime import datetime as _dt
            created = _dt.fromisoformat(created_str.replace("Z", "+00:00"))
            delta = abs(created.timestamp() - since_ts)
        except Exception:
            continue
        if delta <= window_seconds:
            return o
    return None


def sell_position(ticker: str, side: str, contracts: int) -> str | None:
    """Market sell for exits and arb rollback. Returns order_id on success."""
    if contracts < 1:
        return None
    price_key = "yes_price" if side == "yes" else "no_price"
    body = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "type": "market",
        "action": "sell",
        "side": side,
        "count": int(contracts),
        price_key: 1,
    }
    resp = _request("POST", "/portfolio/orders", json_body=body)
    if resp is None:
        return None
    if resp.status_code in (200, 201):
        order = resp.json().get("order", resp.json())
        return order.get("order_id")
    logging.warning("[KALSHI] sell_position failed %d: %s", resp.status_code, resp.text[:200])
    return None


# ─── Portfolio ────────────────────────────────────────────────────────────────
def get_portfolio_balance() -> dict | None:
    """Returns cash, portfolio value, and total equity (all in cents).

    cash_cents       — available for new orders
    portfolio_value  — mark-to-market value of open positions
    balance_cents    — total equity (cash + portfolio_value)
    """
    resp = _request("GET", "/portfolio/balance", timeout=10)
    if resp is None or resp.status_code != 200:
        return None
    data = resp.json()
    # Diagnostic: log the raw response keys so we can verify our field
    # mapping is right. (Kalshi has historically had a `balance` field that
    # is NOT cash — it's total equity. We were treating it as cash.)
    # DEBUG so this doesn't flood INFO every minute (audit H5).
    logging.debug("[KALSHI:balance:raw] %s", {k: data.get(k) for k in data.keys()})
    cash = int(data.get("balance", 0))
    portfolio_value = int(data.get("portfolio_value", 0))
    return {
        "cash_cents": cash,
        "portfolio_value_cents": portfolio_value,
        "balance_cents": cash + portfolio_value,
        "timestamp": time.time(),
    }


def get_market(ticker: str) -> dict | None:
    """Fetch a single market's current state (status, result, prices)."""
    resp = _request("GET", f"/markets/{ticker}", timeout=10)
    if resp is None or resp.status_code != 200:
        return None
    return resp.json().get("market")


def get_filled_orders(limit: int = 100) -> list[dict]:
    """Fetch all BUY orders that resulted in any fill, paginated.

    2026-05-07 fix: previously queried ?status=executed, which misses
    the case where an order partially filled and was then cancelled
    (Kalshi reports such orders as status='canceled' with non-zero
    fill_count_fp, NOT under 'executed'). The PHIL-26MAY08-B69.5
    incident (21/35 partial fill, then cancel of the remaining 14)
    was invisible to the old query. We now fetch every BUY order and
    filter client-side for fill_count_fp > 0, regardless of terminal
    status.
    """
    orders: list[dict] = []
    cursor = ""
    while True:
        path = f"/portfolio/orders?action=buy&limit={limit}"
        if cursor:
            path += f"&cursor={cursor}"
        resp = _request("GET", path)
        if resp is None or resp.status_code != 200:
            break
        data = resp.json()
        batch = data.get("orders") or []
        # Keep only orders with a non-zero fill (executed OR
        # canceled-with-partial-fill).
        for o in batch:
            try:
                fc = float(o.get("fill_count_fp") or o.get("fill_count") or 0)
            except (TypeError, ValueError):
                fc = 0.0
            if fc > 0:
                orders.append(o)
        cursor = data.get("cursor", "")
        if not cursor or len(batch) < limit:
            break
        time.sleep(0.3)
    return orders


def get_resting_buy_orders_cost_cents() -> int:
    """Sum of cash reserved by all currently-resting BUY orders, in cents.

    Used at cycle start to subtract from the cash snapshot — the
    /portfolio/balance endpoint may not net out reservations on resting
    limit orders, which can cause the bot to think it has more available
    cash than Kalshi will actually let it deploy. Each resting BUY order
    reserves (remaining_count × yes_price) cents.

    Returns 0 on any error (fail-open: don't block trading because of a
    diagnostic call).
    """
    total_cents = 0
    order_count = 0
    cursor = ""
    sample_keys: list[str] = []
    while True:
        path = f"/portfolio/orders?status=resting&action=buy&limit=200"
        if cursor:
            path += f"&cursor={cursor}"
        try:
            resp = _request("GET", path)
            if resp is None or resp.status_code != 200:
                logging.info(
                    "[KALSHI:resting] non-200 from %s (status=%s)",
                    path, resp.status_code if resp else "none",
                )
                return total_cents
            data = resp.json()
            batch = data.get("orders") or []
            if not sample_keys and batch:
                sample_keys = list(batch[0].keys())
                logging.info("[KALSHI:resting] sample order keys: %s", sample_keys)
            for o in batch:
                # Kalshi field names: yes_price (cents), remaining_count.
                # Resting orders for the NO side have a no_price; same shape.
                price_cents = int(
                    o.get("yes_price")
                    or o.get("no_price")
                    or o.get("price")
                    or 0
                )
                remaining = int(
                    o.get("remaining_count")
                    or o.get("count")
                    or 0
                )
                total_cents += price_cents * remaining
                order_count += 1
            cursor = data.get("cursor", "")
            if not cursor or len(batch) < 200:
                break
            time.sleep(0.3)
        except Exception as e:
            logging.warning("[KALSHI] get_resting_buy_orders_cost_cents failed: %s", e)
            return total_cents
    # DEBUG: cycle summary line in main.py already surfaces deployment;
    # this per-cycle "0 resting buy orders" line was logging noise (H5).
    log_level = logging.INFO if order_count > 0 else logging.DEBUG
    logging.log(
        log_level,
        "[KALSHI:resting] %d resting buy orders, total reserved=$%.2f",
        order_count, total_cents / 100.0,
    )
    return total_cents


def get_open_positions() -> list[dict]:
    resp = _request("GET", "/portfolio/positions")
    if resp is None or resp.status_code != 200:
        return []
    return resp.json().get("market_positions", []) or []


def verify_api_connection() -> bool:
    resp = _request("GET", "/markets?status=open&limit=1", timeout=10)
    return resp is not None and resp.status_code == 200
