"""
backfill_kalshi_positions.py — recover trades that filled on Kalshi but
were never written to trades.db (e.g. demo lag → executor abandoned the
order under taker_no_fill / *_place_failed before the fill landed).

Source of truth: /portfolio/orders?status=executed&action=buy
Skips any order_id already present in trades.db.
Default is dry-run; pass --commit to actually insert.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import kalshi_client  # noqa: E402
import storage  # noqa: E402
from config import WEATHER_SERIES  # noqa: E402

PREFIX_TO_CITY = {v: k for k, v in WEATHER_SERIES.items()}
# Unknown prefixes (e.g. KXHIGHTNOLA, KXHIGHTSATX added after config.py
# was last edited) get city=None — still recorded, just not categorised.


def parse_ticker(ticker: str) -> tuple[str | None, str | None, str | None]:
    """KXHIGHTHOU-26MAY05-B85.5 → ('Houston', 'high_temp', '2026-05-05')."""
    if not ticker or not ticker.startswith("KXHIGH"):
        return None, None, None
    parts = ticker.split("-")
    if len(parts) < 2:
        return None, None, None
    prefix = parts[0]
    city = PREFIX_TO_CITY.get(prefix)
    settlement = None
    try:
        # parts[1] is e.g. "26MAY05" — interpret as 2026-05-05
        d = datetime.strptime(parts[1], "%y%b%d")
        settlement = d.strftime("%Y-%m-%d")
    except Exception:
        pass
    return city, "high_temp", settlement


def existing_order_ids(db_file: str) -> set[str]:
    with sqlite3.connect(db_file, timeout=10) as conn:
        rows = conn.execute(
            "SELECT order_id FROM trades WHERE order_id IS NOT NULL"
        ).fetchall()
    return {r[0] for r in rows if r[0]}


# Secondary dedup window: catches the maker→cancel→taker replacement
# chain within one execute_opportunity call (maker rest + cancel +
# taker placement ≈ 2 minutes max). 2026-05-07: was 6 HOURS, which
# falsely matched legitimate separate trades on the same market hours
# apart. The legitimate dedup target is "place→cancel→replace within
# one execute_opportunity invocation"; that's seconds-to-minutes, not
# hours. Keep the window tight.
_ATTR_DEDUP_WINDOW_SECONDS = 180


def attribute_match_exists(db_file: str, ticker: str, action: str,
                           contracts: int, opened_at: str) -> bool:
    """True if a non-backfill row already covers this trade by
    (ticker, action, contracts) within ±_ATTR_DEDUP_WINDOW_SECONDS."""
    if not (ticker and action and contracts and opened_at):
        return False
    try:
        from datetime import datetime as _dt, timedelta as _td
        t = _dt.fromisoformat(opened_at.replace("Z", "+00:00"))
    except Exception:
        return False
    lo = (t - _td(seconds=_ATTR_DEDUP_WINDOW_SECONDS)).isoformat()
    hi = (t + _td(seconds=_ATTR_DEDUP_WINDOW_SECONDS)).isoformat()
    with sqlite3.connect(db_file, timeout=10) as conn:
        row = conn.execute(
            """
            SELECT id FROM trades
            WHERE ticker = ? AND action = ? AND contracts = ?
              AND mode IS NOT NULL AND mode NOT IN ('dry-run', 'backfill')
              AND opened_at BETWEEN ? AND ?
            LIMIT 1
            """,
            (ticker, action, int(contracts), lo, hi),
        ).fetchone()
    return row is not None


def _f(v, default=0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def fill_price_dollars(o: dict) -> float:
    """Side-specific executed price in dollars. Kalshi returns both
    yes_price_dollars and no_price_dollars as strings."""
    side = (o.get("side") or "").lower()
    if side == "yes":
        v = o.get("yes_price_dollars") or o.get("yes_price")
    elif side == "no":
        v = o.get("no_price_dollars") or o.get("no_price")
    else:
        v = (o.get("yes_price_dollars") or o.get("no_price_dollars")
             or o.get("price"))
    px = _f(v)
    # Legacy cents fields would be >1; the *_dollars fields are already $.
    return px / 100.0 if px > 1 else px


def filled_count(o: dict) -> int:
    for k in ("fill_count_fp", "filled_count", "count", "executed_count"):
        v = o.get(k)
        if v is not None:
            try:
                return int(float(v))
            except (TypeError, ValueError):
                pass
    return 0


def created_at(o: dict) -> str:
    for k in ("created_time", "last_update_time", "executed_time"):
        v = o.get(k)
        if v:
            return str(v)
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def insert_backfill(o: dict, count_override: int | None = None) -> int:
    ticker = o.get("ticker") or ""
    side = (o.get("side") or "").upper()  # YES/NO
    action_str = f"BUY {side}" if side else "BUY"
    fill_p = fill_price_dollars(o)
    count = int(count_override) if count_override is not None else filled_count(o)
    city, mtype, settle = parse_ticker(ticker)
    # opened_at = NOW (insertion time), not the Kalshi fill time. Faking
    # opened_at back to the historical fill caused autoincrement id
    # ordering and opened_at ordering to disagree, which broke any
    # consumer that assumed id ↔ time monotonicity (notably the exit-loop
    # diagnosis on 2026-05-07 CHI-B62.5: trade #18 looked older than #11
    # by opened_at but younger by id). The historical fill time is
    # preserved in `notes` for forensic traceability.
    discovered_at = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    kalshi_fill_time = created_at(o)
    notes = (
        f"backfill_from_kalshi_positions_endpoint;"
        f"kalshi_fill_time={kalshi_fill_time}"
    )
    if count_override is not None and count_override != filled_count(o):
        notes += f";partial_residual_of_{filled_count(o)}"
    order_id = o.get("order_id") or o.get("id")

    with sqlite3.connect(storage.DB_FILE, timeout=10) as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (
                ticker, city, market_type, action,
                entry_price, contracts, size_usd,
                ensemble_p, calibrated_p, edge_at_entry,
                mode, opened_at, target_settlement, notes,
                paper_trade, order_id, venue
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker, city, mtype, action_str,
                fill_p, count, fill_p * count,
                0.0, 0.0, 0.0,
                "backfill", discovered_at, settle or "",
                notes,
                0, order_id, "kalshi",
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def db_open_net_by_ticker(db_file: str) -> dict[str, int]:
    """Per-ticker net signed contract count from currently-open DB rows.

    Open = no `results` row joined. BUY YES contributes +contracts, BUY NO
    contributes -contracts. Matches Kalshi's `position_fp` sign convention
    so we can subtract directly to compute residual unrecorded exposure.
    """
    net: dict[str, int] = {}
    with sqlite3.connect(db_file, timeout=10) as conn:
        rows = conn.execute(
            """
            SELECT t.ticker, t.action, COALESCE(t.contracts, 0)
            FROM trades t
            LEFT JOIN results r ON r.trade_id = t.id
            WHERE r.id IS NULL
              AND t.venue = 'kalshi'
              AND (t.mode IS NULL OR t.mode != 'dry-run')
            """
        ).fetchall()
    for ticker, action, contracts in rows:
        if not ticker:
            continue
        sign = -1 if str(action or "").upper() == "BUY NO" else 1
        net[ticker] = net.get(ticker, 0) + sign * int(contracts or 0)
    return net


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="Actually write to trades.db (default: dry-run).")
    args = ap.parse_args()

    storage.init_db()
    print(f"DB: {storage.DB_FILE}")
    print("Fetching open positions from Kalshi…")
    positions = kalshi_client.get_open_positions()
    kalshi_net: dict[str, int] = {}
    for p in positions:
        try:
            fp = float(p.get("position_fp") or 0)
        except (TypeError, ValueError):
            fp = 0.0
        if fp == 0:
            continue
        tk = p.get("ticker") or ""
        if tk:
            kalshi_net[tk] = int(fp)
    print(f"  {len(kalshi_net)} tickers currently held on Kalshi")

    db_net = db_open_net_by_ticker(storage.DB_FILE)
    print(f"  DB has open rows for {len(db_net)} tickers")

    # Per-ticker residual: what Kalshi shows minus what our DB tracks.
    # Same sign convention (negative = NO, positive = YES). |residual|
    # contracts on the appropriate side need to be backfilled.
    #
    # Why this is more correct than order-id-only matching: an order_id
    # in /portfolio/orders may correspond to an exposure we've already
    # closed locally via a buy+sell round-trip; inserting it as a fresh
    # open backfill row would over-record exposure. The 2026-05-07
    # CHI-B62.5 incident exposed this — the previous logic happened to
    # produce the right answer there but only by coincidence.
    residuals: dict[str, int] = {}
    all_tickers = set(kalshi_net) | set(db_net)
    for tk in all_tickers:
        diff = kalshi_net.get(tk, 0) - db_net.get(tk, 0)
        if diff != 0:
            residuals[tk] = diff

    if not residuals:
        print("\nNo residual exposure — DB and Kalshi agree per-ticker. "
              "Nothing to backfill.")
        return

    print(f"\n  {len(residuals)} ticker(s) with residual unrecorded exposure:")
    for tk, diff in sorted(residuals.items()):
        print(f"    {tk:36s} kalshi={kalshi_net.get(tk, 0):+d}  "
              f"db={db_net.get(tk, 0):+d}  residual={diff:+d}")

    print("\nFetching executed BUY orders from Kalshi…")
    orders = kalshi_client.get_filled_orders(limit=200)
    print(f"  Kalshi returned {len(orders)} executed BUY orders")
    have = existing_order_ids(storage.DB_FILE)
    print(f"  trades.db already has {len(have)} order_ids")

    # Pick orders to insert per residual: take candidates on this ticker
    # whose order_id isn't already in DB, side matches the residual sign,
    # and accumulate contracts up to |residual|. Largest fills first
    # (typical case is one stranded fill = one order; smaller orders
    # would only be needed if the stranded exposure is split across many).
    to_insert: list[tuple[dict, int]] = []  # (order, count_to_record)
    unreconciled: list[str] = []

    for tk, residual in residuals.items():
        if residual == 0:
            continue
        need_side = "no" if residual < 0 else "yes"
        need_qty = abs(residual)
        cands = [
            o for o in orders
            if o.get("ticker") == tk
            and (o.get("order_id") or o.get("id")) not in have
            and (o.get("side") or "").lower() == need_side
            and filled_count(o) > 0
        ]
        cands.sort(key=lambda o: -filled_count(o))

        remaining = need_qty
        for o in cands:
            if remaining <= 0:
                break
            cnt = min(filled_count(o), remaining)
            # Attribute-dedup safety net: warn but don't auto-skip.
            # Net-residual math has already established this exposure
            # is genuinely unrecorded; an attribute match within 3 min
            # would be unusual and worth surfacing rather than silently
            # suppressing.
            side_up = (o.get("side") or "").upper()
            if attribute_match_exists(
                storage.DB_FILE, tk, f"BUY {side_up}", cnt, created_at(o)
            ):
                print(f"  ⚠ {tk}: candidate matches a non-backfill row by "
                      f"(ticker, action, contracts) within "
                      f"{_ATTR_DEDUP_WINDOW_SECONDS}s — flagging but "
                      f"inserting anyway because residual math says "
                      f"exposure is real")
            to_insert.append((o, cnt))
            remaining -= cnt

        if remaining > 0:
            unreconciled.append(
                f"{tk}: residual={residual:+d} but only "
                f"{need_qty - remaining} contracts of matching candidates "
                f"available — {remaining} contracts unaccounted for"
            )

    if unreconciled:
        print("\n  ⚠ Some residuals could not be fully reconciled from "
              "/portfolio/orders:")
        for u in unreconciled:
            print(f"    {u}")

    if not to_insert:
        print("\nNo candidates. Residuals exist but no matching unrecorded "
              "orders found — manual investigation needed.")
        return

    total_cost = 0.0
    print("\nPlanned inserts:")
    for o, cnt in to_insert:
        ticker = o.get("ticker") or "?"
        side = (o.get("side") or "?").upper()
        full = filled_count(o)
        px = fill_price_dollars(o)
        cost = px * cnt
        total_cost += cost
        partial = f" (of {full})" if cnt != full else ""
        print(f"  {ticker:32s} BUY {side:3s}  {cnt:4d}{partial} @ "
              f"${px:.4f}  cost=${cost:6.2f}  "
              f"oid={o.get('order_id') or o.get('id')}")
    print(f"\n  TOTAL would-be deployed: ${total_cost:.2f}")

    if not args.commit:
        print("\nDry-run only. Re-run with --commit to insert.")
        return

    print("\nInserting…")
    n = 0
    for o, cnt in to_insert:
        try:
            tid = insert_backfill(o, count_override=cnt)
            n += 1
            print(f"  inserted trade_id={tid} ticker={o.get('ticker')} "
                  f"contracts={cnt}")
        except Exception as e:
            print(f"  FAILED for {o.get('ticker')}: {e}")
    print(f"\nDone. Inserted {n} backfill rows.")


if __name__ == "__main__":
    main()
