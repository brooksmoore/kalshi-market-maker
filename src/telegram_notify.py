"""
telegram_notify.py — minimal Telegram notifier.

No-op when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID aren't set. Keeps the same
three public functions v1 used but drops the dashboard-URL / HTML-report noise.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import requests

_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def _md_to_html(text: str) -> str:
    """Translate the tiny Markdown subset we actually use (`*bold*`) to HTML,
    HTML-escaping everything else. Avoids legacy-Markdown 400s on unescaped
    underscores in identifiers like 'high_temp'.
    """
    import re
    # Escape HTML metacharacters first.
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Convert *bold* → <b>bold</b>. Non-greedy, single-line.
    out = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", out)
    return out


def _send(text: str) -> None:
    if not _BOT_TOKEN or not _CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={
                "chat_id": _CHAT_ID,
                "text": _md_to_html(text),
                "parse_mode": "HTML",
            },
            timeout=5,
        )
    except Exception as e:
        logging.warning("[TG] send failed: %s", e)


def notify_trade(opp: dict, fill_result: dict) -> None:
    # Silenced: per-trade pings were too noisy. Trade activity is rolled into
    # the hourly status (open positions, P&L, last resolved).
    return


_halted: bool = False
_last_halt_ping_ts: float = 0.0
_last_halt_reason: str | None = None
# Re-ping every 6 hours while still halted. Prior behavior was once-per-process
# (on the False→True transition) which silenced the user across long halts and
# across bot restarts that started already-halted; the 2026-05-09 audit caught
# this when a 33% drawdown halt produced no telegram alert because the bot
# was restarted after the initial halt fired.
_HALT_REPING_SECONDS: float = 6 * 60 * 60


def notify_halt(reason: str) -> None:
    """Send a halt ping. First call after a resume always sends. Subsequent
    calls while still halted re-ping every _HALT_REPING_SECONDS so a long
    halt stays visible in the user's chat. Reason changes also re-ping
    (e.g. drawdown halt → exposure halt) so the user sees the new cause.
    """
    import time
    global _halted, _last_halt_ping_ts, _last_halt_reason
    now = time.time()
    if _halted:
        if reason == _last_halt_reason and (now - _last_halt_ping_ts) < _HALT_REPING_SECONDS:
            return  # quiet — already pinged recently for the same reason
    _halted = True
    _last_halt_reason = reason
    _last_halt_ping_ts = now
    _send(f"*[HALT]* {reason}")


def notify_resume() -> None:
    global _halted, _last_halt_reason, _last_halt_ping_ts
    if not _halted:
        return
    _halted = False
    _last_halt_reason = None
    _last_halt_ping_ts = 0.0
    _send("*[RESUMED]* trading resumed")


def is_halted() -> bool:
    """Return whether the notifier currently believes the bot is halted.
    Read by the hourly-status renderer so the digest can surface halt state
    even if the dedicated halt ping was missed (e.g. telegram outage)."""
    return _halted


def last_halt_reason() -> str | None:
    return _last_halt_reason


def notify_daily_summary(summary: dict) -> None:
    lines = ["*[DAILY SUMMARY]*"]
    for k, v in summary.items():
        lines.append(f"{k}: {v}")
    _send("\n".join(lines))


def notify_hourly_status(
    bankroll: float,
    starting_bankroll: float,
    today_pnl: float,
    total_pnl: float,
    total_wins: int,
    total_losses: int,
    open_positions: int,
    exposure: float,
    last_resolved: list[dict],
    live: bool = False,
) -> None:
    def _fmt(v: float) -> str:
        sign = "+" if v >= 0 else ""
        return f"${sign}{v:.2f}"

    # Mode reflects both the env flag and the runtime halt state. A bot
    # running with LIVE_TRADING_ENABLED=true but currently halted by risk
    # checks should not advertise "LIVE" — that was a dashboard-side bug
    # caught in the 2026-05-09 audit (notify_halt fires only on the
    # False→True transition, so an already-halted bot at hourly digest
    # time was silently labeled LIVE).
    if is_halted():
        mode = "HALTED"
    elif live:
        mode = "LIVE"
    else:
        mode = "DRY-RUN"
    now = datetime.now().strftime("%b %d, %I:%M %p")
    pct = (bankroll - starting_bankroll) / starting_bankroll * 100
    total_resolved = total_wins + total_losses
    win_rate = f"{total_wins / total_resolved * 100:.1f}%" if total_resolved else "n/a"

    lines = [
        f"🌡 *Kalshi Weather Bot — {mode}*",
        now,
    ]
    if is_halted():
        reason = last_halt_reason() or "(reason unknown)"
        lines.append(f"⛔ {reason}")
    lines.extend([
        "",
        f"📈 All-time P&L: {_fmt(total_pnl)}",
        f"📅 Today: {_fmt(today_pnl)}",
        f"💰 Bankroll: ${bankroll:.2f}  ({pct:+.1f}% vs start)",
        "",
        f"🎯 Win rate: {win_rate}  ({total_wins}W / {total_losses}L)",
        f"📂 Open positions: {open_positions}  (${exposure:.0f} deployed)",
    ])

    if last_resolved:
        lines.append("")
        lines.append("🕐 Last 3 resolved:")
        for t in last_resolved[:3]:
            is_win = (
                (t["action"] == "BUY YES" and t["outcome"] == "yes")
                or (t["action"] == "BUY NO" and t["outcome"] == "no")
            )
            emoji = "✅" if is_win else "❌"
            pnl_str = _fmt(float(t["profit_loss"]))
            ts = str(t.get("resolved_at", ""))[:16]
            city = t.get("city", "?")
            mtype = t.get("market_type", "?")
            action = t.get("action", "?")
            lines.append(f"  {emoji} {city} {mtype} {action}  {pnl_str}  {ts}")

    _send("\n".join(lines))
