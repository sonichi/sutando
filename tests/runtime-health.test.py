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
import tempfile
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

# 8) _gateway_configured() reads the lane file the bridge reads: AG2_DEVICE_ENV,
# else channels/<REMOTE_TASK_CHANNEL_DIR or ag2space>/.env, never prod's by name.
_ct = sys.modules["channel_token"]
_RESOLVER_VARS = ("CLAUDE_CONFIG_DIR", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN",
                  "AG2_DEVICE_ENV", "REMOTE_TASK_CHANNEL_DIR")


def _configured_at(cfg, **env):
    saved = {k: os.environ.get(k) for k in _RESOLVER_VARS}
    _tfv = _ct.token_from_vault
    _ct.token_from_vault = lambda var, vault_get=None: ""   # never the real Keychain
    try:
        for k in _RESOLVER_VARS:
            os.environ.pop(k, None)
        os.environ["CLAUDE_CONFIG_DIR"] = str(cfg)
        os.environ.update(env)
        return rh._gateway_configured()
    finally:
        _ct.token_from_vault = _tfv
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


with tempfile.TemporaryDirectory() as _td:
    _dev = os.path.join(_td, "channels", "dev", ".env")
    os.makedirs(os.path.dirname(_dev))
    with open(_dev, "w") as fh:
        fh.write("REMOTE_TASK_TOKEN=dev-lane-tok\n")
    _device = os.path.join(_td, "device.env")
    with open(_device, "w") as fh:
        fh.write("REMOTE_TASK_TOKEN=device-tok\n")
    _prod = os.path.join(_td, "channels", "ag2space", ".env")
    check("_gateway_configured: dev lane, prod absent, REMOTE_TASK_CHANNEL_DIR=dev -> True",
          not os.path.exists(_prod)
          and _configured_at(_td, REMOTE_TASK_CHANNEL_DIR="dev") is True)
    check("_gateway_configured: same host, lane unset -> None (prod absent = can't tell)",
          _configured_at(_td) is None)
    check("_gateway_configured: AG2_DEVICE_ENV alone -> True",
          _configured_at(_td, AG2_DEVICE_ENV=_device) is True)
    os.makedirs(os.path.dirname(_prod))
    with open(_prod, "w") as fh:
        fh.write("OTHER=1\n")
    check("_gateway_configured: readable prod file with no token, lane unset -> False",
          _configured_at(_td) is False)

print("\n" + ("PASS — runtime-health green" if fails == 0 else "FAIL — %d failing" % fails))
sys.exit(fails)
