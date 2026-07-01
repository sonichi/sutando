#!/usr/bin/env python3
"""Shared relay plumbing for the room-ops capabilities (read / media / react / …).

Every room-ops module is a thin **relay-only** client: it speaks the stable
`/v1` relay protocol and holds NO platform/AppService token — the relay/broker
(box-side) owns the platform creds and does the privileged Matrix ops +
authoritative membership enforcement. This module centralises the pieces every
capability shares so they aren't copy-pasted per module:

  - relay coordinates (RELAY_URL / token from env/vault)
  - the optional per-agent default-deny client gate (defense-in-depth; the relay
    is the real membership boundary)
  - HTTP helpers (json / bytes) + a uniform degrade-reason mapping

No platform literals live here.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

HTTP_TIMEOUT = 15


# --------------------------------------------------------------------------- #
# Optional client gate (defense-in-depth; relay enforces membership)
# --------------------------------------------------------------------------- #
def gate_path(env_key="ROOM_OPS_GATE", default_name="room-ops-gate.json"):
    # The default resolves relative to THIS skill dir (not cwd), so a gate file
    # placed beside the skill is found regardless of the caller's cwd — the
    # client default-deny stays reliable instead of silently None->allow when
    # the process runs from elsewhere. Callers should still set ROOM_OPS_GATE to
    # the workspace-resolved path for the real gate.
    return os.environ.get(env_key) or os.path.join(os.path.dirname(__file__), default_name)


def load_gate(path=None, env_key="ROOM_OPS_GATE"):
    """Missing file -> None (defer to the relay). Present -> dict (default-deny)."""
    path = path or gate_path(env_key)
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return {}


def gate_allows(agent_mxid, room_id, gate):
    """`gate is None` -> no client pre-filter (relay enforces). Else default-deny."""
    if gate is None:
        return True
    entry = gate.get(agent_mxid)
    if not isinstance(entry, dict):
        return False
    if room_id and room_id in (entry.get("rooms") or []):
        return True
    return bool(entry.get("all_member_rooms"))


# --------------------------------------------------------------------------- #
# Relay coordinates + HTTP
# --------------------------------------------------------------------------- #
def relay():
    """Return (base_url, headers). base is '' when no relay is configured."""
    base = (os.environ.get("RELAY_URL") or os.environ.get("REMOTE_TASK_URL") or "").rstrip("/")
    token = os.environ.get("RELAY_TOKEN") or os.environ.get("REMOTE_TASK_TOKEN")
    headers = {"User-Agent": "sutando-room-ops/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return base, headers


def http_request(method, url, headers=None, data=None, max_bytes=None):
    """Raw request → (status, body_bytes, response_headers). Raises on HTTP error.

    When `max_bytes` is set, the body read is BOUNDED to `max_bytes + 1` so a
    hostile/buggy peer can't OOM us before a higher-layer size cap applies —
    reading one extra byte lets the caller detect overflow without buffering the
    whole (possibly multi-GB) response.
    """
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read(max_bytes + 1) if max_bytes is not None else resp.read()
        return resp.status, body, dict(resp.headers)


def http_json(method, url, headers=None, payload=None):
    """JSON request/response. Returns (status, parsed_json)."""
    data = json.dumps(payload).encode() if payload is not None else None
    h = dict(headers or {})
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    status, body, _ = http_request(method, url, h, data)
    return status, json.loads(body.decode("utf-8") or "{}")


def degrade_reason(code):
    """Uniform reason for a non-2xx the caller should degrade on (never raise)."""
    if code == 404:
        return "verb unimplemented (404)"
    if code in (401, 403):
        return f"denied — agent not a joined member ({code})"
    return f"HTTP {code}"


def quote(s):
    return urllib.parse.quote(s, safe="")


def urlencode(d):
    return urllib.parse.urlencode(d)


# Re-export the urllib error types so modules catch from one place.
HTTPError = urllib.error.HTTPError
URLError = urllib.error.URLError
