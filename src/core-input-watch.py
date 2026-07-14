#!/usr/bin/env python3
"""core-input-watch.py — surface "the agent needs your input" to the desktop UI.

The detached bundled core runs Claude Code with no TTY. Most first-run gates are
pre-seeded (folder-trust, bypass-permissions, onboarding) so the core never stops
on them — but some interactions inherently need the user (/login) or can't be
predicted (a mid-session permission request, a model/selection prompt, an unknown
dialog). With no TTY the core silently BLOCKS on those, "alive but stuck".

This watcher polls the core's tmux pane and, when it detects the core parked on
an interactive prompt AND not progressing, writes <workspace>/state/core-needs-
input.json so the app can show an "Action needed" banner with the prompt text +
a button to open the terminal. When the prompt clears, it writes blocked:false.

It is the catch-all behind the pre-seeds: seed what we can, this surfaces the rest.

Usage:
  core-input-watch.py --socket <tmux.sock> --session sutando-core \
      --out <workspace>/state/core-needs-input.json [--interval 3] [--stable 2]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time

# Interactive-prompt signatures. Each: (kind, compiled regex). Order = priority;
# the FIRST match classifies the block. Kept specific so the normal idle prompt
# (a bare "❯ " ready for a task) is NEVER flagged — only genuine input gates.
_SIGNATURES = [
    ("folder-trust", re.compile(r"trust the files in this folder|Do you trust", re.I)),
    ("bypass-permissions", re.compile(r"Bypass Permissions mode|Yes, I accept", re.I)),
    ("login", re.compile(r"Select login method|Paste code here|Browser didn'?t open", re.I)),
    ("press-enter", re.compile(r"Press Enter to continue", re.I)),
    # Generic numbered menu awaiting a choice, e.g. "❯ 1. …" plus a cancel hint.
    ("selection", re.compile(r"(❯\s*\d+\.|\bSelect\b).*", re.S)),
    ("permission", re.compile(r"Do you want to (proceed|allow)|Allow this action|permission to", re.I)),
]

# A pane is only "awaiting input" if it ALSO carries a confirm/cancel affordance —
# this filters out normal agent output that merely happens to contain a digit.
_AWAIT_HINT = re.compile(
    r"Esc to cancel|Enter to confirm|Press Enter|Paste code|to accept|❯\s*\d+\.",
    re.I,
)
# The normal ready-for-work state: a bare prompt with the bypass footer. NEVER flag.
_IDLE = re.compile(r"⏵⏵\s*bypass permissions on|for agents\b", re.I)


def classify(pane: str) -> tuple[str, str] | None:
    """Return (kind, excerpt) if the pane is awaiting user input, else None."""
    tail = "\n".join([ln for ln in pane.splitlines() if ln.strip()][-14:])
    if not _AWAIT_HINT.search(tail):
        return None
    # An idle "ready for a task" prompt shows the bypass footer with no gate above.
    if _IDLE.search(tail) and not any(rx.search(tail) for _, rx in _SIGNATURES[:5]):
        return None
    for kind, rx in _SIGNATURES:
        if rx.search(tail):
            return kind, tail
    return None


def capture(socket: str, session: str) -> str | None:
    try:
        out = subprocess.run(
            ["tmux", "-S", socket, "capture-pane", "-p", "-t", f"{session}:0"],
            capture_output=True, text=True, timeout=8,
        )
        return out.stdout if out.returncode == 0 else None
    except Exception:
        return None


def _atomic_write(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--session", default="sutando-core")
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--stable", type=int, default=2,
                    help="consecutive identical polls before flagging (debounce)")
    a = ap.parse_args()

    last_excerpt = None
    stable = 0
    last_written_blocked = None
    while True:
        pane = capture(a.socket, a.session)
        hit = classify(pane) if pane else None
        if hit:
            kind, excerpt = hit
            # Debounce: only flag once the same prompt persists (not a transient
            # menu the core is actively navigating).
            stable = stable + 1 if excerpt == last_excerpt else 1
            last_excerpt = excerpt
            if stable >= a.stable and last_written_blocked is not True:
                _atomic_write(a.out, {
                    "blocked": True, "kind": kind, "prompt": excerpt,
                    "ts": int(os.environ.get("_NOW_", "0")) or None,
                    "session": a.session,
                })
                last_written_blocked = True
        else:
            stable = 0
            last_excerpt = None
            if last_written_blocked is not False:
                _atomic_write(a.out, {"blocked": False, "session": a.session})
                last_written_blocked = False
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
