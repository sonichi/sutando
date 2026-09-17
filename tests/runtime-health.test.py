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
import shutil
import subprocess
import tempfile
import sys
import time

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

# 1b) _tmux_socket(): a detached probe does not inherit SUTANDO_TMUX_SOCKET, so the
#     import-time default reports a live core as offline. Prefer the recorded socket.
_sock_tmp = tempfile.mkdtemp()
_cores = os.path.join(_sock_tmp, "state", "cores")
os.makedirs(_cores, exist_ok=True)
_host = rh._host_label_safe() or "testhost"
_alive = os.path.join(_cores, _host + ".alive")
_orig_resolve, _orig_host = rh._resolve_workspace, rh._host_label_safe
rh._resolve_workspace = lambda repo: _sock_tmp
rh._host_label_safe = lambda: _host
# The record is only consulted when no explicit socket was given, so these cases
# must run with the variable clear however the suite happened to be launched.
_env_before = os.environ.pop("SUTANDO_TMUX_SOCKET", None)

with open(_alive, "w", encoding="utf-8") as _fh:
    json.dump({"socket": "/run/real.sock"}, _fh)
check("_tmux_socket: prefers the socket the heartbeat recorded",
      rh._tmux_socket() == "/run/real.sock")

with open(_alive, "w", encoding="utf-8") as _fh:
    json.dump({"session": "sutando-core"}, _fh)
check("_tmux_socket: falls back when .alive carries no socket",
      rh._tmux_socket() == rh.TMUX_SOCKET)

with open(_alive, "w", encoding="utf-8") as _fh:
    _fh.write("{not json")
check("_tmux_socket: falls back on an unreadable .alive",
      rh._tmux_socket() == rh.TMUX_SOCKET)

os.remove(_alive)
check("_tmux_socket: falls back when .alive is absent",
      rh._tmux_socket() == rh.TMUX_SOCKET)

rh._host_label_safe = lambda: ""
check("_tmux_socket: falls back when the host label is unknown",
      rh._tmux_socket() == rh.TMUX_SOCKET)

rh._host_label_safe = lambda: _host
# A crashed core leaves its .alive behind; trusting it would pin the probe to a
# dead socket, which is the failure this resolver exists to remove.
with open(_alive, "w", encoding="utf-8") as _fh:
    json.dump({"socket": "/tmp/stale.sock"}, _fh)
os.utime(_alive, (time.time() - 10000, time.time() - 10000))
check("_tmux_socket: refuses a stale .alive record",
      rh._tmux_socket() == rh.TMUX_SOCKET)

os.utime(_alive, None)
check("_tmux_socket: accepts the same record once it is fresh",
      rh._tmux_socket() == "/tmp/stale.sock")

_prev_env = os.environ.get("SUTANDO_TMUX_SOCKET")
os.environ["SUTANDO_TMUX_SOCKET"] = "/tmp/explicit.sock"
check("_tmux_socket: an explicit SUTANDO_TMUX_SOCKET wins over the record",
      rh._tmux_socket() == rh.TMUX_SOCKET)
if _prev_env is None:
    os.environ.pop("SUTANDO_TMUX_SOCKET", None)
else:
    os.environ["SUTANDO_TMUX_SOCKET"] = _prev_env

rh._resolve_workspace, rh._host_label_safe = _orig_resolve, _orig_host
if _env_before is not None:
    os.environ["SUTANDO_TMUX_SOCKET"] = _env_before
shutil.rmtree(_sock_tmp, ignore_errors=True)

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
    set(out) == {"health", "severity", "authenticated", "core_running",
                 "gateway_running", "ag2space_app_running", "station_available",
                 "tmux_socket", "session", "detail", "signals"},
)

# 4) derive() maps every state correctly — drive it by patching the probes so we
#    exercise the working/idle/needs_login/offline/unknown branches without a live
#    tmux (the branches a schema-only test would leave uncovered).
def _derive_with(core, pane, status, gateway=False, ts="fresh"):
    # ts: "fresh" -> now; "stale" -> older than the gate; or an explicit epoch/None.
    if ts == "fresh":
        ts = time.time()
    elif ts == "stale":
        ts = time.time() - (rh.STALE_STATUS_SECONDS + 60)
    orig = (rh._core_running, rh._pane_text, rh._core_status,
            rh._gateway_running, rh._resolve_workspace)
    rh._core_running = lambda: core
    rh._pane_text = lambda: pane
    rh._core_status = lambda ws: (status, ts)
    rh._gateway_running = lambda: gateway
    rh._resolve_workspace = lambda repo: "/tmp/ignored-ws"
    try:
        return rh.derive()
    finally:
        (rh._core_running, rh._pane_text, rh._core_status,
         rh._gateway_running, rh._resolve_workspace) = orig


d = _derive_with(core=True, pane="", status="running", gateway=True, ts="fresh")
check("derive: fresh running -> working", d["health"] == "working" and d["authenticated"] is True)
check("derive: gateway_running surfaced", d["gateway_running"] is True)

d = _derive_with(core=True, pane="", status="running", ts="stale")
check("derive: STALE running -> unknown (not working)", d["health"] == "unknown")

d = _derive_with(core=True, pane="", status="running", ts=None)
check("derive: running with no ts -> working (can't prove stale)", d["health"] == "working")

d = _derive_with(core=True, pane="", status="idle")
check("derive: idle status -> idle", d["health"] == "idle" and d["authenticated"] is True)

# CHANGED by #2456. This previously asserted "login pane wins over status" with a
# FRESH status — which is the short-circuit the issue reports: the agent is
# demonstrably advancing, so a sign-in prompt cannot also be true, and the
# needs_login verdict erased the wedge signal. The code comment justifying cheap
# false positives is about an UNRESPONSIVE agent; it does not reach a fresh one.
d = _derive_with(core=True, pane=STUCK_PANE, status="running", ts="fresh")
check("derive: login marker + FRESH status -> not needs_login (false positive)",
      d["health"] == "working" and d["authenticated"] is True)
check("derive: ...and the marker is still reported, not silently dropped",
      "false positive" in d["detail"])

# The marker is still authoritative wherever there is no positive evidence of
# progress — a real sign-in prompt stops the loop, so these corroborate.
d = _derive_with(core=True, pane=STUCK_PANE, status="running", ts="stale")
check("derive: login marker + STALE status -> needs_login",
      d["health"] == "needs_login" and d["authenticated"] is False)
check("derive: ...and the wedge signal survives in the detail",
      "stale" in d["detail"].lower())

d = _derive_with(core=True, pane=STUCK_PANE, status="running", ts=None)
check("derive: login marker + NO timestamp -> needs_login (absence of evidence is not freshness)",
      d["health"] == "needs_login" and d["authenticated"] is False)

d = _derive_with(core=True, pane=STUCK_PANE, status="idle", ts="fresh")
check("derive: login marker + fresh IDLE -> idle, not needs_login",
      d["health"] == "idle" and d["authenticated"] is True)

d = _derive_with(core=True, pane=STUCK_PANE, status=None, ts="fresh")
check("derive: login marker + unknown status -> needs_login (no proof of acting)",
      d["health"] == "needs_login" and d["authenticated"] is False)

# CONTROL: a clean pane must be unaffected in every one of those shapes.
check("derive: CONTROL clean pane + stale running -> unknown (wedge preserved)",
      _derive_with(core=True, pane="", status="running", ts="stale")["health"] == "unknown")
check("derive: CONTROL clean pane + fresh running -> working",
      _derive_with(core=True, pane="", status="running", ts="fresh")["health"] == "working")

d = _derive_with(core=True, pane="", status=None)
check("derive: running but no status -> unknown", d["health"] == "unknown")

d = _derive_with(core=False, pane="", status="running")
check("derive: no core -> offline", d["health"] == "offline" and d["authenticated"] is None)

# 5) _core_status reads the status field from a fixture core-status.json.
T = tempfile.mkdtemp()
os.makedirs(os.path.join(T, "state"))
with open(os.path.join(T, "state", "core-status.json"), "w") as f:
    f.write('{"status":"running","ts":1}')
check("_core_status: reads (status, ts) from file", rh._core_status(T) == ("running", 1.0))
check("_core_status: missing file -> (None, None)", rh._core_status(tempfile.mkdtemp()) == (None, None))

# core-status.json written by another process could be corrupt OR a valid but
# non-object JSON value (e.g. a stray '[]'); must degrade to (None, None), not crash.
for bad in ("[]", '"idle"', "42", "not json {"):
    Tb = tempfile.mkdtemp()
    os.makedirs(os.path.join(Tb, "state"))
    with open(os.path.join(Tb, "state", "core-status.json"), "w") as f:
        f.write(bad)
    ok_ = rh._core_status(Tb) == (None, None)
    check("_core_status: non-object/corrupt JSON %r -> (None, None) (no crash)" % bad, ok_)

# A non-numeric ts must not crash the float() coercion -> ts None.
Tt = tempfile.mkdtemp()
os.makedirs(os.path.join(Tt, "state"))
with open(os.path.join(Tt, "state", "core-status.json"), "w") as f:
    f.write('{"status":"running","ts":"garbage"}')
check("_core_status: non-numeric ts -> (status, None)", rh._core_status(Tt) == ("running", None))

# 6) Exercise the REAL probe implementations in-process (offline path) so the
#    subprocess-only e2e above doesn't leave _run/_core_running/_gateway_running/
#    _pane_text/main uncovered. Point at a socket with no session → offline, and
#    call each real helper directly (they degrade to empty/false, never crash).
_orig_socket = rh.TMUX_SOCKET
rh.TMUX_SOCKET = "/tmp/rh-inproc-nonexistent-%d.sock" % os.getpid()
try:
    check("real _core_running: false on bogus socket", rh._core_running() is False)
    # _gateway_running() short-circuits to None unless a gateway is CONFIGURED
    # (src/runtime-health.py:229). Clean-install CI has none, so the real probe
    # branch is only reachable when we force the precondition — otherwise this
    # asserts a bool against None and fails host-dependently (qingyun CR #2527).
    _ogc = rh._gateway_configured
    rh._gateway_configured = lambda: True
    try:
        check("real _gateway_running returns a bool (configured host)",
              isinstance(rh._gateway_running(), bool))
    finally:
        rh._gateway_configured = _ogc
    # Explicit unconfigured-host control: not-configured -> None, never a down-vote.
    rh._gateway_configured = lambda: False
    try:
        check("_gateway_running: unconfigured host -> None", rh._gateway_running() is None)
    finally:
        rh._gateway_configured = _ogc
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
# A command that cannot execute returns rc None (UNKNOWN — distinct from a
# command that ran and returned non-zero) so a probe outage is never counted as
# a positive "down" observation (qingyun CR on #2527).
rc, out = rh._run(["/nonexistent-rh-binary-xyz"])
check("_run: missing binary -> (None, '')", rc is None and out == "")


def _fake_run_gw(cmd):
    if cmd[:1] == ["pgrep"]:
        return 1, ""                      # no gateway process
    if "list-windows" in cmd:
        return 0, "core\ngateway\n"       # ...but a 'gateway' window exists
    return 1, ""


_o = rh._run
_ogc2 = rh._gateway_configured
rh._run = _fake_run_gw
# The window-scan fallback is only reached past the configured short-circuit
# (src/runtime-health.py:229), so mark the gateway configured here too — else
# this returns None on a clean host and never exercises the fallback (qingyun CR #2527).
rh._gateway_configured = lambda: True
try:
    check("_gateway_running: window-scan fallback", rh._gateway_running() is True)
finally:
    rh._run = _o
    rh._gateway_configured = _ogc2

_ow = rh._resolve_workspace
rh._resolve_workspace = lambda repo: "/dev/null/cannot-mkdir-here"
try:
    rh.main()  # unwritable state dir -> write swallowed, still prints
    check("main(): survives an unwritable state dir", True)
finally:
    rh._resolve_workspace = _ow

# 7) The tri-state process probe has ONE owner: _core_running delegates to
#    tmux_probe.has_session with this module's socket/session and its 8s budget.
_seen = {}
_oh = rh._tmux_has_session
rh._tmux_has_session = lambda sock, sess, timeout=None: _seen.update(sock=sock, sess=sess, timeout=timeout)
try:
    check("_core_running: delegates to tmux_probe.has_session(TMUX_SOCKET, SESSION, timeout=8)",
          rh._core_running() is None
          and _seen == {"sock": rh.TMUX_SOCKET, "sess": rh.SESSION, "timeout": 8})
finally:
    rh._tmux_has_session = _oh

print("\n" + ("PASS — runtime-health green" if fails == 0 else "FAIL — %d failing" % fails))
sys.exit(fails)
