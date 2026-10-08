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
sys.path.insert(0, os.path.join(REPO, "src"))
import worker_auth_state as was  # noqa: E402

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

# 2b) An old marker left on screen after the CLI signed back in is not a logout.
#     Captured 2026-09-28: a refused turn, a successful /login, then real work.
RECOVERED_PANE = """\
❯ /proactive-loop
  ⎿  Not logged in · Please run /login
✻ Crunched for 0s · done 12:05 PM · 1 monitor still running
❯ /login
  ⎿  Login interrupted
❯ /login
  ⎿  Login successful
  Ran 3 shell commands
⏺ This pass was quiet: no tasks are waiting.
✻ Crunched for 41s · done 12:06 PM · 1 monitor still running
"""
check("needs_login: false once a later /login succeeded", rh.needs_login(RECOVERED_PANE) is False)
check("needs_login: false once a later turn did real work",
      rh.needs_login("  ⎿  Not logged in · Please run /login\n✻ Worked for 0s\n❯ hi\n✻ Cooked for 1m 3s · done\n") is False)
check("needs_login: a 0-1 s turn after the marker is the refusal itself, still logged out",
      rh.needs_login("  ⎿  Not logged in · Please run /login\n✻ Crunched for 0s · done 12:05 PM\n") is True)
check("needs_login: a marker after the success line counts again",
      rh.needs_login("  ⎿  Login successful\n✻ Worked for 20s\n  ⎿  Not logged in · Please run /login\n") is True)

# 2c) "Signed in after the marker" is worker_auth_state's reading, not a copy here:
#     a refusal then a tool call or the agent's own line is signed in for both readers.
_REFUSED_THEN_TOOL = "❯ /startup\n  ⎿  Login expired · Please run /login\n✻ Worked for 0s\n❯ go\n⏺ Bash(ls)\n"
check("needs_login: false once a tool call ran after the marker",
      rh.needs_login(_REFUSED_THEN_TOOL) is False)
check("needs_login: false once the agent answered after the marker",
      rh.needs_login("  ⎿  Login expired · Please run /login\n❯ go\n● Done.\n") is False)
check("needs_login: reads through worker_auth_state.signed_in_since",
      rh.signed_in_since is was.signed_in_since and not hasattr(rh, "_LOGGED_IN_AGAIN")
      and not hasattr(rh, "_REAL_TURN"))
_FOOTER = "────────\n❯ \n────────\n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
_REFUSED = "❯ /startup\n  ⎿  Login expired · Please run /login\n✻ Worked for 0s\n"
for _name, _pane in (
        ("refusal alone", _REFUSED + _FOOTER),
        ("refusal under a newer typed prompt", _REFUSED + _FOOTER.replace("❯ \n", "❯ try again\n", 1)),
        ("refusal under a spinner", _REFUSED + "❯ try again\n✻ Perambulating… (1m 46s · ↓ 5.9k tokens)\n" + _FOOTER),
        ("refusal then a tool call", _REFUSED_THEN_TOOL + _FOOTER),
        ("refusal then the agent's line", _REFUSED + "❯ go\n● Hello.\n" + _FOOTER),
        ("refusal then Login successful", _REFUSED + "❯ /login\n  ⎿  Login successful\n" + _FOOTER),
        ("refusal then a turn that outran it", _REFUSED + "❯ hi\n✻ Cooked for 1m 3s · done 1:00 PM\n" + _FOOTER),
        ("refusal then another 0 s turn", _REFUSED + "❯ hi\n  ⎿  Unknown slash command: /hi\n✻ Worked for 0s\n" + _FOOTER),
        ("a refusal after Login successful", "  ⎿  Login successful\n✻ Worked for 20s\n" + _REFUSED + _FOOTER),
        ("the recovered pane", RECOVERED_PANE),
        ("no marker at all", WORKING_PANE)):
    # Agreement holds wherever the marker line is one both readers name (the CLI's own
    # `… /login` line); runtime-health's extra markers (keychain, API key) are its own.
    check(f"needs_login agrees with worker_auth_state.auth_expired: {_name}",
          rh.needs_login(_pane) is was.auth_expired(_pane))

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

# A clock step leaves a future-dated record; a one-sided age test reads that as
# fresh forever, so the bound has to hold on both sides.
os.utime(_alive, (time.time() + 10000, time.time() + 10000))
check("_tmux_socket: refuses a future-dated .alive",
      rh._tmux_socket() == rh.TMUX_SOCKET)

os.utime(_alive, (time.time() + 1, time.time() + 1))
check("_tmux_socket: tolerates small clock skew",
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
# _core_running() resolves via _tmux_socket(), not the bare TMUX_SOCKET constant --
# on a live fresh-heartbeat host that finds the REAL socket, so mock the function too.
_orig_tmux_socket = rh._tmux_socket
_orig_socket = rh.TMUX_SOCKET
rh._tmux_socket = lambda: "/tmp/rh-inproc-nonexistent-%d.sock" % os.getpid()
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
    check("real _pane_text: no session on the socket -> empty", rh._pane_text() == "")
    check("real _resolve_workspace returns a path", rh._resolve_workspace(REPO).startswith("/"))
    d = rh.derive()
    check("real derive: offline on bogus socket", d["health"] == "offline")
    # main() derives + best-effort persists + prints; must not raise.
    rh.main()
    check("real main() ran without error", True)
finally:
    rh.TMUX_SOCKET = _orig_socket
    rh._tmux_socket = _orig_tmux_socket

# 6b) The real pane read against a real core session: capture-pane needs a pane target, and a
#     bare `=session` is refused by tmux, which would silently read every pane as "".
_tmux = shutil.which("tmux")
if _tmux is None:
    print("  skip  real _pane_text against a scratch session (tmux not installed)")
else:
    _td = tempfile.mkdtemp()
    _sock = os.path.join(_td, "sock")
    subprocess.run([_tmux, "-S", _sock, "new-session", "-d", "-s", rh.SESSION,
                    "printf 'Please run /login\\n'; sleep 60"], check=True)
    subprocess.run([_tmux, "-S", _sock, "new-session", "-d", "-s", rh.SESSION + "-watcher",
                    "printf 'watcher pane\\n'; sleep 60"], check=True)
    rh._tmux_socket = lambda: _sock
    try:
        for _ in range(20):
            _pane = rh._pane_text()
            if "Please run /login" in _pane:
                break
            time.sleep(0.1)
        check("real _pane_text returns the core pane's own text", "Please run /login" in _pane
              and "watcher pane" not in _pane)
        check("real _pane_text feeds needs_login", rh.needs_login(_pane))
    finally:
        rh._tmux_socket = _orig_tmux_socket
        subprocess.run([_tmux, "-S", _sock, "kill-server"], check=False)

    # 6c) A host whose ~/.tmux.conf sets base-index 1: the core window is :1, not :0.
    _conf = os.path.join(_td, "base1.conf")
    with open(_conf, "w") as _fh:
        _fh.write("set -g base-index 1\n")
    _sock1 = os.path.join(_td, "sock1")
    subprocess.run([_tmux, "-f", _conf, "-S", _sock1, "new-session", "-d", "-s", rh.SESSION,
                    "printf 'Please run /login\\n'; sleep 60"], check=True)
    rh._tmux_socket = lambda: _sock1
    try:
        for _ in range(20):
            _pane = rh._pane_text()
            if "Please run /login" in _pane:
                break
            time.sleep(0.1)
        check("real _pane_text reads the core on a base-index-1 host", "Please run /login" in _pane)
        check("real _pane_text feeds needs_login on a base-index-1 host", rh.needs_login(_pane))
    finally:
        rh._tmux_socket = _orig_tmux_socket
        subprocess.run([_tmux, "-S", _sock1, "kill-server"], check=False)

    # 6d) Only the watcher lives: the core is gone, and its pane must not be read as the core's.
    _sock2 = os.path.join(_td, "sock2")
    subprocess.run([_tmux, "-S", _sock2, "new-session", "-d", "-s", rh.SESSION + "-watcher",
                    "printf 'Please run /login\\n'; sleep 60"], check=True)
    rh._tmux_socket = lambda: _sock2
    try:
        time.sleep(0.3)
        check("real _pane_text never reads the watcher when the core is gone", rh._pane_text() == "")
    finally:
        rh._tmux_socket = _orig_tmux_socket
        subprocess.run([_tmux, "-S", _sock2, "kill-server"], check=False)

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
#    tmux_probe.has_session with _tmux_socket()'s result (not the bare
#    constant -- same reason as section 6) plus this module's session/budget.
_seen = {}
_oh = rh._tmux_has_session
_ot = rh._tmux_socket
_injected_sock = "/tmp/rh-delegation-check-%d.sock" % os.getpid()
rh._tmux_has_session = lambda sock, sess, timeout=None: _seen.update(sock=sock, sess=sess, timeout=timeout)
rh._tmux_socket = lambda: _injected_sock
try:
    check("_core_running: delegates to tmux_probe.has_session(_tmux_socket(), SESSION, timeout=8)",
          rh._core_running() is None
          and _seen == {"sock": _injected_sock, "sess": rh.SESSION, "timeout": 8})
finally:
    rh._tmux_has_session = _oh
    rh._tmux_socket = _ot

# 8) core-status says idle, but the Codex pane holds the queue behind a rejected
#    /startup. Driven through a stubbed tmux on PATH so the real capture + pane_gate run.
_bt = tempfile.mkdtemp()
_bin = os.path.join(_bt, "bin")
os.makedirs(_bin)
_pane_file = os.path.join(_bt, "pane.txt")
with open(os.path.join(_bin, "tmux"), "w") as _fh:
    _fh.write('#!/bin/bash\n'
              'for a in "$@"; do\n'
              '  case "$a" in\n'
              '    list-windows) echo 0; exit 0 ;;\n'
              '    show-environment) echo "SUTANDO_CORE_RUNTIME=$STUB_RUNTIME"; exit 0 ;;\n'
              '    capture-pane) cat "$PANE_FILE"; exit 0 ;;\n'
              '  esac\n'
              'done\nexit 0\n')
os.chmod(os.path.join(_bin, "tmux"), 0o755)
_FOOTER_157 = "  GPT-6-Sol ultra · ~/Library/Application Support/sp…  ⚠ 1 warning · f2 to view"
_REJECTED = ("■ Unrecognized command '/startup'. Type \"/\" for a list of supported commands.\n\n"
             f"\x1b[1m»\x1b[0m /startup\n\n{_FOOTER_157}\n")
_EMPTY = f"\x1b[1m»\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m\n\n{_FOOTER_157}\n"


def _derive_blocked_case(pane, queued, runtime="codex"):
    ws = tempfile.mkdtemp()
    for d in ("tasks", "results", "state"):
        os.makedirs(os.path.join(ws, d))
    for i in range(queued):
        with open(os.path.join(ws, "tasks", f"task-{i}.txt"), "w") as fh:
            fh.write("priority: normal\ntask: owner message\n")
    with open(_pane_file, "w") as fh:
        fh.write(pane)
    saved = (rh._core_running, rh._core_status, rh._gateway_running, rh._resolve_workspace,
             rh._tmux_socket)
    env_saved = {k: os.environ.get(k) for k in ("PATH", "PANE_FILE", "STUB_RUNTIME")}
    rh._core_running = lambda: True
    rh._core_status = lambda w: ("idle", time.time())
    rh._gateway_running = lambda: True
    rh._resolve_workspace = lambda repo: ws
    rh._tmux_socket = lambda: "/tmp/rh-stub.sock"
    os.environ.update(PATH=_bin + os.pathsep + os.environ["PATH"], PANE_FILE=_pane_file,
                      STUB_RUNTIME=runtime)
    try:
        return rh.derive()
    finally:
        (rh._core_running, rh._core_status, rh._gateway_running, rh._resolve_workspace,
         rh._tmux_socket) = saved
        for k, v in env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


d = _derive_blocked_case(_REJECTED, queued=1)
check("derive: idle status + queued task + Codex composer holding /startup -> blocked/warn",
      d["health"] == "blocked" and d["severity"] == "warn")
check("derive: ...the detail names the queue and the held text",
      "1 task(s) queued" in d["detail"] and "'/startup'" in d["detail"])
check("derive: ...and the gate reports it, never restarts", rh.severity_gate(d) == "report")
check("derive: an owner's Codex draft holding the queue is blocked too (not cleared, but not Ready)",
      _derive_blocked_case(_REJECTED.replace("m /startup", "m half typed"), queued=2)["health"] == "blocked")
check("derive: CONTROL empty Codex composer + queued task -> idle/ok",
      _derive_blocked_case(_EMPTY, queued=1)["health"] == "idle")
check("derive: CONTROL /startup in the composer but nothing queued -> idle",
      _derive_blocked_case(_REJECTED, queued=0)["health"] == "idle")
check("derive: CONTROL a Claude draft does not hold its queue -> idle",
      _derive_blocked_case(f"❯ /startup\n{_FOOTER}", queued=1, runtime="claude")["health"] == "idle")
check("severity_of: blocked is warn", rh.severity_of("blocked") == "warn")
check("derive: a session that cannot name its runtime is not judged -> idle",
      _derive_blocked_case(_REJECTED, queued=1, runtime="")["health"] == "idle")
check("_queued_tasks: an unreadable workspace counts as no queue, never a crash",
      rh._queued_tasks(None) == 0)
_ocw = rh.cli_wedge.core_target
rh.cli_wedge.core_target = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tmux gone"))
try:
    check("_pane_blocks_dispatch: a failing pane probe is no verdict, never a crash",
          rh._pane_blocks_dispatch(tempfile.mkdtemp()) is None)
finally:
    rh.cli_wedge.core_target = _ocw

print("\n" + ("PASS — runtime-health green" if fails == 0 else "FAIL — %d failing" % fails))
sys.exit(fails)
