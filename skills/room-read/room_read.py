#!/usr/bin/env python3
"""room-read — pull-on-demand room/channel history for an agent.

A synchronous, pull-on-demand read capability that is ORTHOGONAL to the task
file bridge (tasks/ -> results/). The async task-in / result-out loop is left
completely untouched; this is a separate request the agent makes only when it
needs surrounding context it wasn't handed.

## Architecture boundary (load-bearing)

The local client speaks ONLY the stable relay `/v1` protocol — it never holds a
platform/AppService token and never talks to a homeserver directly:

    Matrix room
      <-> relay / broker / AppService        (platform-coupled, box-side)
      <-> /v1 agent relay protocol
      <-> remote-relay-bridge.py             (this client — provider-agnostic)
      <-> tasks/results file queue
      <-> agent core

So this skill has a SINGLE backend: the generic relay verb
`GET {RELAY_URL}/v1/rooms/{room}/messages?limit=N`. Whether the relay backs that
verb with a bot-client CS-API read or an AppService masquerade is the relay's
(box-side) concern, invisible here. The AppService is AG2-side infrastructure
that mints/puppets per-agent virtual users; the agent never sees its token.

## Scope gating — relay-enforced, client pre-filter optional

Membership is the consent gate and it is enforced **relay-side**: an agent reads
only rooms it is a *joined member* of; a non-member read is denied (403) and the
AppService cannot bypass it. This client offers an OPTIONAL local default-deny
pre-filter (`ROOM_READ_GATE`) as defense-in-depth, but it is NOT the security
boundary — the relay is. With no gate file present the client defers entirely to
the relay's enforcement.

Graceful degrade: missing relay config, gate-deny, network error, or any non-2xx
response (a relay that doesn't implement the verb -> 404; a non-member ->
403/deny) all return a structured no-context result and NEVER raise to the
caller, so the agent / file bridge is unaffected.

No platform literals in this file — relay URL/token all come from env/vault — so
it stays provider-agnostic and portable.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_LIMIT = 20
MAX_LIMIT = 100  # clamp ceiling — context-first reads should stay bounded, not pull huge history
HTTP_TIMEOUT = 15


def _result(ok, messages=None, reason=None, room_id=None):
    """Uniform structured return — callers branch on `ok`, never on exceptions."""
    return {
        "ok": bool(ok),
        "room_id": room_id,
        "reason": reason,
        "messages": messages or [],
    }


# --------------------------------------------------------------------------- #
# Optional client-side gate (defense-in-depth; relay is the real enforcer)
# --------------------------------------------------------------------------- #
def _gate_path():
    # Resolving the workspace is the caller's job (it already goes through the
    # sanctioned helper) — the skill just reads the path it is handed via
    # ROOM_READ_GATE, falling back to a cwd-relative file. No workspace-env
    # resolution lives here.
    return os.environ.get("ROOM_READ_GATE") or os.path.join(os.getcwd(), "room-read-gate.json")


def load_gate(path=None):
    """Load the optional opt-in gate. Missing file -> None (defer to the relay).

    Distinguishes "no gate configured" (None -> client does not pre-filter; the
    relay enforces membership) from "gate present but empty" ({} -> deny-all
    locally).
    """
    path = path or _gate_path()
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return {}


def gate_allows(agent_mxid, room_id, gate, *, is_member=None):
    """Client-side pre-filter decision. `gate is None` -> no local pre-filter
    (defer to the relay). Otherwise default-deny unless the agent is opted in.
    """
    if gate is None:
        return True  # no client gate configured -> relay enforces membership
    entry = gate.get(agent_mxid)
    if not isinstance(entry, dict):
        return False
    if room_id and room_id in (entry.get("rooms") or []):
        return True
    if entry.get("all_member_rooms"):
        return is_member is None or bool(is_member)
    return False


# --------------------------------------------------------------------------- #
# HTTP helper
# --------------------------------------------------------------------------- #
def _http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8") or "{}")


def _normalize(items):
    """Collapse the relay's message list to a stable shape for the agent."""
    out = []
    for m in items or []:
        out.append({
            "sender": m.get("sender") or m.get("user_id") or m.get("from"),
            "ts": m.get("ts") or m.get("timestamp"),
            "body": m.get("body") or m.get("text") or m.get("message"),
            "event_id": m.get("event_id") or m.get("id"),
        })
    return out


# --------------------------------------------------------------------------- #
# The single backend: the generic relay verb
# --------------------------------------------------------------------------- #
def _read_via_relay(room_id, limit, before=None):
    relay = (os.environ.get("RELAY_URL") or os.environ.get("REMOTE_TASK_URL") or "").rstrip("/")
    if not relay:
        return _result(False, reason="no RELAY_URL configured", room_id=room_id)
    token = os.environ.get("RELAY_TOKEN") or os.environ.get("REMOTE_TASK_TOKEN")
    headers = {"User-Agent": "sutando-room-read/2"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {"limit": int(limit)}
    if before:
        params["before"] = before
    url = (f"{relay}/v1/rooms/{urllib.parse.quote(room_id, safe='')}/messages?"
           + urllib.parse.urlencode(params))
    try:
        status, body = _http_get_json(url, headers)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            reason = "verb unimplemented (404)"
        elif e.code in (403, 401):
            reason = f"denied — agent not a joined member ({e.code})"
        else:
            reason = f"HTTP {e.code}"
        return _result(False, reason=reason, room_id=room_id)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return _result(False, reason=f"network/parse error: {e}", room_id=room_id)
    items = body.get("messages") if isinstance(body, dict) else body
    return _result(True, _normalize(items), room_id=room_id)


# --------------------------------------------------------------------------- #
# Public entry
# --------------------------------------------------------------------------- #
def read_room(room_id, agent_mxid=None, limit=DEFAULT_LIMIT, *, gate=None, before=None):
    """Pull up to `limit` recent messages from `room_id` via the relay verb.

    Optional client gate-check first, then the relay call (which enforces
    membership authoritatively). Always returns a structured result; never
    raises for an expected failure.
    """
    agent_mxid = agent_mxid or os.environ.get("AGENT_MXID")
    if not room_id:
        return _result(False, reason="no room_id given")
    # Clamp limit: a context-first read should stay bounded — an unbounded
    # caller-supplied limit can turn it into an accidental large history pull.
    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    gate = load_gate() if gate is None else gate
    if not gate_allows(agent_mxid, room_id, gate):
        return _result(False, reason=f"client gate denied for {agent_mxid} (not opted in)", room_id=room_id)
    return _read_via_relay(room_id, limit, before=before)


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="Pull recent room history for an agent via the relay verb (gated).")
    ap.add_argument("room_id")
    ap.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--before", default=None, help="pagination token / event id to read before")
    args = ap.parse_args(argv)
    res = read_room(args.room_id, args.agent_mxid, args.limit, before=args.before)
    print(json.dumps(res, indent=2))
    # Exit 0 for any structured result — "no context available" (ok:false from a
    # gate-deny / 404 / 403 / network degrade) is a valid graceful outcome, not a
    # failed task. Usage errors (argparse) exit 2; unexpected runtime bugs raise.
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
