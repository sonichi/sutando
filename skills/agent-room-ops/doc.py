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

from _gateway import (gate_allows, load_gate, gateway, http_json, degrade_reason,
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
        return _result(False, room_id=room_id, reason=degrade_reason(e.code))
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
