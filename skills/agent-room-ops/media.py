#!/usr/bin/env python3
"""room-ops · media — send / fetch native media for an agent.

Native uploaded media both ways (the discord-bridge att.save inbound + [file:]
outbound parity). Gateway-only; the gateway does the Matrix media-repo upload/
download + membership enforcement. Outbound is constrained by a path allowlist
(ROOM_MEDIA_ALLOW) + a size ceiling (MAX_BYTES).
"""
from __future__ import annotations

import base64
import os
import tempfile

from _gateway import (gate_allows, load_gate, gateway, http_request, degrade_reason,
                    quote, HTTPError, URLError)

MAX_BYTES = 25 * 1024 * 1024


def _result(ok, *, path=None, ref=None, room_id=None, reason=None, bytes_=None):
    return {"ok": bool(ok), "room_id": room_id, "ref": ref, "path": path,
            "bytes": bytes_, "reason": reason}


def _safe_name(name):
    return os.path.basename(name or "media.bin").replace("\x00", "") or "media.bin"


def outbox_dir():
    """The dedicated per-agent outbox the skill owns (ROOM_MEDIA_OUTBOX, else a
    fixed subdir of the temp dir). Only files placed HERE are sendable by default."""
    return os.environ.get("ROOM_MEDIA_OUTBOX") or os.path.join(tempfile.gettempdir(), "sutando-media-outbox")


def _allowed_prefixes():
    env = os.environ.get("ROOM_MEDIA_ALLOW")
    if env:
        return [os.path.realpath(p) for p in env.split(os.pathsep) if p]
    # Fail (mostly) closed: default to ONLY a dedicated per-agent outbox — NOT
    # the whole OS temp dir. Otherwise any file that happens to live under /tmp
    # would be silently uploadable. Callers who need to send from elsewhere set
    # ROOM_MEDIA_ALLOW explicitly.
    return [os.path.realpath(outbox_dir())]


def _path_allowed(path):
    real = os.path.realpath(path)
    return any(real == p or real.startswith(p + os.sep) for p in _allowed_prefixes())


def _inbox_dir(dest_dir=None):
    import urllib.parse  # only used for ref-name fallback
    d = dest_dir or os.environ.get("ROOM_MEDIA_INBOX") or os.path.join(tempfile.gettempdir(), "sutando-media-inbox")
    os.makedirs(d, exist_ok=True)
    return d, urllib.parse


def fetch_media(ref, agent_mxid=None, room_id=None, *, gate=None, dest_dir=None):
    """Ask the gateway to fetch media `ref` and save it locally; return its path."""
    agent_mxid = agent_mxid or os.environ.get("AGENT_MXID")
    if not ref:
        return _result(False, reason="no media ref given", room_id=room_id)
    gate = load_gate() if gate is None else gate
    if not gate_allows(agent_mxid, room_id, gate):
        return _result(False, ref=ref, room_id=room_id, reason=f"client gate denied for {agent_mxid}")
    base, headers = gateway()
    if not base:
        return _result(False, ref=ref, room_id=room_id, reason="no gateway configured")
    from _gateway import urlencode
    q = {"ref": ref}
    if room_id:
        q["room_id"] = room_id
    url = f"{base}/v1/media/fetch?" + urlencode(q)
    try:
        # Bounded read: at most MAX_BYTES+1 so a hostile gateway returning a huge
        # body can't OOM the agent before the cap check below.
        _, body, hdrs = http_request("GET", url, headers, max_bytes=MAX_BYTES)
    except HTTPError as e:
        return _result(False, ref=ref, room_id=room_id, reason=degrade_reason(e.code))
    except (URLError, TimeoutError) as e:
        return _result(False, ref=ref, room_id=room_id, reason=f"network error: {e}")
    cl = hdrs.get("Content-Length")
    declared_over = cl is not None and cl.isdigit() and int(cl) > MAX_BYTES
    if declared_over or len(body) > MAX_BYTES:
        return _result(False, ref=ref, room_id=room_id, reason=f"media exceeds {MAX_BYTES} bytes")
    inbox, urlparse_mod = _inbox_dir(dest_dir)
    fname = _safe_name(hdrs.get("X-Media-Filename")
                       or os.path.basename(urlparse_mod.urlparse(ref).path) or "media.bin")
    out = os.path.join(inbox, fname)
    try:
        with open(out, "wb") as f:
            f.write(body)
    except OSError as e:
        return _result(False, ref=ref, room_id=room_id, reason=f"write failed: {e}")
    return _result(True, path=out, ref=ref, room_id=room_id, bytes_=len(body))


def send_media(room_id, path, agent_mxid=None, *, gate=None, caption=None):
    """Upload a local file via the gateway, which posts it as the agent."""
    agent_mxid = agent_mxid or os.environ.get("AGENT_MXID")
    if not room_id:
        return _result(False, reason="no room_id given")
    # Gate FIRST — cheapest deny; don't stat/read files for an unauthorized agent.
    gate = load_gate() if gate is None else gate
    if not gate_allows(agent_mxid, room_id, gate):
        return _result(False, room_id=room_id, reason=f"client gate denied for {agent_mxid}")
    if not path or not os.path.isfile(path):
        return _result(False, room_id=room_id, reason="file not found")
    if not _path_allowed(path):
        return _result(False, room_id=room_id, path=path, reason="path not in ROOM_MEDIA_ALLOW")
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return _result(False, room_id=room_id, reason=f"stat failed: {e}")
    if size > MAX_BYTES:
        return _result(False, room_id=room_id, path=path, reason=f"file exceeds {MAX_BYTES} bytes")
    base, headers = gateway()
    if not base:
        return _result(False, room_id=room_id, reason="no gateway configured")
    try:
        with open(path, "rb") as f:
            content = f.read()
    except OSError as e:
        return _result(False, room_id=room_id, reason=f"read failed: {e}")
    import json
    headers = {**headers, "Content-Type": "application/json"}
    payload = json.dumps({"filename": _safe_name(os.path.basename(path)),
                          "content_b64": base64.b64encode(content).decode("ascii"),
                          "caption": caption}).encode()
    url = f"{base}/v1/rooms/{quote(room_id)}/media"
    try:
        http_request("POST", url, headers, data=payload)
    except HTTPError as e:
        return _result(False, room_id=room_id, path=path, reason=degrade_reason(e.code))
    except (URLError, TimeoutError) as e:
        return _result(False, room_id=room_id, path=path, reason=f"network error: {e}")
    return _result(True, room_id=room_id, path=path, bytes_=size)
