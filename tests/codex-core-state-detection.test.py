#!/usr/bin/env python3
"""Codex-core detection for the silent-core notices.

A Codex core's logged-out state can't be scraped from Claude's auth prompt.
Instead runtime-health asks `codex login status` (exit 0 = signed in), and
core-input-watch skips the Claude-only pane gate classifier for a codex core so
that neutral verdict (offline/needs_login/working/idle) drives the supervisor
state. Crash/hang/working/idle are already runtime-neutral. Codex rate-limit is
a tracked follow-up (no clean signal yet).

    python3 tests/codex-core-state-detection.test.py
"""
import importlib.util
import os
import sys
import types
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rh = _load("runtime_health_codex", "src/runtime-health.py")
ciw = _load("core_input_watch_codex", "src/core-input-watch.py")

fails = 0


def check(name, cond):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


class _Proc:
    def __init__(self, rc, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def _reset_caches():
    rh._CODEX_LOGIN_CACHE[0] = 0.0
    rh._CODEX_LOGIN_CACHE[1] = None
    rh._RUNTIME_CACHE[0] = 0.0
    rh._RUNTIME_CACHE[1] = None


# 1) core_runtime() reads SUTANDO_CORE_RUNTIME; defaults to claude on failure.
_reset_caches()
with patch.object(rh, "_core_session_env", lambda v: "codex" if v == "SUTANDO_CORE_RUNTIME" else None):
    check("core_runtime: reads codex from the session env", rh.core_runtime() == "codex")
_reset_caches()
with patch.object(rh, "_core_session_env", lambda v: None), \
        patch.dict(os.environ, {}, clear=False):
    os.environ.pop("SUTANDO_CORE_RUNTIME", None)
    check("core_runtime: defaults to claude when unknown", rh.core_runtime() == "claude")

# 2) Genuine logout: exit != 0 AND an explicit "Not logged in" message → True.
_reset_caches()
with patch.object(rh, "core_runtime", lambda: "codex"), \
        patch.object(rh, "_core_session_env", lambda v: None), \
        patch.object(rh.subprocess, "run", lambda *a, **k: _Proc(1, "Not logged in\n")):
    check("codex logged-out (explicit msg): _login_signal True", rh._login_signal() is True)

# 3) Signed in → exit 0 → False, and never scrapes the pane.
_reset_caches()
with patch.object(rh, "core_runtime", lambda: "codex"), \
        patch.object(rh, "_core_session_env", lambda v: None), \
        patch.object(rh, "_pane_text", lambda: (_ for _ in ()).throw(AssertionError("pane read for codex"))), \
        patch.object(rh.subprocess, "run", lambda *a, **k: _Proc(0, "Logged in as user@example\n")):
    check("codex signed-in: _login_signal False (no pane scrape)", rh._login_signal() is False)

# 4) FINDING 1 — a config error / missing-node failure is NOT a logout.
#    codex 0.137 exits 1 for a bad config.toml; node-missing wrappers exit 127.
_reset_caches()
with patch.object(rh, "core_runtime", lambda: "codex"), \
        patch.object(rh, "_core_session_env", lambda v: None), \
        patch.object(rh.subprocess, "run", lambda *a, **k: _Proc(1, "", "Error loading configuration\n")):
    check("codex config error: _codex_login_needed None (not a logout)", rh._codex_login_needed() is None)
_reset_caches()
with patch.object(rh, "core_runtime", lambda: "codex"), \
        patch.object(rh, "_core_session_env", lambda v: None), \
        patch.object(rh.subprocess, "run", lambda *a, **k: _Proc(127, "", "node: command not found\n")):
    check("codex exit 127 (no node): _codex_login_needed None", rh._codex_login_needed() is None)

# 4b) FINDING 2 — a config error that QUOTES "not logged in" mid-line is NOT a
#     logout (must match a full status line, not a substring).
_reset_caches()
_cfg_err = 'Error loading configuration: mode "not logged in" is not valid\n'
with patch.object(rh, "core_runtime", lambda: "codex"), \
        patch.object(rh, "_core_session_env", lambda v: None), \
        patch.object(rh.subprocess, "run", lambda *a, **k: _Proc(1, "", _cfg_err)):
    check("codex config error quoting the phrase: None (not a logout)",
          rh._codex_login_needed() is None)
check("logout matcher: bullet-prefixed status line matches",
      rh._codex_says_logged_out("· Not logged in\n") is True)
check("logout matcher: mid-sentence quote does NOT match",
      rh._codex_says_logged_out('config error: "not logged in" invalid') is False)

# 5) probe can't even start → unknown → not a logout.
_reset_caches()
with patch.object(rh, "core_runtime", lambda: "codex"), \
        patch.object(rh, "_core_session_env", lambda v: None), \
        patch.object(rh.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no codex"))):
    check("codex probe fails: _login_signal False (no false logged-out)", rh._login_signal() is False)

# 6) FINDING 2 — if the core's account can't be resolved, DON'T probe the
#    monitor's account: return unknown (None) instead of a wrong verdict.
_reset_caches()
_seen_env = {}
def _capture_run(cmd, *a, **k):
    _seen_env["CODEX_HOME"] = (k.get("env") or {}).get("CODEX_HOME")
    return _Proc(1, "Not logged in\n")
with patch.dict(os.environ, {"CODEX_HOME": "/monitor/account"}, clear=False), \
        patch.object(rh, "core_runtime", lambda: "codex"), \
        patch.object(rh, "_core_session_env", lambda v: rh._ENV_UNAVAILABLE), \
        patch.object(rh.subprocess, "run", _capture_run):
    check("codex core account unresolved → None (not the monitor's verdict)",
          rh._codex_login_needed() is None)
    check("codex core account unresolved → probe NOT run against monitor account",
          "CODEX_HOME" not in _seen_env)

# 5) Claude path is unchanged: _login_signal defers to needs_login(pane).
_reset_caches()
with patch.object(rh, "core_runtime", lambda: "claude"), \
        patch.object(rh, "_pane_text", lambda: "Please run /login"):
    check("claude path unchanged: pane-scraped login still works", rh._login_signal() is True)

# 6) compose_state: for a codex core, needs_login → logged-out even if the pane
#    would trip a Claude gate signature (classifier is skipped for codex).
claude_login_menu = "Select login method\n❯ 1. Subscription\nPaste code here"
st_codex = ciw.compose_state(claude_login_menu, "needs_login", True, runtime="codex")
check("codex compose_state: needs_login → logged-out (claude classifier skipped)",
      st_codex[0] == "logged-out")
# Same input on the claude runtime still classifies the gate (unchanged).
st_claude = ciw.compose_state(claude_login_menu, "needs_login", True, runtime="claude")
check("claude compose_state: still classifies the login gate",
      st_claude[0] in ("blocked-human", "logged-out"))

# 7) codex crash is runtime-neutral (offline → crashed regardless of runtime).
check("codex crash → crashed (neutral)",
      ciw.compose_state("", "offline", True, runtime="codex")[0] == "crashed")

# 8) Default install: CODEX_HOME unset. Stubs mirror MEASURED listing-form
#    emissions (rc=0 + stdout when the session answers; errors are stderr-only).
_listing = "SUTANDO_CORE_RUNTIME=codex\nSSH_AUTH_SOCK=/tmp/x\n-REMOVED_VAR\n"
_reset_caches()
with patch.object(rh, "_run", lambda cmd: (0, _listing)):
    check("session env: var absent from listing → None (unset)",
          rh._core_session_env("CODEX_HOME") is None)
    check("session env: set var parsed from listing",
          rh._core_session_env("SUTANDO_CORE_RUNTIME") == "codex")
    check("session env: '-VAR' removed marker → None (unset)",
          rh._core_session_env("REMOVED_VAR") is None)
_reset_caches()
with patch.object(rh, "_run", lambda cmd: (1, "")):
    check("session env: real query failure (rc!=0, stderr-only) → _ENV_UNAVAILABLE",
          rh._core_session_env("CODEX_HOME") is rh._ENV_UNAVAILABLE)
_reset_caches()
probed = []
with patch.object(rh, "core_runtime", lambda: "codex"), \
        patch.object(rh, "_core_session_env", lambda v: None), \
        patch.object(rh.subprocess, "run",
                     lambda *a, **k: probed.append(a) or _Proc(1, "Not logged in\n")):
    check("default install (CODEX_HOME unset): login probe RUNS and detects logout",
          rh._login_signal() is True and len(probed) == 1)

# 9) An IDLE codex core routinely has a stale core-status and can never match
#    Claude's idle-footer rescue: it must hold ("unobserved"), not read hung.
check("codex stale-status: unobserved hold, never hung",
      ciw.compose_state("some codex pane text", "unknown", True,
                        runtime="codex")[0] == "unobserved")
check("claude stale-status with no idle footer still reads hung",
      ciw.compose_state("mid-work spinner", "unknown", True,
                        runtime="claude")[0] == "hung")

# 10) The same three verdicts against a REAL throwaway tmux server (never the
#     live core's socket) — fixture-vs-reality is how this bug shipped twice.
import shutil
import subprocess
import tempfile
if shutil.which("tmux"):
    _reset_caches()
    _tdir = tempfile.mkdtemp()
    _sock = os.path.join(_tdir, "test-tmux.sock")
    try:
        subprocess.run(["tmux", "-S", _sock, "new-session", "-d", "-s",
                        "envtest", "sleep", "30"], check=True, timeout=10)
        subprocess.run(["tmux", "-S", _sock, "set-environment", "-t", "envtest",
                        "SUTANDO_CORE_RUNTIME", "codex"], check=True, timeout=10)
        with patch.object(rh, "TMUX_SOCKET", _sock), \
                patch.object(rh, "SESSION", "envtest"):
            check("real tmux: set var read back",
                  rh._core_session_env("SUTANDO_CORE_RUNTIME") == "codex")
            check("real tmux: unset var → None (the shipped-twice case)",
                  rh._core_session_env("CODEX_HOME") is None)
        with patch.object(rh, "TMUX_SOCKET", _sock), \
                patch.object(rh, "SESSION", "no-such-session"):
            check("real tmux: missing session → _ENV_UNAVAILABLE",
                  rh._core_session_env("CODEX_HOME") is rh._ENV_UNAVAILABLE)
    finally:
        subprocess.run(["tmux", "-S", _sock, "kill-server"],
                       capture_output=True, timeout=10)
elif os.environ.get("CI"):
    # The only arm that discriminates must not silently skip where it matters:
    # a guard that can decline to run is a guard whose absence looks like a pass.
    check("real-tmux integration RAN (tmux must be installed under CI)", False)
else:
    print("  skip real-tmux integration (tmux not installed; a red check under CI)")

if fails:
    print(f"\n{fails} FAILURE(S)")
    sys.exit(1)
print("PASS: codex core-state detection (logout via codex login status; neutral crash/hang)")
