#!/usr/bin/env python3
"""Structural tests for install-claude-hooks auto-uninstall (#1085/#1083)
and watch-tasks-stream.sh PID file + cleanup (#1065).

## PR #1083 / #1085 — install-claude-hooks: auto-uninstall deprecated hooks

The watcher-kill Stop hook (`Stop|watch-tasks-stream.pid`) was dropped from
`HOOKS=()` in #1083 because the Turn-end firing killed the live Monitor
watcher every turn. But existing installs had the deprecated entry in their
`settings.json`. PR #1085 added a `DEPRECATED_HOOKS` array + Phase 2 removal
logic so `install-claude-hooks.sh` removes the old entry automatically on
re-run — no manual jq needed.

Key invariants:
  - `DEPRECATED_HOOKS` array defined
  - Deprecated entry: `Stop|watch-tasks-stream.pid`
  - Phase 2 loop: walks hooks, removes by substring match
  - Atomic write: jq → tmp → mv (no partial-read on crash)
  - `Stop|watch-tasks-stream.pid` must NOT be in `HOOKS=()` (removed from active set)
  - Summary line reports `removed=N`

## PR #1065 — watch-tasks-stream.sh: PID file + Stop-hook cleanup

Adds a PID file at `<workspace>/state/watch-tasks-stream.pid` so the Stop
hook can kill the exact fswatch PID on session end instead of a broad
`pkill fswatch` (which could kill other processes). The `trap ... EXIT`
ensures the PID file is cleaned up on graceful shutdown.

Key invariants:
  - PID_FILE written at `<state>/watch-tasks-stream.pid`
  - Resolves to SUTANDO_WORKSPACE/state when env var is set
  - Falls back to `~/.sutando/workspace/state`
  - `echo "$$"` (current shell PID) written to PID_FILE
  - `trap 'rm -f "$PID_FILE"' EXIT` for cleanup on clean exit
  - State dir lives under workspace (not under repo root)

Run: python3 tests/install-hooks-watcher-pid.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL_HOOKS = REPO / "src" / "install-claude-hooks.sh"
WATCH_STREAM = REPO / "src" / "watch-tasks-stream.sh"

_FAILURES: list[str] = []


def fail(msg: str, ctx: str = "") -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("--- context ---", file=sys.stderr)
        print(ctx[:500], file=sys.stderr)


def expect(cond: bool, label: str, ctx: str = "") -> None:
    if not cond:
        fail(label, ctx)


def main() -> None:
    missing = [f for f in [INSTALL_HOOKS, WATCH_STREAM] if not f.exists()]
    for m in missing:
        fail(f"file not found: {m}")
    if missing:
        sys.exit(1)

    hooks = INSTALL_HOOKS.read_text()
    watcher = WATCH_STREAM.read_text()

    # ── install-claude-hooks.sh: DEPRECATED_HOOKS array (#1085/#1083) ────────

    # 1. DEPRECATED_HOOKS array defined
    expect(
        "DEPRECATED_HOOKS=" in hooks,
        "install-claude-hooks.sh: must define DEPRECATED_HOOKS array for auto-uninstall",
    )

    # 2. Deprecated watcher-kill hook entry present
    expect(
        "watch-tasks-stream.pid" in hooks,
        "install-claude-hooks.sh: must list 'Stop|watch-tasks-stream.pid' in DEPRECATED_HOOKS",
    )

    # 3. Entry format is correct (Event|substring pattern)
    expect(
        "Stop|watch-tasks-stream.pid" in hooks,
        "install-claude-hooks.sh: deprecated entry must use 'Stop|watch-tasks-stream.pid' format",
    )

    # 4. DEPRECATED_HOOKS entry is NOT in active HOOKS=() array
    # The watcher-kill hook was removed from HOOKS; it must only appear in DEPRECATED_HOOKS
    hooks_array_start = hooks.find("HOOKS=(")
    hooks_array_end = hooks.find(")", hooks_array_start) if hooks_array_start != -1 else -1
    if hooks_array_start != -1 and hooks_array_end != -1:
        active_hooks_block = hooks[hooks_array_start:hooks_array_end + 1]
        expect(
            "watch-tasks-stream.pid" not in active_hooks_block,
            "install-claude-hooks.sh: deprecated 'watch-tasks-stream.pid' must NOT appear in active HOOKS=()",
            ctx=active_hooks_block[:400],
        )

    # 5. Phase 2 loop implemented (iterates DEPRECATED_HOOKS)
    expect(
        "DEPRECATED_HOOKS" in hooks and "for entry in" in hooks,
        "install-claude-hooks.sh: must have Phase 2 loop iterating DEPRECATED_HOOKS",
    )

    # 6. Removal uses .command | contains(substring) — no exact-string fragility
    expect(
        "contains($sub)" in hooks or "contains($SUBSTR)" in hooks,
        "install-claude-hooks.sh: deprecated hook removal must use contains() substring match (not exact string)",
    )

    # 7. Removal is atomic: jq → tmp file → mv
    expect(
        "mktemp" in hooks and "mv" in hooks,
        "install-claude-hooks.sh: deprecated hook removal must use atomic jq → tmp → mv pattern",
    )

    # 8. Removal skips when no match (idempotent on clean installs)
    expect(
        "continue" in hooks,
        "install-claude-hooks.sh: Phase 2 must skip (continue) when no deprecated hook match found",
    )

    # 9. Empty groups dropped after removal (structure stays tidy)
    expect(
        "length > 0" in hooks or "length > 0" in hooks,
        "install-claude-hooks.sh: must drop now-empty hook groups after deprecated entry removal",
    )

    # 10. Summary reports removed count
    expect(
        "removed=" in hooks and "REMOVED" in hooks,
        "install-claude-hooks.sh: summary line must report removed=N count",
    )

    # 11. ADDED and SKIPPED also tracked (Phase 1 preserved)
    expect(
        "ADDED=" in hooks and "SKIPPED=" in hooks,
        "install-claude-hooks.sh: must track ADDED and SKIPPED counts alongside REMOVED",
    )

    # ── watch-tasks-stream.sh: PID file + cleanup (#1065) ────────────────────

    # 12. PID_FILE defined pointing at watch-tasks-stream.pid
    expect(
        "PID_FILE=" in watcher and "watch-tasks-stream.pid" in watcher,
        "watch-tasks-stream.sh: must define PID_FILE pointing to watch-tasks-stream.pid",
    )

    # 13. PID file lives under state/ dir (workspace contract)
    expect(
        "state" in watcher and "PID_FILE" in watcher,
        "watch-tasks-stream.sh: PID file must live under workspace state/ dir",
    )

    # 14. PID file path resolves via SUTANDO_WORKSPACE when set
    expect(
        "SUTANDO_WORKSPACE" in watcher,
        "watch-tasks-stream.sh: PID_FILE path must respect SUTANDO_WORKSPACE env var",
    )

    # 15. Falls back to ~/.sutando/workspace/state when env var absent
    expect(
        ".sutando/workspace/state" in watcher or "sutando/workspace" in watcher,
        "watch-tasks-stream.sh: PID_FILE must fall back to ~/.sutando/workspace/state",
    )

    # 16. Current shell PID ($$) written to PID_FILE
    expect(
        '"$$"' in watcher or "echo $$" in watcher or 'echo "$$"' in watcher,
        "watch-tasks-stream.sh: must write $$ (current shell PID) to PID_FILE",
    )

    # 17. trap 'rm -f "$PID_FILE"' EXIT — cleanup on clean exit
    expect(
        "trap" in watcher and "rm -f" in watcher and "EXIT" in watcher,
        "watch-tasks-stream.sh: must use trap to rm PID_FILE on EXIT (clean exit path)",
    )

    # 18. state dir created via mkdir -p before writing PID file
    pid_pos = watcher.find("PID_FILE=")
    if pid_pos != -1:
        pid_region = watcher[max(0, pid_pos - 200):pid_pos + 200]
        expect(
            "mkdir -p" in pid_region or "mkdir" in pid_region,
            "watch-tasks-stream.sh: must mkdir -p the state dir before writing PID_FILE",
            ctx=pid_region[:400],
        )

    # 19. Watch-stream does NOT fall back to repo root for tasks dir
    expect(
        "parent.parent" not in watcher and "__file__" not in watcher,
        "watch-tasks-stream.sh: must NOT use repo-root fallback for TASKS_DIR",
    )

    # 20. TASKS_DIR resolves to workspace default when env var absent
    expect(
        ".sutando/workspace/tasks" in watcher,
        "watch-tasks-stream.sh: TASKS_DIR must default to ~/.sutando/workspace/tasks",
    )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        total = 20
        print(f"All {total} tests passed.")


if __name__ == "__main__":
    main()
