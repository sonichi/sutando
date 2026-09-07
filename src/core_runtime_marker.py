#!/usr/bin/env python3
"""Sole writer of the marker and session-start log; launchers inject runtime/session/source.
core-runtime.json is replaced atomically (readers poll it); best-effort — a launch never fails on it."""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

VALID_RUNTIMES = ("claude", "codex")


def write_marker(workspace, runtime: str, session: str, source: str = "start-cli") -> bool:
    """Declare `runtime` as the core in `workspace`. True if both records landed.
    Raises ValueError on an unknown runtime — it would publish a value no reader understands."""
    if runtime not in VALID_RUNTIMES:
        raise ValueError(f"unknown runtime {runtime!r}; expected one of {VALID_RUNTIMES}")
    if not workspace:
        return False
    state = Path(workspace) / "state"
    now = int(time.time())
    ok = True
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    marker = {"runtime": runtime, "session": session, "started_at": now}
    try:
        # Atomic replace: a reader polling mid-write must never see a partial file.
        fd, tmp = tempfile.mkstemp(dir=str(state), prefix=".core-runtime.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(marker) + "\n")
            os.replace(tmp, str(state / "core-runtime.json"))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, ValueError):
        ok = False

    entry = {
        "host": socket.gethostname().split(".")[0],
        "session_started_at": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "source": source,
        "runtime": runtime,
    }
    try:
        with open(state / "session-starts.log", "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        ok = False
    return ok


ABSENT = "-"


def stash_marker(workspace) -> str:
    """Capture the marker before a caller publishes over it. Returns a token for restore_marker:
    ABSENT if no marker exists, else the path of a sibling copy ("" when nothing could be saved)."""
    if not workspace:
        return ""
    marker = Path(workspace) / "state" / "core-runtime.json"
    if not marker.exists():
        return ABSENT
    try:
        fd, tmp = tempfile.mkstemp(dir=str(marker.parent), prefix=".core-runtime.stash.")
        with os.fdopen(fd, "w") as fh:
            fh.write(marker.read_text())
        return tmp
    except OSError:
        return ""


def restore_marker(workspace, token: str) -> bool:
    """Undo a publish whose launch then failed. ABSENT removes the marker entirely —
    no core is live, so no runtime may be claimed. False means the claim may still stand."""
    if not workspace or not token:
        return False
    marker = Path(workspace) / "state" / "core-runtime.json"
    if token == ABSENT:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True
    try:
        os.replace(token, str(marker))
    except OSError:
        try:
            os.unlink(token)
        except OSError:
            pass
        return False
    return True


def main(argv: list[str]) -> int:
    # Rollback modes come first: they take a workspace only, not the publish triple.
    if len(argv) > 1 and argv[1] == "--stash":
        if len(argv) < 3:
            print("usage: core_runtime_marker.py --stash <workspace>", file=sys.stderr)
            return 2
        token = stash_marker(argv[2])
        print(token)
        return 0 if token else 1
    if len(argv) > 1 and argv[1] == "--restore":
        if len(argv) < 4:
            print("usage: core_runtime_marker.py --restore <workspace> <token>", file=sys.stderr)
            return 2
        return 0 if restore_marker(argv[2], argv[3]) else 1
    if len(argv) < 4:
        print("usage: core_runtime_marker.py <workspace> <runtime> <session> [source]\n       core_runtime_marker.py --stash|--restore <workspace> [token]",
              file=sys.stderr)
        return 2
    try:
        ok = write_marker(argv[1], argv[2], argv[3], argv[4] if len(argv) > 4 else "start-cli")
    except ValueError as exc:
        print(f"core_runtime_marker: {exc}", file=sys.stderr)
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
