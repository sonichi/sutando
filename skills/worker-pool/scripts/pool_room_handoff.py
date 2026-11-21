#!/usr/bin/env python3
"""Hand a room bound to a pool worker from the core's agent to the worker's own agent.

The core's agent invites the worker's agent (`room.member.invite`), waits until the room lists it as
joined (`room.members.list`), leaves (`room.leave`), and only then drops the room's binding, so the
room's work goes to the worker's agent and not through the core. Every call is an Action as the core's
own agent (`pool_provision.core_actions`). The worker's agent is confirmed joined right before the core
leaves; when it is not, or the invitation is refused or held, the core stays and the binding is kept.

Each run picks up where the last stopped: a member is not invited again, a core that already left is
not asked to leave again (the worker's agent must see itself in the room and the core gone; a failed
read is never taken as the core having left). The invite and leave carry an operation id per attempt
(`attempt`, kept in state/workers/<worker>/room-handoff-attempts.json): only a definite no, a leave that
was done, or an invitation the worker's agent no longer holds moves to a new id, so a declined invitation is
sent again and a later hand-off never reuses an earlier leave, while an error or a held call is resent as
the same operation. `join_room` brings the worker's agent into a room it only worked in (`worked_rooms`), and the
core stays there. `return_room` undoes a hand-off: the worker's agent invites the core back, and once it is
joined the room is bound again and the worker's agent leaves. `hand_room` returns one result: `handed`, `waiting` or `unknown` (run it again), `skipped`
(the core stays, with why), `not_bound` or `failed` (nothing further was done).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

import connections
import pool_provision as pp
import pool_roster as pr

POLL_S = 5.0
# The newest attributions read for worked rooms: each task file lookup scans the task archive.
RECENT_TASKS = 500


def _local(room: str) -> str:
    return room.partition(":")[0]


def bound_rooms(workspace, worker: str, server: str) -> list:
    """The rooms bound to `worker`, one entry per room however many server names its bindings carry:
    {room (its id on `server`), bindings (each key naming it)}."""
    groups: dict = {}
    for key, bound in sorted(pr.load_bindings(workspace).items()):
        names = bound if isinstance(bound, list) else [bound]
        if worker in names:
            groups.setdefault(_local(key), {"room": f"{_local(key)}:{server}", "bindings": []})["bindings"].append(key)
    return [groups[k] for k in sorted(groups)]


def _said(env: dict) -> str:
    return str(env.get("message") or pp.error_code(env) or env.get("status") or "no reason given")


def joined(actions, room: str) -> "tuple[Optional[set], str]":
    """(the user ids `room` lists as joined, '') as `actions`' agent sees them, or (None, the refusal);
    a transport failure raises `pp.ProvisionError`."""
    env = actions.read("room.members.list", {}, room)
    members = pp.find(env, "members")
    if env.get("is_error") or not isinstance(members, list):
        return None, _said(env)
    # The Action lists joined members only; an entry naming another membership is still not counted.
    return {str(m.get("user_id")) for m in members if isinstance(m, dict) and m.get("user_id")
            and m.get("membership", "join") == "join"}, ""


def operation_id(verb: str, worker: str, agent: str, room: str, attempt: int = 0) -> str:
    return (f"hand-room-{verb}-" + hashlib.sha256(f"{worker}\0{agent}\0{room}".encode()).hexdigest()[:24]
            + f"-{attempt}")


def _attempts_path(ws: Path, worker: str) -> Path:
    return ws / "state" / "workers" / worker / "room-handoff-attempts.json"


def _attempts(ws: Path, worker: str) -> dict:
    try:
        data = json.loads(_attempts_path(ws, worker).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def attempt(ws: Path, worker: str, room: str, verb: str, invite_gone: bool = False) -> int:
    """The attempt number this call's `verb` uses in `room`. The server deduplicates an operation id, so
    the id moves on only once its last call can no longer take effect: a definite no, a leave that was
    done, or a sent invitation the worker's agent no longer holds (`invite_gone`). An error, a retry or a
    call held for the owner keeps its id, so a resend is the same operation and never a second one."""
    rec = (_attempts(ws, worker).get(room) or {}).get(verb) or {}
    n, state = int(rec.get("n") or 0), rec.get("state")
    spent = state == "no" or (state == "sent" and (verb.endswith("leave") or invite_gone))
    return n + 1 if spent else n


@contextlib.contextmanager
def _attempts_locked(ws: Path, worker: str):
    path = _attempts_path(ws, worker)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def record_attempt(ws: Path, worker: str, room: str, verb: str, n: int, state: str, now: float,
                   detail: "Optional[dict]" = None) -> None:
    """Keep one attempt's outcome (`sent`, `no` or `open`), plus for `open` why it is still open; a locked
    read-modify-write, so records other runs make for other rooms or verbs of the same worker survive."""
    with _attempts_locked(ws, worker) as path:
        data = _attempts(ws, worker)
        data.setdefault(room, {})[verb] = {"n": n, "state": state, "at": now, **(detail or {})}
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def _open_detail(env, state: str) -> dict:
    """Why an attempt is still open, for readers that must tell an approval the owner holds from an error:
    the approval state, the error's short code and a retry-after when the answer carries them."""
    out = {"why": state}
    code = pp.error_code(env)
    if code:
        out["code"] = code
    after = pp.find(env, "retry_after_s")
    if isinstance(after, (int, float)) and not isinstance(after, bool) and after >= 0:
        out["retry_after_s"] = float(after)
    return out


def invite_held(as_worker, room: str) -> "Optional[bool]":
    """Whether the worker's agent holds a pending invitation to `room`, from its own `room.invites.list`;
    None when that cannot be read, which is never taken as gone."""
    try:
        env = as_worker().read_account("room.invites.list", {})
    except pp.ProvisionError:
        return None
    invites = pp.find(env, "invites")
    if env.get("is_error") or not isinstance(invites, list):
        return None
    return any(isinstance(i, dict) and _local(str(i.get("room_id") or "")) == _local(room) for i in invites)


def worker_actions(row: dict, vault=None) -> "pp.Actions":
    """Actions as the worker's own agent, from its connection row's own credential."""
    gateway, token = connections.connection_credential(row, vault=vault)
    return pp.Actions(gateway, pp.bearer_of(token), pp.http_json, [])


class _Stop(Exception):
    """A step that ends the call with its result."""

    def __init__(self, status: str, why: str, **extra):
        super().__init__(why)
        self.status, self.why, self.extra = status, why, extra


def _guarded(fn, worker: str, room: str, **kw) -> dict:
    try:
        return fn(worker, room, **kw)
    except (pp.ProvisionError, connections.ConnectionsError, pr.RosterError) as e:
        return {"worker": worker, "room": room, "status": "failed",
                "why": f"{e}; nothing further was done, and the binding is kept unless it says handed"}


def _agents(worker: str, ws: Path, actions, server: str, core_agent: str, vault):
    """(the worker's agent, its row, the core's Actions, the core's server, the core's agent) or None."""
    ident = connections.worker_identity(worker, ws)
    agent = str((ident or {}).get("agent_mxid") or "")
    if not agent or (ident or {}).get("disabled"):
        return None
    if actions is None:
        actions, core = pp.core_actions(ws, vault=vault)
        server = server or str(core.get("server") or "")
        core_agent = core_agent or str(core.get("agent_mxid") or "")
    return agent, ident, actions, server, core_agent


def _no_agent(worker: str, room: str) -> dict:
    return {"worker": worker, "room": room, "status": "failed",
            "why": f"worker {worker} has no enabled agent of its own; nothing was done"}


def _execute(ws: Path, actions, worker: str, agent: str, target: str, verb: str, action: str, arguments: dict,
             wall, invite_gone: bool = False) -> "tuple[str, dict]":
    """One invite or leave under this attempt's operation id; (its approval state, the answer), recorded."""
    n = attempt(ws, worker, target, verb, invite_gone)
    env = actions.execute(action, arguments, operation_id=operation_id(verb, worker, agent, target, n), room_id=target)
    state = pp.approval_state(env)
    if state == "denied" or (state == "done" and verb.endswith("invite") and pp.find(env, "invited") is False):
        outcome = "no"
    else:
        outcome = "sent" if state == "done" else "open"
    record_attempt(ws, worker, target, verb, n, outcome, wall(), _open_detail(env, state) if outcome == "open" else None)
    return state, env


def _invite_until_joined(ws: Path, actions, worker: str, agent: str, target: str, members: set, *, wait_s, poll_s,
                         sleep, clock, wall, as_worker, stays: str, verb: str = "invite") -> None:
    """Returns once `agent` is joined, `actions` inviting it and `as_worker` acting as the invitee; otherwise
    raises `_Stop` with waiting or skipped."""
    if agent in members:
        return
    last = (_attempts(ws, worker).get(target) or {}).get(verb) or {}
    gone = last.get("state") == "sent" and invite_held(as_worker, target) is False
    state, env = _execute(ws, actions, worker, agent, target, verb, "room.member.invite", {"user_id": agent}, wall,
                          invite_gone=gone)
    if state == "held":
        raise _Stop("waiting", "the invitation waits for the owner's approval; run this again once approved")
    if state != "done" or pp.find(env, "invited") is False:
        reason = pp.find(env, "reason") if state == "done" else ""
        raise _Stop("skipped", f"the invitation was refused ({reason or _said(env)}); {stays}")
    expected = {k: pp.find(env, k) for k in ("expected_admission", "waiting_on") if pp.find(env, k)}
    deadline = clock() + wait_s
    while agent not in (joined(actions, target)[0] or set()):
        if clock() >= deadline:
            raise _Stop("waiting", f"invited; {agent} has not joined yet, so {stays}. Run this again once it "
                                   "has", **expected)
        sleep(poll_s)


def hand_room(worker: str, room: str, *, workspace=None, actions=None, server: str = "", core_agent: str = "",
              as_worker: Optional[Callable[[], object]] = None, wait_s: float = 0.0, poll_s: float = POLL_S,
              sleep=time.sleep, clock=time.monotonic, wall=time.time, vault=None) -> dict:
    """One bound room's hand-off as far as it can go now; the result says where it stopped."""
    return _guarded(_hand_room, worker, room, workspace=workspace, actions=actions, server=server,
                    core_agent=core_agent, as_worker=as_worker, wait_s=wait_s, poll_s=poll_s, sleep=sleep,
                    clock=clock, wall=wall, vault=vault)


def _hand_room(worker, room, *, workspace, actions, server, core_agent, as_worker, wait_s, poll_s, sleep, clock,
               wall, vault) -> dict:
    ws = Path(connections._workspace(workspace))
    found = _agents(worker, ws, actions, server, core_agent, vault)
    if found is None:
        return _no_agent(worker, room)
    agent, ident, actions, server, core_agent = found
    as_worker = as_worker or (lambda: worker_actions(ident, vault))
    entry = next((e for e in bound_rooms(ws, worker, server) if _local(e["room"]) == _local(room)), None)
    if entry is None:
        return {"worker": worker, "agent": agent, "room": room, "status": "not_bound",
                "why": f"no binding names worker {worker} for this room"}
    target = entry["room"]
    out = {"worker": worker, "agent": agent, "room": target, "bindings": entry["bindings"]}

    def done(status: str, why: str, **extra) -> dict:
        return {**out, "status": status, "why": why, **extra}

    def unbind(left: bool) -> dict:
        for key in entry["bindings"]:
            pr.unbind_room(ws, key)
        return done("handed", ("the core left and " if left else "the core had already left; ")
                    + "the binding is dropped", core_left=left)

    members, why = joined(actions, target)
    if members is None:
        mine, mine_why = joined(as_worker(), target)
        if mine is not None and agent in mine and core_agent and core_agent not in mine:
            return unbind(False)
        if mine is not None and agent not in mine:
            return done("failed", f"the core cannot read the room's members ({why}) and the worker's agent is not "
                                  "joined; the binding is kept")
        return done("unknown", f"the core cannot read the room's members ({why}), and nothing shows it has left "
                               f"({mine_why or ('the room still lists it' if core_agent else 'its id is unknown')}); "
                               "the binding is kept. Run this again")
    try:
        _invite_until_joined(ws, actions, worker, agent, target, members, wait_s=wait_s, poll_s=poll_s, sleep=sleep,
                             clock=clock, wall=wall, as_worker=as_worker,
                             stays="the core stays and the binding is kept")
    except _Stop as stop:
        return done(stop.status, stop.why, **stop.extra)
    members, why = joined(actions, target)
    if members is None or agent not in members:
        return done("failed", f"the worker's agent could not be confirmed joined right before leaving "
                              f"({why or 'not listed'}); the core stays and the binding is kept")
    state, env = _execute(ws, actions, worker, agent, target, "leave", "room.leave", {}, wall)
    if state == "held":
        return done("waiting", "the core's leave waits for the owner's approval; the binding is kept")
    if state != "done":
        return done("failed", f"the core could not leave ({_said(env)}); the binding is kept")
    return unbind(True)


def worked_rooms(workspace, worker: str, server: str) -> list:
    """Rooms not bound to `worker` where it worked as the core's agent, from what the pool already records:
    the room (`channel_id`) of each task the router attributed to it (`state/attribution`), and the room of
    each interaction the correlation log records for one of those tasks (the newest RECENT_TASKS). One
    entry per room, its id on `server`, with how many of its tasks name it."""
    import correlation_log
    import pool_attribution as pa
    import pool_route_handler as rh
    from local_task_protocol import find_archived_task
    from task_archive import find_task_file
    ws = Path(workspace)
    try:
        files = sorted((p for p in pa.attribution_dir(ws).iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime)
    except OSError:
        files = []
    mine = {p.name for p in files[-RECENT_TASKS:] if pa.worker_for_task(ws, p.name) == worker}
    seen: dict = {}
    for tid in sorted(mine):
        path = find_task_file(ws / "tasks", tid) or find_archived_task(ws / "tasks", tid)
        try:
            room = str(rh.read_task(str(path)).get("channel_id") or "") if path else ""
        except OSError:
            room = ""
        if room:
            seen.setdefault(room, set()).add(tid)
    for rec in correlation_log.read("interaction", workspace=ws):
        if rec.get("task_id") in mine and rec.get("room_id"):
            seen.setdefault(str(rec["room_id"]), set()).add(str(rec["task_id"]))
    bound = {_local(e["room"]) for e in bound_rooms(ws, worker, server)}
    rooms: dict = {}
    for room, tasks in seen.items():
        if room.startswith("!") and ":" in room and _local(room) not in bound:
            rooms.setdefault(_local(room), set()).update(tasks)
    return [{"room": f"{local}:{server}", "tasks": len(rooms[local])} for local in sorted(rooms)]


def join_room(worker: str, room: str, *, workspace=None, actions=None, server: str = "", wait_s: float = 0.0,
              poll_s: float = POLL_S, sleep=time.sleep, clock=time.monotonic, wall=time.time, vault=None,
              as_worker: Optional[Callable[[], object]] = None) -> dict:
    """Bring the worker's agent into a room it worked in as the core's agent; the core stays and no binding
    changes. `joined`, `waiting`, `skipped` (with why) or `failed`."""
    return _guarded(_join_room, worker, room, workspace=workspace, actions=actions, server=server, wait_s=wait_s,
                    poll_s=poll_s, sleep=sleep, clock=clock, wall=wall, vault=vault, as_worker=as_worker)


def _join_room(worker, room, *, workspace, actions, server, wait_s, poll_s, sleep, clock, wall, vault,
               as_worker) -> dict:
    ws = Path(connections._workspace(workspace))
    found = _agents(worker, ws, actions, server, "", vault)
    if found is None:
        return _no_agent(worker, room)
    agent, ident, actions, server, _core = found
    as_worker = as_worker or (lambda: worker_actions(ident, vault))
    target = f"{_local(room)}:{server}"
    out = {"worker": worker, "agent": agent, "room": target}
    members, why = joined(actions, target)
    if members is None:
        return {**out, "status": "skipped", "why": f"the core cannot read this room ({why}), so it cannot invite"}
    try:
        _invite_until_joined(ws, actions, worker, agent, target, members, wait_s=wait_s, poll_s=poll_s, sleep=sleep,
                             clock=clock, wall=wall, as_worker=as_worker,
                             stays="the core stays")
    except _Stop as stop:
        return {**out, "status": stop.status, "why": stop.why, **stop.extra}
    return {**out, "status": "joined", "why": "the worker's agent is a member; the core stays"}


def return_room(worker: str, room: str, *, bindings: "Optional[list]" = None, workspace=None, actions=None,
                server: str = "", core_agent: str = "", as_worker: Optional[Callable[[], object]] = None,
                wait_s: float = 0.0, poll_s: float = POLL_S, sleep=time.sleep, clock=time.monotonic, wall=time.time,
                vault=None) -> dict:
    """Undo one hand-off: the worker's agent invites the core's agent back, and once the core is joined the
    room is bound to the worker again (each of `bindings`, default the room's id on the core's server) and
    the worker's agent leaves. `returned`, `waiting`, `skipped` (with why) or `failed`. Not a rollback: a
    binding restored before a later step fails stays restored, and the worker's agent stays a member when
    its leave is refused or the core cannot be confirmed joined first. A rerun finds a finished return from
    the core's own read (core joined, worker's agent gone)."""
    return _guarded(_return_room, worker, room, bindings=bindings, workspace=workspace, actions=actions,
                    server=server, core_agent=core_agent, as_worker=as_worker, wait_s=wait_s, poll_s=poll_s,
                    sleep=sleep, clock=clock, wall=wall, vault=vault)


def _return_room(worker, room, *, bindings, workspace, actions, server, core_agent, as_worker, wait_s, poll_s, sleep,
                 clock, wall, vault) -> dict:
    ws = Path(connections._workspace(workspace))
    found = _agents(worker, ws, actions, server, core_agent, vault)
    if found is None:
        return _no_agent(worker, room)
    agent, ident, core, server, core_agent = found
    mine = (as_worker or (lambda: worker_actions(ident, vault)))()
    target = f"{_local(room)}:{server}"
    keys = list(bindings or [target])
    out = {"worker": worker, "agent": agent, "room": target, "bindings": keys}
    if not core_agent:
        return {**out, "status": "failed", "why": "the core's agent id is not known; nothing was done"}
    seen, _ = joined(core, target)
    if seen is not None and core_agent in seen and agent not in seen:
        taken = [k for k in keys if not pr.bind_room_if_free(ws, k, worker)]
        return {**out, "status": "returned", "worker_left": True, "taken": taken,
                "why": "the return had already finished: the core is joined and the worker's agent is not"
                       + (f"; not rebound, another worker holds {', '.join(taken)}" if taken else "")}
    if seen is not None and core_agent in seen:
        members = seen
    else:
        members, why = joined(mine, target)
        if members is None:
            return {**out, "status": "skipped", "why": f"neither the core nor the worker's agent can read this room "
                                                       f"({why}), so the core cannot be invited back; the binding "
                                                       "stays dropped"}
    try:
        _invite_until_joined(ws, mine, worker, core_agent, target, members, wait_s=wait_s, poll_s=poll_s, sleep=sleep,
                             clock=clock, wall=wall, as_worker=lambda: core, verb="return-invite",
                             stays="the worker's agent stays and the binding stays dropped")
    except _Stop as stop:
        return {**out, "status": stop.status, "why": stop.why, **stop.extra}
    taken = [k for k in keys if not pr.bind_room_if_free(ws, k, worker)]
    members, why = joined(core, target)
    if members is None:
        members, why = joined(mine, target)
    if members is None or core_agent not in members:
        return {**out, "status": "returned", "worker_left": False, "taken": taken,
                "why": f"the core is back and the room is bound again; the core could not be confirmed joined right "
                       f"before the worker's agent would leave ({why or 'not listed'}), so it stays"}
    state, env = _execute(ws, mine, worker, agent, target, "return-leave", "room.leave", {}, wall)
    left = state == "done"
    return {**out, "status": "returned", "worker_left": left, "taken": taken,
            "why": "the core is back and the room is bound to the worker again"
                   + ("" if left else f"; the worker's agent stays a member ({_said(env)})")
                   + (f"; not rebound, another worker holds {', '.join(taken)}" if taken else "")}
