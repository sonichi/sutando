#!/usr/bin/env python3
"""room-ops · join — accept THIS agent's own pending room invite.

Explicit, owner-directed invite acceptance (`join`) — the self-serve counterpart
to the box-side invite-supervision auto-join (which only fires when the owner
joins the room). Until this verb, agents held invites they could never accept:
room_ops was read/send/react only, so a pending invite 403'd every other op.
Gateway-only; Matrix itself enforces that a join without a standing invite is
rejected for invite-only rooms, and the gateway cleans up the supervision's
pending_join record on success.
"""
from __future__ import annotations

import os

from _gateway import (gate_allows, load_gate, gateway, http_json, degrade_reason,
                    quote, HTTPError, URLError)


def _result(ok, *, room_id=None, reason=None):
    return {"ok": bool(ok), "room_id": room_id, "reason": reason}


def join_room(room_id, agent_mxid=None, *, gate=None):
    agent_mxid = agent_mxid or os.environ.get("AGENT_MXID")
    if not room_id:
        return _result(False, room_id=room_id, reason="room_id is required")
    gate = load_gate() if gate is None else gate
    if not gate_allows(agent_mxid, room_id, gate):
        return _result(False, room_id=room_id,
                       reason=f"client gate denied for {agent_mxid}")
    base, headers = gateway()
    if not base:
        return _result(False, room_id=room_id, reason="no gateway configured")
    url = f"{base}/v1/rooms/{quote(room_id)}/join"
    try:
        http_json("POST", url, headers, {})
    except HTTPError as e:
        return _result(False, room_id=room_id, reason=degrade_reason(e.code))
    except (URLError, TimeoutError) as e:
        return _result(False, room_id=room_id, reason=f"network error: {e}")
    return _result(True, room_id=room_id)
