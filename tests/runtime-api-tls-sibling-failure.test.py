#!/usr/bin/env python3
"""A TLS-sibling failure must not orphan the running cleartext primary.

_maybe_start_wss starts the primary first; if the TLS sibling then raises,
the primary's handle must still be RETURNED (so serve() can clean it up), the
non-loopback exposure warning must already have fired, and the sibling
failure must be logged as non-fatal.

Run: python3 tests/runtime-api-tls-sibling-failure.test.py  (needs aiohttp)
Exit: 0 on pass, 1 on fail.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))

FAILS = []


def check(cond, msg):
    print(("ok  " if cond else "FAIL") + " - " + msg)
    if not cond:
        FAILS.append(msg)


def run_case(tls_raises: bool, host: str):
    import server as srv

    logs = []
    inst = srv.RuntimeServer.__new__(srv.RuntimeServer)
    inst._state_dir = None
    inst._subscribers = set()
    inst._activity_subscribers = set()
    inst._request_subscribers = set()
    inst.dispatcher = None
    inst._advertiser = None
    inst._start_advertiser = lambda *a, **k: None
    inst._wss_token = lambda: "t"
    if tls_raises:
        inst._wss_ssl_context = lambda: object()   # truthy → sibling attempted
    else:
        inst._wss_ssl_context = lambda: None

    class _BoomTransport:
        """First start() succeeds (primary); a TLS start (ssl_context set)
        raises — reproducing a cert/bind failure on the sibling only."""
        def __init__(self, port):
            self.port = port

        async def start(self, ssl_context=None):
            if ssl_context is not None:
                raise OSError("TLS probe failure")

        async def cleanup(self):
            pass

    old_log = srv._log
    srv._log = logs.append
    old_env = os.environ.get("SUTANDO_SCP_WSS_HOST")
    os.environ["SUTANDO_SCP_WSS_HOST"] = host
    os.environ["SUTANDO_SCP_WSS_ENABLE"] = "1"
    try:
        import ws_transport
        old_ws = ws_transport.WsTransport
        # patch at the import site _maybe_start_wss uses
        ws_transport.WsTransport = lambda *a, **k: _BoomTransport(k.get("port"))
        try:
            started = asyncio.run(inst._maybe_start_wss())
        finally:
            ws_transport.WsTransport = old_ws
    finally:
        srv._log = old_log
        if old_env is None:
            os.environ.pop("SUTANDO_SCP_WSS_HOST", None)
        else:
            os.environ["SUTANDO_SCP_WSS_HOST"] = old_env
    return started, logs


def main():
    started, logs = run_case(tls_raises=True, host="0.0.0.0")
    check(started is not None and len(started) == 1,
          f"TLS-sibling failure still returns the primary handle: {started!r}")
    check(any("exposed beyond loopback" in l for l in logs),
          "non-loopback exposure warning fired before the sibling attempt")
    check(any("TLS sibling failed" in l and "still serving" in l for l in logs),
          f"sibling failure logged as non-fatal: {logs!r}")
    check(not any("SCP WSS start failed" in l for l in logs),
          "outer whole-start failure path NOT taken")

    started2, logs2 = run_case(tls_raises=False, host="127.0.0.1")
    check(started2 is not None and len(started2) == 1,
          "no-TLS loopback start returns the primary handle")
    check(not any("exposed beyond loopback" in l for l in logs2),
          "loopback start emits no exposure warning")

    print(f"\n{'PASS' if not FAILS else 'FAIL'} ({6 - len(FAILS)}/6)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
