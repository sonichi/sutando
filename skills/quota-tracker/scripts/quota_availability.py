#!/usr/bin/env python3
"""Shared authority for deciding whether Claude quota telemetry is usable."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

PROXY_PORT = 7846
PROXY_SCHEME = "http"
PROXY_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def points_at_credential_proxy(base_url: "str | None") -> bool:
    """Return whether *base_url* targets this host's credential proxy."""
    if not base_url:
        return False
    try:
        parsed = urlparse(base_url if "//" in base_url else "//" + base_url)
        host = (parsed.hostname or "").strip().lower()
        port = parsed.port
    except ValueError:
        return False
    scheme = (parsed.scheme or PROXY_SCHEME).lower()
    return (
        scheme == PROXY_SCHEME
        and host in PROXY_HOSTS
        and port == PROXY_PORT
    )


def resolve_available(status: str, proxy_available: Any) -> bool:
    """Resolve the proxy's persisted availability signal without coercion."""
    if status == "rejected":
        return False
    if isinstance(proxy_available, bool):
        return proxy_available
    return status == "allowed"


def availability_decision(
    quota: Any,
    *,
    base_url: "str | None",
    stale: bool,
) -> dict[str, Any]:
    """Return the one authoritative routed/fresh/accepted quota decision."""
    payload = quota if isinstance(quota, dict) else {}
    headers = payload.get("headers")
    headers = headers if isinstance(headers, dict) else {}
    status = headers.get("anthropic-ratelimit-unified-status", "unknown")
    routed = points_at_credential_proxy(base_url)
    accepted = resolve_available(str(status), payload.get("available"))
    available = accepted and routed and not stale
    return {
        "available": available,
        "routed": routed,
        "status": status,
        "unavailable_reason": (
            None if available
            else "not-routed" if not routed
            else "stale" if stale
            else "rejected"
        ),
    }
