"""mcp_discovery — where a relay's MCP discovery answer may send the relay bearer and the MCP token.

The relay's `/v1/mcp/discovery` answer names `mint_url` (the relay bearer goes there) and
`mcp_url` (the minted MCP token goes there). Every client that follows that answer checks
it here: the bearer goes only to the relay's own origin and exact mint path, over HTTPS (or
plain http to loopback); the MCP token goes only to the relay host, a host under it, or the
MCP host the DotSpace registry pairs with the relay's server. Standard library plus
`dotspace`, so the ag2-sparrow package vendors it verbatim.
"""
from __future__ import annotations

import urllib.parse

try:
    from . import dotspace
except ImportError:
    import dotspace

DISCOVERY_PATH = "/v1/mcp/discovery"
AGENT_ACCESS_MINT_PATH = "/api/v1/mcp/agent-access-tokens"
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class DiscoveryRefused(ValueError):
    """A discovery answer names a place no token may be sent."""


def safe_url(field: str, value: object) -> str:
    """`value` when it is https with a host, or plain http to loopback."""
    if not isinstance(value, str) or not value:
        raise DiscoveryRefused(f"discovery answered without {field}")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme == "https" and parsed.hostname:
        return value
    if parsed.scheme == "http" and (parsed.hostname or "") in LOOPBACK:
        return value
    raise DiscoveryRefused(f"{field} must be https (or http to loopback): {parsed.scheme}://{parsed.hostname}")


def same_origin(url: str, base: str) -> bool:
    """Scheme, host and port all equal, parsed (never a string prefix); malformed is False."""
    try:
        u, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base)
        if not u.scheme or not u.hostname or u.scheme != b.scheme:
            return False
        default = {"https": 443, "http": 80}.get(u.scheme)
        return (u.hostname.lower() == (b.hostname or "").lower()
                and (u.port or default) == (b.port or default))
    except ValueError:
        return False


def mint_url(relay_url: str, value: object) -> str:
    """The mint URL when the relay bearer may go there: the relay's origin, its exact mint path."""
    url = safe_url("mint_url", value)
    if not same_origin(url, relay_url):
        raise DiscoveryRefused("mint_url is not on the relay's origin; the relay token is not sent there")
    if urllib.parse.urlsplit(url).path != AGENT_ACCESS_MINT_PATH:
        raise DiscoveryRefused(f"mint_url is not the relay's {AGENT_ACCESS_MINT_PATH}; "
                               "the relay token is not sent there")
    return url


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def mcp_host_allowed(url: str, relay_url: str) -> bool:
    """The relay host or a host under it, or the MCP host registered for the relay's server."""
    relay, host = _host(relay_url), _host(url)
    if not relay or not host:
        return False
    if host == relay or host.endswith("." + relay):
        return True
    registered = _host(dotspace.dotspace_for(relay, fetch=dotspace.NO_FETCH).mcp_url)
    return bool(registered) and host == registered


def mcp_url(relay_url: str, value: object) -> str:
    """The MCP URL when the minted token may go there."""
    url = safe_url("mcp_url", value)
    if not mcp_host_allowed(url, relay_url):
        raise DiscoveryRefused("mcp_url is not on the relay's site; no token is sent there")
    return url
