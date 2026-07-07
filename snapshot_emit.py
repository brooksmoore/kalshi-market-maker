"""Emit the umbrella canonical snapshot for kalshi_bot_2.0 (read-only)."""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from umbrella_core.emit import (
    AccountInfo,
    CapitalInfo,
    ComputeInfo,
    HealthInfo,
    IdentityInfo,
    LifecycleInfo,
    PositionInfo,
    Snapshot,
    TimingInfo,
    snapshot_to_dict,
    write_snapshot_atomic,
)
from umbrella_core.snapshot import validate_snapshot

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import config  # noqa: E402

log = logging.getLogger(__name__)

_STATE_LOOP_PATH = _ROOT / "STATE_LOOP.md"
_LIVENESS_SEC = 3600  # hourly scan cadence when running; stale while stopped


def _parse_state_loop() -> dict[str, str]:
    if not _STATE_LOOP_PATH.exists():
        return {}
    text = _STATE_LOOP_PATH.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for key in ("gate_status", "n_oos", "net_ev_oos", "last_run"):
        m = re.search(rf"^\*\*{key}\*\*:\s*(.+)$", text, re.MULTILINE)
        if m:
            out[key] = m.group(1).strip()
    return out


def _last_cycle_at() -> str | None:
    db = Path(config.DB_FILE)
    if not db.exists():
        return None
    try:
        with sqlite3.connect(db, timeout=5) as conn:
            row = conn.execute(
                "SELECT cycle_at FROM scan_log ORDER BY cycle_at DESC LIMIT 1"
            ).fetchone()
            return str(row[0]) if row and row[0] else None
    except sqlite3.Error:
        return None


def _last_fill_at() -> str | None:
    db = Path(config.DB_FILE)
    if not db.exists():
        return None
    try:
        with sqlite3.connect(db, timeout=5) as conn:
            row = conn.execute(
                "SELECT MAX(opened_at) FROM trades"
            ).fetchone()
            return str(row[0]) if row and row[0] else None
    except sqlite3.Error:
        return None


def _load_positions() -> list[PositionInfo]:
    import storage  # noqa: E402

    positions: list[PositionInfo] = []
    try:
        rows = storage.load_open_positions()
    except (sqlite3.Error, OSError):
        return []
    for row in rows:
        ticker = str(row.get("ticker") or "")
        if not ticker:
            continue
        contracts = int(row.get("contracts") or 0)
        if contracts <= 0:
            continue
        entry = float(row.get("entry_price") or 0.0)
        size_usd = float(row.get("size_usd") or 0.0)
        mark = entry if entry > 0 else None
        positions.append(
            PositionInfo(
                symbol=ticker,
                qty=float(contracts),
                avg_cost=entry if entry > 0 else None,
                mark=mark,
                market_value=size_usd if size_usd > 0 else None,
                unrealized_pnl=None,
                weight=None,
            )
        )
    return positions


def _capital_block() -> tuple[CapitalInfo, float]:
    import storage  # noqa: E402

    try:
        stats = storage.get_cycle_stats()
    except (sqlite3.Error, OSError):
        stats = {}
    starting = float(config.STARTING_BANKROLL)
    total_pnl = stats.get("total_pnl")
    own_nav = starting + float(total_pnl or 0.0)
    positions = _load_positions()
    invested = sum(p.market_value or 0.0 for p in positions)
    cash = max(0.0, own_nav - invested)
    return (
        CapitalInfo(
            base_currency="USD",
            own_nav=round(own_nav, 2),
            cash=round(cash, 2),
            invested=round(invested, 2),
            budget_allocation=starting,
            day_pnl=float(stats["today_pnl"]) if stats.get("today_pnl") is not None else None,
            total_pnl=float(total_pnl) if total_pnl is not None else None,
        ),
        own_nav,
    )


def build_kalshi_snapshot(
    *,
    killed: bool = True,
    cycle_at: datetime | None = None,
) -> Snapshot:
    """Map kalshi_bot_2.0 persisted state to the umbrella canonical snapshot."""
    now = cycle_at or datetime.now(UTC)
    loop = _parse_state_loop()
    capital, own_nav = _capital_block()
    positions = _load_positions()
    if own_nav > 0:
        positions = [
            PositionInfo(
                symbol=p.symbol,
                qty=p.qty,
                avg_cost=p.avg_cost,
                mark=p.mark,
                market_value=p.market_value,
                unrealized_pnl=p.unrealized_pnl,
                weight=(p.market_value / own_nav) if p.market_value is not None else None,
            )
            for p in positions
        ]

    last_cycle = _last_cycle_at() or loop.get("last_run") or now.isoformat()
    warnings: list[str] = [
        "bot stopped — FLOORED weather-bracket experiment (paper demo)",
    ]
    if loop.get("gate_status") == "FLOORED":
        warnings.insert(
            0,
            f"loop gate FLOORED: n_oos={loop.get('n_oos', '?')}, "
            f"net_ev_oos={loop.get('net_ev_oos', '?')}/contract",
        )

    live_gate = "armed" if config.LIVE_TRADING_ENABLED else "disarmed"
    overall: str = "down" if killed else ("degraded" if positions else "ok")

    return Snapshot(
        schema_version="1.0",
        identity=IdentityInfo(
            bot_id="kalshi_2",
            display_name="Kalshi Weather Bracket Bot 2.0",
            membrane="independent",
            account=AccountInfo(broker="kalshi-demo" if "demo" in config.KALSHI_API_URL else "kalshi"),
            asset_classes=["event_contract"],
            strategy="GEFS ensemble + isotonic calibration; LLM veto-only (fails open)",
        ),
        lifecycle=LifecycleInfo(
            stage="paper-validating",
            mode="paper",
            live_gate=live_gate,  # type: ignore[arg-type]
            killed=killed,
            cadence="continuous",
            expected_update_interval_sec=_LIVENESS_SEC,
        ),
        timing=TimingInfo(
            generated_at=now.isoformat(),
            last_cycle_at=last_cycle,
            last_fill_at=_last_fill_at(),
        ),
        capital=capital,
        positions=positions,
        compute=ComputeInfo(
            llm_spend_today_usd=0.0,
            llm_budget_usd=0.0,
            budget_remaining_usd=0.0,
            calls_today=0,
            breaker_tripped=True,
        ),
        health=HealthInfo(
            overall=overall,  # type: ignore[arg-type]
            sources={
                "kalshi": "n/a" if killed else "ok",
                "forecast": "n/a" if killed else "ok",
                "ledger": "ok" if Path(config.DB_FILE).exists() else "n/a",
            },
            warnings=warnings,
        ),
        extra={
            "loop_gate_status": loop.get("gate_status", "FLOORED"),
            "n_oos": loop.get("n_oos"),
            "net_ev_oos": loop.get("net_ev_oos"),
            "decisions_path": str(_ROOT / "data" / "decisions.ndjson"),
            "runner": "stopped",
        },
    )


def emit_kalshi_snapshot(
    out_path: Path,
    *,
    killed: bool = True,
    cycle_at: datetime | None = None,
) -> bool:
    """Validate and atomically write state.json. Keeps prior file on validation failure."""
    snapshot = build_kalshi_snapshot(killed=killed, cycle_at=cycle_at)
    payload = snapshot_to_dict(snapshot)
    errors = validate_snapshot(payload)
    if errors:
        log.error(
            "umbrella snapshot validation failed; keeping prior %s: %s",
            out_path,
            errors,
        )
        return False
    write_snapshot_atomic(out_path, payload)
    log.info("umbrella snapshot written to %s", out_path)
    return True