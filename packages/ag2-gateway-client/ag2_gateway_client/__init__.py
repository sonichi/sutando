"""ag2-gateway-client — the ONE credential/identity/request implementation
for AG2 Space gateway clients (PR1 of the extraction; Feature Haul 2026-08-05).

Consumers: ag2-sparrow (resident task/event transport) and the
agent-room-ops skill (on-demand capability client). After PR2/PR3 this
package is the ONLY place that may:
  - read GATEWAY_TOKEN / RELAY_TOKEN / REMOTE_TASK_TOKEN / AG2_REMOTE_TOKEN
  - split the combined ``url|secret`` onboarding form
  - implement the env-precedence chain or the token-file tier
  - resolve the AGENT_MXID / AGENT_ID fallback chain
Consumers re-implementing any of the above is a boundary violation (the
semantic drift guard added with PR2/PR3 fails CI on it).

Day-one constraints (owner-ratified):
  - stdlib only; never imports the Sutando repo's ``src/`` (vault access is
    an INJECTED callable — see ``resolve_credentials(vault_token_reader=)``)
  - no sparrow lease/task logic, no room-ops gate/policy, no resident
    events runtime
  - provider-neutral public API; independently publishable metadata

Identity is a CLIENT CLAIM, not authorization (owner condition 2):
``resolve_identity`` reports what this process declares about itself. The
gateway's authenticated token subject remains the effective agent identity;
servers may use the declared MXID for consistency checks only and MUST NOT
trust it for authorization.
"""
from __future__ import annotations

import json as _json
import os as _os
import urllib.error as _uerror
import urllib.parse as _uparse
import urllib.request as _urequest
from typing import Callable, Mapping, NamedTuple, Optional

__all__ = [
    "Credentials",
    "PROFILES",
    "HTTPError",
    "URLError",
    "resolve_credentials",
    "resolve_identity",
    "request",
    "request_json",
    "degrade_reason",
]

# Named timeout profiles (owner condition 3). ``request`` REQUIRES a profile —
# there is deliberately no default, so call sites never scatter magic numbers
# and the interactive/long-poll split can't be flattened by refactors.
PROFILES: dict[str, int] = {
    "interactive": 15,   # room-ops style request/response
    "long_poll": 35,     # task/event long-poll (server wait + headroom)
    "upload": 120,       # media/artifact push
    "download": 120,     # media/artifact fetch
}

TOKEN_ENV_PRECEDENCE = ("GATEWAY_TOKEN", "RELAY_TOKEN", "REMOTE_TASK_TOKEN",
                        "AG2_REMOTE_TOKEN")
URL_ENV_PRECEDENCE = ("GATEWAY_URL", "RELAY_URL", "REMOTE_TASK_URL")

HTTPError = _uerror.HTTPError
URLError = _uerror.URLError


class Credentials(NamedTuple):
    """Resolved gateway coordinates. ``base_url`` is '' when unconfigured
    (callers degrade, they don't crash). ``source`` names the winning tier
    for diagnostics: 'env' | 'token_file' | 'vault' | 'none'."""
    base_url: str
    token: str
    source: str

    @property
    def headers(self) -> dict:
        h = {"User-Agent": "ag2-gateway-client/0.1"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h


def _split_combined(raw: str) -> "tuple[str, str]":
    """Split the one-token onboarding form ``https://<gateway>|<secret>``.

    Detected by the leading URL scheme (NOT a bare '|'), so an explicit
    bearer that merely contains '|' stays intact while an explicit combined
    token still splits — without this, `GATEWAY_TOKEN=https://…|secret` was
    sent whole as the bearer → 401 misread as "not a joined member".
    Returns (url_from_token, secret)."""
    if "|" in raw and raw.split("|", 1)[0].startswith(("http://", "https://")):
        url, secret = raw.split("|", 1)
        return url, secret
    return "", raw


def _token_file_value(env: Mapping[str, str]) -> str:
    """Sparrow's durable token-file tier (REMOTE_TASK_TOKEN_FILE): a
    dotenv-style file carrying a REMOTE_TASK_TOKEN=/AG2_REMOTE_TOKEN= line,
    or the raw onboarding string alone. Missing/unreadable → '' (tier
    skipped). Documented precedence REMOTE_TASK_TOKEN > AG2_REMOTE_TOKEN
    regardless of line order."""
    path = env.get("REMOTE_TASK_TOKEN_FILE") or ""
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    vals: dict[str, str] = {}
    bare = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip().strip('"').strip("'")
        elif not bare:
            bare = line
    return vals.get("REMOTE_TASK_TOKEN") or vals.get("AG2_REMOTE_TOKEN") or bare


def resolve_credentials(
    env: Optional[Mapping[str, str]] = None,
    vault_token_reader: Optional[Callable[[str], str]] = None,
) -> Credentials:
    """The one true precedence chain (ports room-ops ``_gateway.gateway()``,
    the strictest superset, + sparrow's token-file tier):

      token: explicit GATEWAY_TOKEN/RELAY_TOKEN > REMOTE_TASK_TOKEN
             > token file (REMOTE_TASK_TOKEN_FILE) > injected vault reader
      url:   GATEWAY_URL > RELAY_URL > REMOTE_TASK_URL > url-from-combined

    Any tier's value may be the combined ``url|secret`` form. Later tiers
    never override earlier ones (vault last: a stored value can never shadow
    a fresher env token). ``vault_token_reader(var_name) -> str`` is injected
    by the consumer (e.g. Sutando's channel_token.token_from_vault) — this
    package never imports a vault implementation (no ``src/`` dependency).
    """
    env = _os.environ if env is None else env
    source = "none"
    raw = env.get("GATEWAY_TOKEN") or env.get("RELAY_TOKEN") \
        or env.get("REMOTE_TASK_TOKEN") or ""
    if raw:
        source = "env"
    if not raw:
        raw = _token_file_value(env)
        if raw:
            source = "token_file"
    if not raw and vault_token_reader is not None:
        for var in TOKEN_ENV_PRECEDENCE:
            try:
                raw = vault_token_reader(var) or ""
            except Exception:
                raw = ""
            if raw:
                source = "vault"
                break
    url_from_token, token = _split_combined(raw) if raw else ("", "")
    base = ""
    for var in URL_ENV_PRECEDENCE:
        if env.get(var):
            base = env[var]
            break
    base = (base or url_from_token).rstrip("/")
    if not token:
        source = "none"
    return Credentials(base_url=base, token=token, source=source)


def resolve_identity(env: Optional[Mapping[str, str]] = None) -> str:
    """The CLIENT-DECLARED agent identity: AGENT_MXID, falling back to
    AGENT_ID (live-deployment finding: real installs' durable env carried
    AGENT_ID only). '' when neither is set.

    NON-AUTHORITATIVE by contract: the gateway's authenticated token subject
    is the effective identity; this value is at most a consistency check.
    Nothing server-side may grant authority based on it."""
    env = _os.environ if env is None else env
    return env.get("AGENT_MXID") or env.get("AGENT_ID") or ""


def request(method: str, url: str, *, profile: str,
            headers: Optional[Mapping[str, str]] = None,
            data: Optional[bytes] = None,
            max_bytes: Optional[int] = None):
    """Raw request → (status, body_bytes, response_headers). Raises HTTPError/
    URLError like urllib. ``profile`` is REQUIRED and must be a PROFILES key —
    an unknown profile raises ValueError (no silent default timeout).

    ``max_bytes`` bounds the body read to max_bytes+1 (one extra byte lets
    the caller detect overflow) and skips allocation entirely when the peer
    DECLARES an oversize Content-Length — a hostile/buggy peer can't OOM us
    before a higher-layer size cap applies."""
    if profile not in PROFILES:
        raise ValueError(
            f"unknown timeout profile {profile!r}; choose one of {sorted(PROFILES)}")
    req = _urequest.Request(url, data=data, headers=dict(headers or {}),
                            method=method)
    with _urequest.urlopen(req, timeout=PROFILES[profile]) as resp:
        if max_bytes is not None:
            cl = resp.headers.get("Content-Length")
            if cl is not None and cl.isdigit() and int(cl) > max_bytes:
                body = b""
            else:
                body = resp.read(max_bytes + 1)
        else:
            body = resp.read()
        return resp.status, body, dict(resp.headers)


def request_json(method: str, url: str, *, profile: str,
                 headers: Optional[Mapping[str, str]] = None,
                 payload=None):
    """JSON request/response → (status, parsed). Empty body parses as {}."""
    data = _json.dumps(payload).encode() if payload is not None else None
    h = dict(headers or {})
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    status, body, _ = request(method, url, profile=profile, headers=h, data=data)
    return status, _json.loads(body.decode("utf-8") or "{}")


def degrade_reason(code: int) -> str:
    """Uniform degrade reason for a non-2xx (ported verbatim: 401 is a bearer
    problem, kept distinct from 403 so a token failure is never misread as
    "not a member")."""
    if code == 404:
        return "verb unimplemented (404)"
    if code == 401:
        return "auth failed — check the gateway bearer token (401)"
    if code == 403:
        return "denied — agent not a joined member (403)"
    return f"HTTP {code}"
