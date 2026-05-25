#!/usr/bin/env python3
"""Regression guard: health-check multiple-bridge-process warn shows source paths.

Before this fix, the warn read:
  "multiple processes (2 PIDs: 25023,25065)"

That gave no hint whether the two instances were an expected app-bundle +
dev-repo coexistence or an accidental duplicate spawn of the same script.
After the fix the warn reads:
  "multiple processes (2 PIDs: 25023:/Applications/Sutando.app/.../discord-bridge.py,
                                 25065:/Users/.../sutando/src/discord-bridge.py)"

These tests verify the new path-enriched format via source-grep (no live
process required).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "src" / "health-check.py").read_text(encoding="utf-8")


def test_pid_script_helper_defined():
    """health-check.py defines a _pid_script helper for path extraction."""
    assert "_pid_script" in SRC, (
        "_pid_script helper not found in health-check.py — "
        "multiple-bridge warn will not show source paths"
    )


def test_pid_script_uses_ps_command():
    """_pid_script extracts the script path from `ps -p <pid> -o command=`."""
    assert '"-o", "command="' in SRC or "command=" in SRC, (
        "health-check.py doesn't call `ps -o command=` to extract bridge script path"
    )


def test_pid_paths_in_warn_detail():
    """The multi-pid warn uses pid_paths (enriched with source paths)."""
    assert "pid_paths" in SRC, (
        "pid_paths variable not found — multi-pid warn won't include source paths"
    )


def test_warn_format_uses_pid_paths():
    """The actual warn detail string uses pid_paths, not the bare pids join."""
    # Old pattern: f"...PIDs: {','.join(pids)})"
    # New pattern: f"...PIDs: {pid_paths})"
    assert "pid_paths}" in SRC, (
        "multi-pid warn detail does not reference pid_paths — "
        "source paths won't appear in health-check output"
    )


def test_py_args_extraction():
    """_pid_script filters for .py args to find the script path."""
    assert ".endswith(\".py\")" in SRC or '.endswith(".py")' in SRC, (
        "_pid_script does not filter for .py args — may return wrong path component"
    )


def main():
    tests = [
        test_pid_script_helper_defined,
        test_pid_script_uses_ps_command,
        test_pid_paths_in_warn_detail,
        test_warn_format_uses_pid_paths,
        test_py_args_extraction,
    ]
    failures = []
    for fn in tests:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}: {e}")
    print()
    if failures:
        print(f"{len(failures)} test(s) failed ({len(tests) - len(failures)} passed).")
        sys.exit(1)
    print(f"All {len(tests)} bridge-multi-pid tests passed.")


if __name__ == "__main__":
    main()
