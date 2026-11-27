#!/usr/bin/env python3
"""Room Commons Actions as this session's own agent, through the credential its room posts use.

    room_ops.py action describe <room> <action>
    room_ops.py action read     <room> <action> [--args JSON | --args-file PATH]
    room_ops.py action execute  <room> <action> [--args JSON | --args-file PATH]
                                [--task-file TASK] [--operation-id ID]

Only `room.commons.*` Actions are sent. The session's relay credential is exchanged for a
short-lived delegation and the Action runs on the server's MCP endpoint
(`pool_provision.Actions`, the one client for that exchange); both tokens stay in this process's
memory and are scrubbed from everything returned.

Whose agent: a pool worker's session acts as that worker's own agent on the room's server
(`connections.worker_mcp_credential`), and is refused when it has none; it never falls back to
the core's. The core's session uses the credential `say` uses.

For whom: an `execute` that answers a task passes `--task-file`; the person who asked is read
from the task's own headers and sent as `asked_by`, so the server holds the write to that
person's rights as well as the agent's. With no task there is no `asked_by`; nothing else sets it.
"""
from __future__ import annotations

import functools
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Callable, Mapping, Optional, Tuple

import _gateway

ACTION_RE = re.compile(r"room\.commons\.[a-z0-9_]+(?:\.[a-z0-9_]+)*")
TOOLS = {"describe": "room.actions.describe", "read": "room.action.read", "execute": "room.action.execute"}
# The server's own rule for `asked_by`.
ASKED_BY_RE = re.compile(r"@[^\s:]+:[^\s]+")
ASKED_BY_MAX = 255
REPLY_MAX = 8 * 1024 * 1024


class Refused(Exception):
    """Nothing was sent; the text says why and what to do instead. It never carries a secret."""


def _owners():
    """(connections, pool_provision): who the session acts as, and the Actions client."""
    here = Path(os.path.realpath(__file__)).parent
    for path in (here.parents[1] / "src", here.parent / "worker-pool" / "scripts"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    try:
        import connections
        import pool_provision
    except ImportError as e:
        raise Refused(f"Actions need this checkout's connections record and worker-pool skill ({e.name} "
                      "is not importable); use the s.space MCP tools instead") from None
    return connections, pool_provision


def requester(task_file, workspace=None) -> Optional[str]:
    """The person a task was asked by, from the headers its writer put above the body: the relay's
    `asked_by`, else `user_id`. None for no task or a task that names nobody."""
    if task_file is None:
        return None
    try:
        text = Path(task_file).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        raise Refused(f"the task file cannot be read ({type(e).__name__}), so who asked is unknown; "
                      "nothing was sent") from None
    _owners()
    from task_envelope import attested_task_headers
    headers = attested_task_headers(text, workspace).headers
    who = str(headers.get("asked_by") or headers.get("user_id") or "").strip()
    if not who:
        return None
    if len(who) > ASKED_BY_MAX or not ASKED_BY_RE.fullmatch(who):
        raise Refused("the task's sender is not an s.space user id, so the write cannot be held to "
                      "that person's rights; nothing was sent")
    return who


def credential(room_id: str, agent_mxid: Optional[str] = None, *, workspace=None,
               environ: Optional[Mapping[str, str]] = None, vault=None) -> Tuple[str, str, Optional[str]]:
    """(relay URL, bearer, acting agent) for an Action in `room_id`. A worker's session gets its own
    agent's or Refused; the acting agent is None only for a core install that records none."""
    connections, _ = _owners()
    worker = connections.session_worker(environ)
    if worker:
        server = connections.server_of_room(room_id)
        if not server:
            raise Refused("the room id names no server, so this worker's own agent for it is unknown; "
                          "nothing was sent")
        try:
            cred = connections.worker_mcp_credential(worker, workspace=workspace, environ=environ,
                                                     vault=vault, server=server)
        except connections.ConnectionsError as e:
            raise Refused(f"this worker's own credential is not usable ({e}); nothing was sent") from None
        acting = cred.get("agent_id")
        if cred.get("acts_as") != "worker" or not acting:
            raise Refused("this worker has no agent of its own on the room's server, and a Commons "
                          "Action is never sent as the core's agent; hand the write to the core, or "
                          "give this worker its own agent")
        if agent_mxid and agent_mxid != acting:
            raise Refused(f"--agent names {agent_mxid}, but this worker acts as {acting}; nothing was sent")
        return str(cred["relay"]), str(cred["bearer"]), str(acting)
    base, headers = _gateway.gateway(room_id)
    scheme, _, bearer = str(headers.get("Authorization") or "").partition(" ")
    if not base or scheme != "Bearer" or not bearer:
        raise Refused("no gateway credential is configured for this room; nothing was sent")
    try:
        acting = connections.room_agent_mxid(room_id, workspace=workspace, environ=environ)
    except connections.ConnectionsError as e:
        raise Refused(str(e)) from None
    return base, bearer, acting or agent_mxid or None


def _outcome(verb: str, env: Mapping[str, object], pp) -> dict:
    """The server's answer as {ok, state, ...}: only a completed call is ok."""
    if verb == "describe" and not env.get("is_error"):
        return {"ok": True, "state": "done", "result": dict(env)}
    state = pp.approval_state(env)
    if state == "done" and (verb != "read" or isinstance(env.get("result"), dict)):
        result = env.get("result")
        return {"ok": True, "state": "done", "result": result if isinstance(result, dict) else dict(env)}
    out = {"ok": False, "state": "refused" if state == "done" else state}
    code = pp.error_code(env) or (env.get("code") if isinstance(env.get("code"), str) else "")
    if code:
        out["code"] = code
    said = env.get("message") if isinstance(env.get("message"), str) else ""
    if state == "held":
        out["reason"] = ("waiting for approval in s.space; do NOT run it again (it runs once approved): "
                         "check it with operation.inspect")
    else:
        out["reason"] = said or f"the server did not complete it (status {env.get('status')!r})"
    if isinstance(env.get("suggested_action"), str) and env["suggested_action"]:
        out["suggested_action"] = env["suggested_action"]
    return out


def run(verb: str, room_id: str, action: str, arguments: Optional[dict] = None, *, task_file=None,
        agent_mxid: Optional[str] = None, operation_id: Optional[str] = None, workspace=None,
        environ: Optional[Mapping[str, str]] = None, vault=None,
        request: Optional[Callable[..., tuple]] = None, gate=None) -> dict:
    """One `room.commons.*` Action as this session's agent. Returns {ok, state, verb, room_id,
    action, acting_as, ...}: `result` when it completed, else `reason` (and the server's `code`)."""
    out = {"ok": False, "state": "not_sent", "verb": verb, "room_id": room_id, "action": action}
    known: list = []
    try:
        if verb not in TOOLS:
            raise Refused(f"unknown verb {verb!r}; one of {', '.join(sorted(TOOLS))}")
        if not isinstance(action, str) or not ACTION_RE.fullmatch(action):
            raise Refused("only room.commons.* Actions go through this command; nothing was sent")
        if not room_id:
            raise Refused("room_id required")
        arguments = {} if arguments is None else arguments
        if not isinstance(arguments, dict):
            raise Refused("the Action's arguments must be a JSON object")
        asked_by = requester(task_file, workspace) if verb == "execute" else None
        _, pp = _owners()
        relay, bearer, acting = credential(room_id, agent_mxid, workspace=workspace, environ=environ,
                                           vault=vault)
        known.append(bearer)
        gate = _gateway.load_gate() if gate is None else gate
        if not _gateway.gate_allows(acting or agent_mxid, room_id, gate):
            raise Refused(f"client gate denied for {acting or agent_mxid}")
        out["acting_as"] = acting
        payload = {"room_id": room_id, "action": action}
        if verb != "describe":
            payload["arguments"] = arguments
        if verb == "execute":
            payload["operation_id"] = out["operation_id"] = operation_id or f"ro-{uuid.uuid4().hex}"
            out["asked_by"] = asked_by
            if asked_by:
                payload["asked_by"] = asked_by
        send = request or functools.partial(pp.http_json, max_bytes=REPLY_MAX)
        try:
            env = pp.Actions(relay, bearer, send, known).tool(TOOLS[verb], payload)
        except pp.ProvisionError as e:
            # An execute that got no answer may still have run: its outcome is unknown, not "not sent".
            if verb == "execute":
                out.update(state="unknown", reason=f"{e}; it may have run: check it with operation.inspect, "
                                                   "and send it again only with the same --operation-id")
            else:
                out.update(state="failed", reason=str(e))
        else:
            out.update(_outcome(verb, env, pp))
    except Refused as e:
        out["reason"] = str(e)
    except _gateway.RoomConnectionError as e:
        out["reason"] = str(e)
    if not known:
        return out
    _, pp = _owners()
    return json.loads(pp.scrub(json.dumps(out), known))
