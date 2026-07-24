#!/usr/bin/env python3
"""room-ops · rooms — list the rooms THIS agent is a member of.

Client verb for the `joined_rooms` room-list op (ag2space-backend#180 exposed
it publicly 2026-07-24; the handler always existed for the presence sweep).
Returns both shapes the op emits: `rooms` (bare ids, stable/back-compat) and
`rooms_detailed` ([{room_id, name}] — the usable directory).

Why it matters (the #177 gap): without a room list an agent can't discover
where it lives, and `create` can't be made idempotent. The conventions rule is
list-before-create — call this before `create` instead of retrying blind.

Gateway-only like every verb here: the generic `/v1/room` op envelope; no
platform creds client-side; membership is whatever the agent's own Matrix
session actually holds (nio's synced room set).
"""
from __future__ import annotations

import os

from _gateway import (gate_allows, load_gate, gateway, http_json, degrade_reason,
                    HTTPError, URLError)


def _result(ok, *, rooms=None, rooms_detailed=None, reason=None):
    return {"ok": bool(ok), "rooms": rooms or [],
            "rooms_detailed": rooms_detailed or [], "reason": reason}


def list_rooms(agent_mxid=None, *, gate=None):
    """The agent's joined rooms via op:joined_rooms. No room_id input — the
    subject is the agent itself, so the client gate is checked agent-wide
    (empty room scope)."""
    agent_mxid = agent_mxid or os.environ.get("AGENT_MXID")
    gate = load_gate() if gate is None else gate
    # Room-scoped gates can't apply (no target room); an agent-level deny still
    # must hold. Empty room id = the gate's agent-wide rule.
    if not gate_allows(agent_mxid, "", gate):
        return _result(False, reason=f"client gate denied for {agent_mxid}")
    base, headers = gateway()
    if not base:
        return _result(False, reason="no gateway configured")
    try:
        _status, res = http_json("POST", f"{base}/v1/room", headers,
                                 {"op": "joined_rooms"})
    except HTTPError as e:
        return _result(False, reason=degrade_reason(e.code))
    except (URLError, TimeoutError) as e:
        return _result(False, reason=f"network error: {e}")
    if isinstance(res, dict) and res.get("error"):
        return _result(False, reason=str(res["error"]))
    res = res if isinstance(res, dict) else {}
    return _result(True, rooms=list(res.get("rooms") or []),
                   rooms_detailed=list(res.get("rooms_detailed") or []))
