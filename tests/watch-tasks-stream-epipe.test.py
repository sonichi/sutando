#!/usr/bin/env python3
"""
Structural guards for src/watch-tasks-stream.sh EPIPE fixes (#1088).

Mode A (EPIPE-on-dead-reader): the watcher silently buffered ~100 events
into the kernel pipe before dying, causing a 4h DM-loss incident on
2026-05-23. Fix: replace bare `echo` with `printf ... || exit 0` so the
first failed write propagates EPIPE immediately.

These tests verify the source has the correct shell patterns so a future
refactor can't regress back to bare echo without failing CI.

Run: python3 tests/watch-tasks-stream-epipe.test.py
Exit: 0 on pass, 1 on fail.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "watch-tasks-stream.sh"


def fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def main() -> int:
    if not SRC.exists():
        return fail(f"{SRC} not found")

    src = SRC.read_text()

    # A1: printf used for TASK_FILE output (not bare echo)
    if 'echo "TASK_FILE:' in src or "echo 'TASK_FILE:" in src:
        return fail(
            "watch-tasks-stream.sh still uses bare echo for TASK_FILE output — "
            "must use printf to propagate EPIPE immediately (#1088 Mode A)"
        )

    # A2: printf present for TASK_FILE
    if "printf 'TASK_FILE:" not in src and 'printf "TASK_FILE:' not in src:
        return fail(
            "watch-tasks-stream.sh has no printf 'TASK_FILE:' call — "
            "EPIPE fix (#1088) may be missing"
        )

    # A3: || exit pattern on TASK_FILE printf lines
    # Both the initial sweep and the event loop must use || exit.
    lines = src.splitlines()
    task_file_printf_lines = [
        (i + 1, line.strip())
        for i, line in enumerate(lines)
        if "printf" in line and "TASK_FILE" in line
    ]
    if not task_file_printf_lines:
        return fail(
            "No printf TASK_FILE lines found in watch-tasks-stream.sh"
        )
    for lineno, line in task_file_printf_lines:
        if "|| exit" not in line:
            return fail(
                f"watch-tasks-stream.sh line {lineno}: printf TASK_FILE missing '|| exit' "
                f"— EPIPE not propagated: {line!r} (#1088 Mode A)"
            )

    # A4: both initial sweep and event loop use printf (count >= 2 printf TASK_FILE lines)
    if len(task_file_printf_lines) < 2:
        return fail(
            f"Only {len(task_file_printf_lines)} printf TASK_FILE line(s) found — "
            "expected at least 2 (initial sweep + event loop) (#1088)"
        )

    print(
        f"PASS: watch-tasks-stream.sh uses printf+exit for TASK_FILE output "
        f"({len(task_file_printf_lines)} sites) — EPIPE propagates immediately (#1088)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
