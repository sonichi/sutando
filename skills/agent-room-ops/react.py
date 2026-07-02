#!/usr/bin/env python3
"""room-ops · react — add / remove an agent's reaction on a room event.

Native m.reaction add (`react`) + redact (`unreact`) — the Discord auto-react
instant-ack parity (👀 on receipt, ✅/⚠️ on done/fail). The event is typically
the task's source_message_id. Gateway-only; membership enforced gateway-side.
"""
from __future__ import annotations

import os

from _gateway import (gate_allows, load_gate, gateway, http_json, degrade_reason,
                    quote, HTTPError, URLError)

ACK = {"received": "👀", "working": "⏳", "done": "✅", "fail": "⚠️"}


def _result(ok, *, room_id=None, event_id=None, key=None, reason=None):
    return {"ok": bool(ok), "room_id": room_id, "event_id": event_id, "key": key, "reason": reason}


def _op(verb, room_id, event_id, key, agent_mxid, gate):
    agent_mxid = agent_mxid or os.environ.get("AGENT_MXID")
    if not room_id or not event_id or not key:
        return _result(False, room_id=room_id, event_id=event_id, key=key,
                       reason="room_id, event_id and key are all required")
    gate = load_gate() if gate is None else gate
    if not gate_allows(agent_mxid, room_id, gate):
        return _result(False, room_id=room_id, event_id=event_id, key=key,
                       reason=f"client gate denied for {agent_mxid}")
    base, headers = gateway()
    if not base:
        return _result(False, room_id=room_id, event_id=event_id, key=key,
                       reason="no gateway configured")
    url = f"{base}/v1/rooms/{quote(room_id)}/{verb}"
    try:
        http_json("POST", url, headers, {"event_id": event_id, "key": key})
    except HTTPError as e:
        return _result(False, room_id=room_id, event_id=event_id, key=key, reason=degrade_reason(e.code))
    except (URLError, TimeoutError) as e:
        return _result(False, room_id=room_id, event_id=event_id, key=key, reason=f"network error: {e}")
    return _result(True, room_id=room_id, event_id=event_id, key=key)


def react(room_id, event_id, key, agent_mxid=None, *, gate=None):
    return _op("react", room_id, event_id, key, agent_mxid, gate)


def unreact(room_id, event_id, key, agent_mxid=None, *, gate=None):
    return _op("unreact", room_id, event_id, key, agent_mxid, gate)
