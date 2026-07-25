"""Bounded DNS resolution — a hung resolver must raise (so the poll loop can
emit "reconnecting" and retry) instead of wedging the process forever.

Regression guard for the 2026-07-25 tester incident: the gateway sat in a
"reconnecting" state indefinitely because getaddrinfo (which has no native
timeout) blocked the long-poll loop when DNS for the relay host stopped
answering. urllib's socket timeout does not cover name resolution, so the loop
never raised, never wrote a status update, and never retried.

Plain-script convention (run as `python3 test_dns_timeout.py`), matching the
other ag2-sparrow tests — no pytest.
"""
import importlib
import os
import pathlib
import socket
import sys
import time


def _load():
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)


def _with(mod, *, timeout, resolver, fn):
    """Swap the module's DNS bound + underlying resolver, run fn, restore."""
    orig_t, orig_r = mod._DNS_TIMEOUT_S, mod._orig_getaddrinfo
    mod._DNS_TIMEOUT_S, mod._orig_getaddrinfo = timeout, resolver
    try:
        return fn()
    finally:
        mod._DNS_TIMEOUT_S, mod._orig_getaddrinfo = orig_t, orig_r


def test_hung_resolver_raises_within_bound():
    mod = _load()

    def body():
        start = time.monotonic()
        try:
            mod._resolve_bounded("relay.ag2.space", 443)
        except socket.gaierror:
            elapsed = time.monotonic() - start
            assert elapsed < 5, f"resolver not bounded (took {elapsed:.1f}s)"
            return
        raise AssertionError("hung resolver did not raise")

    _with(mod, timeout=0.3, resolver=lambda *a, **k: time.sleep(30), fn=body)
    print("PASS test_hung_resolver_raises_within_bound")


def test_normal_resolution_passes_through():
    mod = _load()
    sentinel = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))]
    got = _with(
        mod, timeout=5.0, resolver=lambda *a, **k: sentinel,
        fn=lambda: mod._resolve_bounded("relay.ag2.space", 443),
    )
    assert got == sentinel
    print("PASS test_normal_resolution_passes_through")


def test_v4_preference_and_passthrough():
    mod = _load()
    mixed = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443)),
    ]
    # ag2.space host → v4 filtered to the front-only set
    v4only = _with(
        mod, timeout=5.0, resolver=lambda *a, **k: mixed,
        fn=lambda: mod._getaddrinfo_prefer_v4("relay.ag2.space", 443),
    )
    assert all(i[0] == socket.AF_INET for i in v4only), v4only
    # non-ag2 host → untouched
    passthru = _with(
        mod, timeout=5.0, resolver=lambda *a, **k: mixed,
        fn=lambda: mod._getaddrinfo_prefer_v4("example.com", 443),
    )
    assert passthru == mixed
    print("PASS test_v4_preference_and_passthrough")


def test_resolver_error_propagates():
    mod = _load()

    def boom(*a, **k):
        raise socket.gaierror("name or service not known")

    def body():
        try:
            mod._resolve_bounded("nope.invalid", 443)
        except socket.gaierror:
            return
        raise AssertionError("resolver error did not propagate")

    _with(mod, timeout=5.0, resolver=boom, fn=body)
    print("PASS test_resolver_error_propagates")


def test_zero_timeout_disables_bound():
    mod = _load()
    sentinel = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))]
    got = _with(
        mod, timeout=0, resolver=lambda *a, **k: sentinel,
        fn=lambda: mod._resolve_bounded("relay.ag2.space", 443),
    )
    assert got == sentinel
    print("PASS test_zero_timeout_disables_bound")


def test_reload_preserves_true_original_resolver():
    """Regression: re-executing the module must NOT capture our own wrapper as
    the original resolver (reload made _orig_getaddrinfo = the wrapper, so the
    first real resolution recursed to death). Exercises the REAL wrapper chain
    without swapping _orig_getaddrinfo."""
    mod = _load()
    mod = _load()  # second re-exec: socket.getaddrinfo is already the wrapper
    assert mod._orig_getaddrinfo.__name__ != "_getaddrinfo_prefer_v4", (
        "reload captured the wrapper as the original resolver"
    )
    # localhost resolves from system files — no network, no fake resolver, and
    # would RecursionError immediately on the broken capture.
    infos = mod._resolve_bounded("localhost", 80)
    assert infos, "localhost resolution through the real chain returned nothing"
    print("PASS test_reload_preserves_true_original_resolver")


if __name__ == "__main__":
    test_hung_resolver_raises_within_bound()
    test_normal_resolution_passes_through()
    test_v4_preference_and_passthrough()
    test_resolver_error_propagates()
    test_zero_timeout_disables_bound()
    test_reload_preserves_true_original_resolver()
    print("ALL PASS")
