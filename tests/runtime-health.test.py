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

# 4) derive() maps every state correctly — drive it by patching the probes so we
#    exercise the working/idle/needs_login/offline/unknown branches without a live
#    tmux (the branches a schema-only test would leave uncovered).
def _derive_with(core, pane, status, gateway=False):
    orig = (rh._core_running, rh._pane_text, rh._core_status,
            rh._gateway_running, rh._resolve_workspace)
    rh._core_running = lambda: core
    rh._pane_text = lambda: pane
    rh._core_status = lambda ws: status
    rh._gateway_running = lambda: gateway
    rh._resolve_workspace = lambda repo: "/tmp/ignored-ws"
    try:
        return rh.derive()
    finally:
        (rh._core_running, rh._pane_text, rh._core_status,
         rh._gateway_running, rh._resolve_workspace) = orig


d = _derive_with(core=True, pane="", status="running", gateway=True)
check("derive: running status -> working", d["health"] == "working" and d["authenticated"] is True)
check("derive: gateway_running surfaced", d["gateway_running"] is True)

d = _derive_with(core=True, pane="", status="idle")
check("derive: idle status -> idle", d["health"] == "idle" and d["authenticated"] is True)

d = _derive_with(core=True, pane=STUCK_PANE, status="running")
check("derive: login pane wins over status -> needs_login", d["health"] == "needs_login" and d["authenticated"] is False)

d = _derive_with(core=True, pane="", status=None)
check("derive: running but no status -> unknown", d["health"] == "unknown")

d = _derive_with(core=False, pane="", status="running")
check("derive: no core -> offline", d["health"] == "offline" and d["authenticated"] is None)

# 5) _core_status reads the status field from a fixture core-status.json.
import tempfile
T = tempfile.mkdtemp()
os.makedirs(os.path.join(T, "state"))
with open(os.path.join(T, "state", "core-status.json"), "w") as f:
    f.write('{"status":"running","ts":1}')
check("_core_status: reads status from file", rh._core_status(T) == "running")
check("_core_status: missing file -> None", rh._core_status(tempfile.mkdtemp()) is None)

# core-status.json written by another process could be corrupt OR a valid but
# non-object JSON value (e.g. a stray '[]'); must degrade to None, not crash.
for bad in ("[]", '"idle"', "42", "not json {"):
    Tb = tempfile.mkdtemp()
    os.makedirs(os.path.join(Tb, "state"))
    with open(os.path.join(Tb, "state", "core-status.json"), "w") as f:
        f.write(bad)
    ok_ = rh._core_status(Tb) is None
    check("_core_status: non-object/corrupt JSON %r -> None (no crash)" % bad, ok_)

# 6) Exercise the REAL probe implementations in-process (offline path) so the
#    subprocess-only e2e above doesn't leave _run/_core_running/_gateway_running/
#    _pane_text/main uncovered. Point at a socket with no session → offline, and
#    call each real helper directly (they degrade to empty/false, never crash).
_orig_socket = rh.TMUX_SOCKET
rh.TMUX_SOCKET = "/tmp/rh-inproc-nonexistent-%d.sock" % os.getpid()
try:
    check("real _core_running: false on bogus socket", rh._core_running() is False)
    check("real _gateway_running returns a bool", isinstance(rh._gateway_running(), bool))
    check("real _pane_text returns a str", isinstance(rh._pane_text(), str))
    check("real _resolve_workspace returns a path", rh._resolve_workspace(REPO).startswith("/"))
    d = rh.derive()
    check("real derive: offline on bogus socket", d["health"] == "offline")
    # main() derives + best-effort persists + prints; must not raise.
    rh.main()
    check("real main() ran without error", True)
finally:
    rh.TMUX_SOCKET = _orig_socket

# 7) Defensive branches (the degrade-not-crash paths).
rc, out = rh._run(["/nonexistent-rh-binary-xyz"])
check("_run: missing binary -> (127, '')", rc == 127 and out == "")


def _fake_run_gw(cmd):
    if cmd[:1] == ["pgrep"]:
        return 1, ""                      # no gateway process
    if "list-windows" in cmd:
        return 0, "core\ngateway\n"       # ...but a 'gateway' window exists
    return 1, ""


_o = rh._run
rh._run = _fake_run_gw
try:
    check("_gateway_running: window-scan fallback", rh._gateway_running() is True)
finally:
    rh._run = _o

_ow = rh._resolve_workspace
rh._resolve_workspace = lambda repo: "/dev/null/cannot-mkdir-here"
try:
    rh.main()  # unwritable state dir -> write swallowed, still prints
    check("main(): survives an unwritable state dir", True)
finally:
    rh._resolve_workspace = _ow

print("\n" + ("PASS — runtime-health green" if fails == 0 else "FAIL — %d failing" % fails))
sys.exit(fails)
