#!/usr/bin/env python3
"""Shared gateway plumbing for the room-ops capabilities (read / media / react / …).

Every room-ops module is a thin **gateway-only** client: it speaks the stable
`/v1` gateway protocol and holds NO platform/AppService token — the gateway/broker
(box-side) owns the platform creds and does the privileged Matrix ops +
authoritative membership enforcement. This module centralises the pieces every
capability shares so they aren't copy-pasted per module:

  - gateway coordinates (GATEWAY_URL / token from env/vault; RELAY_* honored as aliases)
  - the optional per-agent default-deny client gate (defense-in-depth; the gateway
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
# Optional client gate (defense-in-depth; gateway enforces membership)
# --------------------------------------------------------------------------- #
def gate_path(env_key="ROOM_OPS_GATE", default_name="room-ops-gate.json"):
    # The default resolves relative to THIS skill dir (not cwd), so a gate file
    # placed beside the skill is found regardless of the caller's cwd — the
    # client default-deny stays reliable instead of silently None->allow when
    # the process runs from elsewhere. Callers should still set ROOM_OPS_GATE to
    # the workspace-resolved path for the real gate.
    return os.environ.get(env_key) or os.path.join(os.path.dirname(__file__), default_name)


def load_gate(path=None, env_key="ROOM_OPS_GATE"):
    """Missing file -> None (defer to the gateway). Present -> dict (default-deny)."""
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
    """`gate is None` -> no client pre-filter (gateway enforces). Else default-deny."""
    if gate is None:
        return True
    entry = gate.get(agent_mxid)
    if not isinstance(entry, dict):
        return False
    if room_id and room_id in (entry.get("rooms") or []):
        return True
    return bool(entry.get("all_member_rooms"))


# --------------------------------------------------------------------------- #
# Gateway coordinates + HTTP
# --------------------------------------------------------------------------- #
def gateway():
    """Return (base_url, headers). base is '' when no gateway is configured.

    Honors the one-token onboarding contract used by remote-gateway-bridge.py:
    `REMOTE_TASK_TOKEN` may be the COMBINED `"https://<gateway>|<secret>"` form
    (the URL travels inside the token) or a bare secret. Precedence:
      - explicit GATEWAY_URL (alias RELAY_URL/REMOTE_TASK_URL) > URL-from-combined-token
      - explicit GATEWAY_TOKEN (alias RELAY_TOKEN)     > secret-from-combined-token
    Without this, a standard combined-token install would get base='' (every op
    degrades "no gateway") or send the whole `url|secret` as the bearer.
    """
    # GATEWAY_* is the primary name; RELAY_* and REMOTE_TASK_* are honored as
    # transition aliases so nothing breaks mid-migration.
    explicit_token = os.environ.get("GATEWAY_TOKEN") or os.environ.get("RELAY_TOKEN")
    raw = explicit_token or os.environ.get("REMOTE_TASK_TOKEN") or ""
    url_from_token = ""
    if explicit_token:
        token = explicit_token  # explicit bearer — never split
    elif "|" in raw:
        url_from_token, token = raw.split("|", 1)  # combined onboarding string
    else:
        token = raw  # bare secret
    base = (os.environ.get("GATEWAY_URL") or os.environ.get("RELAY_URL")
            or os.environ.get("REMOTE_TASK_URL") or url_from_token or "").rstrip("/")
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
        if max_bytes is not None:
            # Content-Length up-front: if the peer DECLARES an oversize body,
            # don't allocate it at all. Otherwise read at most max_bytes+1 so an
            # undeclared huge body still can't OOM us.
            cl = resp.headers.get("Content-Length")
            if cl is not None and cl.isdigit() and int(cl) > max_bytes:
                body = b""
            else:
                body = resp.read(max_bytes + 1)
        else:
            body = resp.read()
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
