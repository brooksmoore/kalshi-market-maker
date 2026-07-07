"""Decisions contract wiring — path only while bot is stopped (no live emission)."""

from __future__ import annotations

from pathlib import Path

BOT_ROOT = Path(__file__).resolve().parent
DEFAULT_DECISIONS_PATH = BOT_ROOT / "data" / "decisions.ndjson"


def decisions_path(data_dir: Path | None = None) -> Path:
    """Canonical append-only decisions log for the measurement membrane."""
    return (data_dir or BOT_ROOT / "data") / "decisions.ndjson"