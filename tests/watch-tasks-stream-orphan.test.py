#!/usr/bin/env python3
"""
Structural regression test for issue #1088 — watch-tasks-stream.sh orphan fixes.

Two failure modes fixed:
  Mode A (EPIPE-buffer): old `echo` buffered ~64KB before detecting dead reader.
                         Fix: `printf ... || exit 0` exits immediately on EPIPE.
  Mode B (PPID=1 orphan): old design stored $$ (bash wrapper PID); wrapper dying
                           left fswatch running as an orphan. Fix: PID file stores
                           fswatch's PID so Stop hook / startup reaper kill it
                           directly; when fswatch dies the while-read loop sees EOF.

Guards (structural — reads the script, does not invoke fswatch):
  A. printf with || exit 0 used in emit path (not bare echo)
  B. mkfifo used to create a temp FIFO
  C. fswatch started in background (> FIFO &) before the while-read loop
  D. FSWATCH_PID captured and written to PID_FILE (not $$)
  E. trap kills FSWATCH_PID (not just rm PID_FILE)

Run: python3 tests/watch-tasks-stream-orphan.test.py
Exit: 0 = pass, 1 = fail.
"""

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "watch-tasks-stream.sh"


def fail(msg: str, ctx: str = "") -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("---context---", file=sys.stderr)
        print(ctx[:800], file=sys.stderr)
    return 1


def main() -> int:
    if not SCRIPT.exists():
        return fail(f"{SCRIPT} not found")

    src = SCRIPT.read_text()

    # A. Mode A fix: emit path uses printf ... || exit 0, not bare echo.
    if not re.search(r"printf\s+'TASK_FILE:", src):
        return fail("emit path must use printf (not echo) for Mode A EPIPE fix")
    if "|| exit 0" not in src:
        return fail("printf must be followed by || exit 0 to catch EPIPE immediately")
    # Confirm the two are on the same logical line
    if not re.search(r"printf\s+'TASK_FILE:[^']*'\s+.*\|\|\s*exit\s+0", src):
        return fail("printf 'TASK_FILE: ...' || exit 0 not found on same line")

    # B. Mode B fix: FIFO created via mkfifo.
    if "mkfifo" not in src:
        return fail("mkfifo must be used to create the temp FIFO (Mode B fix)")
    if "_FIFO=" not in src and "FIFO=" not in src:
        return fail("FIFO variable must be defined")

    # C. fswatch started in background writing to FIFO before while-read.
    # Pattern: fswatch ... > "$_FIFO" & (possibly with line continuations)
    if not re.search(r'fswatch[\s\\]+.*>\s*"\$_FIFO"\s*&', src, re.DOTALL):
        return fail("fswatch must be started in background writing to FIFO (> \"$_FIFO\" &)")

    # D. FSWATCH_PID captured from $! and stored in PID_FILE (not $$).
    if not re.search(r"FSWATCH_PID=\s*\$!", src):
        return fail("FSWATCH_PID must be captured from $! after fswatch background start")
    # PID file must store FSWATCH_PID, not $$
    if not re.search(r'echo\s+"\$FSWATCH_PID"\s*>\s*"\$PID_FILE"', src):
        return fail("PID_FILE must store FSWATCH_PID (fswatch's PID), not $$ (wrapper PID)")
    if re.search(r'echo\s+"\$\$"\s*>\s*"\$PID_FILE"', src):
        return fail("PID_FILE must NOT store $$ (that leaves fswatch orphaned)")

    # E. trap kills FSWATCH_PID (not just removes PID file).
    trap_match = re.search(r"trap\s+'([^']+)'\s+EXIT", src)
    if not trap_match:
        return fail("trap ... EXIT not found")
    trap_body = trap_match.group(1)
    if "FSWATCH_PID" not in trap_body:
        return fail(f"EXIT trap must kill FSWATCH_PID, got: {trap_body!r}")
    if "kill" not in trap_body:
        return fail(f"EXIT trap must kill FSWATCH_PID, got: {trap_body!r}")

    print("PASS: watch-tasks-stream.sh orphan guards pass.")
    print("  - printf || exit 0 used for EPIPE detection (Mode A fix)")
    print("  - mkfifo used for temp FIFO (Mode B setup)")
    print("  - fswatch started in background writing to FIFO")
    print("  - PID_FILE stores FSWATCH_PID (not $$)")
    print("  - EXIT trap kills FSWATCH_PID")
    return 0


if __name__ == "__main__":
    sys.exit(main())
