#!/usr/bin/env python3
"""room-ops · mention — @-mention another agent reliably, by friendly handle.

The whole point: an agent should never hand-craft a peer's mxid (and get it wrong,
or forget it — the single most-repeated delivery failure). Give `mention` a handle
("qingyun-001", a label, or the name the room shows — "Bassil's Sutando") and a
message; it resolves the one canonical mxid — the live /v1/agents directory
first, then the broker's room-scoped resolver, then the room's own member list —
and posts an op:message with that mxid leading the body plus `mentions:[mxid]`.
The broker routes on the mxid either way: it stamps `mentions` into `m.mentions`,
and a member mxid in the plain body is matched as a whole token, auto-mentioned
and rendered as a pill — so the peer is actually triggered and a human reading
the room sees who was asked.

Ambiguity never posts: two candidates from any source is a refusal carrying the
candidate list, because mentioning the wrong agent is worse than mentioning
nobody. `build_body` (pure) is separated from the network post so the
mention-construction is unit-tested without a gateway.
"""
from __future__ import annotations

import os

from _gateway import gate_allows, load_gate, gateway, http_json, degrade_reason, HTTPError, URLError
from resolve import resolve_user, match_member, resolve_in_room, slug_handle, is_ambiguous
import receipt as _receipt
from relations import RelationError, relation_fields


def _result(ok, *, room_id=None, mxid=None, event_id=None, candidates=None, reason=None,
            resolved_by=None):
    # `resolved_by` names the source that produced `mxid` OR refused the handle —
    # directory | broker | room — so a hit and a refusal are both traceable.
    return {"ok": bool(ok), "room_id": room_id, "mxid": mxid, "event_id": event_id,
            "candidates": candidates or [], "reason": reason, "resolved_by": resolved_by}


def build_body(mxid: str, message: str) -> str:
    """Compose the room message so the mention actually triggers the peer.

    The mxid LEADS the body: `is_mention` matches the peer's localpart as a
    whole token, and a leading mxid is unambiguous + reads as a directed ask.
    An em dash separates it from the message when there is one.
    """
    message = (message or "").strip()
    return f"{mxid} — {message}" if message else mxid


def _resolve_from_room(handle: str, room_id: str, agent_mxid: str | None) -> "dict | None":
    """Second chance for `handle` inside the target room: broker, then roster.

    (a) The broker's `op: resolve_user` sees the room's display names, so it is
        asked first — with the handle as given, then (on a plain miss) with the
        platform's localpart spelling of it (`slug_handle`), since the broker
        normalises nothing. A hit resolves. An AMBIGUOUS answer is returned as
        the refusal it is: a second source could only turn "too many" into a
        guess, so it is never widened.
    (b) When the gateway has no such op (older broker), the broker found
        nobody, or the network failed, the member list is read and matched
        with its display names, agents preferred on a tie.

    None when neither source could answer, so the caller keeps the directory's
    own reason rather than reporting a membership miss that never happened.
    `members` is imported lazily: it reaches the network, and the directory
    path must not pay for it.
    """
    got = resolve_in_room(handle, room_id)
    if not got.get("ok") and not is_ambiguous(got) and not got.get("unsupported"):
        slug = slug_handle(handle)
        # The broker matches case-insensitively, so a case-only difference is
        # the same query and not worth a round trip.
        if slug and slug != (handle or "").strip().casefold():
            got = resolve_in_room(slug, room_id)
    if got.get("ok"):
        return {**got, "resolved_by": "broker"}
    if is_ambiguous(got):
        return {**got, "resolved_by": "broker"}

    try:
        from members import room_members
    except ImportError:
        return None
    read = room_members(room_id, agent_mxid)
    if not read.get("ok"):
        return None
    hit = match_member(handle, read.get("members") or [], prefer_agents=True)
    if hit.get("ok") or hit.get("candidates"):
        return {**hit, "resolved_by": "room"}
    return None


def mention(handle: str, message: str, room_id: str, agent_mxid: str | None = None,
            *, gate=None, agents: list | None = None,
            reply_to: str | None = None) -> dict:
    """Resolve `handle` → mxid and post a triggering @-mention into `room_id`.

    Returns {ok, room_id, mxid, event_id, candidates, reason, resolved_by}. On
    an ambiguous handle it does NOT post — it returns ok:false + the candidate
    mxids so the caller disambiguates rather than mentioning the wrong agent.
    """
    agent_mxid = agent_mxid or os.environ.get("AGENT_MXID")
    if not room_id:
        return _result(False, room_id=room_id, reason="room_id required")
    if not handle:
        return _result(False, room_id=room_id, reason="handle required")

    # Validated before resolve/gate/network for the same reason as in `say`: a
    # mention citing the wrong event is worse than one that is refused.
    try:
        rel = relation_fields(reply_to=reply_to)
    except RelationError as e:
        return _result(False, room_id=room_id, reason=str(e))

    res = resolve_user(handle, agents=agents)
    # A full mxid counts as the directory's answer: resolve_user short-circuits it.
    source = "directory"
    if not res.get("ok") and not res.get("candidates"):
        # /v1/agents lists only this account's own agents, so a peer agent in
        # the room resolves nowhere and the mention would be unreachable.
        room_res = _resolve_from_room(handle, room_id, agent_mxid)
        if room_res is not None:
            res, source = room_res, room_res.get("resolved_by")
    if not res.get("ok"):
        return _result(False, room_id=room_id, candidates=res.get("candidates"),
                       reason=res.get("reason") or "could not resolve handle",
                       resolved_by=source)
    mxid = res["mxid"]

    gate = load_gate() if gate is None else gate
    if not gate_allows(agent_mxid, room_id, gate):
        return _result(False, room_id=room_id, mxid=mxid, resolved_by=source,
                       reason=f"client gate denied for {agent_mxid}")

    base, headers = gateway()
    if not base:
        return _result(False, room_id=room_id, mxid=mxid, resolved_by=source,
                       reason="no gateway configured")

    body = build_body(mxid, message)
    try:
        # `mentions` → m.mentions (broker, 2026-07-21); the mxid LEADING `body` is
        # the routing token AND the literal pilled for humans. Both stay — never move it.
        cid = os.environ.get("SUTANDO_WORKER_SEAT") or os.environ.get("SUTANDO_CORE_ID")
        worker = os.environ.get("SUTANDO_WORKER_ID") or (f"worker-{cid}" if cid else None)
        _color = (os.environ.get("SUTANDO_WORKER_ACCENT")
                  or os.environ.get("SUTANDO_WORKER_COLOR"))  # COLOR: one-release alias
        _stripe = os.environ.get("SUTANDO_WORKER_STRIPE")
        _attn = os.environ.get("SUTANDO_WORKER_ATTENTION") == "1"
        _style = os.environ.get("SUTANDO_WORKER_STYLE")
        _styles = ("stripe", "highlight", "none")
        _w = ({"id": worker,
               **({"color": _color} if _color else {}),
               **({"stripe": _stripe != "0"} if _stripe in ("0", "1") else {}),
               **({"style": _style} if _style in _styles else {}),
               **({"attention": True} if _attn else {})}
              if worker else None)
        stamp = {"extra_content": {"space.ag2.worker": _w}} if _w else {}
        _status, parsed = http_json(
            "POST", f"{base}/v1/room", headers,
            {"op": "message", "room_id": room_id, "body": body, "mentions": [mxid], **rel, **stamp},
        )
    except HTTPError as e:
        return _result(False, room_id=room_id, mxid=mxid, resolved_by=source,
                       reason=degrade_reason(e.code))
    except (URLError, TimeoutError) as e:
        return _result(False, room_id=room_id, mxid=mxid, resolved_by=source,
                       reason=f"network error: {e}")
    # Same envelope as `say`, so the same reading — see receipt.py.
    _state, event_id, _reason = _receipt.classify(parsed)
    return _result(True, room_id=room_id, mxid=mxid, event_id=event_id, resolved_by=source)
