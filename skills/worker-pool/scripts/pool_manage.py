#!/usr/bin/env python3
"""Manage the pool's workers: list, stop, retire, enable, remove.

The owner's (or the agent's, on the owner's behalf) one supported way to take a
worker out of service and bring it back. Each verb goes through the writer that
already owns its fact, so the remedy, the sweep and the router honour it:

    stop    the per-worker pause (pool_supervise.set_paused), then its tmux
            sessions are killed and its open runs closed. A paused worker is
            never restarted: not by a sweep, `--resume` or `--restart`. Work it
            owes stays its own and waits for `enable`.
    retire  stop, then roster state `retired` (pool_roster.set_worker_state):
            not supervised or advertised, and the router sends traffic addressed
            to it to the core. Work it still owes must be handed to the core
            (`--hand-off`, pool_delivery.hand_off_to_core) or the retire is refused.
    enable  lift both, then the owner restart (pool_remedy.restart).
    remove  drop a retired worker's roster row; its records stay on disk.

    python3 pool_manage.py [--workspace WS] [--repo REPO] list [--json]
    python3 pool_manage.py [--workspace WS] stop   <worker id or label> [--force]
    python3 pool_manage.py [--workspace WS] retire <worker> [--unbind] [--hand-off]
    python3 pool_manage.py [--workspace WS] [--repo REPO] enable <worker> [--no-start]
    python3 pool_manage.py [--workspace WS] remove <worker> [--hand-off]

Every verb prints one JSON line. Exit 0 done, 1 failed, 2 refused.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
for _p in (str(_HERE), str(_REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pool_remedy as rem  # noqa: E402

from workspace_default import resolve_workspace  # noqa: E402

sup, wi, sw, pd = rem.sup, rem.wi, rem.sw, rem.pd
pr = sw.pr

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


def _not_self(wid: str, force: bool) -> None:
    """Killing the caller's own seat ends the call before it can record or report anything."""
    if wid == os.environ.get("SUTANDO_INSTANCE_ID") and not force:
        raise Refused(f"{wid} is this session's own worker; ask the core or another session")


def _outstanding(workspace, wid) -> list:
    """Owed task ids, via the supervisor's own reading of what a worker owes."""
    owed = sup.work_outstanding(workspace, wid)
    if owed is None:
        raise Refused(f"cannot tell what {wid} still owes (deliveries or results unreadable)")
    if not owed:
        return []
    return sorted(t for t, _ in _owed_residue(workspace, wid))


def _owed_residue(workspace, wid):
    for p in pd.accepted(Path(workspace), wid) + pd.pending(Path(workspace), wid):
        task_id = pd.parse_sentinel(p.name)[0]
        state = pd.residue(Path(workspace), wid, task_id)
        if state in pd.OWED_RESIDUE:
            yield task_id, state


def stop_sessions(workspace, worker_id, *, runner=subprocess.run) -> dict:
    """{session name: 'killed' | 'absent' | 'unknown'} for the seat and its companions;
    {} for a worker with no recorded run, which has no session to kill."""
    socket = sup.run_socket(workspace, worker_id)
    if socket is None:
        return {}
    seat = wi.tmux_session_name(worker_id)
    out = {}
    for name in [seat + s for s in COMPANION_SUFFIXES] + [seat]:
        if sw.session_state(name, socket, runner=runner) == "absent":
            out[name] = "absent"
            continue
        try:
            runner(["tmux", "-S", socket, "kill-session", "-t", f"={name}"],
                   capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
        out[name] = "killed" if sw.session_state(name, socket, runner=runner) == "absent" else "unknown"
    return out


def _take_down(workspace, wid, runner) -> dict:
    """Pause, kill, close the run. The pause lands under the remedy's lock first, so a
    sweep in flight cannot restart what is being stopped."""
    with rem.pool_lock(workspace, STOP_LOCK_WAIT_S):
        sup.set_paused(workspace, wid, True)
        sessions = stop_sessions(workspace, wid, runner=runner)
        down = all(v != "unknown" for v in sessions.values())
        # A seat tmux could not confirm gone keeps its run open, so supervision still sees it.
        closed = rem.close_dead_runs(workspace, wid, end_reason="killed") if down else []
    return {"sessions": sessions, "closed_runs": closed, "down": down}


def stop(workspace, target, *, force=False, runner=subprocess.run) -> dict:
    wid, _ = _worker(workspace, target)
    _not_self(wid, force)
    owed = _outstanding(workspace, wid)
    if owed and not force:
        raise Refused(f"{wid} still owes {len(owed)} task(s): {', '.join(owed)}; "
                      "pass --force to stop it anyway (they wait for enable), "
                      "or retire --hand-off to give them to the core")
    t = _take_down(workspace, wid, runner)
    return {"worker_id": wid, "action": "stop", "result": "stopped" if t["down"] else "failed",
            "paused": True, "sessions": t["sessions"], "closed_runs": t["closed_runs"],
            "owed_kept": owed,
            **({} if t["down"] else {"why": "tmux could not confirm every session gone"})}


def _hand_off(workspace, wid, owed, hand_off) -> dict:
    if not owed:
        return {"handed_off": [], "kept": {}}
    if not hand_off:
        raise Refused(f"{wid} still owes {len(owed)} task(s): {', '.join(owed)}; "
                      "pass --hand-off to give them to the core")
    return pd.hand_off_to_core(Path(workspace), wid)


def retire(workspace, target, *, unbind=False, hand_off=False, runner=subprocess.run) -> dict:
    """Every refusal comes before any write; the take-down comes before the roster
    changes, so a tmux failure leaves the worker live and bound, only paused."""
    wid, _ = _worker(workspace, target)
    _not_self(wid, False)
    rooms = pr.rooms_bound_to(pr.load_bindings(workspace), wid)
    if rooms and not unbind:
        raise Refused(str(pr.WorkerBound(wid, rooms)) + " (or pass --unbind)")
    owed = _outstanding(workspace, wid)
    if owed and not hand_off:
        _hand_off(workspace, wid, owed, False)
    t = _take_down(workspace, wid, runner)
    base = {"worker_id": wid, "action": "retire", "paused": True,
            "sessions": t["sessions"], "closed_runs": t["closed_runs"]}
    if not t["down"]:
        return {**base, "result": "failed", "why": "tmux could not confirm every session gone; "
                "the worker is paused but still live, bound and owning its work"}
    moved = _hand_off(workspace, wid, owed, hand_off)
    _, old, _, unbound = pr.set_worker_state(workspace, wid, "retired", unbind=unbind)
    return {**base, "result": "retired", "previous_state": old, "unbound": unbound,
            "handed_off": moved["handed_off"], "kept": moved["kept"]}


def enable(workspace, repo, target, *, start=True, runner=None, spawn=None) -> dict:
    wid, row = _worker(workspace, target)
    old = row.get("state")
    if old == "retired":
        pr.set_worker_state(workspace, wid, "live")
    sup.set_paused(workspace, wid, False)
    out = {"worker_id": wid, "action": "enable", "result": "enabled", "previous_state": old,
           "paused": False}
    if start:
        restarted = rem.restart(workspace, repo, wid, runner=runner, spawn=spawn)
        out["start"] = {"result": restarted["result"], "detail": restarted["detail"]}
        if restarted["result"] == "failed":
            out["result"] = "failed"
    return out


def remove(workspace, target, *, hand_off=False, runner=subprocess.run) -> dict:
    wid, row = _worker(workspace, target)
    if row.get("state") != "retired":
        raise Refused(f"worker {wid} is {row.get('state')!r}; retire it before removing it")
    owed = _outstanding(workspace, wid)
    if owed and not hand_off:
        _hand_off(workspace, wid, owed, False)
    t = _take_down(workspace, wid, runner)
    if not t["down"]:
        return {"worker_id": wid, "action": "remove", "result": "failed", "sessions": t["sessions"],
                "why": "tmux could not confirm every session gone; the row was kept"}
    moved = _hand_off(workspace, wid, owed, hand_off)
    pr.remove_worker(workspace, wid)
    return {"worker_id": wid, "action": "remove", "result": "removed", "sessions": t["sessions"],
            "label": pr.display_label(row, wid), "handed_off": moved["handed_off"]}


def listing(workspace, *, runner=subprocess.run) -> list:
    roster = pr.load_roster(workspace)
    if roster is None:
        raise Refused("no readable roster; no workers to list")
    rows = []
    for wid, row in sorted((roster.get("workers") or {}).items()):
        if not isinstance(row, dict):
            continue
        socket = sup.run_socket(workspace, wid)
        state = sw.session_state(wi.tmux_session_name(wid), socket, runner=runner) if socket else "absent"
        rows.append({"worker_id": wid, "name": pr.display_label(row, wid),
                     "state": row.get("state"), "paused": sup.is_paused(workspace, wid),
                     "session_alive": {"exists": True, "absent": False}.get(state),
                     "owes": sup.work_outstanding(workspace, wid),
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
        if name == "stop":
            sp.add_argument("--force", action="store_true",
                            help="stop it although it owes work (the work waits for enable), "
                                 "or although it is this session's own worker")
        if name == "retire":
            sp.add_argument("--unbind", action="store_true",
                            help="unbind the rooms bound to it instead of refusing")
        if name in ("retire", "remove"):
            sp.add_argument("--hand-off", action="store_true",
                            help="give the tasks it still owes to the core instead of refusing")
        if name == "enable":
            sp.add_argument("--no-start", action="store_true",
                            help="lift the stop/retire without starting a session now")
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
                          f"session={r['session_alive']} owes={r['owes']} rooms={len(r['rooms'])}")
            return 0
        if a.cmd == "stop":
            out = stop(ws, a.worker, force=a.force, runner=runner)
        elif a.cmd == "retire":
            out = retire(ws, a.worker, unbind=a.unbind, hand_off=a.hand_off, runner=runner)
        elif a.cmd == "enable":
            out = enable(ws, a.repo, a.worker, start=not a.no_start,
                         runner=None if runner is subprocess.run else runner)
        else:
            out = remove(ws, a.worker, hand_off=a.hand_off, runner=runner)
    except (Refused, pr.RosterError, wi.IdentityError, ValueError) as e:
        print(json.dumps({"worker": getattr(a, "worker", None), "action": a.cmd,
                          "result": "refused", "why": str(e)}, sort_keys=True))
        return 2
    except (rem.PoolBusy, OSError) as e:
        out = {"worker": a.worker, "action": a.cmd, "result": "failed", "why": str(e)}
    print(json.dumps(out, sort_keys=True))
    return 1 if out.get("result") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
