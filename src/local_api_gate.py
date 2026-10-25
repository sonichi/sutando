"""The one gate for the loopback HTTP services' privileged routes.

The dashboard and the agent API both answer on 127.0.0.1 and both carry routes a page from
another origin must never reach: CORS hides a cross-origin response but does not stop the
request, a safelisted POST needs no preflight, and DNS rebinding puts an attacker's name in
front of a loopback port. The rules live here once:

  * `mutation_request_allowed` — the dashboard's state-changing gate: the Host header must
    name the bind (loopback, or a declared LAN host), the Origin must equal that Host, and a
    body must be JSON.
  * `local_api_token` — the per-install secret for routes a browser page is allowed to call
    only as the owner's own client: `SUTANDO_API_TOKEN` when the operator set one, else a
    secret minted once into `<workspace>/state/agent-api.token` (0600, atomic). The desktop
    shell reads that file and presents it as a bearer.
  * `bearer_request_allowed` — the agent API's connections gate: loopback Host on the bound
    port, loopback or absent Origin, and the bearer above, always; no bearer is 401.
  * `private_history_request_allowed` — the agent API's owner-history gate: with a configured
    `SUTANDO_API_TOKEN` the bearer must match (else 401); without one, only a loopback client
    sending no Origin may read (else 403), so a browser page never reaches localhost history.

A route that does not go through this module has no gate.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from workspace_default import status_read_path

LOCAL_API_TOKEN_NAME = "agent-api.token"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# A wildcard bind names no host, so the legitimate Host set cannot be derived
# from it; DASHBOARD_ALLOWED_HOSTS supplies it explicitly for LAN mode.
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", ""})


def _authority_host(value: str) -> str:
    """Hostname from a Host/Origin authority, minus port and IPv6 brackets."""
    v = (value or "").strip().lower()
    if v.startswith("["):
        return v[1:].split("]", 1)[0]
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def _authority_port(value: str) -> Optional[int]:
    """The explicit port of a Host authority, None when it carries none or is malformed."""
    try:
        return urlparse("//" + (value or "").strip()).port
    except ValueError:
        return None


def _allowed_mutation_hosts(bind: str, declared: str) -> "set[str] | None":
    """Host names a mutation may carry. None = wildcard bind with none declared."""
    names = {h for h in (_authority_host(x) for x in (declared or "").split(",")) if h}
    if (bind or "").strip().lower() in _WILDCARD_BINDS:
        return (_LOOPBACK_HOSTS | names) if names else None
    return _LOOPBACK_HOSTS | {bind.strip().lower()} | names


def mutation_request_allowed(origin, host, content_type, *, expect_body,
                             bind="127.0.0.1", allowed_hosts=""):
    """Fail-closed gate for state-changing dashboard requests → (ok, reason)."""
    permitted = _allowed_mutation_hosts(bind, allowed_hosts)
    if permitted is None:
        # Refusing loudly beats 403-ing every save with "host not allowed": on a
        # wildcard bind the operator must name the hosts, or rebinding is free.
        return False, ("wildcard DASHBOARD_BIND requires DASHBOARD_ALLOWED_HOSTS "
                       "(comma-separated) before mutations are accepted")
    if _authority_host(host) not in permitted:
        # DNS rebinding: the attacker's name resolves to us, so only the Host
        # header still carries it.
        return False, "host not allowed"
    if not origin:
        return False, "missing Origin"
    if urlparse(origin).netloc.strip().lower() != (host or "").strip().lower():
        return False, "cross-origin request refused"
    if expect_body and (content_type or "").split(";", 1)[0].strip().lower() != "application/json":
        # Non-safelisted type: a cross-origin sender must preflight, which this
        # server never grants.
        return False, "Content-Type must be application/json"
    return True, None


def loopback_origin(origin: str) -> bool:
    """A missing Origin is a same-origin proxy or a local tool; a present one must name loopback."""
    if not origin:
        return True
    try:
        host = (urlparse(origin).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS or host.endswith(".localhost")


def local_api_token(workspace: Path, configured: str = "") -> str:
    """The bearer the privileged loopback routes require: `configured` (SUTANDO_API_TOKEN)
    when set, else the per-install secret under state/, minted once."""
    if configured:
        return configured
    path = status_read_path(LOCAL_API_TOKEN_NAME, Path(workspace))
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    value = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return value


def bearer_matches(authorization: str, expected: str) -> bool:
    """Constant-time compare on bytes, so a header outside ASCII is a plain mismatch."""
    presented = (authorization or "").strip()
    if presented.lower().startswith("bearer "):
        presented = presented[7:].strip()
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8", "replace"), expected.encode("utf-8", "replace"))


def bearer_request_allowed(origin, host, authorization, *, expected_token: str,
                           bound_port: Optional[int]) -> "tuple[int, Optional[str]]":
    """(status, reason) for a privileged loopback route; (200, None) when it may proceed.
    Host must name loopback on the bound port (DNS rebinding carries another name),
    Origin must be absent or loopback, and the bearer must match: no bearer is 401."""
    if _authority_host(host) not in _LOOPBACK_HOSTS:
        return 403, "host not allowed"
    port = _authority_port(host)
    if bound_port is not None and port is not None and port != bound_port:
        return 403, "host not allowed"
    if not loopback_origin(origin or ""):
        return 403, "cross-origin request refused"
    if not bearer_matches(authorization or "", expected_token):
        return 401, "unauthorized"
    return 200, None


def private_history_request_allowed(client_ip: str, origin: Optional[str], authorization: str, *,
                                    configured_token: str) -> "tuple[int, Optional[str]]":
    """(status, reason) for an owner-history route: a configured token must be presented as a
    bearer (else 401); with none, only a loopback client sending no Origin passes (else 403)."""
    if configured_token:
        if bearer_matches(authorization or "", configured_token):
            return 200, None
        return 401, "unauthorized"
    try:
        loopback = ipaddress.ip_address(client_ip).is_loopback
    except ValueError:
        loopback = False
    if loopback and not origin:
        return 200, None
    return 403, "task history requires a local proxy or API token"
