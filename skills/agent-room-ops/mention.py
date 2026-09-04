#!/usr/bin/env python3
"""room-ops · mention — @-mention another agent reliably, by friendly handle.

The whole point: an agent should never hand-craft a peer's mxid (and get it wrong,
or forget it — the single most-repeated delivery failure). Give `mention` a handle
("qingyun-001") and a message; it resolves the one canonical mxid from the live
/v1/agents directory and posts an op:message with that mxid leading the body — the
form the broker's `is_mention` matches (localpart as a whole token), so the peer is
actually triggered.

`build_body` (pure) is separated from the network post so the mention-construction
is unit-tested without a gateway.
"""
from __future__ import annotations

import os

from _gateway import gate_allows, load_gate, gateway, http_json, degrade_reason, HTTPError, URLError
from resolve import resolve_user, match_member
import receipt as _receipt
from relations import RelationError, relation_fields


def _result(ok, *, room_id=None, mxid=None, event_id=None, candidates=None, reason=None,
            state=None):
    # Same tri-state as `say` (receipt.py): a caller reading only `ok` cannot
    # tell a timeout from a refusal, and the two license opposite retries.
    return {"ok": bool(ok), "room_id": room_id, "mxid": mxid, "event_id": event_id,
            "candidates": candidates or [], "reason": reason,
            "state": state or (_receipt.CONFIRMED if ok else _receipt.FAILED)}


def build_body(mxid: str, message: str) -> str:
    """Compose the room message so the mention actually triggers the peer.

    The mxid LEADS the body: `is_mention` matches the peer's localpart as a
    whole token, and a leading mxid is unambiguous + reads as a directed ask.
    An em dash separates it from the message when there is one.
    """
    message = (message or "").strip()
    return f"{mxid} — {message}" if message else mxid


def _resolve_from_room(handle: str, room_id: str, agent_mxid: str | None) -> "dict | None":
    """Second chance for `handle` against the target room's membership.

    None when the member list itself could not be read, so the caller keeps the
    directory's own reason rather than reporting a membership miss that never
    happened. Imported lazily: `members` reaches the network, and the directory
    path must not pay for it.
    """
    try:
        from members import room_members
    except ImportError:
        return None
    got = room_members(room_id, agent_mxid)
    if not got.get("ok"):
        return None
    ids = [m if isinstance(m, str) else (m.get("user_id") or m.get("id"))
           for m in got.get("members") or []]
    hit = match_member(handle, [i for i in ids if i])
    return hit if hit.get("ok") or hit.get("candidates") else None


def mention(handle: str, message: str, room_id: str, agent_mxid: str | None = None,
            *, gate=None, agents: list | None = None,
            reply_to: str | None = None) -> dict:
    """Resolve `handle` → mxid and post a triggering @-mention into `room_id`.

    Returns {ok, room_id, mxid, event_id, candidates, reason}. On an ambiguous
    handle it does NOT post — it returns ok:false + the candidate mxids so the
    caller disambiguates rather than mentioning the wrong agent.
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
    if not res.get("ok") and not res.get("candidates"):
        # /v1/agents lists only this account's own agents, so a peer agent in
        # the room resolves nowhere and the mention would be unreachable.
        res = _resolve_from_room(handle, room_id, agent_mxid) or res
    if not res.get("ok"):
        return _result(False, room_id=room_id, candidates=res.get("candidates"),
                       reason=res.get("reason") or "could not resolve handle")
    mxid = res["mxid"]

    gate = load_gate() if gate is None else gate
    if not gate_allows(agent_mxid, room_id, gate):
        return _result(False, room_id=room_id, mxid=mxid,
                       reason=f"client gate denied for {agent_mxid}")

    base, headers = gateway()
    if not base:
        return _result(False, room_id=room_id, mxid=mxid, reason="no gateway configured")

    body = build_body(mxid, message)
    try:
        # `mentions` is forward-compat: the leading mxid in `body` already
        # triggers via the broker's localpart text-match, so this is harmlessly
        # ignored today — but it auto-activates structured push-notifications the
        # moment the broker honors it (a peer-review ask, ties to broker #151).
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
        return _result(False, room_id=room_id, mxid=mxid, reason=degrade_reason(e.code))
    except (URLError, TimeoutError) as e:
        return _result(False, room_id=room_id, mxid=mxid, reason=f"network error: {e}",
                       state=_receipt.UNKNOWN)
    # Same envelope as `say`, so the same reading — see receipt.py.
    _state, event_id, _reason = _receipt.classify(parsed)
    return _result(True, room_id=room_id, mxid=mxid, event_id=event_id, reason=_reason,
                   state=_state)
