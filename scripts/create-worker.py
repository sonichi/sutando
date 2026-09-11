#!/usr/bin/env python3
"""Create a worker in one command: identity, folder, session, binding, roster.

Every piece already existed; none was reachable without writing Python, and the
three steps were separable, which is the defect. Spawning wrote an identity, a
binding was hand-edited, and the roster was recompiled from a `python -c` — miss
the third and the worker runs, has an id, and can receive nothing, because the
router reads only the roster. That had already happened once when this was
written: two worker records on disk, one in the roster.

So this composes; it decides nothing. The roster stays the core's artifact and
the binding stays the owner's declaration.

Run: python3 scripts/create-worker.py --folder <dir> --label "<name>" [--room <id>]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(_REPO / "src"))

import pool_advertise as pa  # noqa: E402
import pool_roster as pr  # noqa: E402

import spawn_worker as sw  # noqa: E402

from workspace_default import resolve_workspace  # noqa: E402

REFUSED = 2


class Refused(Exception):
    """A precondition failed. Nothing was created."""


def existing_workers(workspace) -> dict:
    """The roster's own worker table, or empty when there is no roster yet.

    Read rather than rebuilt from disk: a record with no roster entry was left
    that way by something, and inventing its state here would decide for it.
    """
    roster = pr.load_roster(workspace)
    return dict((roster or {}).get("workers") or {})


def unrostered(workspace, workers: dict) -> list:
    """Worker records with no roster entry — reported, never adopted."""
    d = Path(workspace) / "state" / "workers"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir()
                  if p.is_dir() and p.name not in workers)


def preflight(workspace, repo, room: str) -> None:
    """Refuse before anything is created, not after.

    A worker is four side effects; a failure discovered afterwards leaves some
    of them behind, which is the state this command exists to stop producing.
    """
    if os.environ.get("SUTANDO_INSTANCE_ID"):
        raise Refused("a worker cannot create workers — lifecycle is the core's; "
                      "run this from the core's session")
    if not Path(repo).is_dir():
        raise Refused(f"repo is not a directory: {repo}")
    if room is not None and not str(room).strip():
        raise Refused("--room was given but empty")
    if not sw.per_instance_sentinel_supported(repo):
        raise Refused("this checkout writes ONE watcher sentinel for every "
                      "watcher, so a second watcher would erase the core's "
                      "stamp — see #3875")


def compile_with(workspace, worker_id: str, label: str, room, runtime=None) -> dict:
    """Add this worker to the roster, and its room to the bindings."""
    workers = existing_workers(workspace)
    workers[worker_id] = {"state": "live", "label": label or worker_id}
    if runtime:
        workers[worker_id]["runtime"] = str(runtime)
    bindings = dict(pr.load_bindings(workspace))
    if room:
        bindings[room] = worker_id
    return pr.compile_roster(workspace, workers, bindings)


def report(made: dict, roster: dict, room, orphans: list) -> str:
    # Read through .get: spawn()'s contract is these keys, but a partial
    # report beats a KeyError when a caller upstream reshapes its extras.
    tmux = made.get("tmux") or {}
    lines = [
        f"worker {made['worker_id']}",
        f"  label     {made.get('label') or made['worker_id']}",
        f"  folder    {made.get('cwd', '?')}",
        f"  delivery  {made.get('delivery_dir', '?')}",
        f"  session   tmux -S {tmux.get('socket', '?')} attach -t {tmux.get('session_name', '?')}",
        f"  roster    v{roster['version']} ({len(roster['workers'])} worker(s))",
    ]
    lines.append(f"  room      {room}" if room else
                 "  room      none — bind one with --room, or it receives only "
                 "work addressed to it by id")
    if orphans:
        lines.append("")
        lines.append(f"NOTE: {len(orphans)} worker record(s) not in the roster, "
                     "so nothing routes to them:")
        lines.extend(f"  {o}" for o in orphans)
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="create a worker: identity, folder, session, binding, roster")
    ap.add_argument("--folder", default="", help="the worker's working directory")
    ap.add_argument("--label", default="", help='a name for it, e.g. "code reviewer"')
    ap.add_argument("--room", default=None, help="bind this room/channel id to it")
    ap.add_argument("--runtime", default=None, help="default: the core's own runtime")
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--repo", default=str(_REPO))
    ap.add_argument("--socket", default="")
    ap.add_argument("--new", action="store_true", help="fresh session (the default)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    workspace = a.workspace or str(resolve_workspace())
    try:
        preflight(workspace, a.repo, a.room)
    except Refused as e:
        print(f"create-worker: {e}", file=sys.stderr)
        return REFUSED

    if a.dry_run:
        plan = sw.plan(workspace, a.repo, runtime=a.runtime or "claude",
                       cwd=a.folder, socket=a.socket or None, label=a.label)
        plan["would_bind"] = a.room
        plan["unrostered_records"] = unrostered(workspace, existing_workers(workspace))
        print(json.dumps(plan, indent=2))
        return 0

    try:
        made = sw.spawn(workspace, a.repo, runtime=a.runtime, cwd=a.folder,
                        socket=a.socket or None, label=a.label)
    except sw.SpawnRefused as e:
        print(f"create-worker: {e}", file=sys.stderr)
        return REFUSED

    try:
        roster = compile_with(workspace, made["worker_id"], a.label, a.room,
                              runtime=made.get("runtime"))
    except (pr.RosterError, OSError) as e:
        # The worker exists and the roster does not know it: say so loudly with
        # the repair, or it becomes the silent stale-roster case again.
        print(f"create-worker: worker {made['worker_id']} was created, but the "
              f"roster could not be compiled: {e}\n"
              f"  it will receive nothing until the roster names it.",
              file=sys.stderr)
        return 1

    try:
        # The picker follows the roster only through this file (the bridge
        # sends it); a compile without it leaves the picker one worker behind.
        pa.write_advertisement(workspace)
    except OSError as e:
        print(f"create-worker: worker {made['worker_id']} is routable (roster "
              f"v{roster['version']}), but the advertisement could not be "
              f"written: {e}\n"
              f"  the picker will not show it until this succeeds: "
              f"python3 src/pool_advertise.py --workspace {workspace} --write",
              file=sys.stderr)
        return 1

    orphans = unrostered(workspace, roster.get("workers") or {})
    if a.json:
        print(json.dumps({**made, "roster_version": roster["version"],
                          "room": a.room, "unrostered_records": orphans}, indent=2))
    else:
        print(report(made, roster, a.room, orphans))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
