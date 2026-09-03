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


def _channel_env_file():
    """Path to the ag2space channel `.env`, or None when it cannot be located.

    room-ops IS the ag2space transport, so its credential lives beside the other
    providers' — `channel_token.py` states the rule: each bridge reads its own
    `channels/<name>/.env`. Returned rather than inlined so tests can shadow this
    boundary the way they already shadow the vault.
    """
    try:
        if not _core_src_on_path():
            return None
        from util_paths import claude_home_path
        p = claude_home_path("channels", "ag2space", ".env")
        return p if os.path.isfile(p) else None
    except Exception:
        return None


def _from_channel_env(names, env_file=None):
    """First non-empty value among `names` in the channel `.env`; '' if none.

    Sits between the process env and the vault, so an exported value still wins
    and a stored one still loses. Alias set matches the VAULT tier (which
    includes the legacy AG2_REMOTE_TOKEN), not the narrower env chain.
    """
    try:
        env_file = _channel_env_file() if env_file is None else env_file
        if env_file is None:
            return ""
        if not _core_src_on_path():
            return ""
        from channel_token import token_from_env_file
        from pathlib import Path
    except Exception:
        return ""
    for var in names:
        # token_from_env_file is already total on OSError; UnicodeDecodeError is
        # not an OSError, so decode failures would otherwise read as "no token".
        try:
            got = token_from_env_file(var, Path(env_file))
        except (OSError, UnicodeDecodeError):
            return ""
        if got:
            return got
    return ""


def _token_from_vault(vault_get=None):
    """Vault fallback for the gateway bearer — parity with the channel bridges
    (sonichi#2638) and the sparrow bridge.

    gateway() resolves the token from GATEWAY_TOKEN / RELAY_TOKEN /
    REMOTE_TASK_TOKEN in the process env; when the launcher didn't export any of
    them (the desktop-spawned core is the case that bites — its supervisor uses a
    fixed env whitelist), `vault set REMOTE_TASK_TOKEN <url|secret>` should still
    arm room ops. Nothing here read the vault, so it was a silent no-op — even
    though this module's own docstring already promised "token from env/vault".
    This closes that gap and makes the code match the doc.

    Reuses the shared core policy `channel_token.token_from_vault` (never copies
    it); total-failure-safe; the value is never logged. Tries the names gateway()
    honors, then the legacy `AG2_REMOTE_TOKEN` alias. Prefer the **combined**
    onboarding value (`https://<gateway>|<secret>`) so the URL travels with the
    token: a vault-set BARE secret with no REMOTE_TASK_URL env resolves a bearer
    but no base, which degrades to "no gateway configured" exactly as an env-only
    bare secret does today (caller-consistent — the vault tier adds no new URL
    obligation).
    """
    try:
        if not _core_src_on_path():
            return ""
        from channel_token import token_from_vault
    except Exception:
        return ""
    for var in ("GATEWAY_TOKEN", "RELAY_TOKEN", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN"):
        tok = token_from_vault(var, vault_get=vault_get)
        if tok:
            return tok
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

    PR2 of the credential-contract migration (#2668): parsing/precedence now
    delegates to the vendored shared contract; this facade keeps only the
    room-ops runtime pieces (the vault tier and header shape). Named
    behavior change vs the legacy resolver (enabling-only, ratified in
    #2668): combined onboarding tokens now also split on `%7C`/`%7c` and
    with a case-insensitive scheme — tokens that previously failed auth on
    room-ops (sent whole as the bearer) now work, matching sparrow.

    Env chain is DELIBERATELY unchanged: GATEWAY_TOKEN > RELAY_TOKEN >
    REMOTE_TASK_TOKEN (room-ops has never read AG2_REMOTE_TOKEN from env —
    the vault tier still tries it), URL: GATEWAY_URL > RELAY_URL >
    REMOTE_TASK_URL > url-from-token. Vault stays last so a stored value
    never shadows a fresher env token.
    """
    gc = _credential_contract()
    raw, _name = gc.resolve_alias_precedence(
        os.environ, ("GATEWAY_TOKEN", "RELAY_TOKEN", "REMOTE_TASK_TOKEN"))
    if not raw:
        raw = _from_channel_env(
            ("GATEWAY_TOKEN", "RELAY_TOKEN", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN"))
    if not raw:
        raw = _token_from_vault()
    explicit_url, _ = gc.resolve_alias_precedence(
        os.environ, ("GATEWAY_URL", "RELAY_URL", "REMOTE_TASK_URL"))
    if not explicit_url:
        explicit_url = _from_channel_env(("GATEWAY_URL", "RELAY_URL", "REMOTE_TASK_URL"))
    creds = gc.normalize_credentials(raw, explicit_url=explicit_url,
                                     source="resolved" if raw else "none")
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


# Statuses whose local diagnosis must not be overwritten by the server's prose:
# 401 points at the bearer, 403 at authorization. The server's text is appended.
AUTH_STATUSES = frozenset({401, 403})


def degrade_reason_from(err):
    """degrade_reason() plus what the server actually said (`{"error": ...}`).
    Measured 2026-09-02: a 403 that read "not a joined member" was really
    "platform grant events.subscribe missing" — a different subsystem entirely."""
    reason = degrade_reason(err.code)
    try:
        parsed = json.loads(err.read().decode("utf-8") or "{}")
    except Exception:
        parsed = None
    server_msg = str(parsed.get("error")) if isinstance(parsed, dict) and parsed.get("error") else ""
    if not server_msg:
        return reason
    if err.code in AUTH_STATUSES:
        return f"{reason} (server said: {server_msg})"
    return server_msg


def quote(s):
    return urllib.parse.quote(s, safe="")


def urlencode(d):
    return urllib.parse.urlencode(d)


# Re-export the urllib error types so modules catch from one place.
HTTPError = urllib.error.HTTPError
URLError = urllib.error.URLError
