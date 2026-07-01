#!/usr/bin/env python3
"""room-ops · read — pull recent room/channel history for an agent.

A synchronous pull-on-demand read, orthogonal to the task file bridge. Gateway-only
(the generic verb `GET {GATEWAY}/v1/rooms/{room}/messages`); membership is enforced
gateway-side. See _gateway.py for the shared boundary + gate.
"""
from __future__ import annotations

import os

from _gateway import (gate_allows, load_gate, gateway, http_request, degrade_reason,
                    quote, urlencode, HTTPError, URLError)

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _result(ok, messages=None, reason=None, room_id=None):
    return {"ok": bool(ok), "room_id": room_id, "reason": reason, "messages": messages or []}


def _normalize(items):
    out = []
    for m in items or []:
        out.append({
            "sender": m.get("sender") or m.get("user_id") or m.get("from"),
            "ts": m.get("ts") or m.get("timestamp"),
            "body": m.get("body") or m.get("text") or m.get("message"),
            "event_id": m.get("event_id") or m.get("id"),
        })
    return out


def read_room(room_id, agent_mxid=None, limit=DEFAULT_LIMIT, *, gate=None, before=None):
    """Pull up to `limit` recent messages from `room_id` via the gateway verb."""
    agent_mxid = agent_mxid or os.environ.get("AGENT_MXID")
    if not room_id:
        return _result(False, reason="no room_id given")
    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    gate = load_gate() if gate is None else gate
    if not gate_allows(agent_mxid, room_id, gate):
        return _result(False, reason=f"client gate denied for {agent_mxid}", room_id=room_id)
    base, headers = gateway()
    if not base:
        return _result(False, reason="no gateway configured", room_id=room_id)
    params = {"limit": limit}
    if before:
        params["before"] = before
    url = f"{base}/v1/rooms/{quote(room_id)}/messages?" + urlencode(params)
    try:
        _, body, _h = http_request("GET", url, headers)
    except HTTPError as e:
        return _result(False, reason=degrade_reason(e.code), room_id=room_id)
    except (URLError, TimeoutError) as e:
        return _result(False, reason=f"network error: {e}", room_id=room_id)
    import json
    try:
        parsed = json.loads(body.decode("utf-8") or "{}")
    except ValueError as e:
        return _result(False, reason=f"parse error: {e}", room_id=room_id)
    items = parsed.get("messages") if isinstance(parsed, dict) else parsed
    return _result(True, _normalize(items), room_id=room_id)
