"""
venue.py — abstract Venue interface.

Single source of truth for what every prediction-market exchange must expose
to the rest of the bot. Concrete implementations live in kalshi_venue.py and
polymarket_client.py.

Why this exists: v1 was Kalshi-only and let venue-specific shapes leak into
strategy.py / risk.py / executor.py. Adding Polymarket made the cost of that
leak obvious. The Protocol + canonicalized MarketMeta is the firewall.

Canonicalization (resolution_source / threshold / comparator) is mandatory,
not optional — without it we cannot:
  - match Polymarket markets to Kalshi markets for arb
  - honestly grade Polymarket trades against the venue's own oracle
  - apply a venue-aware paper-fill simulator
The v1 postmortem §3.1 / §4.2 traces the largest false-signal source to
forecast input and outcome being drawn from related sources; the canonical
resolution_source is the field that prevents that mistake on Polymarket.
"""

from __future__ import annotations

from typing import Any, Callable, Literal, Protocol, TypedDict

Side = Literal["yes", "no"]
Comparator = Literal[">=", ">", "<=", "<", "in_range"]


class OrderBook(TypedDict):
    yes_bids: list[tuple[float, float]]   # [(price_dollars, dollar_depth)]
    no_bids: list[tuple[float, float]]
    best_yes_ask: float | None
    best_no_ask: float | None
    yes_depth_at_price: Callable[[float], int]   # contracts at-or-better
    no_depth_at_price: Callable[[float], int]


class MarketMeta(TypedDict, total=False):
    venue: str                   # "kalshi" | "polymarket"
    market_id: str               # ticker (Kalshi) or condition_id (Polymarket)
    city: str
    market_type: str             # "high_temp", etc.
    title: str
    # Canonical resolution rule — see module docstring.
    resolution_source: str       # e.g. "NWS:KNYC:daily_high"
    threshold: float | None
    comparator: Comparator | None
    range_low: float | None
    range_high: float | None
    target_settlement: str       # ISO datetime
    # Live book-derived prices (populated by venue clients on list_markets()
    # so strategy.find_opportunities() doesn't need a per-market round trip).
    yes_ask_dollars: float | None
    no_ask_dollars: float | None
    close_time: str | None
    expected_expiration_time: str | None
    # Backwards-compat: the strategy code reads `ticker` everywhere; for
    # Kalshi it equals market_id. Polymarket sets it to market_id too so
    # downstream code doesn't fork on venue.
    ticker: str
    raw: dict[str, Any]          # original venue payload for debugging


class Venue(Protocol):
    """Every venue must implement this. Phase-1 Polymarket implementation
    raises NotImplementedError on every execution method — read-only only."""

    name: str

    # ─── Discovery ─────────────────────────────────────────────────────────
    def list_markets(self) -> list[MarketMeta]:
        """All open markets relevant to this bot, with canonicalized rule."""
        ...

    def get_book(self, market_id: str) -> OrderBook:
        """Live order book for one market."""
        ...

    def get_market(self, market_id: str) -> dict[str, Any] | None:
        """Single market's current state (status, prices, settlement)."""
        ...

    def get_resolution(self, market_id: str) -> dict[str, Any] | None:
        """Final outcome from the venue's own settlement source.

        For Kalshi: market.result once status == 'finalized'.
        For Polymarket: UMA oracle-resolved outcome.
        Returns None if not yet resolved.

        CRITICAL: this MUST come from the venue's own resolution, never
        from our own forecast or a third-party feed. v1 postmortem §3.1.
        """
        ...

    # ─── Fees ──────────────────────────────────────────────────────────────
    def fee_for_trade(self, price: float, contracts: int, side: Side) -> float:
        """Per-fill fee in dollars. Polymarket returns 0.0 today."""
        ...

    # ─── Execution ─────────────────────────────────────────────────────────
    def place_limit_order(self, market_id: str, side: Side,
                          contracts: int, limit_price_cents: int) -> str | None:
        ...

    def cancel_order(self, order_id: str) -> bool:
        ...

    def get_order_status(self, order_id: str) -> dict[str, Any] | None:
        ...

    def sell_position(self, market_id: str, side: Side,
                      contracts: int) -> str | None:
        ...

    # ─── Portfolio ─────────────────────────────────────────────────────────
    def get_portfolio_balance(self) -> dict[str, Any] | None:
        ...

    def get_open_positions(self) -> list[dict[str, Any]]:
        ...

    def verify_connection(self) -> bool:
        ...
