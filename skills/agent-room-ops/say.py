#!/usr/bin/env python3
"""room-ops · say — post a plain message into a room, addressed to nobody.

`mention` already reaches `op:message`, but it welds a resolved mxid onto the
front of every body so the peer's `is_mention` matcher fires. That makes it the
wrong tool for a status line, an answer to the room, or anything a reader should
see without someone being pinged — and it was the only subcommand that could post
text at all, so "post without mentioning" was unreachable rather than disallowed.

`say` posts the body verbatim and sends no `mentions`. The client gate is applied
exactly as `mention` applies it: a room this agent may not post into is refused
here too.
"""
from __future__ import annotations

import os

from _gateway import gate_allows, load_gate, gateway, http_json, degrade_reason, HTTPError, URLError
import receipt as _receipt
from relations import RelationError, relation_fields


def _result(ok, *, room_id=None, event_id=None, reason=None, state=None):
    return {"ok": bool(ok), "room_id": room_id, "event_id": event_id,
            "reason": reason, "state": state or (_receipt.CONFIRMED if ok else _receipt.FAILED)}


def say(message: str, room_id: str, agent_mxid: str | None = None, gate=None,
        *, reply_to: str | None = None, worker: str | None = None) -> dict:
    """Post `message` into `room_id` verbatim, mentioning no one.

    `reply_to` cites the message being replied to; the post stays in the main
    timeline. See relations.relation_fields.

    Returns {ok, room_id, event_id, reason}. Refuses before any network call when
    the room is missing, the body is empty, or the client gate denies the room.
    """
    if not room_id:
        return _result(False, room_id=room_id, reason="room_id required")
    # An empty body is a no-op post that still lands as a room event; refusing is
    # cheaper than asking a reader to interpret a blank line.
    if not message or not message.strip():
        return _result(False, room_id=room_id, reason="message required")

    # Before the gate and the network: a bad event id is the caller's typo, and
    # posting it unrelated would cite the wrong message silently.
    try:
        rel = relation_fields(reply_to=reply_to)
    except RelationError as e:
        return _result(False, room_id=room_id, reason=str(e))

    if agent_mxid is None:
        agent_mxid = os.environ.get("AGENT_MXID")

    gate = load_gate() if gate is None else gate
    if not gate_allows(agent_mxid, room_id, gate):
        return _result(False, room_id=room_id, reason=f"client gate denied for {agent_mxid}")

    base, headers = gateway()
    if not base:
        return _result(False, room_id=room_id, reason="no gateway configured")

    try:
        # No `mentions` key: `say` must not ping. A body carrying an mxid the
        # caller wrote is theirs; this function never prepends one.
        if worker is None:
            worker = os.environ.get("SUTANDO_WORKER_ID") or (
                f"core-{os.environ['SUTANDO_CORE_ID']}"
                if os.environ.get("SUTANDO_CORE_ID") else None)
        # The client renders attribution from this per-event stamp; a direct
        # post self-declares its worker, and optionally its color.
        _color = os.environ.get("SUTANDO_WORKER_COLOR")
        _stripe = os.environ.get("SUTANDO_WORKER_STRIPE")
        _attn = os.environ.get("SUTANDO_WORKER_ATTENTION") == "1"
        _style = os.environ.get("SUTANDO_WORKER_STYLE")
        _styles = ("stripe", "highlight", "underline", "glow", "gradient", "none")
        _w = ({"id": worker,
               **({"color": _color} if _color else {}),
               **({"stripe": _stripe != "0"} if _stripe in ("0", "1") else {}),
               **({"style": _style} if _style in _styles else {}),
               **({"attention": True} if _attn else {})}
              if worker else None)
        stamp = {"extra_content": {"space.ag2.worker": _w}} if _w else {}
        _status, parsed = http_json(
            "POST", f"{base}/v1/room", headers,
            {"op": "message", "room_id": room_id, "body": message, **rel, **stamp},
        )
    except HTTPError as e:
        return _result(False, room_id=room_id, reason=degrade_reason(e.code))
    except (URLError, TimeoutError) as e:
        return _result(False, room_id=room_id, reason=f"network error: {e}")
    # Shared with mention via receipt.classify — one reading of the envelope.
    # UNCONFIRMED stays ok:true so a caller does not re-send a delivered message.
    state, event_id, reason = _receipt.classify(parsed)
    return _result(True, room_id=room_id, event_id=event_id, reason=reason, state=state)
