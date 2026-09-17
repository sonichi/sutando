#!/usr/bin/env python3
"""Graceful-shutdown sentinel — a durable, cross-process "we are shutting down
on purpose (not crashing)" signal.

Motivation (owner ask 2026-07-17): the core has partial shutdown handling —
core_heartbeat unlinks its .alive file on SIGTERM, voice-agent traps signals,
restart.sh drains-and-waits. What's missing is a signal the CORE agent loop can
check to *finish the current task and exit cleanly* rather than be killed
mid-pass and leave an orphaned task recovered only after the result-watcher
timeout.

This sentinel is that signal on the STOP paths only. Writers (restart.sh
--stop-only, an explicit "stop") call mark_shutdown(); the core launchers clear it on
boot; readers (the proactive loop at the top of a pass, bridges) call
is_shutting_down(). A plain restart marks and clears it within seconds and the
core is meant to survive, so clean core exit is a --stop-only guarantee; what
holds on BOTH paths is the watcher's intake gate. It lives under state/ next to the other liveness
files and carries a reason + timestamp so health-check can distinguish a
graceful stop from a crash.

CLI:
  python3 src/shutdown.py mark [reason]   # write the sentinel
  python3 src/shutdown.py clear           # remove it (startup)
  python3 src/shutdown.py check           # exit 0 if shutting down, 1 if not
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402


def _sentinel_path() -> Path:
    return resolve_workspace() / "state" / "shutdown.sentinel"


def mark_shutdown(reason: str = "manual") -> Path:
    """Write the shutdown sentinel. Idempotent — overwrites any prior one."""
    p = _sentinel_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"reason": reason, "ts": int(time.time())}) + "\n")
    return p


def clear_shutdown() -> None:
    """Remove the sentinel (called on boot). Never raises if it's absent."""
    try:
        _sentinel_path().unlink()
    except FileNotFoundError:
        pass


def is_shutting_down() -> bool:
    """True if a shutdown sentinel is present. Cheap enough to call each pass."""
    return _sentinel_path().exists()


def shutdown_info() -> dict | None:
    """The sentinel's {reason, ts}, or None if not shutting down / unreadable."""
    p = _sentinel_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {"reason": "unknown", "ts": 0}


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd == "mark":
        reason = argv[2] if len(argv) > 2 else "manual"
        print(mark_shutdown(reason))
        return 0
    if cmd == "clear":
        clear_shutdown()
        return 0
    if cmd == "check":
        return 0 if is_shutting_down() else 1
    if cmd == "path":
        # Shell launchers stash/restore the sentinel byte-for-byte around a
        # launch; they must not re-derive this path and drift from it.
        print(_sentinel_path())
        return 0
    print(f"usage: {argv[0]} mark|clear|check|path [reason]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
