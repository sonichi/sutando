#!/usr/bin/env python3
"""runtime-health.py — derive this Sutando core's live health as one JSON object.

The machine-readable "is my agent working, idle, stuck-at-login, or offline?"
signal. The desktop app's Console renders it as a plain-English status strip +
one-click action cards (regular users) instead of making them read a raw
terminal; `sutando-whoami` can embed it too. Owner-designed 2026-07-13 — the
"when she's not responding, I can't tell if she's thinking or stuck" painpoint,
made concrete when a core sat unresponsive at claude's `/login` (locked keychain).

    python3 src/runtime-health.py           # prints JSON; also writes state/runtime-health.json

Output (single JSON object on stdout):
    health           working | idle | needs_login | offline | unknown
    authenticated    bool | null  (false when the core is sitting at claude's login prompt;
                     null when we can't tell, e.g. the core is offline)
    core_running     bool   (a `sutando-core` tmux session exists on the socket)
    gateway_running  bool   (the relay gateway bridge process is up)
    tmux_socket      the SUTANDO_TMUX_SOCKET this probed (private-socket aware)
    session          the tmux session name
    detail           short human string for the status strip

Design: every probe is best-effort and degrades — a missing tmux or an
unreadable status file yields `unknown`, never a crash. This is a read-only
observer; it starts nothing and kills nothing.
"""
import json
import os
import re
import subprocess
import sys

SESSION = "sutando-core"
TMUX_SOCKET = os.environ.get("SUTANDO_TMUX_SOCKET", "/tmp/sutando-tmux.sock")

# Markers that mean the bundled claude CLI is sitting at its auth prompt and the
# core therefore cannot act. Kept broad on purpose — the failure mode is a user
# staring at an unresponsive agent, so a false "needs_login" (rare) is far less
# costly than missing a real one.
_LOGIN_MARKERS = (
    "not logged in",
    "please run /login",
    "run `claude login`",
    "run 'claude login'",
    "unlock-keychain",
    "invalid api key",
    "authentication_error",
)


def _run(cmd):
    """Run a command, returning (rc, stdout). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def _core_running():
    # has-session returns non-zero (and _run yields 127 if tmux is absent), so a
    # missing tmux or socket degrades cleanly to "not running".
    rc, _ = _run(["tmux", "-S", TMUX_SOCKET, "has-session", "-t", SESSION])
    return rc == 0


def _gateway_running():
    rc, _ = _run(["pgrep", "-f", "remote-gateway-bridge"])
    if rc == 0:
        return True
    # Fallback: a window named "gateway" in the core session.
    rc, out = _run(["tmux", "-S", TMUX_SOCKET, "list-windows", "-t", SESSION, "-F", "#{window_name}"])
    return rc == 0 and any(w.strip() == "gateway" for w in out.splitlines())


def _pane_text():
    rc, out = _run(["tmux", "-S", TMUX_SOCKET, "capture-pane", "-p", "-t", SESSION])
    return out if rc == 0 else ""


def needs_login(pane_text):
    """Pure predicate: does the core pane show claude's auth prompt? Testable
    without a live tmux — this is the load-bearing 'stuck vs thinking' decision."""
    low = pane_text.lower()
    return any(m in low for m in _LOGIN_MARKERS)


def _core_status(workspace):
    """Read the agent's own status ('running'|'idle') from core-status.json.

    This is a shared state file written by other processes, so treat it as
    untrusted: a missing/corrupt file (OSError/ValueError) OR a valid-but-non-object
    JSON value (e.g. a stray `[]` — `.get` would AttributeError) degrades to None,
    never a crash — keeping the script's "unknown, not exception" contract.
    """
    try:
        with open(os.path.join(workspace, "state", "core-status.json")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data.get("status") if isinstance(data, dict) else None


def _resolve_workspace(repo):
    rc, out = _run(["bash", os.path.join(repo, "scripts", "sutando-config.sh"), "workspace"])
    return out.strip() if rc == 0 and out.strip() else os.path.join(repo, "workspace")


def derive():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    workspace = _resolve_workspace(repo)

    core = _core_running()
    gateway = _gateway_running()

    if not core:
        health, authed, detail = "offline", None, "Agent is not running"
    else:
        if needs_login(_pane_text()):
            health, authed, detail = "needs_login", False, "Agent needs to sign in"
        else:
            status = _core_status(workspace)
            authed = True
            if status == "running":
                health, detail = "working", "Agent is working"
            elif status == "idle":
                health, detail = "idle", "Agent is online and idle"
            else:
                health, detail = "unknown", "Agent is running (status unknown)"

    return {
        "health": health,
        "authenticated": authed,
        "core_running": core,
        "gateway_running": gateway,
        "tmux_socket": TMUX_SOCKET,
        "session": SESSION,
        "detail": detail,
    }


def main():
    result = derive()
    # Best-effort persist so anything (app, dashboard) can read the latest without
    # re-probing; failure to write must not fail the read.
    try:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ws = _resolve_workspace(repo)
        state_dir = os.path.join(ws, "state")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "runtime-health.json"), "w") as f:
            json.dump(result, f, indent=2)
    except OSError:
        pass
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
