"""Bounded DNS resolution — a hung resolver must raise (so the poll loop can
emit "reconnecting" and retry) instead of wedging the process forever.

Regression guard for the 2026-07-25 tester incident: the gateway sat in a
"reconnecting" state indefinitely because getaddrinfo (which has no native
timeout) blocked the long-poll loop when DNS for the relay host stopped
answering. urllib's socket timeout does not cover name resolution, so the loop
never raised, never wrote a status update, and never retried.
"""
import os
import socket
import sys
import time
import importlib
import pathlib

import pytest


def _load():
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)


def test_hung_resolver_raises_within_bound(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_DNS_TIMEOUT_S", 0.3)

    def _hang(*a, **k):
        time.sleep(30)  # never returns within the test window

    monkeypatch.setattr(mod, "_orig_getaddrinfo", _hang)

    start = time.monotonic()
    with pytest.raises(socket.gaierror):
        mod._resolve_bounded("relay.ag2.space", 443)
    elapsed = time.monotonic() - start
    # Must give up near the bound, not hang for the full 30s.
    assert elapsed < 5, f"resolver was not bounded (took {elapsed:.1f}s)"


def test_normal_resolution_passes_through(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_DNS_TIMEOUT_S", 5.0)
    sentinel = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))]
    monkeypatch.setattr(mod, "_orig_getaddrinfo", lambda *a, **k: sentinel)
    assert mod._resolve_bounded("relay.ag2.space", 443) == sentinel


def test_resolver_error_propagates(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_DNS_TIMEOUT_S", 5.0)

    def _boom(*a, **k):
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr(mod, "_orig_getaddrinfo", _boom)
    with pytest.raises(socket.gaierror):
        mod._resolve_bounded("nope.invalid", 443)


def test_zero_timeout_disables_bound(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_DNS_TIMEOUT_S", 0)
    sentinel = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))]
    monkeypatch.setattr(mod, "_orig_getaddrinfo", lambda *a, **k: sentinel)
    # With the bound disabled it calls straight through (no thread).
    assert mod._resolve_bounded("relay.ag2.space", 443) == sentinel
