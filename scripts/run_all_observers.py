"""
run_all_observers.py — launch prod_observer, poly_observer, metar_logger,
and the phase2_shadow_logger loop in one terminal with prefixed,
interleaved output.

A single Ctrl-C cleanly stops all four (forwards SIGINT to each child,
waits for them to finish their in-flight cycle, then exits). No daemons,
no service files — just a convenience wrapper for `python script.py`-style
runs.

The shadow logger is normally one-shot (designed for cron). This wrapper
provides the hourly retrigger that you'd otherwise put in a `while true;
sleep 3600` shell loop.

Usage:
    venv/bin/python scripts/run_all_observers.py
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / "venv" / "bin" / "python")

SHADOW_INTERVAL_SEC = 3600


def _pump(proc: subprocess.Popen, prefix: str) -> None:
    """Stream child's stdout/stderr to ours, line-prefixed for clarity."""
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(f"[{prefix:<8}] {line}")
        sys.stdout.flush()


def _spawn(args: list[str], prefix: str) -> subprocess.Popen:
    """Spawn a child subprocess and start a thread to pump its output."""
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, cwd=ROOT,
    )
    threading.Thread(target=_pump, args=(proc, prefix), daemon=True).start()
    return proc


def _shadow_loop(stop_flag: threading.Event) -> None:
    """Replicates `while true; do shadow_logger; sleep 3600; done`.
    Each iteration spawns a one-shot, waits for it, then sleeps."""
    while not stop_flag.is_set():
        proc = _spawn([PY, "scripts/phase2_shadow_logger.py"], "shadow")
        proc.wait()
        # Sleep in 5s slices so Ctrl-C exits promptly.
        end = time.time() + SHADOW_INTERVAL_SEC
        while not stop_flag.is_set() and time.time() < end:
            time.sleep(min(5.0, end - time.time()))


def main() -> int:
    print("[launcher] starting prod_observer + poly_observer + metar_logger + shadow loop")
    print("[launcher] Ctrl-C to stop all four cleanly")
    print()

    procs: list[subprocess.Popen] = []
    procs.append(_spawn([PY, "scripts/prod_observer.py"], "kalshi"))
    procs.append(_spawn([PY, "scripts/poly_observer.py"], "poly"))
    procs.append(_spawn([PY, "scripts/metar_logger.py"], "metar"))

    stop_flag = threading.Event()
    shadow_thread = threading.Thread(
        target=_shadow_loop, args=(stop_flag,), daemon=True
    )
    shadow_thread.start()

    def _handle_sigint(signum, frame):
        print("\n[launcher] SIGINT — forwarding to children, waiting for clean exit")
        stop_flag.set()
        for p in procs:
            try:
                p.send_signal(signal.SIGINT)
            except Exception:
                pass

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    # Block until all observer subprocesses exit.
    for p in procs:
        p.wait()

    print("[launcher] all children exited; bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
