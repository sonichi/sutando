#!/usr/bin/env python3
"""room-ops · rooms — list THIS agent's joined rooms (op `joined_rooms`).

Closes a documented gap: the platform agent-card references a `rooms` verb that
never existed client-side — an agent could act IN a room but couldn't enumerate
which rooms it was in. Gateway-only via the generic `/v1/room` op envelope.
There is no room target, so the per-room client gate does not apply — which
rooms the agent is a member of is the gateway's own authoritative verdict, and
the caller's identity travels in the bearer token (the `agent_mxid` parameter
exists only for CLI symmetry with the other verbs).
"""
from __future__ import annotations

from _gateway import gateway, http_json, degrade_reason, HTTPError, URLError


def _result(ok, *, rooms=None, rooms_detailed=None, reason=None):
    # Both shapes always present (lists, never None) so consumers need no
    # None-checks: `rooms` is the plain id list, `rooms_detailed` carries
    # whatever per-room metadata the gateway includes (name, member count, …).
    return {"ok": bool(ok), "rooms": rooms or [], "rooms_detailed": rooms_detailed or [],
            "reason": reason}


def joined_rooms(agent_mxid=None):
    """→ {ok, rooms, rooms_detailed, reason} via op `joined_rooms` (#184)."""
    base, headers = gateway()
    if not base:
        return _result(False, reason="no gateway configured")
    try:
        _status, res = http_json("POST", f"{base}/v1/room", headers, {"op": "joined_rooms"})
    except HTTPError as e:
        return _result(False, reason=degrade_reason(e.code))
    except (URLError, TimeoutError) as e:
        return _result(False, reason=f"network error: {e}")
    if not isinstance(res, dict):
        return _result(False, reason="malformed gateway response")
    if res.get("error"):
        return _result(False, reason=str(res["error"]))
    if res.get("ok") is False:
        return _result(False, reason=str(res.get("reason") or "gateway declined"))
    return _result(True, rooms=res.get("rooms") or [],
                   rooms_detailed=res.get("rooms_detailed") or [])
