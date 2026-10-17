#!/usr/bin/env python3
# DEPRECATION NOTICE: room ops is being replaced by the AG2 Space MCP; use its room Actions.
# Kept only as the fallback when the MCP is unreachable (see SKILL.md).
"""Shared gateway plumbing for the room-ops capabilities (read / media / react / …).

Every room-ops module is a thin **gateway-only** client: it speaks the stable
`/v1` gateway protocol and holds NO platform/AppService token — the gateway/broker
(box-side) owns the platform creds and does the privileged Matrix ops +
authoritative membership enforcement. This module centralises the pieces every
capability shares so they aren't copy-pasted per module:

  - gateway coordinates (resolved by src/relay_credentials.py: env, channel env, vault, repo .env)
  - the optional per-agent default-deny client gate (defense-in-depth; the gateway
    is the real membership boundary)
  - HTTP helpers (json / bytes) + a uniform degrade-reason mapping

No platform literals live here.
"""
from __future__ import annotations

import json
import os
import sys
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
def _core_src_on_path():
    """Put the core `src/` on sys.path so shared helpers import. False if absent."""
    cur = os.path.dirname(os.path.abspath(__file__))
    while True:
        cand = os.path.join(cur, "src")
        if os.path.isfile(os.path.join(cand, "channel_token.py")):
            if cand not in sys.path:
                sys.path.insert(0, cand)
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def _relay_credentials():
    """The core relay resolver (src/relay_credentials.py), or None when unreachable."""
    try:
        if not _core_src_on_path():
            return None
        import relay_credentials
        return relay_credentials
    except Exception:
        return None


def _lane():
    # REMOTE_TASK_CHANNEL_DIR is the gateway bridge's own lane key; a fixed
    # "ag2space" would serve every lane PROD's credential.
    return os.environ.get("REMOTE_TASK_CHANNEL_DIR") or "ag2space"


def _channel_env_file():
    """This lane's channel env file, picked as scripts/channel-env.sh picks it; None if none.

    room-ops IS the ag2space transport, so its credential lives beside the other
    providers'. Kept as a seam so tests can shadow the file the way they shadow the vault.
    """
    try:
        rc = _relay_credentials()
        return None if rc is None else rc.channel_env_file(_lane())
    except Exception:
        return None


def _token_from_vault(vault_get=None):
    """First gateway alias stored in the vault; '' on any failure. Never logged.

    Covers the desktop-spawned core, whose supervisor passes a fixed env
    whitelist: `vault set REMOTE_TASK_TOKEN <url|secret>` still arms room ops.
    """
    try:
        rc = _relay_credentials()
        if rc is None:
            return ""
        from channel_token import token_from_vault
        return rc.vault_token(lambda var: token_from_vault(var, vault_get=vault_get))
    except Exception:
        return ""


def _credential_contract():
    """Import the vendored shared credential contract (generated from
    shared/ag2_gateway_credentials.py — see #2668). Flat sibling import with
    a skill-dir sys.path assist for importlib-loaded contexts (tests load
    this module by file path). This is import plumbing, NOT a fallback
    resolver — there is exactly one parsing implementation."""
    try:
        import gateway_credentials as _gc
    except ImportError:
        d = os.path.dirname(os.path.abspath(__file__))
        if d not in sys.path:
            sys.path.insert(0, d)
        import gateway_credentials as _gc
    return _gc


def gateway():
    """Return (base_url, headers). base is '' when no gateway is configured.

    Tier order and parsing belong to src/relay_credentials.py (env, channel env,
    vault, repo .env). The env chain stays GATEWAY_TOKEN > RELAY_TOKEN >
    REMOTE_TASK_TOKEN: room-ops has never read AG2_REMOTE_TOKEN from env.
    """
    rc = _relay_credentials()
    if rc is not None:
        creds = rc.resolve_relay_credentials(
            _lane(), env_token_names=("GATEWAY_TOKEN", "RELAY_TOKEN", "REMOTE_TASK_TOKEN"),
            channel=_channel_env_file(), vault=_token_from_vault)
    else:
        # No core src/ next to this skill: the env tier is all there is.
        gc = _credential_contract()
        raw, _ = gc.resolve_alias_precedence(
            os.environ, ("GATEWAY_TOKEN", "RELAY_TOKEN", "REMOTE_TASK_TOKEN"))
        url, _ = gc.resolve_alias_precedence(
            os.environ, ("GATEWAY_URL", "RELAY_URL", "REMOTE_TASK_URL"))
        creds = gc.normalize_credentials(raw, explicit_url=url)
    headers = {"User-Agent": "sutando-room-ops/1"}
    if creds.token:
        headers["Authorization"] = f"Bearer {creds.token}"
    return creds.base_url, headers


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
    if code == 401:
        # 401 = the gateway could not authenticate the bearer at all (missing /
        # wrong / un-split combined token) — NOT a membership verdict. Keep this
        # distinct from 403 so a token problem isn't misread as "not a member".
        return "auth failed — check the gateway bearer token (401)"
    if code == 403:
        return "denied — agent not a joined member (403)"
    return f"HTTP {code}"


# Not "auth-ish codes": codes where a wrong reason sends someone to the wrong
# subsystem (401 = the bearer, 403 = authorization), so the server's text is appended.
AUTH_STATUSES = frozenset({401, 403})


def degrade_reason_parsed(code, parsed):
    """degrade_reason() plus the server's own `{"error": ...}` text, read from a
    body the caller has ALREADY parsed. For the caller that must also inspect
    that body itself — resolve.py tells a broker 404 miss from a missing op by
    it — and so cannot let degrade_reason_from() consume it. Pure; the one
    reading of the server's text, which degrade_reason_from() delegates to."""
    reason = degrade_reason(code)
    server_msg = str(parsed.get("error")) if isinstance(parsed, dict) and parsed.get("error") else ""
    if not server_msg:
        return reason
    if code in AUTH_STATUSES:
        return f"{reason} (server said: {server_msg})"
    return server_msg


def degrade_reason_from(err):
    """degrade_reason() plus what the server actually said (`{"error": ...}`).

    CONSUMES the response body: `err.read()` is single-shot, so a caller that
    also wants the parsed body must read it first and use degrade_reason_parsed.
    Measured 2026-09-02: a 403 that read "not a joined member" was really
    "platform grant events.subscribe missing" — a different subsystem entirely."""
    try:
        parsed = json.loads(err.read().decode("utf-8") or "{}")
    except Exception:
        parsed = None
    return degrade_reason_parsed(err.code, parsed)


def quote(s):
    return urllib.parse.quote(s, safe="")


def urlencode(d):
    return urllib.parse.urlencode(d)


# Re-export the urllib error types so modules catch from one place.
HTTPError = urllib.error.HTTPError
URLError = urllib.error.URLError
