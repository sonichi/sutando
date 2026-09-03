#!/usr/bin/env python3
"""room-ops · doc — read/write/delete a room's shared Room Context documents.

The missing half of room participation: `read`/`send`/`react` cover the room
TIMELINE; `doc` covers the room's durable SHARED STATE — the per-room Room Context store
folders (`room-live-context/`, `room-todo/`, `room-memo/`,
`room-live-transcript/` by convention, or any agent-defined folder the gateway
accepts). With these three verbs an agent can maintain a room's context,
action items, and notes itself instead of relying on one designated writer.

Gateway-only like every verb here: speaks the generic `/v1/room` op envelope
(`prep_get` / `prep_put` / `prep_delete`); holds no platform token; membership
is enforced gateway-side. Folder/filename validation is authoritative at the
gateway — the client passes them through verbatim.
"""
from __future__ import annotations

import base64
import os

from _gateway import (gate_allows, load_gate, gateway, http_json, degrade_reason_from,
                      HTTPError, URLError)

DEFAULT_FOLDER = "room-live-context"


def _result(ok, *, room_id=None, folder=None, name=None, content=None,
            sha=None, reason=None):
    return {"ok": bool(ok), "room_id": room_id, "folder": folder, "name": name,
            "content": content, "sha": sha, "reason": reason}


def _call(op, room_id, agent_mxid, gate, extra):
    """Shared plumbing: gate check → op-envelope POST → uniform result dict."""
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
    payload = {"op": op, "room_id": room_id, **extra}
    try:
        _status, res = http_json("POST", f"{base}/v1/room", headers, payload)
    except HTTPError as e:
        # A 4xx may carry a structured {"error": ...} body — e.g. a genuine
        # "<folder>/<name> not found" for a missing doc, which is distinct from
        # an unimplemented verb. degrade_reason() alone flattens every 404 to
        # "verb unimplemented (404)", which misreports an absent doc as a dead
        # doc backend (observed 2026-07-28: a missing plan doc read as "verb
        # unimplemented", masking that prep_get was in fact working). Prefer the
        # server's own error string when it sent one; fall back to degrade_reason.
        #
        # But the override is NOT unconditional. degrade_reason() encodes one
        # distinction the body must never be allowed to erase: 401 ("auth failed
        # — check the gateway bearer token") vs 403 ("denied — agent not a joined
        # member"). See _gateway.py's own comment at degrade_reason(). Those two
        # send a debugger to different places — one to the credential, one to
        # room membership — and the gateway's prose for either is not reliably
        # about the same thing. Unscoped, a structured body on a 401 renders as a
        # membership verdict, which is precisely backwards:
        #
        #   401 + {"error": "denied - agent not a joined member"}
        #        -> read as a membership problem; the real fault is the token
        #   403 + {"error": "roadmap/plan.md not found"}
        #        -> read as a missing doc; the real fault is membership
        #
        # So for auth statuses the local diagnosis stays authoritative and the
        # server's message is APPENDED, never substituted — surfacing what the
        # server said without letting it overwrite what the status code means.
        # Dropping it entirely would trade one silent loss for another.
        reason = degrade_reason_from(e)
        return _result(False, room_id=room_id, folder=extra.get("folder"),
                       name=extra.get("filename"), reason=reason)
    except (URLError, TimeoutError) as e:
        return _result(False, room_id=room_id, reason=f"network error: {e}")
    if isinstance(res, dict) and res.get("error"):
        return _result(False, room_id=room_id, folder=extra.get("folder"),
                       name=extra.get("filename"), reason=str(res["error"]))
    return res


def doc_get(room_id, folder=DEFAULT_FOLDER, name=None, agent_mxid=None, *, gate=None):
    """Fetch one Room Context doc (or the folder's default doc when `name` is None)."""
    extra = {"folder": folder}
    if name:
        extra["filename"] = name
    res = _call("prep_get", room_id, agent_mxid, gate, extra)
    if not isinstance(res, dict) or res.get("ok") is False:
        return res
    return _result(True, room_id=room_id, folder=folder,
                   name=res.get("file") or name,
                   content=res.get("content") or res.get("prep") or "")


def doc_put(room_id, content, folder=DEFAULT_FOLDER, name="CONTEXT.md",
            message=None, agent_mxid=None, *, gate=None):
    """Create or update one Room Context doc (full-content write, gateway commits)."""
    extra = {
        "folder": folder, "filename": name,
        "content_b64": base64.b64encode((content or "").encode()).decode(),
    }
    if message:
        extra["message"] = str(message)
    res = _call("prep_put", room_id, agent_mxid, gate, extra)
    if not isinstance(res, dict) or res.get("ok") is False:
        return res
    return _result(True, room_id=room_id, folder=folder,
                   name=res.get("file") or name, sha=res.get("sha"))


def doc_rm(room_id, name, folder=DEFAULT_FOLDER, agent_mxid=None, *, gate=None):
    """Delete one Room Context doc (404 at the gateway degrades to a graceful ok:false)."""
    res = _call("prep_delete", room_id, agent_mxid, gate,
                {"folder": folder, "filename": name})
    if not isinstance(res, dict) or res.get("ok") is False:
        return res
    return _result(True, room_id=room_id, folder=folder, name=name)
