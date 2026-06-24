#!/usr/bin/env python3
"""Per-host heartbeat for sutando-core sessions.

Writes a small JSON file at `<workspace>/state/cores/<hostname>.alive` every
30 seconds while running. The file's content reports the core's pid, host,
start time, last beat, and a free-form status string; the file's mtime is
the cross-host "is this core still up?" signal.

Why
---
Today's "is the core alive?" check reads `core-status.json` at the workspace
root — a single file written by the proactive-loop each pass. That's fine
for a single-machine install: one core, one status. The moment we want
multi-core (multiple Claude Code sessions sharing a workspace, or sutando
running on both Mac Studio + MacBook against a synced workspace), one file
can no longer represent N processes.

Per-host file at `state/cores/<hostname>.alive`:
  • Each running core writes only its own file (no contention).
  • Any process can read the directory to see who's alive across the fleet.
  • mtime is the authoritative liveness signal (younger than ~90s = alive).
  • Future lease-based scheduler consumes this to know who can pick up work.

This script is intentionally tiny and standalone — startup.sh launches it as
a background process. SIGTERM/SIGINT clean up the .alive file so a graceful
shutdown is visible immediately (vs. waiting for mtime-staleness timeout).

Usage:
  python3 src/core_heartbeat.py                  # default 30s interval
  python3 src/core_heartbeat.py --interval 10    # for tests
  python3 src/core_heartbeat.py --status busy    # set the status string

Runs forever until killed. Exit codes:
  0 — clean shutdown (SIGTERM/SIGINT received)
  Other — fatal write error (unrecoverable; supervisor should restart)
"""
from __future__ import annotations
import argparse
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

# Resolve workspace via the M0 helper (PR #1395 / v0.8 #1440) — the previous
# inlined env-or-legacy-default resolution wrote .alive files where no
# post-M0 reader looks (health-check + dashboard read resolve_workspace()/
# state/cores/), so every core reported dead. workspace_default is a sibling
# module (stdlib-only deps), so the old "dep-free" rationale no longer buys
# anything. Fail loud on import error: a heartbeat written to the wrong tree
# is worse than no heartbeat (supervisor restarts on crash; see module header).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402

WORKSPACE = resolve_workspace()

CORES_DIR = WORKSPACE / "state" / "cores"


def _hostname() -> str:
    """Per-host label for the `.alive` filename. Delegates to
    `util_paths._host_label()` — the single source of truth (honors
    `$SUTANDO_HOST_LABEL`, else short hostname) — so the heartbeat label stays
    in lockstep with the `hosts/<host>/` per-host dir and survives DHCP
    hostname drift (a node whose `hostname` is a DHCP/Comcast name that flaps
    would otherwise write two divergent `<label>.alive` files). Falls back to
    the raw short hostname if util_paths is unavailable."""
    try:
        from util_paths import _host_label
        return _host_label()
    except Exception:
        return socket.gethostname().split(".")[0]


def _alive_path() -> Path:
    return CORES_DIR / f"{_hostname()}.alive"


def write_beat(status: str = "running") -> None:
    """Write one heartbeat record. Atomic-via-tmp-then-rename so a concurrent
    reader never sees a partial file."""
    CORES_DIR.mkdir(parents=True, exist_ok=True)
    target = _alive_path()
    payload = {
        "host": _hostname(),
        "pid": os.getpid(),
        "started_at": _STARTED_AT,
        "last_beat_at": time.time(),
        "status": status,
        "schema_version": 1,
    }
    tmp = target.with_suffix(".alive.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(target)


_STARTED_AT: float = time.time()
_SHUTDOWN_REQUESTED = False


def _handle_signal(signum: int, frame) -> None:
    """Mark shutdown so the loop exits at the top of the next sleep; also
    unlink the .alive file so peers see this core leave immediately rather
    than wait for mtime staleness."""
    global _SHUTDOWN_REQUESTED
    _SHUTDOWN_REQUESTED = True
    try:
        _alive_path().unlink(missing_ok=True)
    except Exception:  # pragma: no cover — best-effort cleanup
        pass


def run_forever(interval: float = 30.0, status: str = "running") -> int:
    """Heartbeat loop. Returns the exit code (0 on graceful shutdown)."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    while not _SHUTDOWN_REQUESTED:
        try:
            write_beat(status=status)
        except Exception as e:
            # Don't die on transient FS hiccups — log + retry next tick.
            print(f"core_heartbeat: write failed: {e}", file=sys.stderr, flush=True)
        # Sleep in small slices so SIGTERM is responsive (signal handler
        # sets the flag; we check it between slices instead of blocking
        # for the full `interval`).
        slept = 0.0
        slice_s = min(1.0, interval)
        while slept < interval and not _SHUTDOWN_REQUESTED:
            time.sleep(slice_s)
            slept += slice_s
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--interval", type=float, default=30.0, help="seconds between beats (default: 30)")
    p.add_argument("--status", type=str, default="running", help="status string written into the .alive file")
    p.add_argument("--once", action="store_true", help="write a single beat and exit (for tests/debugging)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.once:
        write_beat(status=args.status)
        return 0
    return run_forever(interval=args.interval, status=args.status)


if __name__ == "__main__":
    sys.exit(main())
