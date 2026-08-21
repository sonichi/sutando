#!/usr/bin/env python3
"""room-ops · members — enumerate a room's members (op `members`).

Closes the client half of a gap the gateway never had: `POST /v1/room`
{"op": "members"} has always returned every member with a display name, and
nothing exposed it. Until now an agent could see who had SPOKEN (by reading
history) but not who was PRESENT, so a member who had never posted was
invisible — which is how two peer agents in a 20-member room stayed unfindable
while `resolve` returned "no agent matches" for them.

`kind` is DERIVED here, not reported by the gateway: the member record carries
only `user_id` and `display_name`. See `classify_member` for what it can and
cannot tell you.
"""
from __future__ import annotations

from _gateway import gateway, http_json, degrade_reason, HTTPError, URLError

# An `.agent:` localpart suffix is assigned by the platform when an agent
# registers; the `sutando-` prefix is this fleet's own naming convention.
_AGENT_SUFFIX = ".agent:"
_AGENT_PREFIX = "@sutando-"


def classify_member(user_id: str) -> str:
    """"agent" | "human" — a NAMING heuristic, not an authoritative claim.

    The gateway returns no account-type field, so this reads the mxid. It is
    right for every member of every room observed so far and it will be wrong
    for any agent registered outside both conventions; treat a "human" verdict
    as "no agent marker found", which is the weaker statement it actually
    supports. Callers that must not be wrong should ask the platform, not this.
    """
    uid = (user_id or "").strip()
    if _AGENT_SUFFIX in uid or uid.startswith(_AGENT_PREFIX):
        return "agent"
    return "human"


def _result(ok, *, members=None, reason=None):
    # `members` is always a list so consumers need no None-check, matching the
    # shape rooms.joined_rooms() established.
    return {"ok": bool(ok), "members": members or [], "reason": reason}


def room_members(room_id: str, agent_mxid=None):
    """→ {ok, members, reason}; each member is {user_id, display_name, kind}."""
    base, headers = gateway()
    if not base:
        return _result(False, reason="no gateway configured")
    try:
        _status, res = http_json("POST", f"{base}/v1/room", headers,
                                 {"op": "members", "room_id": room_id})
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
    out = []
    for m in res.get("members") or []:
        if not isinstance(m, dict):
            continue
        uid = str(m.get("user_id") or "")
        if not uid:
            continue
        out.append({"user_id": uid,
                    "display_name": str(m.get("display_name") or ""),
                    "kind": classify_member(uid)})
    return _result(True, members=out)
