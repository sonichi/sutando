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
(`attempt`, kept in state/workers/<worker>/room-handoff-attempts.json): a call held for the owner keeps its
id, and one that ended gets a new one, so a declined invitation is sent again and a later hand-off never
reuses an earlier leave. `join_room` brings the worker's agent into a room it only worked in (`worked_rooms`), and the
core stays there. `hand_room` returns one result: `handed`, `waiting` or `unknown` (run it again), `skipped`
(the core stays, with why), `not_bound` or `failed` (nothing further was done).
"""
from __future__ import annotations

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
# A sent invitation the agent has not joined after this long is spent; the next run invites afresh.
INVITE_SETTLE_S = 600
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


def attempt(ws: Path, worker: str, room: str, verb: str, now: float) -> int:
    """The attempt number this call's `verb` uses in `room`. An operation id is deduplicated by the server,
    so an attempt that ended (left; refused; sent and not joined within INVITE_SETTLE_S) is never reused;
    one held for the owner's approval is, so the approval answers it."""
    rec = (_attempts(ws, worker).get(room) or {}).get(verb) or {}
    n, state, at = int(rec.get("n") or 0), rec.get("state"), float(rec.get("at") or 0)
    spent = state in ("done", "refused") if verb == "leave" else \
        state == "refused" or (state == "done" and now - at > INVITE_SETTLE_S)
    return n + 1 if spent else n


def record_attempt(ws: Path, worker: str, room: str, verb: str, n: int, state: str, now: float) -> None:
    data = _attempts(ws, worker)
    data.setdefault(room, {})[verb] = {"n": n, "state": state, "at": now}
    path = _attempts_path(ws, worker)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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
             wall) -> "tuple[str, dict]":
    """One invite or leave under this attempt's operation id; (its approval state, the answer), recorded."""
    now = wall()
    n = attempt(ws, worker, target, verb, now)
    env = actions.execute(action, arguments, operation_id=operation_id(verb, worker, agent, target, n), room_id=target)
    state = pp.approval_state(env)
    refused = state != "done" or (verb == "invite" and pp.find(env, "invited") is False)
    record_attempt(ws, worker, target, verb, n, "held" if state == "held" else "refused" if refused else "done", now)
    return state, env


def _invite_until_joined(ws: Path, actions, worker: str, agent: str, target: str, members: set, *, wait_s, poll_s,
                         sleep, clock, wall, stays: str) -> None:
    """Returns once `agent` is joined; otherwise raises `_Stop` with waiting or skipped."""
    if agent in members:
        return
    state, env = _execute(ws, actions, worker, agent, target, "invite", "room.member.invite", {"user_id": agent}, wall)
    if state == "held":
        raise _Stop("waiting", "the invitation waits for the owner's approval; run this again once approved")
    if state != "done" or pp.find(env, "invited") is False:
        reason = pp.find(env, "reason") if state == "done" else ""
        raise _Stop("skipped", f"the invitation was refused ({reason or _said(env)}); {stays}")
    expected = {k: pp.find(env, k) for k in ("expected_admission", "waiting_on") if pp.find(env, k)}
    deadline = clock() + wait_s
    while agent not in (joined(actions, target)[0] or set()):
        if clock() >= deadline:
            raise _Stop("waiting", f"invited; the worker's agent has not joined yet, so {stays}. Run this again "
                                   "once it has", **expected)
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
                             clock=clock, wall=wall, stays="the core stays and the binding is kept")
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
              poll_s: float = POLL_S, sleep=time.sleep, clock=time.monotonic, wall=time.time, vault=None) -> dict:
    """Bring the worker's agent into a room it worked in as the core's agent; the core stays and no binding
    changes. `joined`, `waiting`, `skipped` (with why) or `failed`."""
    return _guarded(_join_room, worker, room, workspace=workspace, actions=actions, server=server, wait_s=wait_s,
                    poll_s=poll_s, sleep=sleep, clock=clock, wall=wall, vault=vault)


def _join_room(worker, room, *, workspace, actions, server, wait_s, poll_s, sleep, clock, wall, vault) -> dict:
    ws = Path(connections._workspace(workspace))
    found = _agents(worker, ws, actions, server, "", vault)
    if found is None:
        return _no_agent(worker, room)
    agent, _ident, actions, server, _core = found
    target = f"{_local(room)}:{server}"
    out = {"worker": worker, "agent": agent, "room": target}
    members, why = joined(actions, target)
    if members is None:
        return {**out, "status": "skipped", "why": f"the core cannot read this room ({why}), so it cannot invite"}
    try:
        _invite_until_joined(ws, actions, worker, agent, target, members, wait_s=wait_s, poll_s=poll_s, sleep=sleep,
                             clock=clock, wall=wall, stays="the core stays")
    except _Stop as stop:
        return {**out, "status": stop.status, "why": stop.why, **stop.extra}
    return {**out, "status": "joined", "why": "the worker's agent is a member; the core stays"}
