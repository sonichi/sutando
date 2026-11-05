#!/usr/bin/env python3
"""The supervision caller: observe the pool, decide, persist. It never remedies.
Its one other write is housekeeping: the sweep archives spent delivery sentinels.

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
pl = _sibling("pool_limit")
prr = _sibling("pool_routing_receipt")
pd = _sibling("pool_delivery")

_SRC = _HERE.parents[2] / "src"
import cli_wedge as cw  # noqa: E402  (src/ is on the path via pool_delivery)
from delivery import pane_gate, task_dispatch as td  # noqa: E402
from task_disposition import SPENT, task_disposition  # noqa: E402
import tmux_pane_keys  # noqa: E402

STATE_REL = Path("state") / "pool-supervision.json"
# An owner fact, so it is a marker beside the worker's records and not a roster
# state: the core recompiles the roster, and a recompile must never lift a pause.
PAUSED_MARKER = "paused"
# States that are not candidates for recovery at all.
NOT_SUPERVISED = ("retired",)
# A sentinel younger than this is left alone: its payload may still be landing.
SENTINEL_RETIRE_AFTER_S = 3600


def state_path(workspace) -> Path:
    return Path(workspace) / STATE_REL


def is_paused(workspace, worker_id) -> bool:
    """Only the owner writes or removes this file, through `set_paused`."""
    return (wi.worker_dir(workspace, worker_id) / PAUSED_MARKER).exists()


def set_paused(workspace, worker_id, paused: bool) -> None:
    """The owner's pause (`pool_manage.py stop` / `enable`)."""
    marker = wi.worker_dir(workspace, worker_id) / PAUSED_MARKER
    if paused:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    else:
        marker.unlink(missing_ok=True)


def run_socket(workspace, worker_id) -> str | None:
    """The tmux socket of the worker's open run, else of its last run; None when it never ran.
    A wrong socket answers "no server running" for a host whose real socket is fine."""
    runs = [r for r in wi.incarnations(workspace, worker_id) if isinstance(r, dict)]
    open_runs = [r for r in runs if r.get("ended_at") is None]
    for run in reversed(open_runs or runs):
        if (run.get("tmux") or {}).get("socket"):
            return str(run["tmux"]["socket"])
    return None


def probe_session(workspace, worker_id, *, runner=subprocess.run) -> bool | None:
    """Is this worker's OWN tmux session alive? None when it cannot be told.

    The socket is read from the recorded incarnation, never assumed: a wrong
    socket answers "no server running" for a host whose real socket is fine.
    """
    runs = [r for r in wi.incarnations(workspace, worker_id) if isinstance(r, dict)]
    open_runs = [r for r in runs if r.get("ended_at") is None]
    if open_runs:
        run = open_runs[-1]
    elif runs and runs[-1].get("end_reason") == "crashed":
        # A failed recovery closes its attempted run. The last crashed run
        # still names the socket to probe, so escalation must keep its evidence.
        run = runs[-1]
    else:
        return None
    tmux = run.get("tmux") or {}
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


def sentinel_ids(workspace, worker_id) -> tuple[list[str], set[str]]:
    """(owed, spent) ids among this worker's sentinels. Spent is task_disposition.SPENT, the rule
    pool_delivery applies at boot; answered is spent even with its task file still live in tasks/,
    since the core never re-queues an answered task. A malformed id is in neither."""
    owned = td.owned_task_ids(pd.deliveries_dir(workspace, worker_id).parent, worker_id)
    # One index pass for the whole inbox: a live one holds hundreds of answered sentinels.
    ready = td.ready_result_filenames(pd.results_dir(workspace), [f"{t}.txt" for t in owned])
    owed, spent = [], set()
    for t in owned:
        try:
            done = f"{t}.txt" in ready or task_disposition(workspace, t, unanswered=True) in SPENT
        except ValueError:
            continue
        if done:
            spent.add(t)
        else:
            owed.append(t)
    return owed, spent


def owed_task_ids(workspace, worker_id) -> list[str]:
    return sentinel_ids(workspace, worker_id)[0]


def work_outstanding(workspace, worker_id) -> bool | None:
    """Does this worker owe a reply? None when undecidable."""
    try:
        return bool(owed_task_ids(workspace, worker_id))
    except (OSError, ValueError):
        return None


def retire_spent_sentinels(workspace, worker_id, now: float,
                           min_age_s: float = SENTINEL_RETIRE_AFTER_S) -> list[str]:
    """Move spent sentinels older than `min_age_s` into the inbox's archive/ through
    pool_delivery.archive_finished, so the inbox holds what is owed. Returns the names moved.
    An archive/ that cannot be used raises, and the sweep reports it."""
    inbox = pd.deliveries_dir(workspace, worker_id)
    spent = sentinel_ids(workspace, worker_id)[1]
    old: dict[str, list[str]] = {}
    for f in sorted(inbox.iterdir()) if inbox.is_dir() else []:
        tid = next((f.name[:-len(x)] for x in td.WORKER_HOLD_SUFFIXES if f.name.endswith(x)), None)
        if tid not in spent or f.is_symlink() or not f.is_file():
            continue
        if now - f.stat().st_mtime < min_age_s:
            continue
        old.setdefault(tid, []).append(f.name)
    if not old:
        return []
    moved = pd.archive_finished(workspace, worker_id, task_ids=sorted(old))["archived"]
    return sorted(n for t in moved for n in old[t])


def classify_pane_text(text: str | None, runtime: str = "claude", *, workspace=None,
                       socket=None, session=None) -> str:
    """One capture as a pool_supervision PANE_* kind. The gate and the abnormal
    text are the core's readers (pane_gate, cli_wedge); only the mapping is here."""
    if not text or not text.strip():
        return ps.PANE_UNKNOWN
    adapter = pane_gate.ADAPTERS.get(runtime, pane_gate.CLAUDE)
    verdict = pane_gate.classify_pane(text, adapter, workspace, socket, session)
    # cli_wedge's banners anchor at line start; an -e capture's SGR would hide them.
    plain = pane_gate.SGR_STRIP.sub("", text)
    if verdict.state == "abnormal":
        abn = cw.frame_abnormal(plain)
        if abn and abn.kind == "provider-limit":
            return ps.PANE_LIMIT
        if abn and "needs-login" in abn.names:
            return ps.PANE_LOGGED_OUT
        return ps.PANE_ABNORMAL
    if verdict.state == "busy":
        # A limit banner under the composer is status, not a dialog: a live turn above it runs.
        if verdict.reason == "working" or (verdict.reason == "usage-limit" and cw.frame_working(plain)):
            return ps.PANE_WORKING
        return ps.PANE_GATE
    if verdict.state in ("idle-ready", "pending"):
        return ps.PANE_IDLE
    return ps.PANE_UNKNOWN


def observe_pane(workspace, worker_id, runtime: str = "claude", *,
                 runner=subprocess.run, now=None) -> tuple[str | None, str | None, "pl.Reset | None"]:
    """(pane kind, frame id, limit reset); all None when unread."""
    socket, name = _open_tmux(workspace, worker_id)
    if not socket:
        return None, None, None
    try:
        # -e as the notifier captures: both read the same bytes, so a dim placeholder is one verdict.
        done = runner(["tmux", "-S", str(socket), "capture-pane", "-e", "-p", "-t", f"={name}:0"],
                      capture_output=True, text=True, timeout=8)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None, None, None
    text = done.stdout if done.returncode == 0 else None
    if not text or not text.strip():
        return None, None, None
    kind = classify_pane_text(text, runtime, workspace=workspace, socket=socket, session=name)
    if kind in (ps.PANE_ABNORMAL, ps.PANE_UNKNOWN):
        pane_gate.save_frame(workspace, name, kind, text)
    return kind, frame_id(text), (pl.reset(plain_text(text), time.time() if now is None else now)
                                  if kind == ps.PANE_LIMIT else None)


def plain_text(text: str) -> str:
    return pane_gate.SGR_STRIP.sub("", text)


def frame_id(text: str) -> str:
    """"raw:progress": the raw frame proves a frozen turn, the progress half a waiting one.
    Read without SGR: an -e capture's colour changes are not progress, so they must not reset it."""
    plain = pane_gate.SGR_STRIP.sub("", text)
    return f"{cw.raw_state_id(plain)}:{pane_gate.progress_id(plain)}"


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
    blocks = {}
    for wid, row in rows.items():
        session_alive = probe_session(workspace, wid, runner=runner)
        watcher_beat = pb.classify(pb.beat_path(workspace, "watcher", wid), now)
        # The process table is read only when the beat cannot answer: a live
        # beat proves the watcher, and a dead session has no watcher ladder.
        held = None
        if session_alive is True and watcher_beat != pb.LIVE:
            held = session_watcher_holds(workspace, wid, runner=runner)
        pane, pane_id, reset = (observe_pane(workspace, wid, row.get("runtime") or "claude",
                                      runner=runner, now=now)
                         if session_alive is True else (None, None, None))
        socket = _open_tmux(workspace, wid)[0] if session_alive is True else None
        if socket and socket not in blocks:
            blocks[socket] = delivery_block(socket)
        obs[wid] = ps.Observation(
            beat=pb.classify(pb.beat_path(workspace, "worker", wid), now),
            session_alive=session_alive,
            paused=is_paused(workspace, wid),
            watcher_beat=watcher_beat,
            watcher_held=held,
            work_outstanding=work_outstanding(workspace, wid),
            pane=pane,
            pane_id=pane_id,
            limit_reset_at=reset.at if reset else None,
            limit_banner=reset.banner if reset else None,
            delivery_block=blocks.get(socket) if socket else None,
        )
    return obs


def delivery_block(socket) -> str | None:
    """Why typed delivery cannot reach panes on this tmux socket, or None. Keeps the guard's
    files fresh first, so a nightly /tmp sweep cannot be what raises the block."""
    tmux_pane_keys.refresh_guard(socket)
    return "pane-key-fence" if tmux_pane_keys.fence_status(socket) == "uncertain" else None


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
                wedge_kind=ev.get("wedge_kind"),
                last_pane_id=ev.get("last_pane_id"),
                limit_next_at=ev.get("limit_next_at"),
                limit_attempts=int(ev.get("limit_attempts") or 0),
                limit_exhausted=bool(ev.get("limit_exhausted")),
                limit_banner=ev.get("limit_banner"),
                limit_reset_at=ev.get("limit_reset_at"),
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
                        "wedge_kind": e.wedge_kind,
                        "last_pane_id": e.last_pane_id,
                        "limit_next_at": e.limit_next_at,
                        "limit_attempts": e.limit_attempts,
                        "limit_exhausted": e.limit_exhausted,
                        "limit_banner": e.limit_banner,
                        "limit_reset_at": e.limit_reset_at}
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


def acknowledge_cards(workspace, worker_ids) -> None:
    """Persist that these workers' wedge cards were created; a card that was not
    stays owed, and the next tick asks again."""
    if worker_ids:
        save_state(workspace, ps.acknowledge(load_state(workspace), worker_ids))


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
    retired, retire_errors = {}, {}
    if persist:
        save_state(workspace, new_state)
        if worker_ids is None:  # the sweep only: a delivery-time check stays read-only
            for wid in obs:
                try:
                    if n := len(retire_spent_sentinels(workspace, wid, now)):
                        retired[wid] = n
                except (OSError, ValueError) as e:
                    retire_errors[wid] = f"{type(e).__name__}: {e}"
    asked = list(worker_ids or [])
    # Reported on every tick, not only when a card decision is due: a dry run must
    # show the finding after the real sweep already raised (and acknowledged) the card.
    return {"decisions": decisions,
            "auth_expired": sorted(w for w, o in obs.items() if o.pane == ps.PANE_LOGGED_OUT),
            "wedged": sorted(w for w, e in new_state.workers.items() if e.wedge_consecutive),
            "wedge_kinds": {w: e.wedge_kind for w, e in new_state.workers.items()
                            if e.wedge_consecutive and e.wedge_kind},
            "limit_waits": {w: {"next_at": e.limit_next_at, "attempts": e.limit_attempts,
                                  "exhausted": e.limit_exhausted}
                            for w, e in new_state.workers.items() if e.limit_next_at is not None},
            "routing": routing_status(workspace),
            "sentinels_retired": retired,
            "sentinel_retire_errors": retire_errors,
            "delivery_blocks": {w: o.delivery_block for w, o in obs.items() if o.delivery_block},
            "notifier_stalls": notifier_stalls(workspace, obs),
            "not_supervised": [w for w in asked if w not in obs],
            "observations": {w: {"beat": o.beat, "session_alive": o.session_alive,
                                 "paused": o.paused, "watcher_beat": o.watcher_beat,
                                 "watcher_held": o.watcher_held,
                                 "work_outstanding": o.work_outstanding,
                                 "pane": o.pane} for w, o in obs.items()},
            "resumed": ps.is_resume(now, state.last_sample_at, expected_period_s=period)}


def notifier_stalls(workspace, workers) -> dict:
    """{worker id: stall record} for supervised workers whose notifier has timed out waiting
    for an idle pane pane_gate.STALL_ROUNDS times running; the notifier writes the record."""
    by_seat = pane_gate.idle_stalls(workspace)
    return {wid: by_seat[wi.tmux_session_name(wid)] for wid in workers
            if wi.tmux_session_name(wid) in by_seat}


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
        for wid in out["auth_expired"]:
            print(f"{wid[:8]}  login expired: the session is alive but cannot work until "
                  f"/login is run in tmux session {wi.tmux_session_name(wid)}")
        if out["resumed"]:
            print("resumed: this sample was discarded as evidence (host slept)")
        for wid, err in sorted(out.get("sentinel_retire_errors", {}).items()):
            print(f"{wid[:8]}  could not archive spent sentinels: {err}")
        for wid, cause in sorted(out.get("delivery_blocks", {}).items()):
            print(f"{wid[:8]}  typed delivery is blocked ({cause}); see the card for the remedy")
        for wid, rec in sorted(out.get("notifier_stalls", {}).items()):
            print(f"{wid[:8]}  notifier has not seen an idle pane for {rec.get('rounds')} rounds; "
                  f"tasks are waiting (frames in state/pane-frames/)")
        if out["routing"]["alarm"]:
            print(out["routing"]["alarm"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
