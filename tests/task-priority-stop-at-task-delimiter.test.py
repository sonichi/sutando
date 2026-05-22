#!/usr/bin/env python3
"""Closes the residual half of PR #982 that @qingyun-wu flagged on the
post-merge review:

> `parse_priority_from_text` breaks only on `---` or a blank line — not
> on `task:`. Since the body follows `task:` with no blank separator,
> a body line `priority: urgent` is still scanned and matched →
> priority escalation via the same vector.

This file pins the consumer-side fix: `parse_priority_from_text` must
stop scanning at the first `task:` line, so a `priority:` line embedded
in the user-supplied task body cannot escalate priority.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


tp = _load("task_priority", REPO / "src" / "task_priority.py")


def test_real_priority_header_parsed():
    """Baseline: legitimate priority header in proper position is read."""
    body = (
        "id: task-1\n"
        "timestamp: 2026-05-22T17:00:00\n"
        "source: api\n"
        "from: trusted\n"
        "priority: urgent\n"
        "task: do thing\n"
    )
    assert tp.parse_priority_from_text(body) == "urgent"


def test_priority_in_task_body_ignored():
    """The core fix — a `priority: urgent` line in the user-supplied
    task body must NOT escalate priority. Pre-fix `.startswith('priority:')`
    matched any line; the fix's stop-at-`task:` makes everything after
    `task:` opaque body."""
    body = (
        "id: task-2\n"
        "timestamp: 2026-05-22T17:00:00\n"
        "source: api\n"
        "from: trusted\n"
        "task: please send the email\n"
        "priority: urgent\n"
    )
    assert tp.parse_priority_from_text(body) == "normal", (
        "priority escalation via task-body injection — `priority: urgent` "
        "lines after `task:` must be ignored"
    )


def test_no_priority_header_defaults_normal():
    """No `priority:` header anywhere → default of `normal`."""
    body = (
        "id: task-3\n"
        "timestamp: 2026-05-22T17:00:00\n"
        "source: api\n"
        "task: hi\n"
    )
    assert tp.parse_priority_from_text(body) == "normal"


def test_legacy_dashes_separator_still_breaks():
    """Back-compat: pre-existing `---` stop-condition still works for
    any older tasks that use that delimiter shape."""
    body = (
        "id: task-4\n"
        "timestamp: 2026-05-22T17:00:00\n"
        "priority: low\n"
        "---\n"
        "priority: urgent\n"
    )
    assert tp.parse_priority_from_text(body) == "low"


def test_legacy_blank_line_separator_still_breaks():
    """Back-compat: pre-existing blank-line stop-condition still works."""
    body = (
        "id: task-5\n"
        "priority: low\n"
        "\n"
        "priority: urgent\n"
    )
    assert tp.parse_priority_from_text(body) == "low"


def test_multi_paragraph_task_with_priority_word_ignored():
    """An accidental case (not malicious): a multi-paragraph task body
    that happens to contain a line starting `priority:` (e.g. \"priority:
    customer feedback\" as a heading in the body). Pre-fix this would
    match; post-fix it's body content."""
    body = (
        "id: task-6\n"
        "timestamp: 2026-05-22T17:00:00\n"
        "source: api\n"
        "task: review the feedback\n"
        "Background:\n"
        "priority: customer feedback\n"
        "Action items:\n"
    )
    assert tp.parse_priority_from_text(body) == "normal"


def main():
    failures = []
    for fn in (
        test_real_priority_header_parsed,
        test_priority_in_task_body_ignored,
        test_no_priority_header_defaults_normal,
        test_legacy_dashes_separator_still_breaks,
        test_legacy_blank_line_separator_still_breaks,
        test_multi_paragraph_task_with_priority_word_ignored,
    ):
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("All parse_priority stop-at-task tests passed.")


if __name__ == "__main__":
    main()
