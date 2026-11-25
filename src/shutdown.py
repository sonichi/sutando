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
  python3 src/shutdown.py mark-if-absent R  # write one only if none; print its token, else nothing
  python3 src/shutdown.py clear-if TOKEN    # 0 removed; 3 not that stop (left alone); else an error
  python3 src/shutdown.py token             # the token of the stop in place, else nothing
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402


def _sentinel_path() -> Path:
    return resolve_workspace() / "state" / "shutdown.sentinel"


@contextlib.contextmanager
def _locked():
    """The sentinel's path, under an flock on `shutdown.sentinel.lock` shared by its writers here."""
    import fcntl
    p = _sentinel_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_name(p.name + ".lock"), "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield p


def _write(p: Path, reason: str) -> None:
    # `gen` makes every stop's bytes, and so its token, unique.
    p.write_text(json.dumps({"reason": reason, "ts": int(time.time()), "gen": secrets.token_hex(8)}) + "\n")


def _token(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except FileNotFoundError:
        return ""


def mark_shutdown(reason: str = "manual") -> Path:
    """Write the shutdown sentinel. Idempotent — overwrites any prior one."""
    with _locked() as p:
        _write(p, reason)
    return p


def mark_if_absent(reason: str) -> str:
    """Write a sentinel only when none is present; its token (a later compare-and-clear names exactly
    this stop), or '' when one was already there and is not this caller's."""
    with _locked() as p:
        if p.exists():
            return ""
        _write(p, reason)
        return _token(p)


def clear_if(token: str) -> bool:
    """Remove the sentinel only while it is still the stop `token` names; a newer one survives."""
    with _locked() as p:
        if not token or _token(p) != token:
            return False
        p.unlink()
        return True


def current_token() -> str:
    """The token of the stop in place now ('' when none), for a later clear_if of exactly that stop."""
    with _locked() as p:
        return _token(p)


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


NOT_MINE = 3  # clear-if: the stop in place is not the one named, a normal outcome, not an error


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd == "mark":
        reason = argv[2] if len(argv) > 2 else "manual"
        print(mark_shutdown(reason))
        return 0
    if cmd == "clear":
        clear_shutdown()
        return 0
    if cmd == "mark-if-absent":
        token = mark_if_absent(argv[2] if len(argv) > 2 else "manual")
        if token:
            print(token)
        return 0
    if cmd == "clear-if":
        return 0 if clear_if(argv[2] if len(argv) > 2 else "") else NOT_MINE
    if cmd == "token":
        token = current_token()
        if token:
            print(token)
        return 0
    if cmd == "check":
        return 0 if is_shutting_down() else 1
    if cmd == "path":
        # Shell launchers stash/restore the sentinel byte-for-byte around a
        # launch; they must not re-derive this path and drift from it.
        print(_sentinel_path())
        return 0
    print(f"usage: {argv[0]} mark|clear|check|path|token|mark-if-absent|clear-if [arg]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
