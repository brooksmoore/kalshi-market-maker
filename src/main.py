"""
main.py — kalshi_bot_2.0 scan loop.

Boots in halted dry-run by default. Flip LIVE_TRADING_ENABLED=true in .env to
send real orders against the demo Kalshi endpoint.

Boot sequence:
  1. init_db()
  2. verify Kalshi API connection
  3. if calibration.pkl missing, bootstrap from v1 trades.db (fail-open)
  4. loop every SCAN_INTERVAL_SECONDS
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import datetime

import calibration
import cross_venue
import forecast_health
import kalshi_client
import maker_sim
import polymarket_client
import risk
import storage
import strategy
import strategy_arb
import telegram_notify
from config import (
    CALIBRATION_META,
    CALIBRATION_PKL,
    DATA_DIR,
    LIVE_TRADING_ENABLED,
    LOG_FILE,
    SCAN_INTERVAL_SECONDS,
    STARTING_BANKROLL,
    V1_DB_PATH,
)

# Polymarket venue is constructed once at import. Read-only; safe to share.
_POLYMARKET = polymarket_client.PolymarketVenue()

# Cycle writes a fresh snapshot of Polymarket markets here so the dashboard
# can render the /polymarket page without re-fetching live on every request.
# Path mirrors data/forecast_health.json convention. Phase-1 ingest only.
_POLYMARKET_SNAPSHOT_FILE = os.path.join(DATA_DIR, "polymarket_markets.json")

# Same idea for the Kalshi series discovery — surfaces both mapped and
# unmapped series so the user can see when Kalshi adds something we don't
# yet have a city pattern for.
_KALSHI_SERIES_SNAPSHOT_FILE = os.path.join(DATA_DIR, "kalshi_series.json")

# Phase-2 cross-venue arbitrage detections written here each cycle. The
# /cross dashboard page reads from it. Detection-only — no execution.
_CROSS_VENUE_SNAPSHOT_FILE = os.path.join(DATA_DIR, "cross_venue_arb.json")

_STOP = threading.Event()
_cycle_num = 0
_session_cost_usd = 0.0
_last_hourly_ts: float = 0.0  # fires immediately on first cycle

# Show the full session-stats banner on startup and every N cycles thereafter.
_BANNER_EVERY = 12
# LOG_VERBOSE=1 restores per-opportunity INFO logging on stdout. The file log
# always keeps full detail at DEBUG regardless, so post-hoc diagnosis isn't lost.
_VERBOSE = os.environ.get("LOG_VERBOSE", "").lower() in ("1", "true", "yes")


def _install_logging() -> None:
    os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for h in list(root.handlers):
        root.removeHandler(h)
    # Rotate the bot log at 20MB, keep 3 generations. Without rotation, the
    # log was growing unboundedly (~38MB observed before the audit on
    # 2026-05-08). 4 × 20MB = 80MB cap is small enough not to swamp the
    # disk and large enough to keep ~24h of normal cycles for diagnosis.
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=20 * 1024 * 1024, backupCount=3,
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.DEBUG if _VERBOSE else logging.INFO)
    root.addHandler(fh)
    root.addHandler(sh)

    # Silence noisy third-party DEBUG loggers. httpx/httpcore emit DEBUG
    # records during connection close — which Python may run from inside a
    # garbage-collector __del__ that fires while logging.flush() is already
    # active, producing "reentrant call inside <BufferedWriter>" crashes.
    # Capping these at WARNING removes the entire class of those errors.
    for _noisy in ("httpx", "httpcore", "httpcore.connection",
                   "httpcore.http11", "httpcore.proxy", "anthropic",
                   "urllib3", "openai"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)


def _sigint(*_a) -> None:
    _STOP.set()
    logging.info("[MAIN] SIGINT received; shutting down at end of cycle")


def _fmt_pnl(v: float) -> str:
    return ("+$" if v >= 0 else "-$") + f"{abs(v):.2f}"


def _log_cycle_header(cycle_num: int, bankroll: float, interval: int) -> None:
    stats = storage.get_cycle_stats()
    open_pos = stats.get("open_positions", "?")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    today_n     = stats.get("today_resolved", 0)
    today_pnl   = stats.get("today_pnl", 0.0)
    today_wins  = stats.get("today_wins", 0)
    today_loss  = today_n - today_wins
    today_wr    = f"{today_wins/today_n*100:.1f}%" if today_n else "n/a"

    total_n     = stats.get("total_logged", 0)
    total_yes   = stats.get("total_yes", 0)
    total_no    = stats.get("total_no", 0)
    avg_edge    = stats.get("avg_edge", 0.0)
    t_wins      = stats.get("total_wins", 0)
    t_resolved  = stats.get("total_resolved", 0)
    t_loss      = t_resolved - t_wins
    t_wr        = f"{t_wins/t_resolved*100:.1f}%" if t_resolved else "n/a"

    lines = [
        f"",
        f"--- CYCLE {cycle_num} | {ts} [interval={interval}s] ---",
        f"[POSITIONS] {open_pos} open position(s) | bankroll ${bankroll:.2f}",
        f"",
        f"{'='*50}",
        f"TRADING SESSION STATS",
        f"{'='*50}",
        f"Today P&L:      {_fmt_pnl(today_pnl)}  "
        f"({today_wins}W / {today_loss}L = {today_wr} win rate, {today_n} resolved today)",
        f"Total logged:   {total_n} trades  (YES: {total_yes} | NO: {total_no})  "
        f"avg edge: {avg_edge*100:.1f}%",
        f"All-time W/L:   {t_wins}W / {t_loss}L = {t_wr}  ({t_resolved} resolved)",
        f"{'='*50}",
    ]
    logging.info("\n".join(lines))


def _fmt_breakdown(bd: dict) -> str:
    """Compact one-token-per-verdict summary, e.g. 'placed:2 dry:3 thin:1'.

    Skips nested-dict entries (e.g. 'kalshi_rej', 'polymarket_rej' added
    for audit H1) — those are persisted to scan_log.breakdown_json for
    later analysis but would clutter the cycle-footer line.
    """
    if not bd:
        return "-"
    # Shorten noisy skip reasons; keep placed/dry-run prominent.
    order = ["placed", "dry-run"]
    parts = []
    for k in order:
        if k in bd and not isinstance(bd[k], dict):
            parts.append(f"{k.replace('-run', '')}:{bd[k]}")
    for k, v in bd.items():
        if k in order or k in ("halted", "reasons"):
            continue
        if isinstance(v, dict):
            continue  # H1: nested rejection counters skip the footer line
        # Collapse 'skipped:THIN_MARKET' etc. — the 'skipped' prefix is implicit.
        short = k.split(":", 1)[-1].lower()[:8] if k != "skipped" else "skip"
        parts.append(f"{short}:{v}")
    return " ".join(parts)


def _log_cycle_footer(cycle_num: int, bankroll: float, summary: dict,
                      sleep_s: float) -> None:
    """Compact per-cycle status block. Verbose lines (SCAN/STRATEGY/EXIT
    detail) are at DEBUG and still in the file log; this is the stdout
    headline. Cycle-failure case is handled separately by the caller."""
    global _session_cost_usd
    cost = float(summary.get("claude_cost_usd", 0.0))
    _session_cost_usd += cost

    bd = summary.get("breakdown", {}) or {}
    halted = bool(bd.get("halted"))

    entered = int(summary.get("trades_placed", 0))
    exited = int(summary.get("trades_exited", 0))
    blocked = int(summary.get("trades_blocked", 0))
    failed = int(summary.get("trades_failed", 0))
    insuff_bal = int(summary.get("insufficient_balance_count", 0))
    open_pos = storage.get_cycle_stats().get("open_positions", 0)
    today_pnl = storage.get_cycle_stats().get("today_pnl", 0.0)
    exposure = float(summary.get("exposure", 0.0))
    ts = datetime.now().strftime("%H:%M:%S")

    lines = [
        "",
        f"─ C{cycle_num}  {ts} ─────────────────────",
    ]
    if halted:
        lines.append(f"  HALTED: {'; '.join(bd.get('reasons', []))}")
    else:
        lines.append(
            f"  Entered: {entered}   Exited: {exited}   Open: {open_pos}"
        )
        lines.append(f"  Blocked: {blocked}   Failed: {failed}")
        if insuff_bal:
            lines.append(
                f"  ⚠ Kalshi: insufficient_balance ×{insuff_bal} "
                "— check demo UI for lockout"
            )
    lines.append(
        f"  Bankroll ${bankroll:,.2f}   Day {_fmt_pnl(today_pnl)}   "
        f"Exp ${exposure:,.0f}"
    )
    lines.append(f"  Running… next cycle in {sleep_s:.0f}s")
    logging.info("\n".join(lines))


def _send_hourly_notification(bankroll: float) -> None:
    global _last_hourly_ts
    stats = storage.get_cycle_stats()
    exposure = risk.get_total_exposure()
    last_resolved = storage.get_last_resolved(3)
    # Realized P&L from the trades table, NOT (bankroll - starting). Manual
    # venue top-ups (e.g. demo fake-funds reload) inflate `bankroll` without
    # being profit — using the DB-sourced sum keeps the headline honest.
    total_pnl = stats.get("total_pnl", 0.0)
    total_wins = stats.get("total_wins", 0)
    total_resolved = stats.get("total_resolved", 0)
    telegram_notify.notify_hourly_status(
        bankroll=bankroll,
        starting_bankroll=STARTING_BANKROLL,
        today_pnl=stats.get("today_pnl", 0.0),
        total_pnl=total_pnl,
        total_wins=total_wins,
        total_losses=total_resolved - total_wins,
        open_positions=stats.get("open_positions", 0),
        exposure=exposure,
        last_resolved=last_resolved,
        live=LIVE_TRADING_ENABLED,
    )
    _last_hourly_ts = time.time()


def _bootstrap_calibration() -> None:
    if os.path.exists(CALIBRATION_PKL):
        return
    if not os.path.exists(V1_DB_PATH):
        logging.info(
            "[CALIB] v1 trades.db not at %s — running with identity calibration",
            V1_DB_PATH,
        )
        return
    try:
        stats = calibration.fit_from_v1_history(V1_DB_PATH, CALIBRATION_PKL)
        logging.info(
            "[CALIB] bootstrap n=%d brier %.4f -> %.4f shrinkage=%.3f",
            stats["n_samples"],
            stats["brier_before"],
            stats["brier_after"],
            stats["shrinkage_factor"],
        )
    except Exception as e:
        logging.warning("[CALIB] bootstrap failed: %s — running identity", e)


def _snapshot_kalshi_series() -> None:
    """Write data/kalshi_series.json with the current discovery state.
    Cheap call — discover_weather_series() is cached for 30 min, so this
    is mostly a copy from memory to disk."""
    try:
        mapped, unmapped = kalshi_client.discover_weather_series()
    except Exception as e:
        logging.warning("[KALSHI] series snapshot failed: %s", e)
        return
    snapshot = {
        "fetched_at": datetime.now().isoformat(),
        "mapped_count": len(mapped),
        "unmapped_count": len(unmapped),
        "mapped": [{"ticker": t, "city": c} for t, c in mapped],
        "unmapped": unmapped,
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _KALSHI_SERIES_SNAPSHOT_FILE + ".tmp"
        with open(tmp, "w") as f:
            import json as _json
            _json.dump(snapshot, f, indent=2, default=str)
        os.replace(tmp, _KALSHI_SERIES_SNAPSHOT_FILE)
    except Exception as e:
        logging.warning("[KALSHI] series snapshot write failed: %s", e)


def _detect_cross_venue_arbs(kalshi_markets: list[dict],
                              polymarket_markets: list[dict]) -> list[dict]:
    """Run cross-venue arb detection and persist a snapshot for the dashboard.

    Phase 2: detection only. Polymarket execution is not built, so even a
    real arb cannot be acted on automatically. The user can act manually
    from the /cross dashboard page if a high-edge opportunity surfaces.
    """
    try:
        opps = cross_venue.detect_cross_venue_arbs(
            kalshi_markets, polymarket_markets, _POLYMARKET
        )
    except Exception as e:
        logging.warning("[CROSS] detection failed: %s", e)
        opps = []

    snapshot = {
        "fetched_at": datetime.now().isoformat(),
        "count": len(opps),
        "opportunities": opps,
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _CROSS_VENUE_SNAPSHOT_FILE + ".tmp"
        with open(tmp, "w") as f:
            import json as _json
            _json.dump(snapshot, f, indent=2, default=str)
        os.replace(tmp, _CROSS_VENUE_SNAPSHOT_FILE)
    except Exception as e:
        logging.warning("[CROSS] snapshot write failed: %s", e)
    return opps


def _ingest_polymarket() -> list[dict]:
    """Phase-1 read-only Polymarket pull. Writes a snapshot for the dashboard
    and returns the canonicalized markets. Strategy does NOT score these in
    phase 1 — they're observed only.

    Failures are non-fatal: a network error or schema drift returns [] and
    logs a warning. The Kalshi side of the cycle is unaffected.
    """
    try:
        markets = _POLYMARKET.list_markets()
    except Exception as e:
        logging.warning("[POLYMARKET] ingest failed: %s", e)
        markets = []

    snapshot = {
        "fetched_at": datetime.now().isoformat(),
        "count": len(markets),
        "markets": [
            {
                "venue": m.get("venue"),
                "market_id": m.get("market_id"),
                "city": m.get("city"),
                "question": m.get("question") or m.get("title"),
                "resolution_source": m.get("resolution_source"),
                "comparator": m.get("comparator"),
                "threshold": m.get("threshold"),
                "range_low": m.get("range_low"),
                "range_high": m.get("range_high"),
                "yes_price": m.get("yes_ask_dollars"),
                "no_price": m.get("no_ask_dollars"),
                "close_time": m.get("close_time"),
            }
            for m in markets
        ],
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _POLYMARKET_SNAPSHOT_FILE + ".tmp"
        with open(tmp, "w") as f:
            import json as _json
            _json.dump(snapshot, f, indent=2, default=str)
        os.replace(tmp, _POLYMARKET_SNAPSHOT_FILE)
    except Exception as e:
        logging.warning("[POLYMARKET] snapshot write failed: %s", e)
    return markets


def _resolve_pending_paper_orders() -> None:
    """Resolve any pending paper maker orders. Cheap — only re-fetches books
    for markets with pending orders. Failures are non-fatal: a logging
    warning, the rest of the cycle proceeds.

    Extracted from run_cycle for readability (2026-05-09 audit) — the body
    is mechanical wrapping around maker_sim.resolve_pending_orders, with
    no shared state with the rest of the cycle."""
    try:
        ms = maker_sim.resolve_pending_orders()
        if ms["filled"] or ms["expired"]:
            logging.debug(
                "[MAKER_SIM] checked=%d filled=%d expired=%d still_pending=%d",
                ms["checked"], ms["filled"], ms["expired"], ms["still_pending"],
            )
    except Exception as e:
        logging.warning("[MAKER_SIM] resolution failed: %s", e)


def _run_exit_pass(markets: list[dict]) -> dict:
    """Check open positions against the exit rule (audit E6/E7) and sell
    any that meet the criteria. Wired in 2026-05-07 — should_exit_position
    was previously dead code, so every position rode to settlement
    regardless of forecast updates or take-profit opportunities.

    Forecast-driven by design (per user 2026-05-07): we entered based on
    the model, we exit based on the model. `markets` carries the fresh
    Kalshi market list so executor recomputes cal_p per held position
    from the same forecast pipeline that scored entry. Take-profit
    (bid-aware) is the second rule. 30-min hold cooldown applies to all.

    Extracted from run_cycle (2026-05-09 audit) — body is a try/except
    wrapper around executor.process_exits with no shared cycle state."""
    try:
        import executor as _executor_mod
        exits = _executor_mod.process_exits(markets=markets, stop=_STOP)
        if exits["checked"]:
            logging.debug(
                "[EXIT] checked=%d full=%d partial=%d no_fill=%d errors=%d %s",
                exits["checked"], exits["exited_full"], exits["exited_partial"],
                exits["no_fill"], exits["errors"],
                ("by_reason=" + str(exits["by_reason"])) if exits["by_reason"] else "",
            )
        return exits
    except Exception as e:
        logging.warning("[EXIT] pass failed: %s", e)
        return {"checked": 0, "exited_full": 0, "exited_partial": 0,
                "no_fill": 0, "errors": 1, "by_reason": {}}


def _refresh_bankroll_from_kalshi() -> None:
    """Pull live balance from Kalshi, subtract resting-order reservations,
    and persist to performance.json.

    Why subtract resting orders: /portfolio/balance returns the cash field
    without netting out the cash reserved against open (resting) limit
    orders. A maker order placed in a prior cycle stays resting for up to
    MAKER_REST_SECONDS, holding cash that is no longer deployable. If we
    don't subtract it, the cash snapshot overstates available funds and
    the next cycle's first cash_ok check passes a trade that Kalshi then
    rejects with insufficient_balance (observed 2026-05-03 23:44).
    """
    bal = kalshi_client.get_portfolio_balance()
    if not bal:
        logging.warning("[MAIN] could not fetch Kalshi balance — using stored")
        return
    bankroll_usd = bal["balance_cents"] / 100.0
    cash_cents = bal["cash_cents"]
    try:
        reserved_cents = kalshi_client.get_resting_buy_orders_cost_cents()
    except Exception as e:
        logging.warning("[MAIN] could not fetch resting-order reservations: %s", e)
        reserved_cents = 0
    available_cash_usd = max(0.0, (cash_cents - reserved_cents) / 100.0)
    # The cycle-footer line already surfaces bankroll and exposure; this
    # detailed version stays at DEBUG (audit H5). The resting-order
    # subtraction is the surprising bit, so we INFO-log it iff non-zero.
    log_level = logging.INFO if reserved_cents > 0 else logging.DEBUG
    logging.log(
        log_level,
        "[BALANCE] kalshi_balance_cents=%d cash_cents=%d portfolio_value_cents=%d "
        "resting_reserved_cents=%d → snapshot bankroll=$%.2f cash=$%.2f",
        bal.get("balance_cents", 0), cash_cents, bal.get("portfolio_value_cents", 0),
        reserved_cents, bankroll_usd, available_cash_usd,
    )
    risk.write_bankroll_snapshot(bankroll_usd, available_cash_usd)


def _process_opportunity(opp: dict, bankroll: float) -> str:
    """Run full pre-trade stack; return one of placed/dry-run/paper/skipped:<reason>.

    Routing by venue:
      - kalshi: existing risk gates + executor (live or dry-run by
        LIVE_TRADING_ENABLED).
      - polymarket: paper sim only (no wallet yet). Trades persist with
        paper_trade=1 so reconcile can settle them and the per-venue
        calibration scaffold has data to fit on later. Skips Kalshi-
        specific risk gates that read live Kalshi balance.
    """
    size = float(opp.get("recommended_size", 0.0))
    action = opp.get("action", "BUY YES")
    venue = opp.get("venue", "kalshi")

    from config import evaluate_trade as constitution_gate
    # Polymarket paper trades are sized against STARTING_BANKROLL ($100),
    # not the live Kalshi balance. Pass that explicitly so the size cap
    # is computed against the right denominator.
    gate_bankroll = STARTING_BANKROLL if venue == "polymarket" else None
    ok, viols = constitution_gate(opp, bankroll=gate_bankroll)
    if not ok:
        return "skipped:" + "|".join(viols)

    if venue == "polymarket":
        # Polymarket paper path: skip Kalshi-specific gates (portfolio kelly,
        # cash, cluster) — those aggregate against the live Kalshi bankroll.
        # Constitution gate (size cap, edge floor, thin market) above is
        # enough sanity for paper. The per-trade size cap from STARTING_BANKROLL
        # already keeps individual paper trades small.
        #
        # Phase 3b: default to maker mode. Polymarket has zero fees both ways
        # so maker dominates taker if patience is OK. The order persists as
        # a paper_orders row; maker_sim resolves it on subsequent cycles.
        import paper_executor
        fill = paper_executor.execute_paper_opportunity(
            opp, _POLYMARKET, mode="maker",
        )
        if fill.get("filled"):
            # Currently only triggers if mode was explicitly 'taker'.
            opp_with_paper_flag = {**opp, "paper_trade": 1}
            trade_id = storage.log_trade(opp_with_paper_flag, fill)
            return f"paper:{trade_id}:{fill.get('mode')}"
        if "pending" in (fill.get("mode") or ""):
            # Maker order successfully posted, waiting for fill.
            return f"paper:pending:{fill.get('order_id')}"
        return f"skipped:paper_post_failed:{fill.get('notes', '')}"

    # Kalshi path (default)
    pk_ok, pk_reason = risk.portfolio_kelly_ok(size, action)
    if not pk_ok:
        return f"skipped:{pk_reason}"

    cl_ok, cl_reason = risk.settlement_cluster_ok(opp, size)
    if not cl_ok:
        return f"skipped:{cl_reason}"

    cash_ok, cash_reason = risk.cash_ok(size)
    if not cash_ok:
        return f"skipped:{cash_reason}"

    if not LIVE_TRADING_ENABLED:
        # Dry-run opportunities are hypotheticals, not positions. Do NOT write
        # them to the trades table — that would make the exposure cache think
        # they are open positions and trip the portfolio-Kelly halt on the
        # next cycle. The cycle summary (scan_log) already captures the count.
        return "dry-run"

    import executor
    fill = executor.execute_opportunity(opp, _STOP)
    if fill.get("filled"):
        trade_id = storage.log_trade(opp, fill)
        telegram_notify.notify_trade(opp, fill)
        return f"placed:{trade_id}:{fill.get('mode')}"
    return f"skipped:fill_failed:{fill.get('notes', '')}"


def _process_arb_group(legs: list[dict], bankroll: float) -> str:
    """Atomic arb execution: gate on group totals, fill all legs, rollback on partial.

    Each leg is a separate Kalshi market; the no-loss guarantee only holds when
    every leg fills together. Per-leg routing through execute_opportunity() is
    incorrect because _recompute_edge sees calibrated_p == yes_ask (by arb
    construction) and trips MIN_EDGE every time.
    """
    if not legs:
        return "skipped:empty_arb_group"

    from config import evaluate_trade as constitution_gate

    # Per-leg constitution (size cap, thin-market guard, MIN_EDGE_ARB floor).
    for leg in legs:
        ok, viols = constitution_gate(leg)
        if not ok:
            return f"skipped:arb_leg_{leg.get('ticker','?')}:" + "|".join(viols)

    total_size = sum(float(leg.get("recommended_size", 0.0)) for leg in legs)

    # Group-level risk gates: a 3-leg arb is ONE position, not three.
    pk_ok, pk_reason = risk.portfolio_kelly_ok(total_size, "BUY YES")
    if not pk_ok:
        return f"skipped:{pk_reason}"

    cl_ok, cl_reason = risk.settlement_cluster_ok(legs[0], total_size)
    if not cl_ok:
        return f"skipped:{cl_reason}"

    cash_ok, cash_reason = risk.cash_ok(total_size)
    if not cash_ok:
        return f"skipped:{cash_reason}"

    if not LIVE_TRADING_ENABLED:
        return "dry-run"

    import executor
    leg_results = executor.arb_execute_group(legs)

    placed_count = 0
    rolled_back = False
    stranded_count = 0
    for leg, result in zip(legs, leg_results):
        notes = result.get("notes", "")
        if notes == "arb_rolled_back":
            rolled_back = True
        # Stranded legs: rollback couldn't sell them. Persist to trades.db
        # so risk + reconcile + dashboard track the position; the bot needs
        # to know it's holding it. Count separately from successful fills.
        if notes.startswith("arb_stranded:"):
            stranded_count += 1
            # Mark the trade row with a 'stranded:' notes prefix so it's
            # visible in dashboards but doesn't pollute calibration data.
            stranded_leg = dict(leg)
            stranded_leg["notes"] = notes
            storage.log_trade(stranded_leg, result)
            continue
        if result.get("filled"):
            storage.log_trade(leg, result)
            telegram_notify.notify_trade(leg, result)
            placed_count += 1

    if stranded_count:
        # Even one stranded leg breaks the no-loss guarantee. Treat the whole
        # group as failed for accounting; the stranded rows are persisted so
        # we don't lose track, but we shouldn't claim the arb succeeded.
        return f"skipped:fill_failed:arb_partial_with_stranded:{stranded_count}"
    if rolled_back:
        return f"skipped:fill_failed:arb_rolled_back"
    if placed_count == len(legs):
        return f"placed:arb:{legs[0].get('arb_id','')}:{placed_count}_legs"
    if placed_count > 0:
        return f"partial:arb:{placed_count}/{len(legs)}_legs"
    return "skipped:fill_failed:arb_no_fill"


def run_cycle() -> dict:
    cycle_at = datetime.now().isoformat()
    risk.invalidate_exposure_cache()

    _refresh_bankroll_from_kalshi()
    risk.reset_cycle_deployment()  # zero the in-cycle cash debit counter

    # Reconcile BEFORE the can_trade check. Settlement is read-only
    # bookkeeping — the halt rationale (don't take new positions) doesn't
    # apply. Skipping it during halt is self-perpetuating: unsettled
    # trades keep "open exposure" inflated, which keeps the exposure cap
    # tripped, which keeps reconcile from running.
    try:
        import reconcile
        rec = reconcile.reconcile_settled_trades()
        if rec["settled"]:
            logging.debug(
                "[RECONCILE] settled %d trade(s) this cycle (checked=%d, still_open=%d, errors=%d)",
                rec["settled"], rec["checked"], rec["still_open"], rec["api_errors"],
            )
    except Exception as e:
        logging.warning("[RECONCILE] failed: %s", e)
    risk.invalidate_exposure_cache()

    ok, reasons = risk.can_trade()
    if not ok:
        logging.warning("[RISK] halted: %s", "; ".join(reasons))
        telegram_notify.notify_halt("; ".join(reasons))
        return {
            "markets_scanned": 0, "opportunities": 0, "trades_placed": 0,
            "claude_cost_usd": 0.0, "exposure": risk.get_total_exposure(),
            "breakdown": {"halted": True, "reasons": reasons},
        }
    telegram_notify.notify_resume()

    bankroll, _age = risk.get_active_bankroll()

    # Arbs first — mechanical edge, runs before the weather model.
    markets = kalshi_client.get_all_weather_markets()
    logging.debug("[SCAN] fetched %d Kalshi weather markets", len(markets))
    _snapshot_kalshi_series()

    # Polymarket ingest + cross-venue arb detection are skipped while the
    # Polymarket strategy is paused (see POLYMARKET_STRATEGY_ENABLED below).
    # Cross-venue arbs were detection-only — the bot never executed the
    # Kalshi leg — so skipping the scan loses no actionable signal, just
    # the dashboard's /polymarket and /cross pages going stale.
    polymarket_markets: list[dict] = []
    cross_arbs: list[dict] = []

    # Phase 3b: resolve pending paper maker orders FIRST so any newly
    # filled trades land in this cycle's stats.
    _resolve_pending_paper_orders()

    # Exit pass: forecast-driven exits + bid-aware take-profit on held
    # positions. Both helpers are non-fatal on error.
    exits = _run_exit_pass(markets)

    arb_opps = strategy_arb.scan_arbitrage(markets, bankroll)
    weather_opps = strategy.find_opportunities(markets, bankroll, venue="kalshi")
    _kalshi_rej = strategy.get_last_rejections()[1]

    # Polymarket scoring (paper-only). Uses STARTING_BANKROLL as a fixed
    # paper bankroll — phase 3b will switch to a dynamic paper bankroll
    # that tracks paper P&L. The MAX_SINGLE_BET_PCT cap on $100 means
    # individual paper trades stay small even if Kelly suggests otherwise.
    #
    # 2026-05-05 checkpoint: Polymarket strategy is PAUSED while we
    # investigate (a) the no-fitted-calibration-curve issue, (b) why 10
    # 1°F-bin trades reached the executor despite the gate, and (c) the
    # 9/10-wrong direction signal. Read-only ingest and cross-venue arb
    # detection (above) remain on. Re-enable by setting
    # POLYMARKET_STRATEGY_ENABLED = True.
    POLYMARKET_STRATEGY_ENABLED = False
    if POLYMARKET_STRATEGY_ENABLED:
        polymarket_opps = strategy.find_opportunities(
            polymarket_markets, STARTING_BANKROLL, venue="polymarket"
        )
        _poly_rej = strategy.get_last_rejections()[1]
    else:
        polymarket_opps = []
        _poly_rej = {"paused_at_checkpoint_2026_05_05": 1}

    # One-line per-cycle telemetry — surfaces why find_opportunities returned
    # 0 (or close to it). Distinguishes our directional filters (yes_filter,
    # no_filter) from upstream gates (bin_gate, no_forecast) and the generic
    # min_edge floor. Empty/zero counts are dropped to keep the line short.
    def _rej_str(d: dict[str, int]) -> str:
        return " ".join(f"{k}={v}" for k, v in sorted(d.items()) if v) or "none"
    logging.debug("[STRATEGY:rej] kalshi: %s | polymarket: %s",
                 _rej_str(_kalshi_rej), _rej_str(_poly_rej))
    if polymarket_opps:
        logging.debug("[STRATEGY] polymarket scored %d opportunit%s",
                     len(polymarket_opps),
                     "y" if len(polymarket_opps) == 1 else "ies")

    # ── Arb opps must be filled atomically as a group (audit B4 rollback
    # depends on it). Group by arb_id and skip groups whose legs overlap an
    # already-held position — partial coverage breaks the no-loss guarantee.
    held_tickers = {p["ticker"] for p in storage.load_open_positions()}
    arb_groups: dict[str, list[dict]] = {}
    arb_skipped_held: set[str] = set()
    for opp in arb_opps:
        aid = opp.get("arb_id")
        if not aid:
            continue
        if opp.get("ticker") in held_tickers:
            arb_skipped_held.add(aid)
            continue
        arb_groups.setdefault(aid, []).append(opp)
    for aid in arb_skipped_held:
        arb_groups.pop(aid, None)

    placed = 0
    blocked = 0
    failed = 0
    insufficient_balance_count = 0
    breakdown: dict[str, int] = {}

    def _record(opp: dict, verdict: str) -> None:
        nonlocal placed, blocked, failed, insufficient_balance_count
        key = verdict.split(":")[0]
        breakdown[key] = breakdown.get(key, 0) + 1
        logging.debug(
            "  -> %s %s edge=%.3f size=$%.2f [%s]",
            opp.get("action"), opp.get("ticker"),
            float(opp.get("edge", 0.0)), float(opp.get("recommended_size", 0.0)),
            verdict,
        )
        if key == "placed":
            placed += 1
        elif key == "skipped" and "fill_failed" in verdict:
            failed += 1
        elif key == "skipped":
            blocked += 1
        if "insufficient_balance" in verdict.lower():
            insufficient_balance_count += 1

    # Arb groups first — atomic, share one verdict across all legs.
    for aid, legs in arb_groups.items():
        verdict = _process_arb_group(legs, bankroll)
        for leg in legs:
            _record(leg, verdict)

    if arb_skipped_held:
        logging.debug(
            "[ARB] skipped %d group(s) overlapping held positions: %s",
            len(arb_skipped_held), ", ".join(sorted(arb_skipped_held)),
        )

    # Weather opps: existing per-opp pipeline (Kalshi).
    for opp in weather_opps:
        verdict = _process_opportunity(opp, bankroll)
        _record(opp, verdict)

    # Polymarket opps: same pipeline, routes to paper_executor inside
    # _process_opportunity based on opp.venue.
    for opp in polymarket_opps:
        verdict = _process_opportunity(opp, STARTING_BANKROLL)
        _record(opp, verdict)


    # Post-cycle bookkeeping. (Reconcile now runs at the top of run_cycle
    # so it isn't skipped when risk halts the bot.)
    risk.invalidate_exposure_cache()
    exposure = risk.get_total_exposure()
    realized_pnl = risk.realized_pnl_total()
    peak = risk.peak_pnl_update(realized_pnl)
    today_pnl = risk._todays_pnl()
    storage.write_snapshot(bankroll, exposure, peak, today_pnl)

    # Audit H1: persist per-cycle rejection counts (yes_filter, no_filter,
    # bin_gate, etc.) into scan_log.breakdown_json so we can measure the
    # filters' false-positive rate over time. The cycle-footer line skips
    # these nested dicts; analysis reads them with:
    #   SELECT json_extract(breakdown_json, '$.kalshi_rej') FROM scan_log
    breakdown["kalshi_rej"] = dict(_kalshi_rej)
    breakdown["polymarket_rej"] = dict(_poly_rej)

    summary = {
        "cycle_at": cycle_at,
        "markets_scanned": len(markets) + len(polymarket_markets),
        "opportunities": (
            sum(len(legs) for legs in arb_groups.values())
            + len(weather_opps)
            + len(polymarket_opps)
        ),
        "trades_placed": placed,
        "trades_blocked": blocked,
        "trades_failed": failed + int(exits.get("errors", 0)),
        "trades_exited": int(exits.get("exited_full", 0))
                         + int(exits.get("exited_partial", 0)),
        "insufficient_balance_count": insufficient_balance_count,
        "claude_cost_usd": strategy.pop_claude_cost(),
        "breakdown": breakdown,
        "exposure": exposure,
        "venues": {
            "kalshi": len(markets),
            "polymarket": len(polymarket_markets),
        },
        "polymarket_opps_scored": len(polymarket_opps),
        "cross_venue_arbs": len(cross_arbs),
    }
    storage.log_scan(summary)
    return summary


def main() -> None:
    _install_logging()
    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    logging.info("[MAIN] kalshi_bot_2.0 boot. LIVE_TRADING_ENABLED=%s",
                 LIVE_TRADING_ENABLED)

    storage.init_db()

    if not kalshi_client.verify_api_connection():
        logging.error(
            "[MAIN] Kalshi API connection failed — check .env and KALSHI_API_URL"
        )
        # Don't exit — dry-run may still be valuable for local testing.

    if _POLYMARKET.verify_connection():
        logging.info("[MAIN] Polymarket Gamma API reachable (read-only ingest)")
    else:
        logging.warning(
            "[MAIN] Polymarket Gamma API unreachable — phase-1 ingest will be empty"
        )

    _bootstrap_calibration()

    # Seed performance.json if missing so staleness checks have something to read.
    risk.write_bankroll_snapshot(STARTING_BANKROLL)

    # Start the web dashboard as a daemon thread. It is read-only against the
    # SQLite DB and performance.json — no shared state with the bot loop, so a
    # dashboard bug cannot take the bot down. Non-fatal if Flask isn't installed.
    try:
        import dashboard
        dashboard.start_in_thread(host="127.0.0.1", port=8082)
    except ImportError:
        logging.warning(
            "[MAIN] Flask not installed — dashboard disabled. "
            "Run: pip install flask"
        )
    except Exception as e:
        logging.warning("[MAIN] dashboard failed to start (non-fatal): %s", e)

    # Start the forecast-health background monitor. Computes 14-day rolling
    # GFS-vs-ASOS MAE / bias / RMSE per city on startup and every 24h; writes
    # data/forecast_health.json, which strategy.py reads to gate city trading.
    try:
        forecast_health.start_background_refresh()
    except Exception as e:
        logging.warning("[MAIN] forecast-health monitor failed to start (non-fatal): %s", e)

    while not _STOP.is_set():
        global _cycle_num
        _cycle_num += 1
        start = time.time()

        bankroll, _ = risk.get_active_bankroll()
        if _VERBOSE and (_cycle_num == 1 or _cycle_num % _BANNER_EVERY == 0):
            _log_cycle_header(_cycle_num, bankroll, SCAN_INTERVAL_SECONDS)

        cycle_error: str | None = None
        try:
            summary = run_cycle()
        except Exception as e:
            logging.exception("[MAIN] cycle failed: %s", e)
            summary = {}
            cycle_error = f"{type(e).__name__}: {e}"

        elapsed = time.time() - start
        sleep_s = max(1.0, SCAN_INTERVAL_SECONDS - elapsed)
        bankroll_after, _ = risk.get_active_bankroll()
        if cycle_error:
            ts = datetime.now().strftime("%H:%M:%S")
            logging.info(
                "\n─ C%d  %s ─────────────────────\n"
                "  CYCLE FAILED: %s\n"
                "  Retrying in %.0fs",
                _cycle_num, ts, cycle_error, sleep_s,
            )
        else:
            _log_cycle_footer(_cycle_num, bankroll_after, summary, sleep_s)

        if time.time() - _last_hourly_ts >= 3600:
            try:
                _send_hourly_notification(bankroll_after)
            except Exception as e:
                logging.warning("[TG] hourly notification failed: %s", e)

        _STOP.wait(timeout=sleep_s)

    logging.info("[MAIN] shutdown complete.")


if __name__ == "__main__":
    main()
