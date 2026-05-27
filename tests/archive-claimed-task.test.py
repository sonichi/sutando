#!/usr/bin/env python3
"""Structural tests for issue #933 — archive_file handles .claimed-core-N.txt variants.

Verifies that all three bridges (slack, discord, telegram) have the glob fallback
so .claimed-core-N.txt files are archived instead of stranded in tasks/.

Run: python3 tests/archive-claimed-task.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SLACK_BRIDGE = REPO / "src" / "slack-bridge.py"
DISCORD_BRIDGE = REPO / "src" / "discord-bridge.py"
TELEGRAM_BRIDGE = REPO / "src" / "telegram-bridge.py"

_FAILURES: list[str] = []


def fail(msg: str, ctx: str = "") -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("--- context ---", file=sys.stderr)
        print(ctx[:400], file=sys.stderr)


def expect(cond: bool, label: str, ctx: str = "") -> None:
    if not cond:
        fail(label, ctx)


def extract_archive_file_body(src: str) -> str:
    """Extract the body of the archive_file function."""
    import re
    m = re.search(r"def archive_file\(.*?\n(?=def |\Z)", src, re.DOTALL)
    return m.group(0) if m else ""


def check_bridge(path: Path, name: str) -> None:
    if not path.exists():
        fail(f"{name}: file not found: {path}")
        return

    src = path.read_text()
    fn_body = extract_archive_file_body(src)

    expect(
        fn_body != "",
        f"{name}: archive_file function must exist",
    )

    # 1. Glob fallback present — catches .claimed-core-N.txt
    expect(
        "glob(" in fn_body and "claimed-core" not in fn_body.lower() or ".glob(" in fn_body,
        f"{name}: archive_file must use glob() to find claimed variants",
        ctx=fn_body[:300],
    )

    # 2. task_id used as glob prefix (pattern: f"{task_id}*.txt")
    expect(
        '{task_id}*.txt' in fn_body or "{task_id}*.txt" in fn_body,
        f"{name}: archive_file glob must use {{task_id}}*.txt pattern",
        ctx=fn_body[:300],
    )

    # 3. Fallback only fires for kind == "tasks"
    expect(
        "tasks" in fn_body and ("kind" in fn_body),
        f"{name}: archive_file claimed-variant path must be guarded by kind check",
        ctx=fn_body[:300],
    )

    # 4. claim_task comment references issue #933
    expect(
        "#933" in fn_body or "claim_task" in fn_body,
        f"{name}: archive_file claimed-variant path must reference claim_task or issue #933",
        ctx=fn_body[:300],
    )

    # 5. The glob fallback is only reachable when kind == "tasks" (no regression for results)
    # Accepted patterns: "kind != 'tasks'" early-return OR "kind == 'tasks'" guard on glob block
    expect(
        'kind != "tasks"' in fn_body or "kind != 'tasks'" in fn_body
        or 'kind == "tasks"' in fn_body or "kind == 'tasks'" in fn_body,
        f"{name}: archive_file glob fallback must be guarded by a kind == 'tasks' / kind != 'tasks' check",
        ctx=fn_body[:300],
    )


def main() -> None:
    check_bridge(SLACK_BRIDGE, "slack-bridge.py")
    check_bridge(DISCORD_BRIDGE, "discord-bridge.py")
    check_bridge(TELEGRAM_BRIDGE, "telegram-bridge.py")

    # Cross-bridge: verify all three are consistent — all have the fix
    bridges = [SLACK_BRIDGE, DISCORD_BRIDGE, TELEGRAM_BRIDGE]
    fixed_count = sum(
        1 for b in bridges
        if b.exists() and '{task_id}*.txt' in b.read_text()
    )
    expect(
        fixed_count == 3,
        f"All 3 bridges must have the claimed-variant glob fix; only {fixed_count}/3 do",
    )

    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        count = 5 * 3 + 1  # 5 per bridge + 1 cross-bridge
        print(f"All {count} structural tests passed.")


if __name__ == "__main__":
    main()
