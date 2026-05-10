"""
strategy_arb.py — implied-probability arbitrage with audit B4 fixes.

Within a single bracket series on a single day, all legs are mutually exclusive
and exhaustive: exactly one resolves YES. If the sum of YES asks (plus fees)
is below $1.00, buying every leg is guaranteed profit.

Audit B4 fixes implemented here:
  1. sum_yes_with_fees includes exact Kalshi per-contract fee per leg.
  2. profit_per_unit = 1.00 - sum_yes_with_fees (NOT sum_yes alone).
  3. Every leg's orderbook depth is validated before any order is returned.
  4. Partial-fill rollback is handled in executor.arb_execute_group.

Isolation invariant — DO NOT BREAK:
  Arbitrage is mechanical and side-independent; directional filters must
  never apply to arb legs. The pipelines are deliberately separate:

    discovery:    strategy_arb.scan_arbitrage()       (this module)
                  vs. strategy.find_opportunities()   (directional)
    constitution: config.evaluate_trade has explicit `market_type == "arbitrage"`
                  carve-outs (MIN_EDGE_ARB, price-window skip).
    risk:         _process_arb_group in main.py gates on GROUP totals.
                  Per-city exposure excludes arbs (risk.py).
    execution:    executor.arb_execute_group is atomic with rollback.

  Arb legs are tagged action="BUY YES" with calibrated_p == yes_price by
  construction (see opportunity dict below). Any directional filter that
  reads calibrated_p, edge, or action without first checking
  market_type != "arbitrage" will silently break arbs. The 2026-05-03
  BUY YES guards in strategy.py are correctly scoped to find_opportunities;
  keep new directional logic there, not in any code path arbs traverse.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from math import ceil

import math

import kalshi_client
from config import (
    ARB_MIN_LEGS,
    MAX_SINGLE_BET_PCT,
    MIN_EDGE_ARB,
    SLIPPAGE_BUFFER_PCT,
    kalshi_trade_fee,
)


def _taker_cross_price(price: float) -> float:
    """The actual price executor.arb_execute_group will cross at.
    Audit C6: depth checks must be done at this price, not at `price`,
    or the executor can pass preflight only to partial-fill (because the
    book at price+slippage is thinner than at price)."""
    cents = max(1, min(99, int(math.ceil(price * (1.0 + SLIPPAGE_BUFFER_PCT) * 100))))
    return cents / 100.0


def _group_key(ticker: str) -> str | None:
    parts = ticker.split("-")
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return None


def _human_label(group_key: str, city: str, market_type: str) -> str:
    date_part = group_key.split("-")[1] if "-" in group_key else group_key
    m = re.search(r"(\d{2})([A-Z]{3})(\d{2})", date_part)
    if m:
        return f"{city} {market_type} {m.group(2)} {int(m.group(1))}"
    return f"{city} {market_type} {date_part}"


def scan_arbitrage(all_markets: list[dict], bankroll: float) -> list[dict]:
    """Fee-inclusive arb scan with per-leg depth validation."""
    groups: dict[str, list[dict]] = {}

    for m in all_markets:
        ticker = m.get("ticker", "")
        yes_price = float(m.get("yes_ask_dollars") or 0.0)
        if not ticker or yes_price <= 0 or yes_price >= 1:
            continue
        key = _group_key(ticker)
        if not key:
            continue
        groups.setdefault(key, []).append(
            {
                "ticker": ticker,
                "yes_price": yes_price,
                "city": m.get("city", ""),
                "title": m.get("title", ""),
                "market_type": m.get("market_type", "high_temp"),
                "close_time": m.get("close_time") or m.get("expected_expiration_time"),
            }
        )

    opportunities: list[dict] = []

    for key, legs in groups.items():
        # Drop $0.01-floor legs — resolved-NO settled losers (false signal).
        legs = [leg for leg in legs if leg["yes_price"] > 0.02]
        if len(legs) < ARB_MIN_LEGS:
            continue

        sum_yes = sum(leg["yes_price"] for leg in legs)
        if sum_yes >= 1.0:
            continue  # no arb

        # ── Per-unit sizing ──────────────────────────────────────────────────
        # Start from the per-leg cap (5% of bankroll on the priciest leg) and
        # then sanity-clamp so total capital deployed stays under bankroll.
        max_leg_price = max(leg["yes_price"] for leg in legs)
        per_leg_cap_usd = bankroll * MAX_SINGLE_BET_PCT
        n_contracts = max(1, int(per_leg_cap_usd / max(max_leg_price, 0.01)))
        # Keep total cost under bankroll so we don't over-deploy.
        if n_contracts * sum_yes > bankroll:
            n_contracts = max(1, int(bankroll / max(sum_yes, 0.01)))

        # ── Fee-inclusive edge (audit B4) ────────────────────────────────────
        sum_yes_with_fees = 0.0
        for leg in legs:
            fee = kalshi_trade_fee(n_contracts, leg["yes_price"])
            # Express fee as per-unit addition to the leg price.
            sum_yes_with_fees += leg["yes_price"] + (fee / max(n_contracts, 1))
        profit_per_unit = 1.00 - sum_yes_with_fees

        if profit_per_unit < MIN_EDGE_ARB:
            continue

        # ── Per-leg depth validation (audit B4 + C6) ─────────────────────────
        # Check depth at the price the executor will ACTUALLY cross at —
        # leg["yes_price"] * (1 + SLIPPAGE_BUFFER_PCT), ceiled to a cent.
        # Checking at the unadjusted price was optimistic: the book at
        # cross_price is often a tick thinner, leading to partial fills
        # that triggered the rollback path even though preflight passed.
        enough_depth = True
        for leg in legs:
            try:
                ob = kalshi_client.get_orderbook(leg["ticker"])
            except Exception:
                enough_depth = False
                break
            cross_price = _taker_cross_price(leg["yes_price"])
            depth = 0
            try:
                depth = int(ob["yes_depth_at_price"](cross_price))
            except Exception:
                depth = 0
            if depth < n_contracts:
                logging.debug(
                    "[ARB] depth fail on %s: need %d at cross_price $%.2f "
                    "(quoted $%.2f), have %d — skipping group %s",
                    leg["ticker"], n_contracts, cross_price,
                    leg["yes_price"], depth, key,
                )
                enough_depth = False
                break
        if not enough_depth:
            continue

        arb_id = f"arb:{key}:{datetime.now().strftime('%Y%m%d%H%M%S')}"
        city = legs[0]["city"]
        mtype = legs[0]["market_type"]
        label = _human_label(key, city, mtype)
        guaranteed_usd = round(n_contracts * profit_per_unit, 2)

        for leg in legs:
            leg_cost = round(n_contracts * leg["yes_price"], 2)
            opportunities.append(
                {
                    "ticker": leg["ticker"],
                    "city": leg["city"],
                    "title": leg["title"],
                    "market_type": "arbitrage",
                    "action": "BUY YES",
                    "yes_price": leg["yes_price"],
                    "entry_price": leg["yes_price"],
                    "recommended_size": leg_cost,
                    "contracts": n_contracts,
                    "edge": round(profit_per_unit, 4),
                    "calibrated_p": round(leg["yes_price"], 4),
                    "raw_probability": round(leg["yes_price"], 4),
                    "target_settlement": leg.get("close_time"),
                    "reasoning": (
                        f"ARB {label}: {len(legs)} legs, sum+fees={sum_yes_with_fees:.4f}, "
                        f"profit/unit={profit_per_unit:.4f}, guaranteed=${guaranteed_usd:.2f}"
                    ),
                    "notes": arb_id,
                    "arb_id": arb_id,
                    "arb_group_key": key,
                    "arb_legs": len(legs),
                    "arb_sum_yes": round(sum_yes, 4),
                    "arb_sum_yes_with_fees": round(sum_yes_with_fees, 4),
                    "guaranteed_profit_usd": guaranteed_usd,
                }
            )

    return opportunities
