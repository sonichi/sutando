#!/usr/bin/env python3
"""Behavioral regression test for issue #1922: voice_desired_state races.

/voice/toggle does a read-modify-write on module-global voice_desired_state.
Under a threaded server (#1921), two simultaneous toggles can interleave the
read and the write and land on the wrong state. The fix serializes the toggle
(and /voice/set) under voice_state_lock.

This test imports the REAL module and serves it over a real ThreadingHTTPServer,
then proves the handler actually acquires the lock: with voice_state_lock held
by the test, a /voice/toggle request must block; releasing the lock must let it
complete with a correctly-toggled state. Revert the `with voice_state_lock:` in
the handler and the block-assertion goes red.

Run: python3 tests/agent-api-voice-state-lock.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# No token in env → check_auth() passes without Authorization headers.
# Clear both the current name and the legacy name so a CI/dev env that
# exports either doesn't put the test server into token-required mode.
os.environ.pop("SUTANDO_API_TOKEN", None)
os.environ.pop("AGENT_API_TOKEN", None)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


api = _load("agent_api", REPO / "src" / "agent-api.py")

failures = []


def check(name: str, cond: bool, detail: str = ""):
    tag = "ok" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def post(base: str, path: str, body: bytes = b"") -> dict:
    req = urllib.request.Request(base + path, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        # --- sanity: toggle flips and flips back ---
        api.voice_desired_state = "disconnected"
        s1 = post(base, "/voice/toggle")["state"]
        s2 = post(base, "/voice/toggle")["state"]
        check("toggle flips disconnected->connected", s1 == "connected", f"got {s1}")
        check("second toggle flips back", s2 == "disconnected", f"got {s2}")

        # --- the lock is really acquired by the handler ---
        api.voice_desired_state = "disconnected"
        result: dict = {}

        def bg_toggle():
            result["state"] = post(base, "/voice/toggle")["state"]

        api.voice_state_lock.acquire()
        try:
            t = threading.Thread(target=bg_toggle, daemon=True)
            t.start()
            t.join(timeout=0.5)
            check(
                "toggle blocks while test holds voice_state_lock",
                t.is_alive() and "state" not in result,
                f"request completed with lock held (state={result.get('state')!r}) "
                "— handler is not acquiring voice_state_lock",
            )
        finally:
            api.voice_state_lock.release()
        t.join(timeout=5)
        check("toggle completes after lock release", result.get("state") == "connected",
              f"got {result.get('state')!r}")
        check("module state updated exactly once", api.voice_desired_state == "connected",
              f"got {api.voice_desired_state!r}")

        # --- /voice/set honors the same lock ---
        api.voice_desired_state = "disconnected"
        result2: dict = {}

        def bg_set():
            result2["state"] = post(base, "/voice/set", b'{"state": "connected"}')["state"]

        api.voice_state_lock.acquire()
        try:
            t2 = threading.Thread(target=bg_set, daemon=True)
            t2.start()
            t2.join(timeout=0.5)
            check(
                "/voice/set blocks while test holds voice_state_lock",
                t2.is_alive() and "state" not in result2,
                "set completed with lock held — handler is not acquiring voice_state_lock",
            )
        finally:
            api.voice_state_lock.release()
        t2.join(timeout=5)
        check("/voice/set completes after lock release", result2.get("state") == "connected",
              f"got {result2.get('state')!r}")
    finally:
        server.shutdown()

    if failures:
        print(f"\n{len(failures)} failure(s): {failures}")
        return 1
    print("\nAll voice-state-lock tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
