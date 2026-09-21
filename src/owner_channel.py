#!/usr/bin/env python3
"""May the core speak in this channel? Bindings decide, not recent activity.

`state/last-owner-activity.json` records where the owner was last active; it
says nothing about who the room is BOUND to. `state/bindings.json` does
(`{"bindings": {"<channel_id>": "<worker_id>"}}`). A room bound to a worker is
that worker's voice — the core speaks there only when the worker is dead and
recovery is what is speaking. Every core-side "post to the owner" path resolves
its target through here.

CLI: `owner_channel.py --workspace <ws> [--channel <id>]` prints
`allow|refuse <channel> <reason>`; exit 0 on allow, 3 on refuse.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

WATCHER_SENTINEL_STEM = "watch-tasks-stream"


def bound_worker(workspace: Path, channel_id: str) -> str | None:
    """The worker a channel is bound to, or None. Unreadable bindings are NOT
    "unbound": the safe failure for a speaking decision is silence, so any
    error other than an absent file propagates."""
    path = Path(workspace) / "state" / "bindings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    bindings = data.get("bindings") if isinstance(data, dict) else None
    if not isinstance(bindings, dict):
        raise ValueError(f"{path}: expected {{'bindings': {{channel: worker}}}}")
    worker = bindings.get(channel_id)
    return str(worker) if worker else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


ALIVE, DEAD, UNKNOWN = "alive", "dead", "unknown"


def worker_liveness(workspace: Path, worker_id: str) -> str:
    """ALIVE / DEAD / UNKNOWN from the worker's own watcher sentinel.

    A sentinel is written once at watcher startup, so its absence cannot
    distinguish "no watcher" from "a live watcher whose file is gone".
    """
    state = Path(workspace) / "state"
    try:
        sentinels = sorted(state.glob(f"{WATCHER_SENTINEL_STEM}-*{worker_id}*.pid"))
    except OSError:
        return UNKNOWN
    if not sentinels:
        return UNKNOWN
    readable = False
    for p in sentinels:
        try:
            pid = int(p.read_text(encoding="utf-8").split()[0])
        except (OSError, ValueError, IndexError):
            continue
        readable = True
        if _pid_alive(pid):
            return ALIVE
    return DEAD if readable else UNKNOWN


def worker_alive(workspace: Path, worker_id: str) -> bool:
    """True only for a positively observed live watcher; UNKNOWN is not alive."""
    return worker_liveness(workspace, worker_id) == ALIVE


def may_core_speak(workspace: Path, channel_id: str) -> tuple[bool, str]:
    """(allowed, reason). Unbound → allowed. Only a POSITIVELY dead worker
    releases the room; alive and unknown both refuse, because speaking over a
    live worker is the harm this gate exists to prevent.
    """
    worker = bound_worker(workspace, channel_id)
    if worker is None:
        return True, "unbound"
    state = worker_liveness(workspace, worker)
    if state == ALIVE:
        return False, f"bound to worker {worker[:8]}, which is alive — its voice, not the core's"
    if state == UNKNOWN:
        return False, (f"bound to worker {worker[:8]}, liveness UNKNOWN (no readable watcher "
                       f"sentinel) — refusing; a missing sentinel is not evidence of death")
    return True, f"bound to worker {worker[:8]}, which is DEAD — recovery may speak"


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--channel", default="",
                    help="channel id to check; default: the owner's last-active channel")
    a = ap.parse_args(argv)
    ws = Path(a.workspace)
    channel = a.channel
    if not channel:
        try:
            act = json.loads((ws / "state" / "last-owner-activity.json").read_text(encoding="utf-8"))
            channel = str(act.get("channel_id", "")).strip()
        except (OSError, ValueError):
            channel = ""
        if not channel:
            print("refuse - no owner channel recorded")
            return 3
    ok, why = may_core_speak(ws, channel)
    print(f"{'allow' if ok else 'refuse'} {channel} {why}")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
