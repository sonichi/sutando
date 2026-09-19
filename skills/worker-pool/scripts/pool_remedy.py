#!/usr/bin/env python3
"""The remedy for one supervision decision: bring a dead worker back on its own session.

`pool_supervision` decides and `pool_supervise` observes; this acts. It is the
design's pre-authorised restart — deterministic, so it still works while the core
is stalled or out of quota — and it is only ever a RESUME: the worker keeps its
id, its label, its inbox and its conversation. Moving a task to another executor
is the owner's decision and is not reachable from here.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _sibling(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(_HERE))
sup = _sibling("pool_supervise")
sw = _sibling("spawn_worker")
ps, wi = sup.ps, sup.wi

RECOVERED, ALREADY_RUNNING, PAUSED, NO_SESSION, FAILED = (
    "recovered", "already-running", "paused", "no-recorded-session", "failed")


def _last_run(workspace, worker_id) -> dict:
    runs = wi.incarnations(workspace, worker_id)
    return runs[-1] if runs else {}


def close_dead_runs(workspace, worker_id) -> list:
    """The ladder established death, so every run still open is over. Left open,
    the next probe reads the newest of them as the worker's live run."""
    closed = []
    for run in wi.incarnations(workspace, worker_id):
        if run.get("ended_at") is None:
            wi.end_incarnation(workspace, worker_id, run["incarnation_id"], "crashed")
            closed.append(run["incarnation_id"])
    return closed


def recover(workspace, repo, worker_id, *, runner=None, spawn=None) -> dict:
    """Resume one worker. Never raises for an expected refusal: the caller is a
    timer with nobody watching, so every outcome is a value it can record."""
    spawn = spawn or sw.spawn
    if sup.is_paused(workspace, worker_id):
        return {"worker_id": worker_id, "outcome": PAUSED}

    sessions = wi.sessions(workspace, worker_id)
    if not sessions:
        return {"worker_id": worker_id, "outcome": NO_SESSION}
    session = sessions[-1]
    last = _last_run(workspace, worker_id)
    # The socket the worker LAST ran on, never the environment's default: a timer
    # has no SUTANDO_TMUX_SOCKET, and the default names a server that is not there.
    socket = (last.get("tmux") or {}).get("socket") or None

    kw = {"resume": session["session_id"], "socket": socket,
          "cwd": (session.get("transcript") or {}).get("cwd") or str(repo)}
    if runner is not None:
        kw["runner"] = runner

    probe = sw.session_state(wi.tmux_session_name(worker_id), socket,
                             **({"runner": runner} if runner is not None else {}))
    if probe == "exists":
        return {"worker_id": worker_id, "outcome": ALREADY_RUNNING}

    closed = close_dead_runs(workspace, worker_id)
    try:
        out = spawn(workspace, repo, **kw)
    except (sw.SpawnRefused, sw.SpawnRetained) as e:
        return {"worker_id": worker_id, "outcome": FAILED, "why": str(e),
                "closed_runs": closed}
    return {"worker_id": worker_id, "outcome": RECOVERED, "closed_runs": closed,
            "session_id": out.get("runtime_session_id")}


def apply(workspace, repo, decisions: dict, *, runner=None, spawn=None) -> dict:
    """Act on a tick's decisions. Only `recover` acts; `escalate` is returned
    untouched, because asking the owner is the core's, not a timer's."""
    done = {}
    for worker_id, decision in decisions.items():
        if decision == ps.RECOVER:
            done[worker_id] = recover(workspace, repo, worker_id,
                                      runner=runner, spawn=spawn)
    return {"recoveries": done,
            "escalations": sorted(w for w, d in decisions.items() if d == ps.ESCALATE)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--recipient", help="check and remedy one worker")
    p.add_argument("--sweep", action="store_true", help="check and remedy the pool")
    p.add_argument("--dry-run", action="store_true",
                   help="decide and report, but neither remedy nor advance the ladder")
    a = p.parse_args(argv)
    if bool(a.recipient) == bool(a.sweep):
        p.error("pass exactly one of --recipient or --sweep")
    try:
        tick = sup.tick(a.workspace, time.time(),
                        worker_ids=[a.recipient] if a.recipient else None,
                        persist=not a.dry_run)
    except (wi.IdentityError, ValueError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    acted = ({"recoveries": {}, "escalations": [], "dry_run": True} if a.dry_run
             else apply(a.workspace, a.repo, tick["decisions"]))
    print(json.dumps({"decisions": tick["decisions"], **acted}, indent=2, sort_keys=True))
    failed = [w for w, r in acted["recoveries"].items() if r["outcome"] == FAILED]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
