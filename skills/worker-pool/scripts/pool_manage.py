#!/usr/bin/env python3
"""Manage the pool's workers: list, stop, retire, enable, remove, reseat, retry, plugins, set-model, bind, unbind.

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
            A worker with its own agent loses it too: its connection row, lane
            and vault secret (connections.retire_worker_identity). Deleting the
            agent on s.space is left to the owner (no server Action retires a live
            agent yet), and the owner is told in their DM which agent to delete.
    enable  lift both, then the owner restart (pool_remedy.restart).
    remove  drop a retired worker's roster row; its records stay on disk.
    reseat  restart a running seat that owes nothing, so what a seat decides at start
            (its agent identity, plugin dirs, model) is decided again; one that owes
            work is left.
    retry   restart a seat stopped at a provider limit now, on the owner's "retry now" to its
            pause (its input watcher runs it): owed work is kept and delivered again. The verb
            hands the restart to a detached child (`--in-place`) and returns: the take-down
            closes the input watcher's own session, which would end a child of it mid-run. The
            child never leaves the seat paused; a restart that fails is told to the owner.
    bind    bind a room to a worker on the shared folders (pool_roster.bind_room), so the
            router delivers that room's tasks to it; a worker on its own folders is refused,
            since the router never delivers to one.
    unbind  drop a room's binding (pool_roster.unbind_room): its tasks go to the core.
    plugins record or drop an extra Claude Code plugin dir (a folder holding
            .claude-plugin/plugin.json) that every launch and reseat of this worker
            loads with --plugin-dir. The owner's opt-in: run it only on their word.
            It takes effect at the next start; `reseat` applies it now.
    set-model record the --model this worker launches with (`--clear` drops it, back to
            the pool default WORKER_POOL_DEFAULT_MODEL), then reseat its running seat so
            it applies now; a seat that owes work keeps its model until its next start.
            It never sends /model: /model rewrites the shared settings.json default even
            in a session started with --model, which changes every unpinned session.

    python3 pool_manage.py [--workspace WS] [--repo REPO] list [--json]
    python3 pool_manage.py [--workspace WS] stop   <worker id or label> [--force]
    python3 pool_manage.py [--workspace WS] retire <worker> [--unbind] [--hand-off]
    python3 pool_manage.py [--workspace WS] [--repo REPO] enable <worker> [--no-start]
    python3 pool_manage.py [--workspace WS] remove <worker> [--hand-off]
    python3 pool_manage.py [--workspace WS] [--repo REPO] reseat <worker>
    python3 pool_manage.py [--workspace WS] [--repo REPO] retry <worker id> [--in-place]
    python3 pool_manage.py [--workspace WS] plugins <worker> add|remove <dir>
    python3 pool_manage.py [--workspace WS] plugins <worker> list
    python3 pool_manage.py [--workspace WS] [--repo REPO] set-model <worker> <model> | --clear
    python3 pool_manage.py [--workspace WS] bind <room id> <worker id or label>
    python3 pool_manage.py [--workspace WS] unbind <room id>

Every verb prints one JSON line. Exit 0 done, 1 failed, 2 refused.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
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
#: What the detached retry child runs: this script.
RETRY_ENTRY = Path(__file__).resolve()
#: The detached retry waits this long first, so the watcher that asked records its answer before
#: the take-down closes it.
RETRY_HANDOFF_S = 2.0


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


@contextlib.contextmanager
def _lifecycle_lock(workspace, wid: str, action: str):
    """Retire and enable each run whole under one worker's lock; either refuses while the other holds it."""
    path = Path(workspace) / "state" / "locks" / f"worker-lifecycle-{wid}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(f"worker {wid} has a retire or enable in progress; {action} it again once that ends") from None
        yield


def _not_self(wid: str, force: bool) -> None:
    """Killing the caller's own seat ends the call before it can record or report anything."""
    if wid == os.environ.get("SUTANDO_INSTANCE_ID") and not force:
        raise Refused(f"{wid} is this session's own worker; ask the core or another session")


def _outstanding(workspace, wid) -> list:
    """Owed task ids, via the supervisor's own reading of what a worker owes: its owed sentinels and,
    on (or switching to) its own folders, the unanswered tasks in its own inbox (`sup.own_inbox_owed`)."""
    owed = sup.work_outstanding(workspace, wid)
    if owed is None:
        raise Refused(f"cannot tell what {wid} still owes (deliveries or results unreadable)")
    if not owed:
        return []
    own = [t for t, _f in sup.own_inbox_owed(workspace, wid)]
    return sorted(set(t for t, _ in _owed_residue(workspace, wid)) | set(own))


def _owes(workspace, wid) -> list:
    """Every task the worker owes, its own inbox's and any shared sentinel it kept from before a move to
    its own folders; `_outstanding` lists only shared ones. Refused when unreadable, never read as none."""
    import connections
    try:
        own = sup.owed_task_ids(workspace, wid, strict=True)
        return sorted(set(own) | set(sup.sentinel_ids(workspace, wid)[0]))
    except (OSError, ValueError, connections.ConnectionsError):
        raise Refused(f"cannot tell what {wid} still owes (its folders, deliveries, inbox or results "
                      "unreadable)") from None


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


def _take_down(workspace, wid, runner, *, unless_owed=None) -> dict:
    """Pause, kill, close the run. The pause lands under the remedy's lock first, so a
    sweep in flight cannot restart what is being stopped. `unless_owed` is asked again with the
    worker's deliveries folder locked too, the lock every delivery to it takes, and that lock is
    held until the seat is killed: work it names leaves the seat running, returned under `owed`."""
    with rem.pool_lock(workspace, STOP_LOCK_WAIT_S), contextlib.ExitStack() as fence:
        if unless_owed:
            try:
                fence.enter_context(pd.arbitration(workspace, wid))
            except OSError as e:
                raise Refused(f"cannot lock {wid}'s deliveries folder ({e.strerror or e}); "
                              "its seat was left running") from None
            owed = unless_owed()
            if owed:
                return {"sessions": {}, "closed_runs": [], "down": False, "owed": owed}
        sup.set_paused(workspace, wid, True)
        sessions = stop_sessions(workspace, wid, runner=runner)
        fence.close()
        down = all(v != "unknown" for v in sessions.values())
        # A seat tmux could not confirm gone keeps its run open, so supervision still sees it.
        closed = rem.close_dead_runs(workspace, wid, end_reason="killed") if down else []
    return {"sessions": sessions, "closed_runs": closed, "down": down}


def stop(workspace, target, *, force=False, runner=subprocess.run) -> dict:
    wid, _ = _worker(workspace, target)
    _not_self(wid, force)
    def refuse(owed):
        raise Refused(f"{wid} still owes {len(owed)} task(s): {', '.join(owed)}; "
                      "pass --force to stop it anyway (they wait for enable), "
                      "or retire --hand-off to give them to the core")
    owed = _outstanding(workspace, wid)
    if owed and not force:
        refuse(owed)
    t = _take_down(workspace, wid, runner, unless_owed=None if force else lambda: _outstanding(workspace, wid))
    if t.get("owed"):
        refuse(t["owed"])
    return {"worker_id": wid, "action": "stop", "result": "stopped" if t["down"] else "failed",
            "paused": True, "sessions": t["sessions"], "closed_runs": t["closed_runs"],
            "owed_kept": owed,
            **({} if t["down"] else {"why": "tmux could not confirm every session gone"})}


def _hand_off(workspace, wid, owed, hand_off) -> dict:
    if not hand_off:
        raise Refused(f"{wid} still owes {len(owed)} task(s): {', '.join(owed)}; "
                      "pass --hand-off to give them to the core")
    return pd.hand_off_to_core(Path(workspace), wid)


def retire(workspace, target, *, unbind=False, hand_off=False, runner=subprocess.run, tell=None) -> dict:
    """Fence, then take down, then hand off, then drop bindings. The fence (roster state
    `retired`, which the router re-checks under the delivery lock) comes first, so no
    new or replayed task can commit to the worker once the owed set is read."""
    wid, _ = _worker(workspace, target)
    with _lifecycle_lock(workspace, wid, "retire"):
        return _retire(workspace, wid, unbind=unbind, hand_off=hand_off, runner=runner, tell=tell)


def _retire(workspace, wid, *, unbind, hand_off, runner, tell) -> dict:
    _not_self(wid, False)
    rooms = pr.rooms_bound_to(pr.load_bindings(workspace), wid)
    if rooms and not unbind:
        raise Refused(str(pr.WorkerBound(wid, rooms)) + " (or pass --unbind)")
    if not hand_off and _outstanding(workspace, wid):
        _hand_off(workspace, wid, _outstanding(workspace, wid), False)
    _, old, _, _ = pr.set_worker_state(workspace, wid, "retired", keep_bindings=True)
    owed = _outstanding(workspace, wid)
    if owed and not hand_off:
        # Nothing was taken down yet: reopening is the refusal, not a failure.
        pr.set_worker_state(workspace, wid, old)
        _hand_off(workspace, wid, owed, False)
    t = _take_down(workspace, wid, runner)
    base = {"worker_id": wid, "action": "retire", "paused": True, "previous_state": old,
            "sessions": t["sessions"], "closed_runs": t["closed_runs"]}
    if not t["down"]:
        return {**base, "result": "failed", "owed_kept": owed,
                "why": "tmux could not confirm every session gone; the worker stays retired "
                       "(routing closed) and paused, owning its work: re-run retire, or enable"}
    if hand_off:
        moved = pd.hand_off_to_core(Path(workspace), wid)
    else:
        late = _outstanding(workspace, wid)
        if late:
            return {**base, "result": "failed", "owed_kept": late,
                    "why": "work arrived during retirement; the worker stays retired and "
                           "paused, owning it: re-run retire --hand-off, or enable"}
        moved = {"handed_off": [], "kept": {}}
    _, _, _, unbound = pr.set_worker_state(workspace, wid, "retired", unbind=unbind)
    identity = _retire_identity(workspace, wid, hand_off=hand_off, tell=tell)
    moved = {"handed_off": moved["handed_off"] + identity.pop("handed_off", []),
             "kept": {**moved["kept"], **identity.pop("kept", {})}}
    if identity["result"] == "failed":
        return {**base, "result": "failed", "unbound": unbound, "handed_off": moved["handed_off"],
                "kept": moved["kept"], "identity": identity,
                "why": f"{identity['why']}; the worker stays retired with its agent's row kept: re-run retire"}
    return {**base, "result": "retired", "unbound": unbound,
            "handed_off": moved["handed_off"], "kept": moved["kept"], "identity": identity,
            "pending_approval": _withdraw_approval(workspace, wid, tell=tell)}


# s.space's account.agents.retire refuses a live identity (identity_retire_unavailable), so
# deleting the agent itself is the owner's step in the app.
AGENT_REMOVAL_OWNER_STEP = ("the server cannot delete a live agent for us yet: delete it in the app "
                            "(your agents list), or it stays there with no worker behind it")


def agent_removal_note(worker_label: str, agent: str, withdrawn=(), withdraw_error: str = "") -> str:
    """The owner's DM once a retired worker's agent is cut loose here but still exists on s.space."""
    note = (f"[dm-only]\n**Delete agent `{agent}` in the app**\n\nYour worker {worker_label} was retired: "
            "it no longer uses that agent, and its token is gone from this computer. The server cannot delete "
            "a live agent for me yet, so it stays in your agents list until you delete it there.")
    if withdrawn:
        note += (f"\n\nIts {len(withdrawn)} open decision card(s) were withdrawn, since nothing can answer them "
                 "now; a click on one does nothing. The questions stay open in Pending questions:\n"
                 + "\n".join(f"- {w}" for w in withdrawn))
    if withdraw_error:
        note += (f"\n\nIts open decision cards could not be withdrawn ({withdraw_error}). Nothing can answer them "
                 "now; resolve each in Pending questions.")
    return note


def _withdraw_cards(ws: Path, wid: str) -> dict:
    """The retiring worker's open decision cards, cancelled: no other lane may post or answer them."""
    try:
        from hitl.cards import withdraw_worker_cards
        return {"cards_withdrawn": withdraw_worker_cards(wid, ws)}
    except Exception as e:  # noqa: BLE001 — reported with the retirement, never blocks it
        return {"cards_withdrawn": [], "cards_error": f"{type(e).__name__}: {e}"}


def _tell_owner(workspace, text: str) -> bool:
    try:
        import pool_device_connect
        pool_device_connect.send_to_owner(Path(workspace), text)
        return True
    except Exception:  # noqa: BLE001 — the retirement is done; the note is reported as not sent
        return False


def _withdraw_approval(workspace, wid: str, tell=None) -> dict:
    """A device-flow approval still open for the retired worker: withdrawn here (pool_device_connect.withdraw).
    The server has no cancel, so the flow keeps asking; an approval that lands later is reported by name."""
    try:
        import pool_device_connect
        return pool_device_connect.withdraw(Path(workspace), wid, deliver=tell)
    except Exception as e:  # noqa: BLE001 — the retirement is done; the approval is reported, never raised
        return {"result": "failed", "why": f"{type(e).__name__}: {e}"}


def _retire_identity(workspace, wid: str, hand_off: bool = False, tell=None) -> dict:
    """The worker's own agents, one per server, go with it: every lane is stopped and every row marked
    retiring (`connections.begin_worker_retirement`), the tasks it owes in its own folder are handed to the
    core under its switch lock (`hand_off`; else they refuse), and only then its rows and vault secrets go
    (`connections.retire_worker_identity`). Anything short of that is "failed" with the rows kept."""
    import connections
    ws = Path(workspace)
    if not connections.worker_identities(wid, ws):
        return {"result": "none"}
    try:
        connections.begin_worker_retirement(wid, workspace=ws)
    except connections.ConnectionsError as e:
        return {"result": "failed", "why": f"its lane did not stop ({type(e).__name__}: {e})"}
    got = {"handed_off": [], "kept": {}}
    try:
        with connections.worker_switch_lock(wid, ws):
            owed = sup.own_inbox_owed(ws, wid)
            if owed and not hand_off:
                return {"result": "failed", "owed_kept": sorted(t for t, _f in owed),
                        "why": f"it owes {len(owed)} task(s) in its own folder (pass --hand-off)"}
            if owed:
                got = pd.hand_off_own_inbox(ws, wid, owed)
    except OSError as e:
        return {"result": "failed", **got, "why": f"its own folder's tasks were not handed off ({type(e).__name__}: {e})"}
    if got["kept"]:
        return {"result": "failed", **got,
                "why": f"{len(got['kept'])} task(s) in its own folder could not be handed off (a task of that name "
                       "is already in the shared tasks/)"}
    try:
        rows = connections.retire_worker_identity(wid, workspace=ws)
    except connections.ConnectionsError as e:
        return {"result": "failed", **got, "why": f"{type(e).__name__}: {e}"}
    # Only once the identity is gone: a failed retirement keeps the worker, and its cards with it.
    got.update(_withdraw_cards(ws, wid))
    agent = ", ".join(str(r.get("agent_mxid") or "") for r in rows if r.get("agent_mxid"))
    label = str(((pr.load_roster(workspace) or {}).get("workers") or {}).get(wid, {}).get("label") or wid)
    told = (tell or (lambda text: _tell_owner(workspace, text)))(
        agent_removal_note(label, agent or "?", got["cards_withdrawn"], got.get("cards_error", "")))
    return {"result": "removed", "instance": ", ".join(str(r["instance"]) for r in rows), "agent": agent,
            "agent_removal": "owner_step", "owner_told": bool(told),
            "note": AGENT_REMOVAL_OWNER_STEP, **got}


def enable(workspace, repo, target, *, start=True, runner=None, spawn=None) -> dict:
    wid, _ = _worker(workspace, target)
    with _lifecycle_lock(workspace, wid, "enable"):
        return _enable(workspace, repo, wid, start=start, runner=runner, spawn=spawn)


def _enable(workspace, repo, wid, *, start, runner, spawn) -> dict:
    import connections
    wid, row = _worker(workspace, wid)
    old = row.get("state")
    if old == "retired":
        pr.set_worker_state(workspace, wid, "live")
    sup.set_paused(workspace, wid, False)
    out = {"worker_id": wid, "action": "enable", "result": "enabled", "previous_state": old,
           "paused": False}
    if connections.worker_retiring(wid, Path(workspace)):
        # A retire that stopped part-way kept the agent's row: this takes it back, lane and all.
        lanes = []
        for lane in connections.end_worker_retirement(wid, workspace=Path(workspace)) or []:
            try:
                lanes.append(connections.start_added(lane, repo=Path(repo), logs_dir=Path(workspace) / "logs",
                                                     workspace=Path(workspace)))
            except connections.ConnectionsError as e:
                lanes.append({"ok": False, "reason": str(e)})
        out["lane"] = lanes[0] if len(lanes) == 1 else lanes
    if start:
        restarted = rem.restart(workspace, repo, wid, runner=runner, spawn=spawn)
        out["start"] = {"result": restarted["result"], "detail": restarted["detail"]}
        if restarted["result"] == "failed":
            out["result"] = "failed"
    return out


def reseat(workspace, repo, target, *, runner=subprocess.run, spawn=None) -> dict:
    """Restart `target`'s running seat, resuming its conversation, so what a seat decides once at start
    is decided again: whom its s-space tools act as, its plugin dirs, its --model."""
    wid, row = _worker(workspace, target)
    base = {"worker_id": wid, "action": "reseat"}
    if row.get("state") == "retired" or sup.is_paused(workspace, wid):
        return {**base, "result": "skipped", "why": "stopped or retired; it starts with the change when enabled"}
    socket = sup.run_socket(workspace, wid)
    seat = wi.tmux_session_name(wid)
    if socket is None or sw.session_state(seat, socket, runner=runner) != "exists":
        return {**base, "result": "skipped", "why": "no seat running; its next start applies the change"}
    _not_self(wid, False)
    owed = _owes(workspace, wid)
    t = None if owed else _take_down(workspace, wid, runner, unless_owed=lambda: _owes(workspace, wid))
    if owed or t.get("owed"):
        owed = owed or t["owed"]
        return {**base, "result": "deferred", "owed": owed,
                "why": f"it owes {len(owed)} task(s), so its seat was left running as before; once they are "
                       f"done, run: pool_manage.py reseat {wid}"}
    if not t["down"]:
        return {**base, "result": "failed", "sessions": t["sessions"],
                "why": "tmux could not confirm the seat gone; the worker is stopped: run pool_manage.py enable"}
    sup.set_paused(workspace, wid, False)
    restarted = rem.restart(workspace, repo, wid, runner=None if runner is subprocess.run else runner, spawn=spawn)
    return {**base, "result": "reseated" if restarted["result"] == "restarted" else "failed",
            "start": {"result": restarted["result"], "detail": restarted["detail"]}}


def plugins(workspace, target, op: str, path: str = "") -> dict:
    """The worker's recorded plugin dirs after `op`; a dir that is not a Claude plugin is refused."""
    wid, row = _worker(workspace, target)
    base = {"worker_id": wid, "action": "plugins", "op": op}
    if op == "list":
        dirs = pr.recorded_plugin_dirs(workspace, wid)
        return {**base, "result": "listed", "plugin_dirs": dirs,
                "states": {d: pr.plugin_dir_state(d) for d in dirs}}
    if not path:
        raise Refused(f"plugins {op} needs a directory")
    _, dirs, roster = pr.set_worker_plugin_dirs(workspace, wid, **{op: path})
    result = ("added" if op == "add" else "removed") if roster is not None else "unchanged"
    return {**base, "result": result, "plugin_dirs": dirs,
            "applies": "at this worker's next start; pool_manage.py reseat applies it now"}


def set_model(workspace, repo, target, model=None, *, runner=subprocess.run, spawn=None) -> dict:
    """Record `target`'s model (None drops it) and reseat its running seat with the new --model.
    Only that worker's row and seat change; the shared settings.json default is never touched.
    A worker on its own folders is not restarted: its own lane writes its inbox under no lock the
    restart can hold, so a task could start and be killed. Its next start applies the model."""
    import connections
    wid, before, roster = pr.set_worker_model(workspace, target, model)
    out = {"worker_id": wid, "action": "set-model", "recorded": model, "previous": before,
           "model": pr.worker_model(workspace, wid)}
    try:
        own = connections.worker_dirs(wid, Path(workspace), strict=True) is not None
    except (OSError, ValueError, connections.ConnectionsError):
        own = None
    if roster is None:
        why = ("already recorded; a seat started before that takes it on pool_manage.py reseat" if own is False
               else "already recorded; its next start applies the model, since a restart here is unfenced")
        return {**out, "result": "unchanged", "why": why}
    try:
        if own is None:
            raise ValueError("folders record unreadable")
        if own:
            applied = {"result": "next-start", "why": "it runs on its own folders, whose deliveries no lock "
                       "fences, so its seat was not restarted; its next start applies the model"}
        else:
            applied = reseat(workspace, repo, wid, runner=runner, spawn=spawn)
    except (OSError, ValueError, connections.ConnectionsError):
        applied = {"result": "refused", "why": f"cannot tell what {wid} still owes (its folders record "
                   "unreadable); its seat was left running"}
    except Refused as e:
        applied = {"result": "refused", "why": str(e)}
    out["apply"] = {k: v for k, v in applied.items() if k not in ("worker_id", "action")}
    out["result"] = {"reseated": "applied", "failed": "failed"}.get(applied["result"], "recorded")
    return out


def remove(workspace, target, *, hand_off=False, runner=subprocess.run) -> dict:
    """A retired worker is already fenced, so its owed set cannot grow while this runs."""
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
    moved = pd.hand_off_to_core(Path(workspace), wid) if hand_off else {"handed_off": []}
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


def bind(workspace, room: str, target: str) -> dict:
    """Bind `room` to the worker `target` names; refused for a worker on its own folders."""
    import connections
    roster = pr.load_roster(workspace) or {}
    wid = pr.resolve_label(roster, target) if roster else target
    if connections.worker_target_dirs(wid, Path(workspace)) is not None:
        raise Refused(f"worker {wid} keeps (or is switching to) its own folders: the router never delivers to it, so a room "
                      "is not bound to it (the core asks it with pool_ask)")
    # The checked id, not the label: a rename in between must not bind another worker.
    out = pr.bind_room(workspace, room, wid)
    return {"action": "bind", "room": room, "worker": wid, "result": "bound",
            "roster_version": out.get("version")}


def unbind(workspace, room: str) -> dict:
    out = pr.unbind_room(workspace, room)
    return {"action": "unbind", "room": room, "result": "unbound", "roster_version": out.get("version")}


def retry(workspace, repo, target, *, runner=subprocess.run, spawn=None) -> dict:
    """Restart `target`'s seat at once, owed work kept: the owner's "retry now" to its limit pause.
    A stopped or retired worker is left alone; a seat that is already gone is just started.
    Run in place (the detached child of `dispatch_retry`): whatever happens, the pause this sets
    for the take-down is lifted again, and every ending (restarted, failed, or an error raised on
    the way) has one terminal result: for a card's tap, reported under its detached key (the card
    says it); for a bare reply, one DM line."""
    wid, row = _worker(workspace, target)
    base = {"worker_id": wid, "action": "retry"}
    key = os.environ.get(_DETACHED_KEY_ENV) or None
    gate = os.environ.get(_GATE_ENV) or None
    if key and gate and not _handoff_confirmed(workspace, gate):
        # The watcher that asked never confirmed it recorded the tap: nothing is taken down.
        out = {**base, "result": "failed", "why": "the tap was not recorded in time"}
        _retry_ended(workspace, row.get("name") or wid, out, key, tell=False, reason=_HANDOFF)
        return out
    if key and not _still_current(workspace, key):
        # The tap this child was started for already ended (reported or past its deadline).
        return {**base, "result": "failed", "why": "the retry it was started for already ended"}
    if row.get("state") == "retired" or sup.is_paused(workspace, wid):
        out = {**base, "result": "failed", "why": "the worker is stopped or retired; enable it first"}
        _retry_ended(workspace, row.get("name") or wid, out, key, tell=False)  # nothing was attempted
        return out
    out = None
    try:
        socket = sup.run_socket(workspace, wid)
        if socket is not None and sw.session_state(wi.tmux_session_name(wid), socket, runner=runner) == "exists":
            t = _take_down(workspace, wid, runner)
            sup.set_paused(workspace, wid, False)
            if not t["down"]:
                out = {**base, "result": "failed", "sessions": t["sessions"],
                       "why": "tmux could not confirm the seat gone"}
        if out is None:
            restarted = rem.restart(workspace, repo, wid, runner=None if runner is subprocess.run else runner,
                                    spawn=spawn)
            ok = restarted["result"] in ("restarted", "already-running")
            out = {**base, "result": "restarted" if ok else "failed",
                   "start": {"result": restarted["result"], "detail": restarted["detail"]}}
            if not ok:
                out["why"] = "the seat did not start again"
    except rem.PoolBusy:
        out = {**base, "result": "failed", "why": "the pool was busy with another change"}
    except OSError:
        out = {**base, "result": "failed", "why": "the seat could not be taken down or started"}
    finally:
        sup.set_paused(workspace, wid, False)  # never left stopped: the pool supervises it again
        if out is None:  # an unexpected error is on its way out: it still ends with one result
            _retry_ended(workspace, row.get("name") or wid,
                         {**base, "result": "failed", "why": "something went wrong while restarting it"}, key)
    _retry_ended(workspace, row.get("name") or wid, out, key)
    return out


#: The key and gate claim a recovery card's tap hands its retry (detached_action.KEY_ENV and
#: GATE_ENV in src), and the reason reported when that claim was never confirmed (HANDOFF).
_DETACHED_KEY_ENV = "SUTANDO_DETACHED_KEY"
_GATE_ENV = "SUTANDO_RECOVERY_GATE"
_HANDOFF = "handoff"


def _handoff_confirmed(workspace, gate: str) -> bool:
    import detached_action
    return detached_action.wait_for_handoff(Path(workspace), gate)


def _still_current(workspace, key: str) -> bool:
    import detached_action
    return detached_action.status(Path(workspace), key).state == detached_action.RUNNING


def _retry_ended(workspace, name: str, out: dict, key, tell: bool = True, reason=None) -> None:
    """The retry's one terminal result. A card's tap reads it from its detached key, so the card
    is the one surface; a bare reply's owner gets one DM line when a restart it started failed."""
    failed = out.get("result") == "failed"
    line = (f"I could not restart {name}{': ' + out['why'] if out.get('why') else ''}. It is not left "
            "stopped: the pool keeps supervising it and starts it again by itself.") if failed else None
    if key:
        import detached_action
        detached_action.report(Path(workspace), key, detached_action.FAILED if failed else detached_action.DONE,
                               reason=reason or line)
    elif failed and tell:
        _tell_owner_retry_failed(workspace, line)


def dispatch_retry(workspace, repo, target, *, popen=subprocess.Popen) -> dict:
    """Start `retry --in-place` detached in its own session, and return at once: the caller (the
    seat's input watcher) lives in a tmux session the take-down closes."""
    wid, row = _worker(workspace, target)
    base = {"worker_id": wid, "action": "retry"}
    if row.get("state") == "retired" or sup.is_paused(workspace, wid):
        return {**base, "result": "failed", "why": "the worker is stopped or retired; enable it first"}
    log = Path(workspace) / "logs" / f"pool-retry-{wid}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, str(RETRY_ENTRY), "--workspace", str(workspace), "--repo", str(repo),
            "retry", wid, "--in-place", "--after", str(RETRY_HANDOFF_S)]
    with open(log, "ab") as out:
        popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True, close_fds=True)
    return {**base, "result": "dispatched", "log": str(log)}


def _tell_owner_retry_failed(workspace, line: str) -> None:
    """One DM line: the retry did not restart the worker; the pool keeps supervising it."""
    from local_record import write_text_whole
    from proactive_routing import proactive_filename
    import time as _time
    try:
        write_text_whole(Path(workspace) / "results" / proactive_filename(f"pool-retry-{int(_time.time() * 1000)}"),
                         f"[dm-only]\n{line}\n")
    except OSError:
        pass


def main(argv=None, *, runner=subprocess.run) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", default=None)
    p.add_argument("--repo", default=str(_REPO))
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--json", action="store_true")
    for name in ("stop", "retire", "enable", "remove", "reseat", "retry"):
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
        if name == "retry":
            sp.add_argument("--in-place", action="store_true",
                            help="run the restart in this process (the detached child of a retry)")
            sp.add_argument("--after", type=float, default=0.0, help="seconds to wait before the take-down")
        if name == "enable":
            sp.add_argument("--no-start", action="store_true",
                            help="lift the stop/retire without starting a session now")
    bd = sub.add_parser("bind")
    bd.add_argument("room")
    bd.add_argument("worker")
    ub = sub.add_parser("unbind")
    ub.add_argument("room")
    pl = sub.add_parser("plugins")
    pl.add_argument("worker")
    pl.add_argument("op", choices=("add", "remove", "list"))
    pl.add_argument("dir", nargs="?", default="")
    sm = sub.add_parser("set-model")
    sm.add_argument("worker")
    sm.add_argument("model", nargs="?", default=None)
    sm.add_argument("--clear", action="store_true", help="drop the recorded model: back to the pool default")
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
        elif a.cmd == "reseat":
            out = reseat(ws, a.repo, a.worker, runner=runner)
        elif a.cmd == "retry":
            if a.in_place and a.after > 0:
                import time as _time
                _time.sleep(min(a.after, 30.0))
            out = (retry(ws, a.repo, a.worker, runner=runner) if a.in_place
                   else dispatch_retry(ws, a.repo, a.worker))
        elif a.cmd == "plugins":
            out = plugins(ws, a.worker, a.op, a.dir)
        elif a.cmd == "set-model":
            if a.clear == (a.model is not None):
                raise Refused("set-model takes a model or --clear, not both or neither")
            out = set_model(ws, a.repo, a.worker, None if a.clear else a.model, runner=runner)
        elif a.cmd == "bind":
            out = bind(ws, a.room, a.worker)
        elif a.cmd == "unbind":
            out = unbind(ws, a.room)
        else:
            out = remove(ws, a.worker, hand_off=a.hand_off, runner=runner)
    except (Refused, pr.RosterError, wi.IdentityError, ValueError) as e:
        print(json.dumps({"worker": getattr(a, "worker", None), "action": a.cmd,
                          "result": "refused", "why": str(e)}, sort_keys=True))
        return 2
    except (rem.PoolBusy, OSError) as e:
        out = {"worker": getattr(a, "worker", None), "action": a.cmd, "result": "failed", "why": str(e)}
    print(json.dumps(out, sort_keys=True))
    return 1 if out.get("result") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
