#!/usr/bin/env python3
"""The supervision caller: observe the pool, decide, persist. It never remedies.

Two samplers, one ladder. A delivery-time check runs when someone is actually
waiting on a worker; the design's five-minute sweep covers workers nobody is
addressing. Both feed `pool_supervision.evaluate`, and both share one persisted
state so a worker's clock runs from FIRST detection whichever sampler saw it.

`pool_supervision` decides and this module does the I/O, so the decision stays
testable without a filesystem, a tmux server or a clock.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _sibling(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


ps = _sibling("pool_supervision")
pb = _sibling("pool_beat")
pr = _sibling("pool_roster")
wi = _sibling("worker_identity")
prr = _sibling("pool_routing_receipt")
pd = _sibling("pool_delivery")

_SRC = _HERE.parents[2] / "src"
import cli_wedge as cw  # noqa: E402  (src/ is on the path via pool_delivery)
from delivery import pane_gate, task_dispatch as td  # noqa: E402

STATE_REL = Path("state") / "pool-supervision.json"
# An owner fact, so it is a marker beside the worker's records and not a roster
# state: the core recompiles the roster, and a recompile must never lift a pause.
PAUSED_MARKER = "paused"
# States that are not candidates for recovery at all.
NOT_SUPERVISED = ("retired",)


def state_path(workspace) -> Path:
    return Path(workspace) / STATE_REL


def is_paused(workspace, worker_id) -> bool:
    """Only the owner writes or removes this file; nothing here ever does."""
    return (wi.worker_dir(workspace, worker_id) / PAUSED_MARKER).exists()


def probe_session(workspace, worker_id, *, runner=subprocess.run) -> bool | None:
    """Is this worker's OWN tmux session alive? None when it cannot be told.

    The socket is read from the recorded incarnation, never assumed: a wrong
    socket answers "no server running" for a host whose real socket is fine.
    """
    open_runs = [r for r in wi.incarnations(workspace, worker_id)
                 if isinstance(r, dict) and r.get("ended_at") is None]
    if not open_runs:
        return None
    tmux = open_runs[-1].get("tmux") or {}
    socket = tmux.get("socket")
    name = tmux.get("session_name") or wi.tmux_session_name(worker_id)
    if not socket:
        return None
    try:
        # `=<name>`: a bare name prefix-matches, so a short id can match the
        # wrong worker's session (worker_identity.tmux_session_name).
        done = runner(["tmux", "-S", str(socket), "has-session", "-t", f"={name}"],
                      capture_output=True, text=True)
    except (OSError, ValueError):
        return None
    if done.returncode == 0:
        return True
    err = (done.stderr or "").lower()
    if "no server" in err or "can't find session" in err or "session not found" in err:
        return False
    return None


def _open_tmux(workspace, worker_id) -> tuple[str | None, str]:
    """(socket, session) of the worker's open run; socket None when there is none."""
    open_runs = [r for r in wi.incarnations(workspace, worker_id)
                 if isinstance(r, dict) and r.get("ended_at") is None]
    tmux = (open_runs[-1].get("tmux") or {}) if open_runs else {}
    return tmux.get("socket") or None, tmux.get("session_name") or wi.tmux_session_name(worker_id)


def work_outstanding(workspace, worker_id) -> bool | None:
    """Does this worker owe a reply? What task_dispatch says was handed to it, less
    what already has a ready result (live or archived). None when undecidable."""
    try:
        owned = td.owned_task_ids(pd.deliveries_dir(workspace, worker_id).parent, worker_id)
        results = pd.results_dir(workspace)
        return any(not td.has_ready_result(results, f"{t}.txt") for t in owned)
    except (OSError, ValueError):
        return None


def classify_pane_text(text: str | None, runtime: str = "claude", *, workspace=None,
                       socket=None, session=None) -> str:
    """One capture as a pool_supervision PANE_* kind. The gate and the abnormal
    text are the core's readers (pane_gate, cli_wedge); only the mapping is here."""
    if not text or not text.strip():
        return ps.PANE_UNKNOWN
    adapter = pane_gate.ADAPTERS.get(runtime, pane_gate.CLAUDE)
    verdict = pane_gate.classify_pane(text, adapter, workspace, socket, session)
    if verdict.state == "abnormal":
        abn = cw.frame_abnormal(text)
        return ps.PANE_LIMIT if abn and abn.kind == "provider-limit" else ps.PANE_ABNORMAL
    if verdict.state == "busy":
        return ps.PANE_WORKING if verdict.reason == "working" else ps.PANE_GATE
    if verdict.state in ("idle-ready", "pending"):
        return ps.PANE_IDLE
    return ps.PANE_UNKNOWN


def observe_pane(workspace, worker_id, runtime: str = "claude", *,
                 runner=subprocess.run) -> tuple[str | None, str | None]:
    """(pane kind, raw frame id) of this worker's pane; (None, None) when unread."""
    socket, name = _open_tmux(workspace, worker_id)
    if not socket:
        return None, None
    try:
        done = runner(["tmux", "-S", str(socket), "capture-pane", "-p", "-t", f"={name}:0"],
                      capture_output=True, text=True, timeout=8)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None, None
    text = done.stdout if done.returncode == 0 else None
    if not text or not text.strip():
        return None, None
    return (classify_pane_text(text, runtime, workspace=workspace, socket=socket, session=name),
            cw.raw_state_id(text))


def supervised_workers(workspace) -> dict:
    """Worker rows the pool may recover, from the roster. Empty when unreadable —
    a caller must not invent a pool from a missing file.
    """
    roster = pr.load_roster(workspace)
    if not roster:
        return {}
    out = {}
    for wid, row in (roster.get("workers") or {}).items():
        if wid == getattr(pr, "CORE", "core"):
            continue
        if not isinstance(row, dict):
            continue
        if row.get("state") in NOT_SUPERVISED:
            continue
        out[wid] = row
    return out


def observe(workspace, now: float, *, worker_ids=None,
            runner=subprocess.run) -> dict:
    """One Observation per supervised worker."""
    rows = supervised_workers(workspace)
    if worker_ids is not None:
        for w in worker_ids:
            wi.worker_dir(workspace, w)     # a malformed id is refused, not "unknown"

        # Naming a worker narrows the supervised set; it never widens it. A
        # retired or unknown id must not reach the ladder through this door.
        rows = {w: rows[w] for w in worker_ids if w in rows}
    obs = {}
    for wid, row in rows.items():
        session_alive = probe_session(workspace, wid, runner=runner)
        watcher_beat = pb.classify(pb.beat_path(workspace, "watcher", wid), now)
        # The process table is read only when the beat cannot answer: a live
        # beat proves the watcher, and a dead session has no watcher ladder.
        held = None
        if session_alive is True and watcher_beat != pb.LIVE:
            held = session_watcher_holds(workspace, wid, runner=runner)
        pane, pane_id = (observe_pane(workspace, wid, row.get("runtime") or "claude",
                                      runner=runner)
                         if session_alive is True else (None, None))
        obs[wid] = ps.Observation(
            beat=pb.classify(pb.beat_path(workspace, "worker", wid), now),
            session_alive=session_alive,
            paused=is_paused(workspace, wid),
            watcher_beat=watcher_beat,
            watcher_held=held,
            work_outstanding=work_outstanding(workspace, wid),
            pane=pane,
            pane_id=pane_id,
        )
    return obs


def session_watcher_holds(workspace, worker_id, *, runner=subprocess.run) -> bool | None:
    """Does a session-role watcher provably serve this worker's inbox?

    True: one holds it. False: the table was read, nothing undecidable was seen,
    and no session-role holder exists. None: unobservable, or undecided lines
    remain, because refusing evidence is cheaper than a re-arm on a watcher that
    merely could not be read.
    """
    inbox = pd.deliveries_dir(workspace, worker_id)
    try:
        done = runner([sys.executable, str(_SRC / "watcher_identity.py"),
                       "inbox-holders", "--inbox", str(inbox)],
                      capture_output=True, text=True, timeout=15)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    roles = [line.split()[1] for line in (done.stdout or "").splitlines()
             if len(line.split()) == 2]
    if "session" in roles:
        return True
    if "undecided=" in (done.stderr or ""):
        return None
    return False


def routing_status(workspace) -> dict:
    """Is this host routing at all? Bindings say it should; the handler's receipt
    says whether the watcher ever consults it. A task the core processed after
    the last receipt went past the handler — which is the core answering for a
    worker, the failure the pool exists to prevent, and it is silent otherwise.
    """
    roster = pr.load_roster(workspace) or {}
    core = getattr(pr, "CORE", "core")
    # A binding is {room: target}; a room pinned to the core is not one a worker owes.
    bound = sorted(room for room, target in (roster.get("bindings") or {}).items()
                   if any(t != core for t in (target if isinstance(target, list) else [target])))
    receipt = prr.read(workspace)
    consulted = receipt["consulted_at"] if receipt else None
    newest, newest_id = None, None
    # The receipt is stamped before the task it names is routed, so that task's own
    # archive is always newer than its consult; every OTHER task past it is unrouted.
    own = receipt.get("task_id") if receipt else None
    unrouted = None
    for f in (Path(workspace) / "tasks" / "archive").glob("task-*.txt"):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest, newest_id = mtime, f.stem
        if (consulted is not None and mtime > consulted and f.stem != own
                and (unrouted is None or mtime > unrouted[0])):
            unrouted = (mtime, f.stem)
    alarm = None
    if bound and consulted is None:
        alarm = (f"unrouted: {len(bound)} room(s) bound to workers, but the route handler "
                 "has never been consulted on this host — the watcher runs without it")
    elif bound and unrouted is not None:
        alarm = (f"unrouted: {unrouted[1]} arrived at {unrouted[0]:.0f} after the route handler "
                 f"was last consulted at {consulted:.0f} — the core processed it unrouted")
    return {"bound_rooms": bound, "handler_consulted_at": consulted,
            "newest_task_at": newest, "newest_task_id": newest_id, "alarm": alarm}


def load_state(workspace) -> ps.SupervisionState:
    try:
        raw = json.loads(state_path(workspace).read_text())
    except (FileNotFoundError, NotADirectoryError, ValueError, OSError):
        return ps.SupervisionState()
    if not isinstance(raw, dict):
        return ps.SupervisionState()
    workers = {}
    for wid, ev in (raw.get("workers") or {}).items():
        if isinstance(ev, dict):
            workers[wid] = ps.WorkerEvidence(
                first_detected_at=ev.get("first_detected_at"),
                consecutive=int(ev.get("consecutive") or 0),
                recover_issued_at=ev.get("recover_issued_at"),
                escalated=bool(ev.get("escalated")),
                watcher_first_detected_at=ev.get("watcher_first_detected_at"),
                watcher_consecutive=int(ev.get("watcher_consecutive") or 0),
                rearm_issued_at=ev.get("rearm_issued_at"),
                watcher_escalated=bool(ev.get("watcher_escalated")),
                wedge_first_detected_at=ev.get("wedge_first_detected_at"),
                wedge_consecutive=int(ev.get("wedge_consecutive") or 0),
                wedge_escalated=bool(ev.get("wedge_escalated")),
                last_pane_id=ev.get("last_pane_id"),
            )
    last = raw.get("last_sample_at")
    return ps.SupervisionState(
        last_sample_at=float(last) if isinstance(last, (int, float)) else None,
        workers=workers)


def save_state(workspace, state: ps.SupervisionState) -> None:
    path = state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_sample_at": state.last_sample_at,
        "workers": {w: {"first_detected_at": e.first_detected_at,
                        "consecutive": e.consecutive,
                        "recover_issued_at": e.recover_issued_at,
                        "escalated": e.escalated,
                        "watcher_first_detected_at": e.watcher_first_detected_at,
                        "watcher_consecutive": e.watcher_consecutive,
                        "rearm_issued_at": e.rearm_issued_at,
                        "watcher_escalated": e.watcher_escalated,
                        "wedge_first_detected_at": e.wedge_first_detected_at,
                        "wedge_consecutive": e.wedge_consecutive,
                        "wedge_escalated": e.wedge_escalated,
                        "last_pane_id": e.last_pane_id}
                    for w, e in state.workers.items()},
    }
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".pool-supervision.")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def tick(workspace, now: float, *, worker_ids=None, runner=subprocess.run,
         persist: bool = True) -> dict:
    """Sample, decide, persist. Returns one decision per observed worker.

    `expected_period_s` is always the SWEEP's period, never the caller's own
    cadence: the two samplers share `last_sample_at`, so any gap the sweep can
    explain is normal — a faster delivery-time check only shortens it.
    """
    state = load_state(workspace)
    obs = observe(workspace, now, worker_ids=worker_ids, runner=runner)
    period = ps.SAMPLE_PERIOD_S
    new_state, decisions = ps.evaluate(state, obs, now, expected_period_s=period)
    if persist:
        save_state(workspace, new_state)
    asked = list(worker_ids or [])
    return {"decisions": decisions,
            "wedged": sorted(w for w, e in new_state.workers.items() if e.wedge_consecutive),
            "routing": routing_status(workspace),
            "not_supervised": [w for w in asked if w not in obs],
            "observations": {w: {"beat": o.beat, "session_alive": o.session_alive,
                                 "paused": o.paused, "watcher_beat": o.watcher_beat,
                                 "watcher_held": o.watcher_held,
                                 "work_outstanding": o.work_outstanding,
                                 "pane": o.pane} for w, o in obs.items()},
            "resumed": ps.is_resume(now, state.last_sample_at, expected_period_s=period)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", required=True)
    p.add_argument("--recipient", help="delivery-time check for one worker")
    p.add_argument("--sweep", action="store_true", help="the five-minute sweep")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-persist", action="store_true",
                   help="decide without advancing the ladder (diagnostic)")
    a = p.parse_args(argv)
    if bool(a.recipient) == bool(a.sweep):
        p.error("pass exactly one of --recipient or --sweep")
    try:
        out = tick(a.workspace, time.time(),
                   worker_ids=[a.recipient] if a.recipient else None,
                   persist=not a.no_persist)
    except (wi.IdentityError, ValueError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        for wid, decision in sorted(out["decisions"].items()):
            o = out["observations"][wid]
            print(f"{wid[:8]}  {decision:13}  beat={o['beat']:7} "
                  f"session={o['session_alive']!s:5} paused={o['paused']!s:5} "
                  f"watcher={o['watcher_beat']:7} held={o['watcher_held']} "
                  f"work={o['work_outstanding']} pane={o['pane']}")
        for wid in out["not_supervised"]:
            print(f"{wid[:8]}  not supervised (retired, or not a worker in the roster)")
        if out["resumed"]:
            print("resumed: this sample was discarded as evidence (host slept)")
        if out["routing"]["alarm"]:
            print(out["routing"]["alarm"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
