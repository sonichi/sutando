#!/usr/bin/env python3
"""Structural tests for watch-tasks-stream.sh orphan-leak fixes (issue #1088).

## Two failure modes fixed

Mode A — EPIPE-on-dead-reader:
  `echo` silently buffers into the kernel pipe (~64KB / ~100 events) when
  the consumer (Monitor) is gone. On low-volume traffic (few DMs/day) this
  means events are swallowed for hours — the 2026-05-23 4h task-drop.
  Fix: `printf ... || exit 0` — printf returns non-zero on the FIRST failed
  write, triggering an immediate clean exit.

Mode B — PPID=1 reparent orphan:
  When Monitor wrapper exits without sending SIGTERM, the script gets
  reparented to launchd (PPID=1) and runs indefinitely with no consumer.
  Stop hook covers clean exits; PPID watchdog covers dirty exits.
  Fix: capture $PPID before a background subshell; poll `kill -0 $PPID`
  every 5 s; SIGTERM self when parent gone.

Run: python3 tests/watch-tasks-stream-orphan.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WATCHER = REPO / "src" / "watch-tasks-stream.sh"

_FAILURES: list[str] = []


def fail(msg: str, ctx: str = "") -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("--- context ---", file=sys.stderr)
        print(ctx[:600], file=sys.stderr)


def expect(cond: bool, label: str, ctx: str = "") -> None:
    if not cond:
        fail(label, ctx)


def main() -> None:
    if not WATCHER.exists():
        fail(f"file not found: {WATCHER}")
        sys.exit(1)

    src = WATCHER.read_text()
    lines = src.splitlines()

    # ── Mode A: EPIPE fix ────────────────────────────────────────────────────

    # 1. printf used in fswatch loop (not bare echo for TASK_FILE events)
    expect(
        "printf 'TASK_FILE: %s\\n'" in src,
        "watch-tasks-stream.sh: must use printf for TASK_FILE events in fswatch loop (not bare echo)",
    )

    # 2. `|| exit 0` present — immediate exit on broken pipe
    expect(
        "|| exit 0" in src,
        "watch-tasks-stream.sh: printf must be followed by '|| exit 0' to exit on EPIPE immediately",
    )

    # 3. Both on the same line (the Mode A fix is a single expression)
    printf_exit_lines = [
        ln.strip() for ln in lines
        if "printf 'TASK_FILE: %s\\n'" in ln and "|| exit 0" in ln
    ]
    expect(
        len(printf_exit_lines) >= 1,
        "watch-tasks-stream.sh: printf 'TASK_FILE: ...' and '|| exit 0' must be on the same line",
        ctx="\n".join(printf_exit_lines[:3]),
    )

    # 4. No bare `echo "TASK_FILE: $(basename "$path")"` in the fswatch
    #    streaming loop. The initial sweep uses $f (acceptable — fires before
    #    Monitor attaches). The streaming loop used $path — that's the site
    #    that must be replaced with printf.
    bare_echo_in_loop = [
        ln.strip() for ln in lines
        if re.search(r'echo\s+"TASK_FILE:\s+\$\(basename.*\$path', ln)
    ]
    expect(
        len(bare_echo_in_loop) == 0,
        f"watch-tasks-stream.sh: bare echo 'TASK_FILE: $(basename \"$path\")' in fswatch loop must be replaced with printf (found {len(bare_echo_in_loop)} site(s))",
        ctx="\n".join(bare_echo_in_loop[:3]),
    )

    # ── Mode B: PPID watchdog ────────────────────────────────────────────────

    # 5. _PARENT_PID captures $PPID before the watchdog subshell
    expect(
        "_PARENT_PID=${PPID}" in src or "_PARENT_PID=$PPID" in src,
        "watch-tasks-stream.sh: must capture _PARENT_PID=${PPID} before watchdog subshell "
        "(inside a subshell $PPID would be this script's PID, not Monitor wrapper)",
    )

    # 6. PPID watchdog subshell present
    expect(
        "kill -0" in src and "_PARENT_PID" in src,
        "watch-tasks-stream.sh: PPID watchdog must poll parent liveness with 'kill -0 $_PARENT_PID'",
    )

    # 7. Watchdog uses kill -0 on _PARENT_PID (not raw $PPID — would be wrong inside subshell)
    watchdog_kill0 = [
        ln.strip() for ln in lines
        if "kill -0" in ln and "_PARENT_PID" in ln
    ]
    expect(
        len(watchdog_kill0) >= 1,
        "watch-tasks-stream.sh: 'kill -0 \"${_PARENT_PID}\"' must appear in watchdog loop",
        ctx="\n".join(watchdog_kill0[:3]),
    )

    # 8. Watchdog sends SIGTERM to self via kill "$$"
    expect(
        'kill "$$"' in src or "kill '$$'" in src or "kill $$" in src,
        "watch-tasks-stream.sh: watchdog must kill self ('kill \"$$\"') when parent is gone",
    )

    # 9. Watchdog runs in background (&)
    watchdog_bg = [
        ln.strip() for ln in lines
        if "_PARENT_PID" in ln and "kill -0" in ln
    ]
    # The subshell block ends with ) & — find the closing line
    subshell_bg_lines = [
        ln.strip() for ln in lines
        if re.search(r'kill\s+"\$\$".*\)\s*&|^\s*\)\s*&\s*$', ln)
    ]
    # At minimum, & must appear somewhere near the watchdog block
    watchdog_block_start = next(
        (i for i, ln in enumerate(lines) if "_PARENT_PID=${PPID}" in ln or "_PARENT_PID=$PPID" in ln),
        None,
    )
    if watchdog_block_start is not None:
        watchdog_region = "\n".join(lines[watchdog_block_start:watchdog_block_start + 10])
        expect(
            "&" in watchdog_region,
            "watch-tasks-stream.sh: PPID watchdog subshell must run in background (&)",
            ctx=watchdog_region,
        )
    else:
        fail("watch-tasks-stream.sh: _PARENT_PID assignment not found — cannot verify watchdog structure")

    # 10. sleep in watchdog loop (not a busy-wait)
    expect(
        "sleep" in src,
        "watch-tasks-stream.sh: PPID watchdog loop must sleep between polls (not busy-wait)",
    )

    # ── Existing invariants still hold ──────────────────────────────────────

    # 11. PID file still written
    expect(
        'echo "$$" > "$PID_FILE"' in src,
        "watch-tasks-stream.sh: PID_FILE write must still be present",
    )

    # 12. EXIT trap still cleans up PID file
    expect(
        "trap" in src and "PID_FILE" in src and "EXIT" in src,
        "watch-tasks-stream.sh: EXIT trap for PID_FILE cleanup must still be present",
    )

    # 13. SUTANDO_WORKSPACE resolution still present (workspace contract)
    expect(
        "SUTANDO_WORKSPACE" in src,
        "watch-tasks-stream.sh: must still resolve STATE_DIR via SUTANDO_WORKSPACE",
    )

    # 14. TASKS_DIR fallback to ~/.sutando/workspace/tasks still present
    expect(
        ".sutando/workspace/tasks" in src,
        "watch-tasks-stream.sh: must still fall back to ~/.sutando/workspace/tasks",
    )

    # 15. Parent-dir filter still present (prevents archive re-fires)
    expect(
        "TASKS_DIR_ABS" in src,
        "watch-tasks-stream.sh: parent-dir filter (TASKS_DIR_ABS) must still be present",
    )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"All 15 tests passed.")


if __name__ == "__main__":
    main()
