#!/usr/bin/env python3
"""Which worker tmux sessions a viewer could attach, and how.

Read-only and additive: it introduces no state of its own and nothing in the
pool calls it. Its two inputs already exist — the roster (who the workers are,
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
        inc = live_incarnation(workspace, worker_id)
        tmux = (inc or {}).get("tmux") or {}
        socket = tmux.get("socket") or ""
        name = tmux.get("session_name") or wi.tmux_session_name(worker_id)
        if inc and socket:
            availability, detail = probe(name, socket=socket)
        else:
            availability, detail = "absent", "no open incarnation" if not inc else "no socket recorded"
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
        }
        # `=` forces tmux to match the name exactly; without it a short id
        # prefix-matches and attaches to a different worker.
        entry["attach_argv"] = (
            ["tmux", "-S", socket, "attach-session", "-t", f"={name}"] if entry["live"] else None
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
