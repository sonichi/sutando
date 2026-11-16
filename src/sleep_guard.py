#!/usr/bin/env python3
"""Sleep guard: the one definition of the long-lived macOS keep-awake process, used by startup and health-check.

Only a long-lived caffeinate holding -s keeps a lid-closed Mac on AC awake; `caffeinate -s -t N`,
`-w PID` or one wrapping a command ends on its own and must not count. `check` reads a `PID PPID ARGS`
snapshot (None means ps could not run, so the answer is unknown, never absent); `start` launches the
holder detached and returns only once it is seen alive. The match is host-wide on purpose: staying
awake is a property of the host, so any user's or checkout's holder counts. `sleep_guard.enabled: false`
in sutando config turns it off: nothing is started and the row reads ok; a running holder is never killed.

CLI (startup.sh):  sleep_guard.py ensure   start the holder unless one already runs (no-op off macOS)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Callable, Optional

NAME = "sleep-guard"
COMMAND = ("caffeinate", "-d", "-i", "-s")
_VALUE_FLAGS = {"t", "w"}  # each takes a value and makes caffeinate exit on its own
SETTLE_S = 0.5
CONFIG_KEY = "sleep_guard.enabled"
_OFF_HINT = 'turn it off with {"sleep_guard": {"enabled": false}} in sutando.config.local.json'


def is_enabled() -> bool:
    """The config switch; on when the key is absent or the config cannot be read."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from sutando_config import resolve_sleep_guard_enabled
        return resolve_sleep_guard_enabled()
    except Exception:  # noqa: BLE001 — an unreadable switch leaves the guard on
        return True


class StartFailed(Exception):
    """The holder was spawned but is not running."""


def holds(args: str) -> bool:
    """True when one process's argv string is a caffeinate that holds -s until it is killed."""
    tokens = args.split()
    if not tokens or os.path.basename(tokens[0]) != "caffeinate":
        return False
    flags, i = set(), 1
    while i < len(tokens) and tokens[i].startswith("-") and len(tokens[i]) > 1:
        letters = tokens[i][1:]
        if not letters.isalpha():
            return False
        flags.update(letters)
        i += 1 + sum(1 for c in letters if c in _VALUE_FLAGS)
    return "s" in flags and not flags & _VALUE_FLAGS and i >= len(tokens)


def holder_present(ps_output: Optional[str]) -> Optional[bool]:
    """True/False from a `PID PPID ARGS` table; None when the table is unavailable."""
    if ps_output is None:
        return None
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and holds(parts[2]):
            return True
    return False


def check(ps_output: Optional[str], platform: str = sys.platform,
          enabled: Optional[bool] = None) -> Optional[dict]:
    """Health row for the sleep guard, or None off macOS. `enabled` None reads the config."""
    if platform != "darwin":
        return None
    if not (is_enabled() if enabled is None else enabled):
        return {"name": NAME, "status": "ok", "detail": f"disabled by config ({CONFIG_KEY})"}
    present = holder_present(ps_output)
    if present is None:
        return {"name": NAME, "status": "unknown",
                "detail": "ps could not run, so whether a caffeinate -s holder exists is unknown"}
    if present:
        return {"name": NAME, "status": "ok", "detail": "caffeinate -s holder running"}
    return {"name": NAME, "status": "warn",
            "detail": ("no caffeinate -s holder: a lid-closed Mac on AC will sleep and agents answer "
                       "in bursts. Fix: python3 src/health-check.py --fix (starts caffeinate -d -i -s), or "
                       f"{_OFF_HINT}")}


def start(spawn: Callable = subprocess.Popen, settle_s: float = SETTLE_S) -> int:
    """Start the holder in its own session with no inherited stdio; returns its pid once it is still alive.

    Raises OSError when caffeinate cannot be executed and StartFailed when it exits within settle_s.
    """
    devnull = open(os.devnull, "r+b")
    try:
        proc = spawn(list(COMMAND), stdin=devnull, stdout=devnull, stderr=devnull,
                     start_new_session=True, close_fds=True)
    finally:
        devnull.close()
    deadline = time.monotonic() + settle_s
    while True:
        rc = proc.poll()
        if rc is not None:
            raise StartFailed(f"caffeinate exited with status {rc}")
        if time.monotonic() >= deadline:
            return proc.pid
        time.sleep(0.05)


def ensure(snapshot: Callable[[], Optional[str]], platform: str = sys.platform,
           spawn: Callable = subprocess.Popen, enabled: Optional[bool] = None) -> tuple:
    """(ok, message): start the holder when none is seen. An unreadable table starts nothing."""
    if platform != "darwin":
        return True, "skipped: not macOS"
    if not (is_enabled() if enabled is None else enabled):
        return True, f"sleep guard off by config ({CONFIG_KEY}): caffeinate not started"
    present = holder_present(snapshot())
    if present is None:
        return False, "sleep guard unknown: ps could not run, caffeinate not started"
    if present:
        return True, "caffeinate -s already running"
    try:
        pid = start(spawn)
    except (OSError, StartFailed) as e:
        return False, f"caffeinate did not start ({e}): a lid-closed Mac on AC will sleep"
    return True, f"caffeinate started (pid {pid}; prevents display and system sleep on AC)"


def main(argv: list) -> int:
    if argv[1:] != ["ensure"]:
        print("usage: sleep_guard.py ensure", file=sys.stderr)
        return 2
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sutando_platform import process_snapshot
    ok, message = ensure(process_snapshot)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
