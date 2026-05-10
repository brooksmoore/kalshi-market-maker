"""
demo_vs_prod_audit.py — side-by-side comparison of Kalshi Demo vs Prod for
weather markets.

Pulls the same series from both environments (demo via signed client, prod
via public unauth GETs) and reports:
  - Event/market availability (which strikes exist where)
  - Orderbook spread + best bid/ask sizes per matched ticker
  - Notional + price-grid metadata (fee-relevant fields)

Run: python scripts/demo_vs_prod_audit.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalshi_client import _request as demo_request  # signed demo client

PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Pick a few weather event tickers known to exist on both venues. Daily-high
# series for major cities are the most likely to be replicated on demo.
EVENTS_TO_PROBE = [
    "KXHIGHNY",   # NYC daily high
    "KXHIGHLAX",  # LA daily high
    "KXHIGHCHI",  # Chicago
    "KXHIGHMIA",  # Miami
    "KXHIGHDEN",  # Denver
    "KXHIGHAUS",  # Austin
    "KXHIGHPHIL", # Philadelphia
]


def prod_get(path: str) -> dict | None:
    try:
        r = requests.get(f"{PROD_BASE}{path}", timeout=15)
        if r.status_code != 200:
            return {"_status": r.status_code, "_body": r.text[:200]}
        return r.json()
    except Exception as e:
        return {"_error": str(e)}


def demo_get(path: str) -> dict | None:
    r = demo_request("GET", path)
    if r is None:
        return {"_error": "no response"}
    if r.status_code != 200:
        return {"_status": r.status_code, "_body": r.text[:200]}
    return r.json()


def list_event_markets(env: str, series: str) -> list[dict]:
    path = f"/markets?series_ticker={series}&status=open&limit=200"
    out: list[dict] = []
    cursor = ""
    for _ in range(5):
        p = path + (f"&cursor={cursor}" if cursor else "")
        data = (prod_get if env == "prod" else demo_get)(p)
        if not isinstance(data, dict) or "_error" in data or "_status" in data:
            return out
        out.extend(data.get("markets") or [])
        cursor = data.get("cursor") or ""
        if not cursor:
            break
    return out


def get_orderbook(env: str, ticker: str) -> dict | None:
    path = f"/markets/{ticker}/orderbook?depth=10"
    return (prod_get if env == "prod" else demo_get)(path)


def best_bid_ask(book: dict | None) -> tuple[float | None, float | None, float | None, float | None]:
    """Return (yes_best_bid, yes_best_ask, yes_bid_size, yes_ask_size) in $."""
    if not book or "_error" in book or "_status" in book:
        return (None, None, None, None)
    ob = book.get("orderbook_fp") or book.get("orderbook") or {}
    yes = ob.get("yes_dollars") or ob.get("yes") or []
    no = ob.get("no_dollars") or ob.get("no") or []
    # In Kalshi book: yes_dollars is bids on YES (sorted ascending price).
    # Best YES bid = highest price on yes side. Best YES ask = 1 - highest NO bid.
    if yes:
        yes_levels = sorted(((float(p), float(s)) for p, s in yes), key=lambda x: -x[0])
        yes_bid_p, yes_bid_sz = yes_levels[0]
    else:
        yes_bid_p = yes_bid_sz = None
    if no:
        no_levels = sorted(((float(p), float(s)) for p, s in no), key=lambda x: -x[0])
        no_bid_p, no_bid_sz = no_levels[0]
        yes_ask_p = round(1.0 - no_bid_p, 4)
        yes_ask_sz = no_bid_sz
    else:
        yes_ask_p = yes_ask_sz = None
    return (yes_bid_p, yes_ask_p, yes_bid_sz, yes_ask_sz)


def market_meta(m: dict) -> dict:
    return {
        "ticker": m.get("ticker"),
        "subtitle": m.get("subtitle"),
        "status": m.get("status"),
        "fractional": m.get("fractional_trading_enabled"),
        "notional": m.get("notional_value_dollars") or m.get("notional_value"),
        "price_units": m.get("response_price_units"),
        "price_struct": m.get("price_level_structure"),
        "price_ranges": m.get("price_ranges"),
        "open_interest": m.get("open_interest_fp") or m.get("open_interest"),
        "volume_24h": m.get("volume_24h_fp") or m.get("volume_24h"),
        "yes_bid": m.get("yes_bid_dollars") or m.get("yes_bid"),
        "yes_ask": m.get("yes_ask_dollars") or m.get("yes_ask"),
    }


def compare_event(series: str) -> dict:
    print(f"\n{'='*78}\n=== {series} ===\n{'='*78}")
    prod_mkts = list_event_markets("prod", series)
    demo_mkts = list_event_markets("demo", series)
    prod_by_t = {m["ticker"]: m for m in prod_mkts}
    demo_by_t = {m["ticker"]: m for m in demo_mkts}

    prod_keys = set(prod_by_t)
    demo_keys = set(demo_by_t)
    only_prod = prod_keys - demo_keys
    only_demo = demo_keys - prod_keys
    both = prod_keys & demo_keys

    print(f"  prod markets: {len(prod_mkts)}  demo markets: {len(demo_mkts)}  shared: {len(both)}")
    if only_prod:
        print(f"  only-on-prod ({len(only_prod)}): {sorted(only_prod)[:8]}")
    if only_demo:
        print(f"  only-on-demo ({len(only_demo)}): {sorted(only_demo)[:8]}")

    # Compare top-of-book on the most active 4 shared tickers (by prod volume).
    shared_sorted = sorted(
        both,
        key=lambda t: float(prod_by_t[t].get("volume_24h_fp") or 0),
        reverse=True,
    )
    rows = []
    for t in shared_sorted[:6]:
        pm = prod_by_t[t]; dm = demo_by_t[t]
        # use book if list endpoint already returns top, else fetch
        prod_book = get_orderbook("prod", t)
        demo_book = get_orderbook("demo", t)
        time.sleep(0.1)
        p_bb, p_ba, p_bs, p_as = best_bid_ask(prod_book)
        d_bb, d_ba, d_bs, d_as = best_bid_ask(demo_book)
        rows.append({
            "ticker": t,
            "subtitle": pm.get("subtitle"),
            "prod_yes_bid": p_bb, "prod_yes_ask": p_ba,
            "prod_bid_sz": p_bs, "prod_ask_sz": p_as,
            "prod_spread_c": (round((p_ba - p_bb) * 100, 1) if (p_bb is not None and p_ba is not None) else None),
            "demo_yes_bid": d_bb, "demo_yes_ask": d_ba,
            "demo_bid_sz": d_bs, "demo_ask_sz": d_as,
            "demo_spread_c": (round((d_ba - d_bb) * 100, 1) if (d_bb is not None and d_ba is not None) else None),
            "prod_meta": market_meta(pm),
            "demo_meta": market_meta(dm),
        })
        print(f"  {t} [{pm.get('subtitle')}]")
        print(f"    prod: yes_bid={p_bb} ({p_bs}) / ask={p_ba} ({p_as})  spread={rows[-1]['prod_spread_c']}c  vol24h={pm.get('volume_24h_fp')}  OI={pm.get('open_interest_fp')}")
        print(f"    demo: yes_bid={d_bb} ({d_bs}) / ask={d_ba} ({d_as})  spread={rows[-1]['demo_spread_c']}c  vol24h={dm.get('volume_24h_fp')}  OI={dm.get('open_interest_fp')}")

    return {
        "series": series,
        "prod_count": len(prod_mkts),
        "demo_count": len(demo_mkts),
        "only_prod": sorted(only_prod),
        "only_demo": sorted(only_demo),
        "shared": len(both),
        "rows": rows,
    }


def main() -> None:
    out: dict[str, Any] = {"timestamp": time.time(), "events": []}
    for series in EVENTS_TO_PROBE:
        try:
            out["events"].append(compare_event(series))
        except Exception as e:
            print(f"  !! {series} failed: {e}")
    # Aggregate spread comparison
    print(f"\n{'='*78}\n=== AGGREGATE SPREAD COMPARISON ===\n{'='*78}")
    pairs: list[tuple[float, float, str]] = []
    for ev in out["events"]:
        for r in ev["rows"]:
            if r["prod_spread_c"] is not None and r["demo_spread_c"] is not None:
                pairs.append((r["prod_spread_c"], r["demo_spread_c"], r["ticker"]))
    if pairs:
        prod_spreads = [p[0] for p in pairs]
        demo_spreads = [p[1] for p in pairs]
        print(f"  matched contracts with both books: {len(pairs)}")
        print(f"  prod spread (cents): mean={sum(prod_spreads)/len(prod_spreads):.2f} median={sorted(prod_spreads)[len(prod_spreads)//2]:.1f} max={max(prod_spreads):.1f}")
        print(f"  demo spread (cents): mean={sum(demo_spreads)/len(demo_spreads):.2f} median={sorted(demo_spreads)[len(demo_spreads)//2]:.1f} max={max(demo_spreads):.1f}")
        wider_demo = sum(1 for p, d, _ in pairs if d > p)
        wider_prod = sum(1 for p, d, _ in pairs if p > d)
        equal = sum(1 for p, d, _ in pairs if p == d)
        print(f"  demo wider: {wider_demo}  prod wider: {wider_prod}  equal: {equal}")
        # Worst examples
        worst = sorted(pairs, key=lambda x: x[1] - x[0], reverse=True)[:5]
        print("  largest demo-vs-prod gaps (demo - prod, cents):")
        for p, d, t in worst:
            print(f"    {t}: prod={p}c demo={d}c (delta={d-p:+.1f}c)")

    Path(ROOT / "data").mkdir(exist_ok=True)
    out_path = ROOT / "data" / "demo_vs_prod_audit.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\n  full dump → {out_path}")


if __name__ == "__main__":
    main()
