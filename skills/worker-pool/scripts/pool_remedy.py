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
import shlex
import shutil
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
wc = _sibling("pool_wedge_cards")
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


INPUT_WATCH_SUFFIX = "-input"
WATCHING = "watching"


seat_label = wc.seat_label


def input_watch_command(workspace, repo, worker_id, socket, seat) -> list:
    """The tmux argv for this seat's input watcher. `--socket=` is spelled joined so
    the core launchers' `--socket <sock>` liveness match never mistakes it for theirs."""
    name = wi.tmux_session_name(worker_id)
    out = Path(workspace) / "state" / f"core-supervisor.{name}.json"
    tmux = shutil.which("tmux") or "tmux"
    watch = shlex.join([sys.executable, str(Path(repo) / "src" / "core-input-watch.py"),
                        f"--socket={socket}", f"--session={name}", f"--out={out}",
                        f"--seat={seat}"])
    alive = f"{shlex.quote(tmux)} -S {shlex.quote(str(socket))} has-session -t {shlex.quote('=' + name)}"
    # The watcher ends with its seat; the next tick starts one for a new session.
    loop = (f"{watch} & w=$!; while {alive} 2>/dev/null && kill -0 $w 2>/dev/null; "
            f"do sleep 30; done; kill $w 2>/dev/null")
    return [tmux, "-S", str(socket), "new-session", "-d", "-s", name + INPUT_WATCH_SUFFIX,
            "bash", "-c", loop]


def ensure_input_watch(workspace, repo, worker_id, *, runner=None) -> dict:
    """One core-input-watch per live worker seat, idempotent by its tmux session,
    so a gate on the worker's pane reaches the owner as a card naming the seat."""
    run = runner or subprocess.run
    socket, name = sup._open_tmux(workspace, worker_id)
    if not socket:
        return {"worker_id": worker_id, "outcome": NOT_RUNNING}

    def has(session):
        try:
            return run(["tmux", "-S", str(socket), "has-session", "-t", f"={session}"],
                       capture_output=True, text=True, timeout=15).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return None

    try:
        if has(name + INPUT_WATCH_SUFFIX):
            return {"worker_id": worker_id, "outcome": WATCHING}
        if has(name) is not True:
            return {"worker_id": worker_id, "outcome": NOT_RUNNING}
        cmd = input_watch_command(workspace, repo, worker_id, socket,
                                  seat_label(workspace, worker_id))
        r = run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"worker_id": worker_id, "outcome": SUPERVISOR_FAILED, "why": str(e)}
    if r.returncode != 0:
        return {"worker_id": worker_id, "outcome": SUPERVISOR_FAILED,
                "why": (r.stderr or r.stdout or "").strip()[-300:]}
    return {"worker_id": worker_id, "outcome": WATCHING, "started": True}


def ensure_input_watches(workspace, repo, observations: dict, *, runner=None) -> dict:
    return {w: ensure_input_watch(workspace, repo, w, runner=runner)
            for w, o in observations.items() if o.get("session_alive") is True}


def apply(workspace, repo, decisions: dict, *, runner=None, spawn=None) -> dict:
    """Act on a tick's decisions. `recover` resumes the session; `rearm_watcher`
    ensures the inbox's supervisor, whose standby arms once no session watcher
    holds the inbox. A wedge card is raised and never acts on the session: only a
    dead session is ever respawned. `escalate` is returned untouched, because
    asking the owner is the core's, not a timer's."""
    done = {}
    rearms = {}
    cards = {}
    for worker_id, decision in decisions.items():
        if decision == ps.RECOVER:
            done[worker_id] = recover(workspace, repo, worker_id,
                                      runner=runner, spawn=spawn)
        elif decision in (ps.CARD_CAUSE, ps.CARD_FROZEN):
            cards[worker_id] = wc.raise_card(workspace, worker_id, decision,
                                             runner=runner or subprocess.run)
        elif decision == ps.REARM_WATCHER:
            rearms[worker_id] = ensure_supervisor(workspace, repo, worker_id,
                                                  runner=runner)
    return {"recoveries": done,
            "rearms": rearms,
            "cards": cards,
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
    acted = ({"recoveries": {}, "rearms": {}, "cards": {}, "escalations": [], "dry_run": True}
             if a.dry_run else apply(a.workspace, a.repo, tick["decisions"]))
    if not a.dry_run:
        acted["supervisors"] = ensure_supervisors(a.workspace, a.repo, tick["observations"])
        acted["input_watches"] = ensure_input_watches(a.workspace, a.repo, tick["observations"])
        acted["escapes"] = wc.drive_escapes(a.workspace)
        clear = {w for w, o in tick["observations"].items()
                 if o.get("session_alive") is True and w not in tick.get("wedged", [])}
        acted["cards_closed"] = wc.resolve_cleared(a.workspace, clear)
    print(json.dumps({"decisions": tick["decisions"], **acted}, indent=2, sort_keys=True))
    failed = [w for w, r in acted["recoveries"].items() if r["outcome"] == FAILED]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
