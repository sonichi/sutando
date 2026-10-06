#!/usr/bin/env python3
"""The remedy for one supervision decision: bring a dead worker back on its own id.

`pool_supervision` decides and `pool_supervise` observes; this acts. It is the
design's pre-authorised restart — deterministic, so it still works while the core
is stalled or out of quota. The worker keeps its id, label and inbox. Claude
resumes its conversation; Codex starts a fresh one because its CLI does not
accept a caller-chosen new session id. Moving a task to another executor is
the owner's decision and is not reachable from here.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import replace
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
sys.path.insert(0, str(_HERE.parents[2] / "src"))
import pool_suspension  # noqa: E402
sup = _sibling("pool_supervise")
sw = _sibling("spawn_worker")
pd = _sibling("pool_delivery")
wc = _sibling("pool_wedge_cards")
ps, wi = sup.ps, sup.wi

RECOVERED, ALREADY_RUNNING, INDETERMINATE, PAUSED, NO_SESSION, FAILED = (
    "recovered", "already-running", "indeterminate", "paused", "no-recorded-session",
    "failed")
SUSPENDED = "suspended"
SUSPENDED_REL = pool_suspension.REL


def suspended_path(workspace) -> Path:
    return pool_suspension.path(workspace)


def _marker(workspace) -> dict | None:
    """The suspension record {reason, at, stopped}, or None when not suspended."""
    try:
        return pool_suspension.read(workspace)
    except OSError:
        return None


def suspension(workspace) -> str | None:
    """The recorded reason the pool is suspended, or None. Never expires: only
    --resume lifts it."""
    rec = _marker(workspace)
    if rec is None:
        return None
    return f"{rec.get('reason') or SUSPENDED} {rec.get('at') or ''}".strip()


def suspend(workspace, reason: str, now: float | None = None) -> str:
    """Suspend, recording which workers the stop takes down: every supervised worker not
    already in a death episode. Read from the ladder's own file, so no probe delays a quit."""
    try:
        state = sup.load_state(workspace)
        in_episode = {w for w, e in state.workers.items() if e.consecutive or e.escalated}
        stopped = sorted(w for w in sup.supervised_workers(workspace)
                         if w not in in_episode and not sup.is_paused(workspace, w))
    except Exception:  # noqa: BLE001 — an unreadable pool must never leave the stop unsuspended
        stopped = []
    path = suspended_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps({"reason": reason, "at": int(now if now is not None else time.time()),
                               "stopped": stopped}) + "\n")
    os.replace(tmp, path)
    return suspension(workspace)


def resume(workspace, repo, *, runner=None, spawn=None) -> dict:
    """Lift a suspension and bring back, now, the workers the stop took down: a deliberate
    quit is not a failure, so they skip the death ladder. A worker already dead or escalated
    before the stop is left to its own ladder."""
    rec = _marker(workspace)
    was = suspension(workspace)
    suspended_path(workspace).unlink(missing_ok=True)
    restarted = {}
    if rec is not None:
        stopped = set(rec.get("stopped") or [])
        obs = sup.observe(workspace, time.time(), runner=runner or subprocess.run)
        dead = [w for w, o in obs.items()
                if w in stopped and o.session_alive is False and not o.paused]
        restarted = {w: recover(workspace, repo, w, runner=runner, spawn=spawn) for w in dead}
        _reset_ladders(workspace, forget=restarted)
    return {"was_suspended": was, "restarted": restarted}


LOCK_REL = Path("state") / "pool-remedy.lock"
RESTART_LOCK_WAIT_S = 45.0


class PoolBusy(Exception):
    pass


@contextlib.contextmanager
def pool_lock(workspace, timeout: float | None = None):
    """Exclusive for one act-and-save (a sweep, a resume, a restart), so a click and a sweep
    never recover the same worker or save over each other's ladder. None waits indefinitely."""
    path = Path(workspace) / LOCK_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | (0 if deadline is None else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PoolBusy(f"pool busy: another remedy held the lock for {timeout:g}s") from None
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _reset_ladders(workspace, *, forget=(), death_only=()) -> None:
    """A new session makes all evidence about the old one stale (`forget`); a session that was
    already alive keeps its wedge and watcher ladders and loses only its death ladder."""
    state = sup.load_state(workspace)
    state = ps.clear_death_ladder(state, [w for w in death_only if w not in forget])
    sup.save_state(workspace, replace(state, workers={
        w: e for w, e in state.workers.items() if w not in forget}))


RESTART_RESULTS = {RECOVERED: "restarted", ALREADY_RUNNING: "already-running", PAUSED: "paused",
                   SUSPENDED: "suspended"}


FAILED_WHY = {NO_SESSION: "no recorded session to resume", INDETERMINATE: "tmux could not answer",
              FAILED: "the spawn failed"}


def restart(workspace, repo, worker_id, *, runner=None, spawn=None,
            lock_timeout: float = RESTART_LOCK_WAIT_S) -> dict:
    """The owner's restart of one dead worker, outside the death ladder. Idempotent: a live
    worker, wedged or not, is left alone and keeps its wedge and watcher ladders."""
    wi.worker_dir(workspace, worker_id)  # raises IdentityError for a malformed id
    try:
        with pool_lock(workspace, lock_timeout):
            if suspension(workspace):
                out = {"worker_id": worker_id, "outcome": SUSPENDED}
            else:
                out = recover(workspace, repo, worker_id, runner=runner, spawn=spawn)
            result = RESTART_RESULTS.get(out["outcome"], "failed")
            detail = {k: v for k, v in out.items() if k not in ("worker_id", "outcome")}
            if result == "failed":
                detail["outcome"] = out["outcome"]
                detail.setdefault("why", FAILED_WHY.get(out["outcome"], out["outcome"]))
            elif result in ("restarted", "already-running"):
                _after_restart(workspace, repo, worker_id, result, detail, runner)
    except PoolBusy as e:
        result, detail = "failed", {"why": str(e)}
    return {"worker_id": worker_id, "result": result, "detail": detail}


def _after_restart(workspace, repo, worker_id, result, detail, runner) -> None:
    """Bookkeeping after the session is up: a failure here is reported, never raised, since
    the worker's state no longer depends on it and the next sweep repeats it."""
    steps = (("ladder", lambda: _reset_ladders(
                 workspace, **({"forget": [worker_id]} if result == "restarted" else {"death_only": [worker_id]}))),
             ("supervisor", lambda: ensure_supervisor(workspace, repo, worker_id, runner=runner)["outcome"]),
             ("input_watch", lambda: ensure_input_watch(workspace, repo, worker_id, runner=runner)["outcome"]))
    for name, step in steps:
        try:
            value = step()
        except Exception as e:  # noqa: BLE001 — the app's button reads one JSON line, never a traceback
            detail.setdefault("errors", {})[name] = f"{type(e).__name__}: {e}"
        else:
            if value is not None:
                detail[name] = value


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
    """Recover one worker. Never raises for an expected refusal: the caller is a
    timer with nobody watching, so every outcome is a value it can record."""
    spawn = spawn or sw.spawn
    if sup.is_paused(workspace, worker_id):
        return {"worker_id": worker_id, "outcome": PAUSED}

    sessions = wi.sessions(workspace, worker_id)
    last = _last_run(workspace, worker_id)
    # The socket the worker LAST ran on, never the environment's default: a timer
    # has no SUTANDO_TMUX_SOCKET, and the default names a server that is not there.
    socket = (last.get("tmux") or {}).get("socket") or None

    roster = sw.pr.load_roster(workspace)
    row = ((roster or {}).get("workers") or {}).get(worker_id)
    if not isinstance(row, dict):
        return {"worker_id": worker_id, "outcome": INDETERMINATE,
                "why": "worker is absent from the readable roster"}
    runtime = row.get("runtime") or "claude"
    if runtime not in sw.WORKER_MODE_RUNTIMES:
        return {"worker_id": worker_id, "outcome": INDETERMINATE,
                "why": f"unknown worker runtime {runtime!r}"}
    if runtime == "codex":
        if not last:
            return {"worker_id": worker_id, "outcome": NO_SESSION}
        kw = {"existing_worker_id": worker_id, "runtime": "codex",
              "socket": socket, "cwd": last.get("cwd") or str(repo)}
    else:
        if not sessions:
            return {"worker_id": worker_id, "outcome": NO_SESSION}
        session = sessions[-1]
        kw = {"resume": session["session_id"], "runtime": "claude", "socket": socket,
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
    roster = sw.pr.load_roster(workspace)
    row = ((roster or {}).get("workers") or {}).get(worker_id)
    if not isinstance(row, dict):
        return {"worker_id": worker_id, "outcome": INDETERMINATE,
                "why": "worker is absent from the readable roster"}
    runtime = row.get("runtime") or "claude"
    if runtime not in sw.WORKER_MODE_RUNTIMES:
        return {"worker_id": worker_id, "outcome": INDETERMINATE,
                "why": f"unknown worker runtime {runtime!r}"}
    env = {**os.environ,
           "SUTANDO_PY": sys.executable,
           "SUTANDO_INSTANCE_ID": worker_id,
           "SUTANDO_WORKER_RUNTIME": runtime,
           "SUTANDO_TASKS_DIR": str(pd.deliveries_dir(workspace, worker_id)),
           "SUTANDO_TMUX_SESSION": wi.tmux_session_name(worker_id),
           "SUTANDO_INBOX_KIND": "deliveries",
           "SUTANDO_WORKSPACE_DIR": str(workspace),
           "SUTANDO_RESULTS_DIR": str(pd.results_dir(workspace)),
           # The timer's env carries none of the spawner's; the standby watcher needs
           # the resolver or it announces nothing for a delivery inbox.
           "SUTANDO_INBOX_RESOLVER": str(Path(repo) / "skills" / "worker-pool" / "scripts" / "resolve-inbox-entry"),
           "SUTANDO_POOL_DELIVERY_SCRIPT": str(Path(repo) / "skills" / "worker-pool" / "scripts" / "pool_delivery.py")}
    if runtime == "codex":
        env["SUTANDO_TASK_EVENT_HANDLER"] = ""
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
    dead session is ever respawned, and a logged-out one is carded for its /login
    because a fresh session meets the same expired login. `escalate` is returned
    untouched, because asking the owner is the core's, not a timer's."""
    done = {}
    rearms = {}
    cards = {}
    for worker_id, decision in decisions.items():
        # Re-read per action: an app quit can land mid-sweep, just before its tmux kill-server.
        if decision != ps.ESCALATE and suspension(workspace):
            if decision == ps.RECOVER:
                done[worker_id] = {"worker_id": worker_id, "outcome": SUSPENDED}
            continue
        if decision == ps.RECOVER:
            done[worker_id] = recover(workspace, repo, worker_id,
                                      runner=runner, spawn=spawn)
        elif decision in (ps.CARD_CAUSE, ps.CARD_FROZEN, ps.CARD_LOGIN):
            cards[worker_id] = wc.raise_card(workspace, worker_id, decision,
                                             runner=runner or subprocess.run)
        elif decision == ps.REARM_WATCHER:
            rearms[worker_id] = ensure_supervisor(workspace, repo, worker_id,
                                                  runner=runner)
    sup.acknowledge_cards(workspace, [w for w, r in cards.items() if r.get("outcome") == "carded"])
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
    p.add_argument("--suspend", metavar="REASON",
                   help="stop remedying until --resume (the app writes this on a real quit)")
    p.add_argument("--resume", action="store_true",
                   help="lift a suspension, restart the workers it left dead, then sweep")
    p.add_argument("--restart", metavar="WORKER_ID",
                   help="the owner's restart of one worker, outside the death ladder")
    p.add_argument("--dry-run", action="store_true",
                   help="decide and report, but neither remedy nor advance the ladder")
    a = p.parse_args(argv)
    if sum(map(bool, (a.recipient, a.sweep, a.suspend, a.resume, a.restart is not None))) != 1:
        p.error("pass exactly one of --recipient, --sweep, --suspend, --resume or --restart")
    if a.restart is not None and a.dry_run:
        p.error("--restart acts; it has no --dry-run")
    if a.restart is not None:
        rc = 0
        try:
            out = restart(a.workspace, a.repo, a.restart)
        except (wi.IdentityError, ValueError) as e:
            out, rc = {"worker_id": a.restart, "result": "failed", "detail": {"why": str(e)}}, 2
        except Exception as e:  # noqa: BLE001 — the app's button reads one JSON line, never a traceback
            out = {"worker_id": a.restart, "result": "failed", "detail": {"why": f"{type(e).__name__}: {e}"}}
        print(json.dumps(out, sort_keys=True))
        return rc or (1 if out["result"] == "failed" else 0)
    if a.suspend:
        print(json.dumps({"suspended": suspend(a.workspace, a.suspend)}))
        return 0
    with pool_lock(a.workspace):
        return _sweep(a)


def _sweep(a) -> int:
    try:
        resumed = resume(a.workspace, a.repo) if a.resume else None
        held = suspension(a.workspace)
        tick = sup.tick(a.workspace, time.time(),
                        worker_ids=[a.recipient] if a.recipient else None,
                        persist=not a.dry_run and not held)
    except (wi.IdentityError, ValueError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    idle = {"recoveries": {}, "rearms": {}, "cards": {}, "escalations": []}
    escalations = sorted(w for w, d in tick["decisions"].items() if d == ps.ESCALATE)
    acted = ({**idle, "dry_run": True} if a.dry_run
             else {**idle, "escalations": escalations, "suspended": held} if held
             else apply(a.workspace, a.repo, tick["decisions"]))
    if resumed is not None:
        acted["resume"] = resumed
    if not a.dry_run and not held and not suspension(a.workspace):
        acted["supervisors"] = ensure_supervisors(a.workspace, a.repo, tick["observations"])
        acted["input_watches"] = ensure_input_watches(a.workspace, a.repo, tick["observations"])
        acted["escapes"] = wc.drive_escapes(a.workspace)
        clear = {w for w, o in tick["observations"].items()
                 if o.get("session_alive") is True and w not in tick.get("wedged", [])}
        now_asks = {w: ps.WEDGE_CARDS.get(k) for w, k in tick.get("wedge_kinds", {}).items()}
        acted["cards_closed"] = wc.resolve_cleared(a.workspace, clear, wedges=now_asks)
    print(json.dumps({"decisions": tick["decisions"], "auth_expired": tick["auth_expired"],
                      **acted}, indent=2, sort_keys=True))
    outcomes = list(acted["recoveries"].values()) + list(((resumed or {}).get("restarted") or {}).values())
    failed = [r for r in outcomes if r["outcome"] == FAILED]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
