#!/usr/bin/env python3
"""Security regression guard: `_is_safe_callback_url` must reject IPv4-mapped
IPv6 hostnames that point at private IPv4 ranges.

The function loops over `getaddrinfo` results and checks each
`ipaddress.ip_address(...)` against a list of private networks
(127.0.0.0/8, 10.0.0.0/8, fc00::/7, etc.). For IPv4-mapped IPv6
addresses like `::ffff:127.0.0.1`, the loop returns False:

    >>> ipaddress.ip_address('::ffff:127.0.0.1') in ipaddress.ip_network('127.0.0.0/8')
    False  # cross-family `in` always returns False

So a webhook URL of `https://[::ffff:127.0.0.1]/exfil` would pass the
SSRF check even though it resolves to localhost. Practical exploit is
TLS-gated (no public cert covers IPv6 IP literals), but defense-in-
depth says the validator should catch it — the function's docstring
explicitly claims to block "Hostnames that resolve to private IPs."

The fix projects `IPv6Address.ipv4_mapped` onto the IPv4 private-range
checks. This test pins:

  1. Bypass is closed for IPv4-mapped IPv6 → loopback.
  2. Bypass is closed for IPv4-mapped IPv6 → RFC1918.
  3. Pre-existing behavior on plain IPv4 private IPs is preserved.
  4. Public IPv6 (Cloudflare 2606:4700::) still passes.
  5. Plain IPv6 loopback `::1` still rejected.

We monkey-patch `socket.getaddrinfo` so the test doesn't depend on DNS
or networking — pure validation logic.
"""

import importlib.util
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


api = _load("agent_api", REPO / "src" / "agent-api.py")


def _stub_getaddrinfo(addr_str: str, family: int):
    def fake(_host, _port, _af=None, _sock=None, *args, **kwargs):
        if family == socket.AF_INET6:
            return [(family, socket.SOCK_STREAM, 0, "", (addr_str, 0, 0, 0))]
        return [(family, socket.SOCK_STREAM, 0, "", (addr_str, 0))]

    return fake


def _with_stub(addr_str: str, family: int, fn):
    original = api.socket.getaddrinfo
    api.socket.getaddrinfo = _stub_getaddrinfo(addr_str, family)
    try:
        fn()
    finally:
        api.socket.getaddrinfo = original


def test_ipv4_mapped_ipv6_loopback_is_blocked():
    """The core bypass. `https://[::ffff:127.0.0.1]/` resolves to the
    IPv4-mapped IPv6 form of 127.0.0.1. The pre-fix code passed it;
    the new code MUST reject it via the `IPv6Address.ipv4_mapped`
    projection onto the private-IPv4 ranges."""
    def run():
        safe, reason = api._is_safe_callback_url("https://example.test/")
        assert safe is False, (
            "IPv4-mapped IPv6 loopback bypassed SSRF check — "
            f"reason returned: {reason!r}. The fix's ipv4_mapped projection "
            "should have caught 127.0.0.1."
        )
        assert "127.0.0.1" in reason, f"reason should mention 127.0.0.1: {reason}"

    _with_stub("::ffff:127.0.0.1", socket.AF_INET6, run)


def test_ipv4_mapped_ipv6_rfc1918_is_blocked():
    """Same bypass class for RFC1918 (10.0.0.0/8) targets."""
    def run():
        safe, reason = api._is_safe_callback_url("https://example.test/")
        assert safe is False, (
            "IPv4-mapped IPv6 → 10.0.0.5 bypassed SSRF check — "
            f"reason returned: {reason!r}"
        )
        assert "10.0.0.5" in reason, f"reason should mention 10.0.0.5: {reason}"

    _with_stub("::ffff:10.0.0.5", socket.AF_INET6, run)


def test_plain_ipv4_loopback_still_blocked():
    """Backwards-compat: no regression on plain-IPv4 loopback check."""
    def run():
        safe, reason = api._is_safe_callback_url("https://example.test/")
        assert safe is False
        assert "127.0.0.1" in reason

    _with_stub("127.0.0.1", socket.AF_INET, run)


def test_plain_ipv6_loopback_still_blocked():
    """Backwards-compat: `::1` rejected by existing `::1/128` range."""
    def run():
        safe, reason = api._is_safe_callback_url("https://example.test/")
        assert safe is False
        assert "::1" in reason

    _with_stub("::1", socket.AF_INET6, run)


def test_public_ipv6_still_passes():
    """Backwards-compat: a public IPv6 (Cloudflare DNS 2606:4700:4700::1111)
    must still pass. Defends against an over-zealous fix that rejects
    all IPv6 by accident."""
    def run():
        safe, reason = api._is_safe_callback_url("https://example.test/")
        assert safe is True, f"public IPv6 rejected: {reason}"

    _with_stub("2606:4700:4700::1111", socket.AF_INET6, run)


def test_public_ipv4_still_passes():
    """Sanity: a public IPv4 (Cloudflare DNS 1.1.1.1) must still pass."""
    def run():
        safe, reason = api._is_safe_callback_url("https://example.test/")
        assert safe is True, f"public IPv4 rejected: {reason}"

    _with_stub("1.1.1.1", socket.AF_INET, run)


def main():
    test_ipv4_mapped_ipv6_loopback_is_blocked()
    test_ipv4_mapped_ipv6_rfc1918_is_blocked()
    test_plain_ipv4_loopback_still_blocked()
    test_plain_ipv6_loopback_still_blocked()
    test_public_ipv6_still_passes()
    test_public_ipv4_still_passes()
    print("All SSRF IPv4-mapped-IPv6 tests passed.")


if __name__ == "__main__":
    main()
