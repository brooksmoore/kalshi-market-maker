#!/usr/bin/env bash
# Start / stop kalshi_bot_2.0 (bot + observers + dashboard) SAFELY.
#
# Usage:
#   ./run.sh start    # stop our own stale procs, then launch fresh
#   ./run.sh stop     # stop only THIS bot's processes
#   ./run.sh status    # show what THIS bot has running
#
# WHY THIS EXISTS (2026-05-24):
# These services were previously launched by typing one-liners into the terminal:
#     nohup python3 src/main.py ...
#     pkill -9 -f "src/main.py"; pkill -9 -f "observer"; pkill -9 -f "dashboard.py"
# Two problems with that:
#   1) Bare `python3` is NOT this project's venv, so flask/numpy were missing and the
#      processes crashed instantly (ModuleNotFoundError). Always launch with venv python.
#   2) `pkill -f "dashboard.py"` / `"observer"` match the FULL command line of EVERY
#      process on the machine. The other bots (pure_arb_bot, Multi_Agent_Asset_
#      Competitive_Bot) also run a dashboard.py / observers, so those bare patterns were
#      killing the OTHER bots too — the real cause of the dashboards "interfering."
# This script fixes both: it uses venv/bin/python, and it only ever kills processes
# whose working directory is THIS bot's folder.

set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

PY="$HERE/venv/bin/python"

# Script command-line fragments that belong to this bot. We never match on these
# alone (dashboard.py / observers are shared names with the other bots) — we also
# require the process cwd to be inside $HERE before killing.
SCRIPTS="src/main.py scripts/run_all_observers.py src/dashboard.py"

# Echo PIDs of matching processes that are genuinely OURS (cwd under $HERE).
our_pids() {
    local pids="" p cmd cwd matched s
    for p in $(pgrep -f "python" 2>/dev/null); do
        cmd=$(ps -o command= -p "$p" 2>/dev/null) || continue
        matched=""
        for s in $SCRIPTS; do
            case "$cmd" in *"$s"*) matched=1;; esac
        done
        [ -n "$matched" ] || continue
        cwd=$(lsof -a -d cwd -p "$p" -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
        case "$cwd" in "$HERE"|"$HERE"/*) pids="$pids $p";; esac
    done
    echo $pids
}

stop_ours() {
    local pids
    pids=$(our_pids)
    if [ -z "$pids" ]; then
        echo "  (nothing of ours to stop)"
        return 0
    fi
    for p in $pids; do
        printf "  killing PID %-7s %s\n" "$p" "$(ps -o command= -p "$p" 2>/dev/null)"
        kill -9 "$p" 2>/dev/null || true
    done
    sleep 2
}

require_venv() {
    if [ ! -x "$PY" ]; then
        echo "ERROR: $PY is missing or not executable."
        echo "Rebuild the venv first (see LEDGER 2026-05-24):"
        echo "    /opt/homebrew/bin/python3.11 -m venv venv   # or your python@3.11 path"
        echo "    venv/bin/pip install -r requirements.txt"
        exit 1
    fi
}

cmd="${1:-start}"
case "$cmd" in
    stop)
        echo "── stopping kalshi_bot_2.0 ──"
        stop_ours
        echo "  done"
        ;;
    status)
        echo "── kalshi_bot_2.0 processes ──"
        pids=$(our_pids)
        if [ -z "$pids" ]; then echo "  (none running)"; else
            for p in $pids; do ps -o pid=,command= -p "$p"; done
        fi
        echo "  dashboard (if up): http://127.0.0.1:8082"
        ;;
    start)
        require_venv
        echo "── stopping any of our stale instances ──"
        stop_ours
        mkdir -p logs
        echo "── starting fresh (venv python) ──"
        nohup "$PY" src/main.py                       > logs/bot.log       2>&1 &
        nohup "$PY" scripts/run_all_observers.py       > logs/observers.log 2>&1 &
        nohup "$PY" src/dashboard.py                   > logs/dashboard.log 2>&1 &
        sleep 3
        echo "── now running ──"
        pids=$(our_pids)
        for p in $pids; do printf "  PID %-7s %s\n" "$p" "$(ps -o command= -p "$p")"; done
        echo
        echo "  dashboard: http://127.0.0.1:8082"
        echo "  logs:      tail -f logs/bot.log"
        echo "  stop:      ./run.sh stop"
        echo
        echo "── first dashboard log lines (watch for ModuleNotFoundError) ──"
        tail -5 logs/dashboard.log 2>/dev/null || true
        ;;
    *)
        echo "Usage: ./run.sh {start|stop|status}"
        exit 1
        ;;
esac
