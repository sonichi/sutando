#!/usr/bin/env python3
"""Tests for src/runtime-health.py — the core health-state derivation.

Covers the load-bearing 'stuck-at-login vs thinking' predicate against the REAL
pane text a stuck core shows, and the offline path end-to-end. tmux-dependent
paths (working/idle) are covered by the pure predicate + the offline e2e; a full
working-state e2e would need a live core, which CI doesn't have.

    python3 tests/runtime-health.test.py
"""
import importlib.util
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "runtime_health", os.path.join(REPO, "src", "runtime-health.py")
)
rh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rh)

fails = 0


def check(name, cond):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


# 1) needs_login() fires on the REAL text a keychain-locked core shows.
#    (Captured verbatim 2026-07-13 from a core stuck after an SSH-launched start.)
STUCK_PANE = """\
  ⎿  Not logged in · Please run /login
     · Run in another terminal: security unlock-keychain
✻ Worked for 0s
                                                     Not logged in · Run /login
"""
check("needs_login: true on a stuck-at-login pane", rh.needs_login(STUCK_PANE) is True)

# 2) It does NOT fire on a normal working pane (no false 'needs sign-in').
WORKING_PANE = """\
  connect-flow → recommended path (b), #8 merged, memory maintenance. Then it
  Running 1 shell command…
✢ Perambulating… (1m 46s · ↓ 5.9k tokens)
"""
check("needs_login: false on a working pane", rh.needs_login(WORKING_PANE) is False)
check("needs_login: false on empty pane", rh.needs_login("") is False)

# 3) offline end-to-end: a socket with no session → health=offline, authed=null.
env = dict(os.environ)
env["SUTANDO_TMUX_SOCKET"] = "/tmp/rh-test-nonexistent-%d.sock" % os.getpid()
p = subprocess.run(
    [sys.executable, os.path.join(REPO, "src", "runtime-health.py")],
    capture_output=True, text=True, timeout=30, env=env,
)
try:
    out = json.loads(p.stdout)
except (ValueError, json.JSONDecodeError):
    out = {}
check("offline: valid JSON emitted", bool(out))
check("offline: health == offline", out.get("health") == "offline")
check("offline: authenticated is null", out.get("authenticated") is None)
check("offline: core_running is false", out.get("core_running") is False)
check(
    "contract keys present",
    set(out) == {"health", "authenticated", "core_running", "gateway_running",
                 "tmux_socket", "session", "detail"},
)

print("\n" + ("PASS — runtime-health green" if fails == 0 else "FAIL — %d failing" % fails))
sys.exit(fails)
