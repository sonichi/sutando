#!/usr/bin/env python3
"""Manage the pool's workers: list, stop, retire, enable, remove.

The owner's (or the agent's, on the owner's behalf) one supported way to take a
worker out of service and bring it back. Each verb goes through the writer that
already owns its fact, so the remedy, the sweep and the router honour it:

    stop    the per-worker `paused` marker (pool_supervise.is_paused), then its
            tmux sessions are killed and its open runs closed. A paused worker
            is never restarted: not by a sweep, `--resume` or `--restart`.
    retire  stop, plus roster state `retired` (pool_roster.set_worker_state):
            no longer supervised or advertised, and the router sends traffic
            addressed to it to the core. Refused while a room is bound to it.
    enable  lift both, then the owner restart (pool_remedy.restart).
    remove  drop a retired worker's roster row; its records stay on disk.

    python3 pool_manage.py [--workspace WS] [--repo REPO] list [--json]
    python3 pool_manage.py [--workspace WS] stop   <worker id or label>
    python3 pool_manage.py [--workspace WS] retire <worker> [--unbind]
    python3 pool_manage.py [--workspace WS] [--repo REPO] enable <worker> [--no-start]
    python3 pool_manage.py [--workspace WS] remove <worker>

Every verb prints one JSON line. Exit 0 done, 1 failed, 2 refused.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
for _p in (str(_HERE), str(_REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pool_remedy as rem  # noqa: E402
import tmux_probe  # noqa: E402

from workspace_default import resolve_workspace  # noqa: E402

sup, wi = rem.sup, rem.wi
pr = rem.sw.pr

# Killed first to last: the inbox supervisor, then the input watcher, then the seat.
COMPANION_SUFFIXES = ("-watcher", rem.INPUT_WATCH_SUFFIX)
STOP_LOCK_WAIT_S = rem.RESTART_LOCK_WAIT_S


class Refused(Exception):
    pass


def _worker(workspace, target: str) -> tuple[str, dict]:
    roster = pr.load_roster(workspace)
    if roster is None:
        raise Refused("no readable roster; no workers to manage")
    try:
        wid = pr.resolve_label(roster, target)
    except pr.AmbiguousWorkerName as e:
        raise Refused(str(e)) from e
    row = (roster.get("workers") or {}).get(wid)
    if wid == pr.CORE or not isinstance(row, dict):
        raise Refused(f"{target!r} is not a worker id or label")
    return wid, row


def _socket(workspace, worker_id) -> str:
    """The socket the worker last ran on, else the spawner's default."""
    runs = [r for r in wi.incarnations(workspace, worker_id) if isinstance(r, dict)]
    open_runs = [r for r in runs if r.get("ended_at") is None]
    for run in reversed(open_runs or runs):
        sock = (run.get("tmux") or {}).get("socket")
        if sock:
            return str(sock)
    return rem.sw.default_socket()


def _has(socket, name, runner):
    try:
        r = runner(["tmux", "-S", socket, "has-session", "-t", f"={name}"],
                   capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return tmux_probe.classify(r.returncode, r.stderr)


def stop_sessions(workspace, worker_id, *, runner=subprocess.run) -> dict:
    """{session name: 'killed' | 'absent' | 'unknown'} for the seat and its companions."""
    socket = _socket(workspace, worker_id)
    seat = wi.tmux_session_name(worker_id)
    out = {}
    for name in [seat + s for s in COMPANION_SUFFIXES] + [seat]:
        if _has(socket, name, runner) is False:
            out[name] = "absent"
            continue
        try:
            runner(["tmux", "-S", socket, "kill-session", "-t", f"={name}"],
                   capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
        out[name] = "killed" if _has(socket, name, runner) is False else "unknown"
    return out


def _close_open_runs(workspace, worker_id) -> list:
    closed = []
    for run in wi.incarnations(workspace, worker_id):
        if isinstance(run, dict) and run.get("ended_at") is None:
            wi.end_incarnation(workspace, worker_id, run["incarnation_id"], "killed")
            closed.append(run["incarnation_id"])
    return closed


def _pause(workspace, worker_id) -> None:
    marker = wi.worker_dir(workspace, worker_id) / sup.PAUSED_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def stop(workspace, target, *, runner=subprocess.run) -> dict:
    """Pause the worker, then take its sessions down. The pause lands under the remedy's
    lock first, so a sweep in flight cannot restart what is being stopped."""
    wid, _ = _worker(workspace, target)
    with rem.pool_lock(workspace, STOP_LOCK_WAIT_S):
        _pause(workspace, wid)
        sessions = stop_sessions(workspace, wid, runner=runner)
        seat = sessions[wi.tmux_session_name(wid)]
        # A seat tmux could not confirm gone keeps its run open, so supervision still sees it.
        closed = _close_open_runs(workspace, wid) if seat in ("killed", "absent") else []
    ok = all(v != "unknown" for v in sessions.values())
    return {"worker_id": wid, "action": "stop", "result": "stopped" if ok else "failed",
            "paused": True, "sessions": sessions, "closed_runs": closed,
            **({} if ok else {"why": "tmux could not confirm every session gone"})}


def retire(workspace, target, *, unbind=False, runner=subprocess.run) -> dict:
    wid, _ = _worker(workspace, target)
    rooms = pr.rooms_bound_to(pr.load_bindings(workspace), wid)
    if rooms and not unbind:
        raise Refused(str(pr.WorkerBound(wid, rooms)) + " (or pass --unbind)")
    for room in rooms:
        pr.unbind_room(workspace, room)
    _, old, _ = pr.set_worker_state(workspace, wid, "retired")
    out = stop(workspace, wid, runner=runner)
    return {**out, "action": "retire", "result": "retired" if out["result"] == "stopped" else "failed",
            "previous_state": old, "unbound": rooms}


def enable(workspace, repo, target, *, start=True, runner=None, spawn=None) -> dict:
    wid, row = _worker(workspace, target)
    old = row.get("state")
    if old == "retired":
        pr.set_worker_state(workspace, wid, "live")
    (wi.worker_dir(workspace, wid) / sup.PAUSED_MARKER).unlink(missing_ok=True)
    out = {"worker_id": wid, "action": "enable", "result": "enabled", "previous_state": old,
           "paused": False}
    if start:
        restarted = rem.restart(workspace, repo, wid, runner=runner, spawn=spawn)
        out["start"] = {"result": restarted["result"], "detail": restarted["detail"]}
        if restarted["result"] == "failed":
            out["result"] = "failed"
    return out


def remove(workspace, target, *, runner=subprocess.run) -> dict:
    wid, row = _worker(workspace, target)
    if row.get("state") != "retired":
        raise Refused(f"worker {wid} is {row.get('state')!r}; retire it before removing it")
    sessions = stop_sessions(workspace, wid, runner=runner)
    if any(v == "unknown" for v in sessions.values()):
        return {"worker_id": wid, "action": "remove", "result": "failed", "sessions": sessions,
                "why": "tmux could not confirm every session gone; the row was kept"}
    _close_open_runs(workspace, wid)
    pr.remove_worker(workspace, wid)
    return {"worker_id": wid, "action": "remove", "result": "removed", "sessions": sessions,
            "label": pr.display_label(row, wid)}


def listing(workspace, *, runner=subprocess.run) -> list:
    roster = pr.load_roster(workspace)
    if roster is None:
        raise Refused("no readable roster; no workers to list")
    rows = []
    for wid, row in sorted((roster.get("workers") or {}).items()):
        if not isinstance(row, dict):
            continue
        alive = _has(_socket(workspace, wid), wi.tmux_session_name(wid), runner)
        rows.append({"worker_id": wid, "name": pr.display_label(row, wid),
                     "state": row.get("state"), "paused": sup.is_paused(workspace, wid),
                     "session_alive": alive,
                     "rooms": pr.rooms_bound_to(roster.get("bindings") or {}, wid)})
    return rows


def main(argv=None, *, runner=subprocess.run) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", default=None)
    p.add_argument("--repo", default=str(_REPO))
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--json", action="store_true")
    for name in ("stop", "retire", "enable", "remove"):
        sp = sub.add_parser(name)
        sp.add_argument("worker")
        if name == "retire":
            sp.add_argument("--unbind", action="store_true",
                            help="unbind the rooms bound to it instead of refusing")
        if name == "enable":
            sp.add_argument("--no-start", action="store_true",
                            help="lift the stop/retire without starting a session")
    a = p.parse_args(argv)
    ws = Path(a.workspace) if a.workspace else resolve_workspace()
    try:
        if a.cmd == "list":
            rows = listing(ws, runner=runner)
            if a.json:
                print(json.dumps(rows, sort_keys=True))
            else:
                for r in rows:
                    print(f"{r['name']} ({r['worker_id']})  state={r['state']} paused={r['paused']} "
                          f"session={r['session_alive']} rooms={len(r['rooms'])}")
            return 0
        if a.cmd == "stop":
            out = stop(ws, a.worker, runner=runner)
        elif a.cmd == "retire":
            out = retire(ws, a.worker, unbind=a.unbind, runner=runner)
        elif a.cmd == "enable":
            out = enable(ws, a.repo, a.worker, start=not a.no_start,
                         runner=None if runner is subprocess.run else runner)
        else:
            out = remove(ws, a.worker, runner=runner)
    except (Refused, pr.RosterError, wi.IdentityError, ValueError) as e:
        print(json.dumps({"worker": getattr(a, "worker", None), "action": a.cmd,
                          "result": "refused", "why": str(e)}, sort_keys=True))
        return 2
    except rem.PoolBusy as e:
        out = {"worker": a.worker, "action": a.cmd, "result": "failed", "why": str(e)}
    print(json.dumps(out, sort_keys=True))
    return 1 if out.get("result") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
