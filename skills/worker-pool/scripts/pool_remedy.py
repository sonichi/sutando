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
import os
import subprocess
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
pd = _sibling("pool_delivery")
ps, wi = sup.ps, sup.wi

RECOVERED, ALREADY_RUNNING, INDETERMINATE, PAUSED, NO_SESSION, FAILED = (
    "recovered", "already-running", "indeterminate", "paused", "no-recorded-session",
    "failed")


def _last_run(workspace, worker_id) -> dict:
    runs = wi.incarnations(workspace, worker_id)
    return runs[-1] if runs else {}


def close_dead_runs(workspace, worker_id, closed: list | None = None) -> list:
    """The ladder established death, so every run still open is over. Left open,
    the next probe reads the newest of them as the worker's live run.
    `closed` is appended in place, so a caller sees the partial list if a write fails."""
    closed = [] if closed is None else closed
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
    if probe != "absent":
        # tmux could not answer, so the worker may be alive: nothing is written.
        # Closing its runs here would leave it with none, invisible to supervision.
        return {"worker_id": worker_id, "outcome": INDETERMINATE, "probe": probe}

    closed: list = []
    try:
        close_dead_runs(workspace, worker_id, closed)
        out = spawn(workspace, repo, **kw)
    except (sw.SpawnRefused, sw.SpawnRetained, OSError) as e:
        return {"worker_id": worker_id, "outcome": FAILED, "why": str(e),
                "closed_runs": closed}
    return {"worker_id": worker_id, "outcome": RECOVERED, "closed_runs": closed,
            "session_id": out.get("runtime_session_id")}


SUPERVISOR_SCRIPT = "skills/worker-pool/scripts/worker-watcher-supervisor.sh"
SUPERVISED, NOT_RUNNING, HELD, SUPERVISOR_FAILED = "supervised", "not-running", "held", "supervisor-failed"


def ensure_supervisor(workspace, repo, worker_id, *, runner=None) -> dict:
    """Make sure this worker's inbox has its watcher supervisor. Idempotent by the
    script's own has-session check; a worker whose session is gone gets none."""
    run = runner or subprocess.run
    last = _last_run(workspace, worker_id)
    socket = (last.get("tmux") or {}).get("socket") or ""
    env = {**os.environ,
           "SUTANDO_INSTANCE_ID": worker_id,
           "SUTANDO_TASKS_DIR": str(pd.deliveries_dir(workspace, worker_id)),
           "SUTANDO_TMUX_SESSION": wi.tmux_session_name(worker_id),
           "SUTANDO_INBOX_KIND": "deliveries",
           "SUTANDO_WORKSPACE_DIR": str(workspace),
           "SUTANDO_RESULTS_DIR": str(pd.results_dir(workspace)),
           # The timer's env carries none of the spawner's; the standby watcher needs
           # the resolver or it announces nothing for a delivery inbox.
           "SUTANDO_INBOX_RESOLVER": str(Path(repo) / "skills" / "worker-pool" / "scripts" / "resolve-inbox-entry")}
    if socket:
        env["SUTANDO_TMUX_SOCKET"] = socket
    try:
        r = run(["bash", str(Path(repo) / SUPERVISOR_SCRIPT)], env=env,
                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"worker_id": worker_id, "outcome": SUPERVISOR_FAILED, "why": str(e)}
    if r.returncode == 0:
        return {"worker_id": worker_id, "outcome": SUPERVISED}
    if r.returncode == 3:
        return {"worker_id": worker_id, "outcome": NOT_RUNNING}
    if r.returncode == 4:
        return {"worker_id": worker_id, "outcome": HELD}
    return {"worker_id": worker_id, "outcome": SUPERVISOR_FAILED,
            "why": (r.stderr or r.stdout or "").strip()[-300:]}


def ensure_supervisors(workspace, repo, observations: dict, *, runner=None) -> dict:
    """One ensure per worker whose session answered alive this tick: a session
    that is gone is the recover rung's business, not a supervisor's."""
    return {w: ensure_supervisor(workspace, repo, w, runner=runner)
            for w, o in observations.items() if o.get("session_alive") is True}


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
    if not a.dry_run:
        acted["supervisors"] = ensure_supervisors(a.workspace, a.repo, tick["observations"])
    print(json.dumps({"decisions": tick["decisions"], **acted}, indent=2, sort_keys=True))
    failed = [w for w, r in acted["recoveries"].items() if r["outcome"] == FAILED]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
