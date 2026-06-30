#!/usr/bin/env python3
"""room-read — pull recent room/channel history for an agent, on demand.

A synchronous, pull-on-demand read capability that is ORTHOGONAL to the task
file bridge (tasks/ -> results/). The async task-in / result-out loop is left
completely untouched; this is a separate request the agent makes only when it
needs surrounding context it wasn't handed.

Two interchangeable backends sit behind ONE per-agent gate and ONE interface:

  - generic     GET {RELAY_URL}/v1/rooms/{room_id}/messages?limit=N
                Provider-agnostic. The relay maps the generic verb to whatever
                platform it fronts, so a self-hoster gets reads with no platform
                credentials on the client.
  - appservice  GET {HOMESERVER}/_matrix/client/v3/rooms/{room_id}/messages
                    ?user_id={agent_mxid}&dir=b&limit=N    (Bearer AS_TOKEN)
                Platform-optimised: full CS API via masquerade.

Backend selection: explicit ROOM_READ_BACKEND ("generic"|"appservice"), else
auto — appservice when AS_TOKEN+HOMESERVER are present, else generic when
RELAY_URL is present, else none (graceful no-op).

Per-agent scope gating (opt-in, never blanket): an agent may read a room only
if a gate config opts it in. DEFAULT-DENY. Config is JSON at ROOM_READ_GATE
(default <workspace>/state/room-read-gate.json):

    {
      "@agent.a:hs": {"rooms": ["!roomA:hs"]},
      "@agent.b:hs": {"all_member_rooms": true}
    }

  - rooms: explicit allowed room ids.
  - all_member_rooms: any room the agent is a *member* of is allowed (the
    backend still enforces membership; the gate is the opt-in layer on top).

Graceful degrade: missing creds, gate-deny, unknown backend, network error, or
any non-2xx response all return a structured no-context result and NEVER raise
to the caller — so the agent / file bridge is unaffected (criterion: graceful
degrade where a provider can't back the read).

No platform-specific literals live in this file — homeserver, relay URL, token
and agent MXID all come from env/vault — so it stays provider-agnostic and
portable.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_LIMIT = 20
HTTP_TIMEOUT = 15


def _result(ok, messages=None, backend=None, reason=None, room_id=None):
    """Uniform structured return — callers branch on `ok`, never on exceptions."""
    return {
        "ok": bool(ok),
        "backend": backend,
        "room_id": room_id,
        "reason": reason,
        "messages": messages or [],
    }


# --------------------------------------------------------------------------- #
# Gate (per-agent scope gating, default-deny)
# --------------------------------------------------------------------------- #
def _gate_path():
    p = os.environ.get("ROOM_READ_GATE")
    if p:
        return p
    ws = os.environ.get("SUTANDO_WORKSPACE_RESOLVED") or os.environ.get("ROOM_READ_WORKSPACE")
    if ws:
        return os.path.join(ws, "state", "room-read-gate.json")
    return os.path.join(os.getcwd(), "room-read-gate.json")


def load_gate(path=None):
    """Load the opt-in gate config. Missing/unparsable file -> empty (deny-all)."""
    path = path or _gate_path()
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def gate_allows(agent_mxid, room_id, gate, *, is_member=None):
    """Decide if `agent_mxid` is opted in to read `room_id`. Default-deny.

    `is_member` (None = unknown) lets all_member_rooms grants resolve when the
    caller has already checked membership; when unknown, an all_member_rooms
    grant is allowed at the gate and the backend remains the membership
    enforcer (it returns no context for a non-member room).
    """
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


# --------------------------------------------------------------------------- #
# Normalisation — both backends collapse to the same shape for the agent
# --------------------------------------------------------------------------- #
def _normalize_matrix(events):
    out = []
    for ev in events or []:
        if ev.get("type") != "m.room.message":
            continue
        content = ev.get("content") or {}
        out.append({
            "sender": ev.get("sender"),
            "ts": ev.get("origin_server_ts"),
            "body": content.get("body"),
            "event_id": ev.get("event_id"),
        })
    return out


def _normalize_generic(items):
    out = []
    for m in items or []:
        out.append({
            "sender": m.get("sender") or m.get("user_id") or m.get("from"),
            "ts": m.get("ts") or m.get("timestamp"),
            "body": m.get("body") or m.get("text") or m.get("message"),
            "event_id": m.get("id") or m.get("event_id"),
        })
    return out


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
def _read_generic(room_id, limit):
    relay = (os.environ.get("RELAY_URL") or os.environ.get("REMOTE_TASK_URL") or "").rstrip("/")
    if not relay:
        return _result(False, backend="generic", reason="no RELAY_URL configured", room_id=room_id)
    token = os.environ.get("RELAY_TOKEN") or os.environ.get("REMOTE_TASK_TOKEN")
    headers = {"User-Agent": "sutando-room-read/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{relay}/v1/rooms/{urllib.parse.quote(room_id, safe='')}/messages?limit={int(limit)}"
    try:
        status, body = _http_get_json(url, headers)
    except urllib.error.HTTPError as e:
        # 404 = relay doesn't implement the verb -> graceful no-op (versioned, additive).
        reason = "verb unimplemented (404)" if e.code == 404 else f"HTTP {e.code}"
        return _result(False, backend="generic", reason=reason, room_id=room_id)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return _result(False, backend="generic", reason=f"network/parse error: {e}", room_id=room_id)
    items = body.get("messages") if isinstance(body, dict) else body
    return _result(True, _normalize_generic(items), backend="generic", room_id=room_id)


def _read_appservice(room_id, limit, agent_mxid):
    hs = (os.environ.get("HOMESERVER") or os.environ.get("HOMESERVER_URL") or "").rstrip("/")
    as_token = os.environ.get("AS_TOKEN") or os.environ.get("APPSERVICE_TOKEN")
    if not hs or not as_token:
        return _result(False, backend="appservice", reason="no HOMESERVER/AS_TOKEN configured", room_id=room_id)
    if not agent_mxid:
        return _result(False, backend="appservice", reason="no agent_mxid for masquerade", room_id=room_id)
    q = urllib.parse.urlencode({"user_id": agent_mxid, "dir": "b", "limit": int(limit)})
    url = f"{hs}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id, safe='')}/messages?{q}"
    headers = {"Authorization": f"Bearer {as_token}", "User-Agent": "sutando-room-read/1"}
    try:
        status, body = _http_get_json(url, headers)
    except urllib.error.HTTPError as e:
        # 403 = masquerading agent is not a member -> membership enforced server-side.
        reason = "agent not a member (403)" if e.code == 403 else f"HTTP {e.code}"
        return _result(False, backend="appservice", reason=reason, room_id=room_id)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return _result(False, backend="appservice", reason=f"network/parse error: {e}", room_id=room_id)
    events = body.get("chunk") if isinstance(body, dict) else None
    return _result(True, _normalize_matrix(events), backend="appservice", room_id=room_id)


def _pick_backend():
    explicit = (os.environ.get("ROOM_READ_BACKEND") or "").strip().lower()
    if explicit in ("generic", "appservice"):
        return explicit
    if (os.environ.get("AS_TOKEN") or os.environ.get("APPSERVICE_TOKEN")) and \
       (os.environ.get("HOMESERVER") or os.environ.get("HOMESERVER_URL")):
        return "appservice"
    if os.environ.get("RELAY_URL") or os.environ.get("REMOTE_TASK_URL"):
        return "generic"
    return None


# --------------------------------------------------------------------------- #
# Public entry
# --------------------------------------------------------------------------- #
def read_room(room_id, agent_mxid=None, limit=DEFAULT_LIMIT, *, gate=None, backend=None):
    """Pull up to `limit` recent messages from `room_id` as `agent_mxid`.

    Gate-checks first (default-deny), then dispatches to the selected backend.
    Always returns a structured result; never raises for an expected failure.
    """
    agent_mxid = agent_mxid or os.environ.get("AGENT_MXID")
    if not room_id:
        return _result(False, reason="no room_id given")
    gate = load_gate() if gate is None else gate
    if not gate_allows(agent_mxid, room_id, gate):
        return _result(False, reason=f"gate denied for {agent_mxid} (not opted in)", room_id=room_id)
    backend = backend or _pick_backend()
    if backend == "appservice":
        return _read_appservice(room_id, limit, agent_mxid)
    if backend == "generic":
        return _read_generic(room_id, limit)
    return _result(False, reason="no backend configured", room_id=room_id)


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="Pull recent room history for an agent (gated, pull-on-demand).")
    ap.add_argument("room_id")
    ap.add_argument("--agent", dest="agent_mxid", default=os.environ.get("AGENT_MXID"))
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--backend", choices=["generic", "appservice"], default=None)
    args = ap.parse_args(argv)
    res = read_room(args.room_id, args.agent_mxid, args.limit, backend=args.backend)
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
