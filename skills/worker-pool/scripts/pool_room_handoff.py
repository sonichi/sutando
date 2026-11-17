#!/usr/bin/env python3
"""Hand a room bound to a pool worker from the core's agent to the worker's own agent.

The core's agent invites the worker's agent (`room.member.invite`), waits until the room lists it as
joined (`room.members.list`), leaves (`room.leave`), and only then drops the room's binding, so the
room's work goes to the worker's agent and not through the core. Every call is an Action as the core's
own agent (`pool_provision.core_actions`). The worker's agent is confirmed joined right before the core
leaves; when it is not, or the invitation is refused or held, the core stays and the binding is kept.

Each run picks up where the last stopped: a member is not invited again, a core that already left is
not asked to leave again (the worker's agent confirms it is in the room instead), and the invite and
leave carry operation ids fixed by the worker, its agent and the room, so a repeated call is the same
operation. `hand_room` returns one result: `handed`, `waiting` (run it again), `skipped` (the core
stays, with why), `not_bound` or `failed` (nothing further was done).
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Callable, Optional

import connections
import pool_provision as pp
import pool_roster as pr

POLL_S = 5.0


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
    return {str(m.get("user_id")) for m in members if isinstance(m, dict) and m.get("user_id")}, ""


def operation_id(verb: str, worker: str, agent: str, room: str) -> str:
    return f"hand-room-{verb}-" + hashlib.sha256(f"{worker}\0{agent}\0{room}".encode()).hexdigest()[:24]


def worker_actions(row: dict, vault=None) -> "pp.Actions":
    """Actions as the worker's own agent, from its connection row's own credential."""
    gateway, token = connections.connection_credential(row, vault=vault)
    return pp.Actions(gateway, pp.bearer_of(token), pp.http_json, [])


def hand_room(worker: str, room: str, *, workspace=None, actions=None, server: str = "",
              as_worker: Optional[Callable[[], object]] = None, wait_s: float = 0.0, poll_s: float = POLL_S,
              sleep=time.sleep, clock=time.monotonic, vault=None) -> dict:
    """One room's hand-off as far as it can go now; the result says where it stopped."""
    try:
        return _hand_room(worker, room, workspace=workspace, actions=actions, server=server, as_worker=as_worker,
                          wait_s=wait_s, poll_s=poll_s, sleep=sleep, clock=clock, vault=vault)
    except (pp.ProvisionError, connections.ConnectionsError, pr.RosterError) as e:
        return {"worker": worker, "room": room, "status": "failed",
                "why": f"{e}; nothing further was done, and the binding is kept unless it says handed"}


def _hand_room(worker, room, *, workspace, actions, server, as_worker, wait_s, poll_s, sleep, clock, vault) -> dict:
    ws = Path(connections._workspace(workspace))
    ident = connections.worker_identity(worker, ws)
    agent = str((ident or {}).get("agent_mxid") or "")
    if not agent or (ident or {}).get("disabled"):
        return {"worker": worker, "room": room, "status": "failed",
                "why": f"worker {worker} has no enabled agent of its own; nothing was done"}
    if actions is None:
        actions, core = pp.core_actions(ws, vault=vault)
        server = server or str(core.get("server") or "")
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
        if mine is not None and agent in mine:
            return unbind(False)
        return done("failed", f"the core cannot read the room's members ({why}) and the worker's agent is not "
                              f"seen joined ({mine_why or 'not listed'}); the binding is kept")
    if agent not in members:
        env = actions.execute("room.member.invite", {"user_id": agent},
                              operation_id=operation_id("invite", worker, agent, target), room_id=target)
        state = pp.approval_state(env)
        if state == "held":
            return done("waiting", "the invitation waits for the owner's approval; run this again once approved")
        if state != "done" or pp.find(env, "invited") is False:
            reason = pp.find(env, "reason") if state == "done" else ""
            return done("skipped", f"the invitation was refused ({reason or _said(env)}); the core stays and the "
                                   "binding is kept")
        expected = {k: pp.find(env, k) for k in ("expected_admission", "waiting_on") if pp.find(env, k)}
        deadline = clock() + wait_s
        while True:
            members = joined(actions, target)[0] or set()
            if agent in members:
                break
            if clock() >= deadline:
                return done("waiting", "invited; the worker's agent has not joined yet, so the core stays. Run "
                                       "this again once it has", **expected)
            sleep(poll_s)
    members, why = joined(actions, target)
    if members is None or agent not in members:
        return done("failed", f"the worker's agent could not be confirmed joined right before leaving "
                              f"({why or 'not listed'}); the core stays and the binding is kept")
    env = actions.execute("room.leave", {}, operation_id=operation_id("leave", worker, agent, target),
                          room_id=target)
    state = pp.approval_state(env)
    if state == "held":
        return done("waiting", "the core's leave waits for the owner's approval; the binding is kept")
    if state != "done":
        return done("failed", f"the core could not leave ({_said(env)}); the binding is kept")
    return unbind(True)
