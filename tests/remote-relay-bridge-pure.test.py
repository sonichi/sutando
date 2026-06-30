#!/usr/bin/env python3
"""Tests for pure security-gate functions in src/remote-relay-bridge.py.

Covers the two functions that run entirely in-process with no I/O:
  - _valid_tid(): rejects unsafe task IDs to block path-traversal attacks
  - _one_line(): strips CR/LF to prevent header-injection from relay-supplied fields

These are the narrow trust boundary between the untrusted relay and the local
filesystem. A bypass of _valid_tid allows arbitrary file writes/reads;
a bypass of _one_line allows forging extra `key: value` lines in task files
(e.g. a second access_tier: owner line).

Run: python3 tests/remote-relay-bridge-pure.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

spec = importlib.util.spec_from_file_location(
    "remote_relay_bridge", REPO / "src" / "remote-relay-bridge.py"
)
rrb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rrb)


def check(label: str, cond: bool, fails: list) -> None:
    if not cond:
        fails.append(label)


# ---------------------------------------------------------------------------
# _valid_tid — path-traversal guard
# ---------------------------------------------------------------------------

def test_valid_tid_normal_task_id() -> list[str]:
    """Standard 'task-<unix_ts>' form is accepted."""
    fails: list[str] = []
    check("task-1717000000 rejected", rrb._valid_tid("task-1717000000"), fails)
    return fails


def test_valid_tid_alphanumeric_only() -> list[str]:
    """Pure alphanumeric IDs (no separators) are accepted."""
    fails: list[str] = []
    check("abc123 rejected", rrb._valid_tid("abc123"), fails)
    check("UPPERCASE rejected", rrb._valid_tid("TASK123"), fails)
    return fails


def test_valid_tid_allowed_chars() -> list[str]:
    """Dots, underscores, and hyphens are allowed within the charset."""
    fails: list[str] = []
    check("dots rejected", rrb._valid_tid("a.b.c"), fails)
    check("underscores rejected", rrb._valid_tid("task_123"), fails)
    check("hyphens rejected", rrb._valid_tid("task-chat-123"), fails)
    return fails


def test_valid_tid_rejects_dot() -> list[str]:
    """Bare '.' is explicitly rejected (current dir reference)."""
    fails: list[str] = []
    check("'.' accepted", not rrb._valid_tid("."), fails)
    return fails


def test_valid_tid_rejects_dotdot() -> list[str]:
    """'..' is explicitly rejected (parent dir traversal)."""
    fails: list[str] = []
    check("'..' accepted", not rrb._valid_tid(".."), fails)
    return fails


def test_valid_tid_rejects_path_traversal() -> list[str]:
    """Path traversal strings containing '/' are rejected."""
    fails: list[str] = []
    check("'../secret' accepted", not rrb._valid_tid("../secret"), fails)
    check("'task/../../etc/passwd' accepted", not rrb._valid_tid("task/../../etc/passwd"), fails)
    check("'/absolute/path' accepted", not rrb._valid_tid("/absolute/path"), fails)
    return fails


def test_valid_tid_rejects_empty_string() -> list[str]:
    """Empty string is rejected (regex requires 1+ chars)."""
    fails: list[str] = []
    check("'' accepted", not rrb._valid_tid(""), fails)
    return fails


def test_valid_tid_rejects_too_long() -> list[str]:
    """IDs longer than 64 chars are rejected."""
    fails: list[str] = []
    long_id = "a" * 65
    check(f"65-char ID accepted", not rrb._valid_tid(long_id), fails)
    edge = "a" * 64
    check(f"64-char ID rejected", rrb._valid_tid(edge), fails)
    return fails


def test_valid_tid_rejects_spaces() -> list[str]:
    """Spaces are not in the allowed charset and must be rejected."""
    fails: list[str] = []
    check("'task 123' accepted", not rrb._valid_tid("task 123"), fails)
    return fails


def test_valid_tid_rejects_null_byte() -> list[str]:
    """Null bytes are rejected (filesystem truncation attack vector)."""
    fails: list[str] = []
    check("'task\\x00etc' accepted", not rrb._valid_tid("task\x00etc"), fails)
    return fails


def test_valid_tid_rejects_unicode() -> list[str]:
    """Non-ASCII unicode characters are rejected."""
    fails: list[str] = []
    check("'tâsk-123' accepted", not rrb._valid_tid("tâsk-123"), fails)
    check("'тask-1' accepted", not rrb._valid_tid("тask-1"), fails)
    return fails


# ---------------------------------------------------------------------------
# _one_line — header-injection prevention
# ---------------------------------------------------------------------------

def test_one_line_passthrough_clean_string() -> list[str]:
    """Clean single-line strings pass through unchanged."""
    fails: list[str] = []
    result = rrb._one_line("hello world")
    check(f"clean string mangled: {result!r}", result == "hello world", fails)
    return fails


def test_one_line_strips_newline() -> list[str]:
    """\\n is replaced with a space."""
    fails: list[str] = []
    result = rrb._one_line("line1\nline2")
    check(f"newline not stripped: {result!r}", result == "line1 line2", fails)
    check("newline char still present", "\n" not in result, fails)
    return fails


def test_one_line_strips_carriage_return() -> list[str]:
    """\\r is replaced with a space."""
    fails: list[str] = []
    result = rrb._one_line("line1\rline2")
    check(f"CR not stripped: {result!r}", result == "line1 line2", fails)
    check("CR char still present", "\r" not in result, fails)
    return fails


def test_one_line_strips_crlf() -> list[str]:
    """\\r\\n (Windows line ending) becomes two spaces (both chars replaced)."""
    fails: list[str] = []
    result = rrb._one_line("header-val\r\nforged: injected")
    check("CR present after strip", "\r" not in result, fails)
    check("LF present after strip", "\n" not in result, fails)
    check("forged header text preserved", "forged: injected" in result, fails)
    return fails


def test_one_line_coerces_int() -> list[str]:
    """Non-string values are coerced via str() before stripping."""
    fails: list[str] = []
    result = rrb._one_line(42)
    check(f"int 42 not coerced to '42': {result!r}", result == "42", fails)
    return fails


def test_one_line_coerces_none() -> list[str]:
    """None is coerced to 'None' (str(None))."""
    fails: list[str] = []
    result = rrb._one_line(None)
    check(f"None not coerced to 'None': {result!r}", result == "None", fails)
    return fails


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    cases = [
        ("_valid_tid: standard task-<ts> form accepted", test_valid_tid_normal_task_id),
        ("_valid_tid: pure alphanumeric accepted", test_valid_tid_alphanumeric_only),
        ("_valid_tid: dots/underscores/hyphens allowed", test_valid_tid_allowed_chars),
        ("_valid_tid: '.' rejected (current-dir)", test_valid_tid_rejects_dot),
        ("_valid_tid: '..' rejected (parent-dir traversal)", test_valid_tid_rejects_dotdot),
        ("_valid_tid: path traversal with '/' rejected", test_valid_tid_rejects_path_traversal),
        ("_valid_tid: empty string rejected", test_valid_tid_rejects_empty_string),
        ("_valid_tid: > 64 chars rejected; 64 chars accepted", test_valid_tid_rejects_too_long),
        ("_valid_tid: spaces rejected", test_valid_tid_rejects_spaces),
        ("_valid_tid: null byte rejected", test_valid_tid_rejects_null_byte),
        ("_valid_tid: unicode chars rejected", test_valid_tid_rejects_unicode),
        ("_one_line: clean string unchanged", test_one_line_passthrough_clean_string),
        ("_one_line: \\n → space", test_one_line_strips_newline),
        ("_one_line: \\r → space", test_one_line_strips_carriage_return),
        ("_one_line: \\r\\n both stripped", test_one_line_strips_crlf),
        ("_one_line: int coerced to str", test_one_line_coerces_int),
        ("_one_line: None coerced to 'None'", test_one_line_coerces_none),
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
    print(f"\nremote-relay-bridge pure functions: {total}/{total} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
