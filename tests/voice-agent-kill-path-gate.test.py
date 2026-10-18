#!/usr/bin/env python3
"""T4's grep-based CI gate: no NEW unguarded kill/kickstart of voice-agent.

Every kill-and-replace of voice-agent must run as ONE guarded
`scripts/voice-lock.py takeover` transaction (validate identity → TERM →
wait → KILL → revalidate → unlink under the held fcntl guard) — either
directly or via the guarded wrapper `scripts/restart-voice-agent.sh`
(validation-then-kickstart). This gate greps every tracked source file for
the unguarded shapes and fails when one appears outside the allowlisted
helper files:

  * `launchctl kickstart -k … voice-agent`   (kill-and-restart of the job)
  * `pkill … voice`                          (blind argv-match kill)
  * `kill` on the same line as `9900`        (port-match kill of :9900)
  * `reap_wedged_listener … 9900`            (generic lsof|xargs-kill reap
                                              pointed at the voice port)

Comment lines (#, //, *), doc/tests trees and advisory `echo` lines without
command substitution are skipped; known documentation strings inside code are
allowlisted individually so any new hit is a conscious allowlist edit.

Run: python3 tests/voice-agent-kill-path-gate.test.py
Exit: 0 = clean, 1 = an unguarded kill path appeared
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The two files ALLOWED to signal voice-agent:
#   * scripts/voice-lock.py — the single guarded transaction implementation
#     (the only code that ever sends the signals, under the held guard).
#   * scripts/restart-voice-agent.sh — the guarded restart wrapper: one
#     voice-lock.py takeover for the pre-kickstart validation, then the
#     launchd kickstart (a restart of launchd's own job) + verification.
ALLOWED_FILES = {
    "scripts/voice-lock.py",
    "scripts/restart-voice-agent.sh",
}

# Documentation strings inside code (not executed): (path, required substring).
ALLOWED_LINES = [
    ("src/health-check.py", "then `launchctl kickstart -k"),
]

PATTERNS = [
    re.compile(r"kickstart[^-\n]*-k.*voice-agent"),
    re.compile(r"pkill.*voice"),
    re.compile(r"\bkill\b.*9900|9900.*\bkill\b"),
    re.compile(r"reap_wedged_listener.*9900"),
]

COMMENT_PREFIXES = ("#", "//", "*", "/*", "REM ")


def tracked_files():
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    for rel in out.splitlines():
        if rel.startswith(("tests/", "docs/")) or rel.endswith(".md"):
            continue
        yield rel


def is_comment(stripped):
    return stripped.startswith(COMMENT_PREFIXES)


def is_advisory_echo(stripped):
    """A pure `echo "…"` advisory line — allowed ONLY when it cannot execute
    anything (no unescaped command substitution)."""
    if not stripped.startswith("echo"):
        return False
    return "$(" not in stripped.replace("\\$(", "")


def is_allowlisted_line(rel, line):
    return any(rel == path and token in line for path, token in ALLOWED_LINES)


def main():
    offenders = []
    for rel in tracked_files():
        if rel in ALLOWED_FILES:
            continue
        path = REPO / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, IsADirectoryError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if not any(p.search(line) for p in PATTERNS):
                continue
            stripped = line.strip()
            if is_comment(stripped) or is_advisory_echo(stripped):
                continue
            if is_allowlisted_line(rel, line):
                continue
            offenders.append(f"{rel}:{lineno}: {stripped}")

    if offenders:
        print("Unguarded voice-agent kill/kickstart path(s) found (amendment T4):")
        for o in offenders:
            print(f"  {o}")
        print(
            "\nRoute the kill through `scripts/voice-lock.py takeover` (or the\n"
            "guarded wrapper scripts/restart-voice-agent.sh). If this line is\n"
            "provably documentation, add it to ALLOWED_LINES in this gate."
        )
        return 1
    print("voice-agent kill-path gate: clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
