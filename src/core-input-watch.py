#!/usr/bin/env python3
"""core-input-watch.py — the core supervisor MONITOR (M1).

The detached bundled core runs Claude Code's interactive TUI with no TTY. Most
first-run gates are pre-seeded so the core never stops on them, but some
interactions inherently need the user (/login) or can't be predicted (a
mid-session permission request, a model/selection prompt, an unknown dialog).
With no TTY the core silently BLOCKS, "alive but stuck".

This is the MONITOR layer of the core supervisor (design: notes/design-core-
supervisor.md). Each tick it composes the core's state from three cheap signals —
process liveness, the tmux pane (via classify()), and gateway liveness — and
writes a single `state/core-supervisor.json` the desktop app reads for BOTH its
status display AND the "Action needed" banner. It never acts; PREVENT (seeds),
AUTO-ANSWER, ESCALATE (the banner), and RECOVER (restart) are the other layers.

State (exactly one per tick):
  running · idle-ready · blocked-known · blocked-human · hung · crashed ·
  gateway-down · logged-out

Signal schema (state/core-supervisor.json):
  { "state": "<state>", "detail": "<human string>",
    "prompt": "<pane excerpt if blocked, else null>",
    "kind": "<gate kind if blocked, else null>",
    "session": "sutando-core" }

Usage:
  core-input-watch.py --socket <tmux.sock> --session sutando-core \
      --out <workspace>/state/core-supervisor.json [--app-data <dir>] \
      [--interval 3] [--stable 2]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time

# ---- ESCALATE-detection: interactive-prompt signatures. First match classifies.
# Specific so the idle "❯ " prompt (ready for a task) is NEVER flagged.
_SIGNATURES = [
    ("folder-trust", re.compile(r"trust the files in this folder|Do you trust", re.I)),
    ("bypass-permissions", re.compile(r"Bypass Permissions mode|Yes, I accept", re.I)),
    ("login", re.compile(r"Select login method|Paste code here|Browser didn'?t open", re.I)),
    ("press-enter", re.compile(r"Press Enter to continue", re.I)),
    ("selection", re.compile(r"(❯\s*\d+\.|\bSelect\b).*", re.S)),
    ("permission", re.compile(r"Do you want to (proceed|allow)|Allow this action|permission to", re.I)),
]
_AWAIT_HINT = re.compile(
    r"Esc to cancel|Enter to confirm|Press Enter|Paste code|to accept|❯\s*\d+\.", re.I)
_IDLE = re.compile(r"⏵⏵\s*bypass permissions on|for agents\b", re.I)
_LOGGED_OUT = re.compile(r"Not logged in|Please run /login|Invalid API key", re.I)

# Gates that need a human (can't be auto-answered): login + any unrecognized
# selection/permission. The rest (trust/bypass/press-enter) are known-safe.
_HUMAN_GATES = {"login", "selection", "permission", "unknown"}


def classify(pane: str):
    """Return (kind, excerpt) if the pane is awaiting user input, else None."""
    tail = "\n".join([ln for ln in pane.splitlines() if ln.strip()][-14:])
    if not _AWAIT_HINT.search(tail):
        return None
    if _IDLE.search(tail) and not any(rx.search(tail) for _, rx in _SIGNATURES[:5]):
        return None
    for kind, rx in _SIGNATURES:
        if rx.search(tail):
            return kind, tail
    # No specific signature — but an input affordance IS present and this is NOT
    # the idle prompt. That means an UNFORESEEN prompt. Surface it rather than
    # leave a silent dead-end (owner's no-dead-end requirement 2026-07-14): we
    # cannot enumerate every possible TUI question, so ANY await-affordance that
    # isn't the known idle-ready state is treated as needing the user.
    return "unknown", tail


def compose_state(pane, core_alive, gateway_alive, progressing):
    """Pure state-machine core: map the signals + pane to a supervisor state.

    Returns (state, detail, prompt_or_None, kind_or_None). `progressing` = the pane
    changed since the previous tick (only decisive when not at a prompt / idle).
    """
    if not core_alive:
        return "crashed", "core process/session not found", None, None
    hit = classify(pane) if pane else None
    if hit:
        kind, excerpt = hit
        if kind in _HUMAN_GATES:
            return "blocked-human", f"awaiting user: {kind}", excerpt, kind
        return "blocked-known", f"at known gate: {kind}", excerpt, kind
    # No interactive prompt showing.
    if pane and _LOGGED_OUT.search(pane):
        return "logged-out", "core not authenticated (needs /login)", None, None
    if not gateway_alive:
        return "gateway-down", "core up but relay gateway not running", None, None
    if pane and _IDLE.search(pane):
        return "idle-ready", "ready for a task", None, None
    if progressing:
        return "running", "actively processing", None, None
    # Stalled: alive, not idle, no recognized affordance, not progressing. Could be
    # an unforeseen prompt with no await-hint we matched. Carry the pane so the UI
    # shows WHAT it's stuck on — never a silent dead-end.
    tail = "\n".join([ln for ln in (pane or "").splitlines() if ln.strip()][-14:])
    return "hung", "core alive but stalled (no progress, no recognized prompt)", tail or None, "unknown"


# ---- Liveness signals (cheap, pgrep/tmux-based). --------------------------- #
def _has_session(socket, session):
    try:
        return subprocess.run(["tmux", "-S", socket, "has-session", "-t", session],
                              capture_output=True, timeout=6).returncode == 0
    except Exception:
        return False


def _pgrep(pattern):
    try:
        return subprocess.run(["pgrep", "-f", pattern],
                              capture_output=True, timeout=6).returncode == 0
    except Exception:
        return False


def core_alive(socket, session):
    # A live core = the tmux session exists AND a claude bound to it is running.
    return _has_session(socket, session) and _pgrep(f"claude.*--name.*{session}")


def gateway_alive(app_data):
    # The bundled gateway runs from the app-data interpreter (see console_status:
    # keepalive cd's into $ENGINE so the argv is relative — match the app-unique
    # runtime-python dir, which uniquely scopes to THIS app's gateway).
    if app_data:
        return _pgrep(os.path.join(app_data, "engine", "runtime", "python"))
    return _pgrep("remote-gateway-bridge")


def capture(socket, session):
    try:
        out = subprocess.run(["tmux", "-S", socket, "capture-pane", "-p", "-t", f"{session}:0"],
                             capture_output=True, text=True, timeout=8)
        return out.stdout if out.returncode == 0 else None
    except Exception:
        return None


def _atomic_write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--session", default="sutando-core")
    ap.add_argument("--out", required=True)
    ap.add_argument("--app-data", default="", help="app-support dir (for gateway probe)")
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--stable", type=int, default=2,
                    help="consecutive identical prompt polls before escalating (debounce)")
    ap.add_argument("--once", action="store_true", help="one tick then exit (for tests/probes)")
    a = ap.parse_args()

    last_hash = None
    last_sig = None
    stable_prompt = 0
    last_prompt = None
    while True:
        pane = capture(a.socket, a.session)
        cur_hash = hashlib.sha1((pane or "").encode()).hexdigest()
        progressing = cur_hash != last_hash
        last_hash = cur_hash

        state, detail, prompt, kind = compose_state(
            pane or "", core_alive(a.socket, a.session),
            gateway_alive(a.app_data), progressing)

        # Debounce prompt escalation: only surface once the SAME prompt persists
        # (not a menu the core is actively navigating through).
        if state in ("blocked-human", "blocked-known"):
            stable_prompt = stable_prompt + 1 if prompt == last_prompt else 1
            last_prompt = prompt
            if stable_prompt < a.stable:
                state, detail, prompt, kind = "running", "processing (prompt settling)", None, None
        else:
            stable_prompt = 0
            last_prompt = None

        sig = (state, prompt)
        if sig != last_sig:
            _atomic_write(a.out, {"state": state, "detail": detail,
                                  "prompt": prompt, "kind": kind, "session": a.session})
            last_sig = sig
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
