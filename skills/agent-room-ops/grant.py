#!/usr/bin/env python3
"""grant — make a room's access policy authoritative (design-response-policy-v0.2).

Writes the room's `space.ag2.policy` state event so its `authoritative`/`tiers`/
`default_tier` GRANT access — the room admits (and tiers) senders an agent's local
`allowFrom` would drop, per governance.py `resolve_policy`/`gate_inbound`. This is
the client half of PR #429: the governance core already honors these keys; without
a way to set them a room can never be made authoritative from the app.

Read-modify-write via the gateway op envelope: `op:get_state` reads the current
`space.ag2.policy`, we merge the grant fields (preserving other keys like
`respond`), and `op:state` writes the whole event back. Synapse power levels still
gate the actual write, so an under-privileged caller gets a clean error.
"""
from __future__ import annotations

try:
    from _gateway import gateway, http_json
except ImportError:  # pragma: no cover - path shim when imported by basename
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _gateway import gateway, http_json

POLICY_TYPE = "space.ag2.policy"
VALID_TIERS = ("owner", "guest")


def current_policy(events: "list | None") -> dict:
    """The `space.ag2.policy` (state_key "") content from an op:get_state reply.

    op:get_state returns {"events":[{type,state_key,content},...]} filtered to
    space.ag2.*; pick the room-level policy event, {} when absent."""
    for ev in events or []:
        if ev.get("type") == POLICY_TYPE and str(ev.get("state_key") or "") == "":
            c = ev.get("content")
            return dict(c) if isinstance(c, dict) else {}
    return {}


def build_grant_content(current: "dict | None", *, tiers: "dict | None" = None,
                        default_tier: "str | None" = None,
                        authoritative: bool = True, revoke: bool = False) -> dict:
    """Pure: the next `space.ag2.policy` content for a room grant.

    Starts from `current` so non-grant fields (respond, rate, read) survive the
    write. `revoke` disables the grant (authoritative -> False) without touching
    the room's other policy fields; a non-authoritative room keeps the historical
    restrict-only meaning of `tiers`."""
    out = dict(current or {})
    if revoke:
        out["authoritative"] = False
        return out
    out["authoritative"] = bool(authoritative)
    if tiers:
        merged = dict(out.get("tiers") or {})
        merged.update(tiers)
        out["tiers"] = merged
    if default_tier is not None:
        out["default_tier"] = default_tier
    return out


def grant_room(room_id: str, *, tiers: "dict | None" = None,
               default_tier: "str | None" = None, authoritative: bool = True,
               revoke: bool = False, agent_mxid: "str | None" = None) -> dict:
    """Set (or revoke) the room's authoritative grant. Read-merge-write; returns
    a normalized result dict (never raises on a gateway/HTTP fault)."""
    base, headers = gateway()
    if not base:
        return {"ok": False, "reason": "no gateway configured"}
    read_payload = {"op": "get_state", "room_id": room_id}
    if agent_mxid:
        read_payload["agent_mxid"] = agent_mxid
    try:
        _s, got = http_json("POST", f"{base}/v1/room", headers, read_payload)
    except Exception as e:  # noqa: BLE001 - degrade, don't raise
        return {"ok": False, "reason": f"read current policy failed: {e}"}
    if isinstance(got, dict) and got.get("error"):
        return {"ok": False, "reason": got["error"]}
    cur = current_policy((got or {}).get("events"))
    content = build_grant_content(cur, tiers=tiers, default_tier=default_tier,
                                  authoritative=authoritative, revoke=revoke)
    write_payload = {"op": "state", "room_id": room_id, "type": POLICY_TYPE,
                     "state_key": "", "content": content}
    if agent_mxid:
        write_payload["agent_mxid"] = agent_mxid
    try:
        _s, res = http_json("POST", f"{base}/v1/room", headers, write_payload)
    except Exception as e:  # noqa: BLE001 - degrade, don't raise
        return {"ok": False, "reason": f"policy write failed: {e}"}
    if isinstance(res, dict) and res.get("error"):
        return {"ok": False, "reason": res["error"], "content": content}
    return {"ok": True, "room_id": room_id, "content": content,
            "event_id": (res or {}).get("event_id") if isinstance(res, dict) else None}


def parse_tier_pairs(pairs: "list | None") -> dict:
    """`@user:hs=owner` strings -> {mxid: tier}. Rejects unknown tiers."""
    out: dict = {}
    for p in pairs or []:
        if "=" not in p:
            raise ValueError(f"--tier expects @user:hs=owner|guest, got {p!r}")
        who, _, tier = p.partition("=")
        who, tier = who.strip(), tier.strip()
        if not who or tier not in VALID_TIERS:
            raise ValueError(f"--tier tier must be one of {VALID_TIERS}, got {p!r}")
        out[who] = tier
    return out