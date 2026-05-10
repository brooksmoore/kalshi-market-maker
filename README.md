# kalshi_bot_2.0

A from-scratch rewrite of the Kalshi weather-market autotrader.

v1 (at `../polymarket-bot/`) ran live and lost 44% of a $100 bankroll over 135
resolved trades. The forensic audit at
`../polymarket-bot/AUDIT_REPORT_2026-04-23.md` identified three root causes:

1. Gaussian + Claude-blended probabilities were systematically overconfident by
   ~20 percentage points.
2. A single NOAA point forecast cannot beat the marginal informed counterparty.
3. Claude LLM blend adds noise and is mis-calibrated.

v2 replaces all three with:

- **Probability:** GEFS 31-member ensemble via Open-Meteo's free
  `/v1/ensemble` endpoint. `P(above X) = fraction of members >= X`. No
  Gaussian, no LLM blend.
- **Calibration:** isotonic regression fit once from v1's resolved-trade
  history, pickled to `data/calibration.pkl`, applied statically before every
  edge calc.
- **Claude:** veto-only filter. One YES/NO call per candidate. Never touches
  the probability number. Fails-open.

## Audit items addressed

| Audit | Where |
| --- | --- |
| M1 true ensemble | `src/forecast.py` |
| M7 exact per-contract fee | `src/config.py::kalshi_trade_fee`, used in strategy + arb |
| M8 Kelly shrinkage | `src/risk.py::kelly_size` reads `calibration.shrinkage_factor()` |
| M9 portfolio Kelly cap | `src/risk.py::portfolio_kelly_ok` |
| M10 settlement cluster cap | `src/risk.py::settlement_cluster_ok` |
| M11 isotonic calibration | `src/calibration.py` |
| E3 live depth >= 2x | `src/executor.py` |
| E4 split across book levels | `src/executor.py` |
| E5 live-refetch edge | `src/executor.py` before maker and taker |
| E6/E7 Bayesian exit | `src/executor.py::should_exit_position` |
| E9 paper-trade filter for calibration | `src/calibration.py` |
| B1 parameterized SQL | `src/storage.py` (zero f-string SQL) |
| B4 arb fee-inclusive + depth + rollback | `src/strategy_arb.py`, `src/executor.py::arb_execute_group` |
| B8/R1 atomic monotonic peak_pnl | `src/risk.py::peak_pnl_update` |
| R5 fail-closed on stale bankroll | `src/risk.py::can_trade` |

## Demo setup

1. Create a Kalshi demo account at https://demo.kalshi.co.
2. Generate an RSA API key in the demo dashboard and download the private key
   (`.pem`).
3. Copy `.env.example` to `.env` and fill in:
   - `KALSHI_API_KEY_ID`
   - `KALSHI_PRIVATE_KEY_PATH` (absolute path to your `.pem`)
   - optional: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
4. Leave `KALSHI_API_URL` as the demo default and `LIVE_TRADING_ENABLED=false`
   for the first cycle.

## Quickstart

```bash
cd kalshi_bot_2.0
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Bootstrap the isotonic calibration from v1's history (optional but recommended)
PYTHONPATH=src python scripts/bootstrap_calibration.py

# Run a cycle of dry-run scans
PYTHONPATH=src python src/main.py

# Inspect state at any time
PYTHONPATH=src python scripts/status.py
```

## Flipping halted -> live

1. Watch a few dry-run cycles in `logs/bot.log`. Confirm the opportunities look
   sane.
2. Edit `.env`: `LIVE_TRADING_ENABLED=true`.
3. Restart `python src/main.py`.

The bot will still be trading the demo endpoint unless you also change
`KALSHI_API_URL` to a production URL.

## Directory layout

```
kalshi_bot_2.0/
  README.md
  requirements.txt
  .env.example
  .gitignore
  src/
    main.py              boot + scan loop, dashboard wiring, signal handling
    config.py            flat config + kalshi_trade_fee + evaluate_trade gate
    venue.py             Venue Protocol + canonical OrderBook / MarketMeta types
    kalshi_client.py     signed HTTP client, orderbook parsing, order placement
    kalshi_venue.py      Venue Protocol adapter for Kalshi (with C2 verify)
    polymarket_client.py PolymarketVenue: read-only ingest + paper-only stubs
    resolution_rules.py  canonical (resolution_source, comparator, threshold)
    forecast.py          Open-Meteo GEFS ensemble + Laplace smoothing + CLI bias
    forecast_health.py   14-day GFS-vs-ASOS skill monitor (city-level alerts)
    calibration.py       isotonic fit + shrinkage_factor (currently identity, see C1)
    strategy.py          weather high-temp scoring (ensemble + edge + Kelly)
    strategy_arb.py      fee-inclusive within-Kalshi arbitrage
    cross_venue.py       Kalshi↔Polymarket arb DETECTION (no execution yet)
    risk.py              exposure cache, Kelly, portfolio/cluster caps, halts
    executor.py          maker-first state machine, arb rollback, exit rule
    paper_executor.py    honest paper fill (taker VWAP / maker post)
    maker_sim.py         resolves pending paper maker orders cycle-to-cycle
    reconcile.py         settle resolved trades against venue oracle
    storage.py           SQLite schema + parameterized insert/query helpers
    dashboard.py         single-file Flask dashboard on :8082 (read-only)
    telegram_notify.py   minimal notifier (no-op when env vars unset)
  tests/
    test_math.py         Kelly, fees, ensemble (Laplace), isotonic, arb, SQL
    test_execution.py    paper fills, maker strict-below, reconcile, exits, C2
  scripts/
    bootstrap_calibration.py    fit isotonic from v1 trade history
    backfill_kalshi_positions.py recover stranded fills into trades.db
    backfill_trades.py          (variant for the trades-only side)
    cli_gap_audit.py            measure CLI vs ASOS bias per city
    ensemble_audit.py           ensemble dispersion sanity checks
    veto_backtest.py            backtest the Claude veto prompt offline
    preflight_audit.py          one-shot diagnostic before a run
    exit_and_reset.py           operational tool: close positions + zero state
    reconcile_trades.py         standalone reconcile entry point
    status.py                   one-shot status print
  data/                  trades.db, performance.json, snapshots, _archive/
  logs/                  bot.log + .1/.2/.3 rotations (20MB each)
```

## Multi-venue (Polymarket)

`venue.py` defines the `Venue` Protocol; both Kalshi and Polymarket implement
it. `cross_venue.py` detects arbitrage across the two venues by canonicalizing
both sides to `(resolution_source, comparator, threshold(s), date)` tuples
(see `resolution_rules.py`). Detection is wired in; execution is paper-only
on Polymarket today (Polygon wallet + EIP-712 signing not yet built).

Polymarket strategy execution is currently PAUSED in `main.run_cycle` —
toggle `POLYMARKET_STRATEGY_ENABLED` to re-enable after fitting a real
per-venue calibration curve.

## Tests

```bash
cd kalshi_bot_2.0
PYTHONPATH=src pytest tests/ -v
```

Last verified: 53 passed, 1 xfailed (planned tripwire).
