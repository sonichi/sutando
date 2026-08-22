#!/usr/bin/env python3
"""room-ops · topic — a room's declared name/topic/alias (op `topic`).

Reads via the gateway's `get_state` and extracts the three descriptive
m.room.* types. Until backend #735 deploys, get_state filters those types
out and every field is null — that is the honest degrade, not an error:
"the gateway did not return it" and "the room has none" are reported
distinctly via `served_by_gateway`.

The map stores DECLARED purpose (this) separately from INFERRED purpose
(traffic): divergence between them is itself a signal.
"""
from __future__ import annotations

from _gateway import gateway, http_json, degrade_reason, HTTPError, URLError

_META = ("m.room.topic", "m.room.name", "m.room.canonical_alias")


def _result(ok, *, name=None, topic=None, alias=None, served=False, reason=None):
    return {"ok": bool(ok), "name": name, "topic": topic, "alias": alias,
            "served_by_gateway": served, "reason": reason}


def room_topic(room_id: str, agent_mxid=None):
    """→ {ok, name, topic, alias, served_by_gateway, reason}."""
    base, headers = gateway()
    if not base:
        return _result(False, reason="no gateway configured")
    try:
        _status, res = http_json("POST", f"{base}/v1/room", headers,
                                 {"op": "get_state", "room_id": room_id})
    except HTTPError as e:
        return _result(False, reason=degrade_reason(e.code))
    except (URLError, TimeoutError) as e:
        return _result(False, reason=f"network error: {e}")
    if not isinstance(res, dict) or res.get("error"):
        return _result(False, reason=str((res or {}).get("error") or
                                         "malformed gateway response"))
    fields = {}
    served = False
    for ev in res.get("events") or []:
        if ev.get("type") in _META:
            served = True
            fields[ev["type"]] = ev.get("content") or {}
    return _result(True, served=served,
                   name=fields.get("m.room.name", {}).get("name"),
                   topic=fields.get("m.room.topic", {}).get("topic"),
                   alias=fields.get("m.room.canonical_alias", {}).get("alias"))
