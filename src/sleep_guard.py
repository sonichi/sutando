#!/usr/bin/env python3
"""Sleep guard: the one definition of the long-lived macOS keep-awake process, used by startup and health-check.

Only a caffeinate holding -s keeps a lid-closed Mac on AC awake; short-lived `caffeinate -i -t N`
processes must not count. `check` reads a `PID PPID ARGS` snapshot (None means ps could not run, so
the answer is unknown, never absent); `start` launches the holder detached so it outlives its caller.

CLI (startup.sh):  sleep_guard.py ensure   start the holder unless one already runs (no-op off macOS)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Callable, Optional

NAME = "sleep-guard"
PATTERN = r"(^|/)caffeinate( [^ ]+)* -[a-zA-Z]*s( |$)"
COMMAND = ("caffeinate", "-d", "-i", "-s")
_RX = re.compile(PATTERN)


def holds(args: str) -> bool:
    """True when one process's argv string is a caffeinate holding -s."""
    return bool(_RX.search(args))


def holder_present(ps_output: Optional[str]) -> Optional[bool]:
    """True/False from a `PID PPID ARGS` table; None when the table is unavailable."""
    if ps_output is None:
        return None
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and holds(parts[2]):
            return True
    return False


def check(ps_output: Optional[str], platform: str = sys.platform) -> Optional[dict]:
    """Health row for the sleep guard, or None off macOS."""
    if platform != "darwin":
        return None
    present = holder_present(ps_output)
    if present is None:
        return {"name": NAME, "status": "unknown",
                "detail": "ps could not run, so whether a caffeinate -s holder exists is unknown"}
    if present:
        return {"name": NAME, "status": "ok", "detail": "caffeinate -s holder running"}
    return {"name": NAME, "status": "warn",
            "detail": ("no caffeinate -s holder: a lid-closed Mac on AC will sleep and agents answer "
                       "in bursts. Fix: python3 src/health-check.py --fix (starts caffeinate -d -i -s)")}


def start(spawn: Callable = subprocess.Popen) -> int:
    """Start the holder in its own session with no inherited stdio; returns its pid."""
    devnull = open(os.devnull, "r+b")
    try:
        proc = spawn(["nohup", *COMMAND], stdin=devnull, stdout=devnull, stderr=devnull,
                     start_new_session=True, close_fds=True)
    finally:
        devnull.close()
    return proc.pid


def ensure(snapshot: Callable[[], Optional[str]], platform: str = sys.platform,
           spawn: Callable = subprocess.Popen) -> str:
    """Start the holder when none is seen; an unreadable table starts one too, since a duplicate is harmless."""
    if platform != "darwin":
        return "skipped: not macOS"
    if holder_present(snapshot()):
        return "caffeinate -s already running"
    pid = start(spawn)
    return f"caffeinate started (pid {pid}; prevents display and system sleep on AC)"


def main(argv: list) -> int:
    if argv[1:] != ["ensure"]:
        print("usage: sleep_guard.py ensure", file=sys.stderr)
        return 2
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sutando_platform import process_snapshot
    print(ensure(process_snapshot))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
