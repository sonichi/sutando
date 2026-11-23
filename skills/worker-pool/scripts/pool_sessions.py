#!/usr/bin/env python3
"""Which worker tmux sessions a viewer could attach, and how.

Read-only and additive: it introduces no state of its own (it only creates the
empty launch lock it reads under) and nothing in the pool calls it. Its two inputs already exist — the roster (who the workers are,
and which room each is bound to) and each worker's incarnations file (the tmux
socket and session name of the run that is still open).

It deliberately does NOT describe the core. Resolving the core's socket is the
desktop app's policy — bundled private socket, external shared socket, legacy
/tmp fallback — and a second implementation here would be a copy that drifts.
The app names the core tab from its own resolution and asks this only for the
workers, so an install with no workers gets an empty list and never sends a
session key at all.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pool_roster  # noqa: E402
import spawn_worker  # noqa: E402

import worker_identity as wi  # noqa: E402

from workspace_default import resolve_workspace  # noqa: E402


def live_incarnation(workspace, worker_id: str) -> "dict | None":
    """The open run, or None. `ended_at is None` is what open means here; the
    newest such row wins, because a crash can leave an older one unclosed."""
    try:
        rows = wi.incarnations(workspace, worker_id)
    except Exception:
        return None
    open_rows = [r for r in rows if isinstance(r, dict) and r.get("ended_at") is None]
    return open_rows[-1] if open_rows else None


def agent_mxid(workspace, worker_id: str) -> str:
    """The worker's own agent id from its local identity row, or ''. A viewer matches it against a
    room's members, so a worker's room offers its session before the broker profile lists it.
    Reads `agent_mxid` only, never a credential reference."""
    try:
        import connections  # noqa: PLC0415 — engine src, only needed for this field
        row = connections.worker_enabled_identity(worker_id, workspace)
    except Exception:
        return ""
    return str((row or {}).get("agent_mxid") or "").strip()


def is_seat(workspace, runtime: str, session_name: str, incarnation: "dict | None" = None) -> bool:
    """Whether the session shows a Codex app-server seat, whose pane is the seat's log, not a
    prompt: typing there reaches seat.py, and Ctrl-C ends it. A viewer keeps such a pane
    read-only. The Codex runtime is required as well as the seat's host declaration
    (seat-launch.sh: <workspace>/state/codex-seat/<session>/host.json), as the desktop's core
    rule does: a leftover declaration under a Claude worker must not make it untypable.

    The declaration must belong to the open run. Both Codex modes reuse the session name and a
    TUI launch leaves an earlier seat's host.json in place, so the seat launcher stamps the run's
    incarnation id beside it (`incarnation`) and a different id is a previous run's seat. When
    either id is unknown the bare rule holds, so an unknown case stays read-only."""
    if runtime != "codex":
        return False
    seat_dir = Path(workspace) / "state" / "codex-seat" / session_name
    if not (seat_dir / "host.json").is_file():
        return False
    try:
        stamped = (seat_dir / "incarnation").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        stamped = ""
    current = str((incarnation or {}).get("incarnation_id") or "")
    return not (stamped and current) or stamped == current


@contextlib.contextmanager
def read_fence(workspace, session_name: str):
    """Hold spawn's launch lock (spawn_worker.launch_lock) shared across one row's reads, so a
    launch cannot finish between them. Yields False when a launch holds it or it cannot be
    taken: the row is then unknown, and a Codex row fails closed (read-only)."""
    path = Path(workspace) / "state" / "worker-launch" / f"{session_name}.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
    except OSError:
        yield False
        return
    with fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def rooms_bound_to(roster: dict, worker_id: str) -> list:
    """Rooms whose binding resolves to this worker, via the roster's OWN
    resolution — a binding may be a bare id or a set, and only it decides."""
    out = []
    for room in (roster.get("bindings") or {}):
        if worker_id in pool_roster.targets_for(roster, room):
            out.append(room)
    return sorted(out)


def sessions(workspace, probe=None) -> list:
    """One row per roster worker, whether or not it has a session right now.

    A worker is never dropped: "exists but has no session" and "no such worker"
    are different answers, and a caller that cannot tell them apart shows an
    empty tab strip for a pool that is merely between incarnations.

    Liveness is PROBED, never inferred from the records. A crash leaves an
    incarnation unclosed by design, so an open row plus a socket string proves
    only that a run was once started there. `availability` carries the probe's
    three states and `attach_argv` is emitted for `exists` alone — an argv that
    cannot attach is worse than none, because the caller acts on it.
    """
    roster = pool_roster.load_roster(workspace)
    if not roster:
        return []
    # Resolved per call, so a test's injected prober is the one that runs.
    probe = probe or spawn_worker.session_probe
    rows = []
    for worker_id, row in sorted((roster.get("workers") or {}).items()):
        row = row or {}
        runtime = str(row.get("runtime") or "")
        with read_fence(workspace, wi.tmux_session_name(worker_id)) as fenced:
            inc = live_incarnation(workspace, worker_id)
            tmux = (inc or {}).get("tmux") or {}
            socket = tmux.get("socket") or ""
            name = tmux.get("session_name") or wi.tmux_session_name(worker_id)
            if inc and socket:
                availability, detail = probe(name, socket=socket)
            else:
                availability, detail = "absent", "no open incarnation" if not inc else "no socket recorded"
            seat = is_seat(workspace, runtime, name, inc) if fenced else runtime == "codex"
        entry = {
            "worker_id": worker_id,
            "label": pool_roster.display_label(row, worker_id),
            "routing_label": row.get("label") or worker_id,
            "state": row.get("state") or "unknown",
            "bound_rooms": rooms_bound_to(roster, worker_id),
            "session_name": name,
            "tmux_socket": socket,
            "availability": availability,
            "availability_detail": detail,
            "live": availability == "exists",
            "runtime": runtime,
            "agent_mxid": agent_mxid(workspace, worker_id),
            "seat": seat,
        }
        entry["attach_argv"] = (
            spawn_worker.attach_argv(socket, name) if entry["live"] else None
        )
        rows.append(entry)
    return rows


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("verb", choices=["list"])
    p.add_argument("--workspace", default=None)
    p.add_argument("--json", action="store_true", help="machine-readable (the default for `list`)")
    p.add_argument("--room", default=None, help="only the worker bound to this room id")
    args = p.parse_args(argv)

    workspace = Path(args.workspace) if args.workspace else resolve_workspace()
    rows = sessions(workspace)
    if args.room:
        rows = [r for r in rows if args.room in r["bound_rooms"]]
    print(json.dumps({"workspace": str(workspace), "sessions": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
