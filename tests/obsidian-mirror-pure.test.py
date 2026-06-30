#!/usr/bin/env python3
"""Tests for pure functions in src/obsidian-mirror.py.

Covers the two utility functions that require no filesystem access:
  - _task_id_from_path(): regex extraction of task ID from filename
  - _parse_since(): human-readable duration string → seconds

Run: python3 tests/obsidian-mirror-pure.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# obsidian-mirror exits early if SUTANDO_OBSIDIAN_MIRROR is not set.
# Set it before module import so exec_module() reaches the function defs.
_saved_env = os.environ.get("SUTANDO_OBSIDIAN_MIRROR")
os.environ["SUTANDO_OBSIDIAN_MIRROR"] = "1"

spec = importlib.util.spec_from_file_location("obsidian_mirror", REPO / "src" / "obsidian-mirror.py")
om = importlib.util.module_from_spec(spec)
spec.loader.exec_module(om)

# Restore env so we don't bleed SUTANDO_OBSIDIAN_MIRROR into other code.
if _saved_env is not None:
    os.environ["SUTANDO_OBSIDIAN_MIRROR"] = _saved_env
else:
    os.environ.pop("SUTANDO_OBSIDIAN_MIRROR", None)


def check(label: str, cond: bool, fails: list) -> None:
    if not cond:
        fails.append(label)


# ---------------------------------------------------------------------------
# _task_id_from_path
# ---------------------------------------------------------------------------

def test_task_id_standard() -> list[str]:
    """Standard 'task-<id>.txt' filename extracts to 'task-<id>'."""
    fails: list[str] = []
    result = om._task_id_from_path(Path("task-1717000000.txt"))
    check(
        f"expected 'task-1717000000', got {result!r}",
        result == "task-1717000000",
        fails,
    )
    return fails


def test_task_id_alphanumeric() -> list[str]:
    """Alphanumeric task IDs are preserved."""
    fails: list[str] = []
    result = om._task_id_from_path(Path("task-abc123xyz.txt"))
    check(
        f"expected 'task-abc123xyz', got {result!r}",
        result == "task-abc123xyz",
        fails,
    )
    return fails


def test_task_id_with_dashes() -> list[str]:
    """Task IDs containing extra dashes are preserved (chat-<ts> style)."""
    fails: list[str] = []
    result = om._task_id_from_path(Path("task-chat-1717000000.txt"))
    check(
        f"expected 'task-chat-1717000000', got {result!r}",
        result == "task-chat-1717000000",
        fails,
    )
    return fails


def test_task_id_no_match_result_file() -> list[str]:
    """Result files ('results/task-<id>.txt' — different dir stem) and
    non-task names return None."""
    fails: list[str] = []
    non_task_names = [
        "proactive-1717000000.txt",
        "result-1717.txt",
        "task.txt",                # no ID part
        "task-.txt",               # empty ID after dash
        "image.png",
    ]
    for name in non_task_names:
        result = om._task_id_from_path(Path(name))
        check(f"'{name}' should return None, got {result!r}", result is None, fails)
    return fails


def test_task_id_uses_filename_not_parent() -> list[str]:
    """Only the filename is matched — parent path is irrelevant."""
    fails: list[str] = []
    p = Path("/workspace/tasks/task-99.txt")
    result = om._task_id_from_path(p)
    check(f"expected 'task-99', got {result!r}", result == "task-99", fails)
    return fails


# ---------------------------------------------------------------------------
# _parse_since
# ---------------------------------------------------------------------------

def test_parse_since_minutes() -> list[str]:
    """'30m' → 1800 seconds."""
    fails: list[str] = []
    result = om._parse_since("30m")
    check(f"30m: expected 1800, got {result}", result == 1800, fails)
    return fails


def test_parse_since_hours() -> list[str]:
    """'1h' → 3600 seconds."""
    fails: list[str] = []
    result = om._parse_since("1h")
    check(f"1h: expected 3600, got {result}", result == 3600, fails)
    return fails


def test_parse_since_days() -> list[str]:
    """'1d' → 86400 seconds."""
    fails: list[str] = []
    result = om._parse_since("1d")
    check(f"1d: expected 86400, got {result}", result == 86400, fails)
    return fails


def test_parse_since_seconds_suffix() -> list[str]:
    """'120s' → 120 seconds."""
    fails: list[str] = []
    result = om._parse_since("120s")
    check(f"120s: expected 120, got {result}", result == 120, fails)
    return fails


def test_parse_since_plain_int() -> list[str]:
    """Plain integer string → seconds directly."""
    fails: list[str] = []
    result = om._parse_since("300")
    check(f"'300': expected 300, got {result}", result == 300, fails)
    return fails


def test_parse_since_empty_string() -> list[str]:
    """Empty string → 0 (no window, full sweep)."""
    fails: list[str] = []
    result = om._parse_since("")
    check(f"empty string: expected 0, got {result}", result == 0, fails)
    return fails


def test_parse_since_case_insensitive() -> list[str]:
    """Uppercase suffix letters are accepted ('6H', '2D', '45M')."""
    fails: list[str] = []
    check("6H != 21600", om._parse_since("6H") == 21600, fails)
    check("2D != 172800", om._parse_since("2D") == 172800, fails)
    check("45M != 2700", om._parse_since("45M") == 2700, fails)
    return fails


def test_parse_since_multi_digit_hours() -> list[str]:
    """Multi-digit numbers are parsed correctly ('24h' → 86400)."""
    fails: list[str] = []
    result = om._parse_since("24h")
    check(f"24h: expected 86400, got {result}", result == 86400, fails)
    return fails


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    cases = [
        ("task_id: standard timestamp filename", test_task_id_standard),
        ("task_id: alphanumeric ID", test_task_id_alphanumeric),
        ("task_id: ID with extra dashes (chat-<ts>)", test_task_id_with_dashes),
        ("task_id: non-task filenames → None", test_task_id_no_match_result_file),
        ("task_id: parent path is ignored", test_task_id_uses_filename_not_parent),
        ("parse_since: 30m → 1800", test_parse_since_minutes),
        ("parse_since: 1h → 3600", test_parse_since_hours),
        ("parse_since: 1d → 86400", test_parse_since_days),
        ("parse_since: 120s → 120", test_parse_since_seconds_suffix),
        ("parse_since: plain int '300' → 300", test_parse_since_plain_int),
        ("parse_since: '' → 0", test_parse_since_empty_string),
        ("parse_since: uppercase suffix (6H, 2D, 45M)", test_parse_since_case_insensitive),
        ("parse_since: multi-digit '24h' → 86400", test_parse_since_multi_digit_hours),
    ]
    all_failures: list[str] = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as exc:
            fails = [f"raised {type(exc).__name__}: {exc}"]
        if fails:
            print(f"  ✗ {label}")
            for f in fails:
                print(f"      {f}")
            all_failures.extend(fails)
        else:
            print(f"  ✓ {label}")

    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    total = len(cases)
    print(f"\nobsidian-mirror pure functions: {total}/{total} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
