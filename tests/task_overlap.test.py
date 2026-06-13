#!/usr/bin/env python3
"""Tests for src/task_overlap.py — Python port of src/task-overlap.ts.

Mirrors the TS test suite (tests/task-overlap.test.ts) so the two
implementations stay in sync. Any divergence between the TS and Python
classifier outputs should show up as a failing test here.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from task_overlap import (  # noqa: E402
    are_safe_to_overlap,
    classify_task_category,
    extract_task_body,
    has_serial_dependency,
)


# ---------------------------------------------------------------------------
# extract_task_body
# ---------------------------------------------------------------------------

def test_extract_task_body_returns_from_task_line():
    raw = "id: task-123\nsource: voice\ntask: find the best sushi near me\nmore text"
    body = extract_task_body(raw)
    assert body.startswith("task: find")
    assert "more text" in body
    assert "id: task-123" not in body


def test_extract_task_body_returns_full_when_no_task_line():
    raw = "just a plain string with no delimiter"
    assert extract_task_body(raw) == raw


def test_extract_task_body_stops_at_first_occurrence():
    raw = "id: task-1\ntask: first\ntask: second"
    body = extract_task_body(raw)
    assert body.startswith("task: first")


# ---------------------------------------------------------------------------
# classify_task_category
# ---------------------------------------------------------------------------

CATEGORY_CASES = [
    # cancel
    ("task: CANCEL_INSTRUCTION: stop processing task-123", "cancel"),
    # email
    ("task: check my gmail for new messages", "email"),
    ("task: send email to John about the meeting", "email"),
    ("task: draft email to the team", "email"),
    ("task: reply to the unread email from Sarah", "email"),
    # calendar
    ("task: schedule a meeting with the team tomorrow", "calendar"),
    ("task: add a reminder to call Bob at 3pm", "calendar"),
    ("task: what appointments do I have today", "calendar"),
    # code
    ("task: fix the bug in health-check.py", "code"),
    ("task: create a PR to refactor the auth module", "code"),
    ("task: run tests for the new feature", "code"),
    ("task: deploy the latest build", "code"),
    ("task: run tests and commit the changes", "code"),
    # file
    ("task: open file /tmp/notes.txt", "file"),
    ("task: save the file and close", "file"),
    ("task: create a folder called projects", "file"),
    # research
    ("task: search for the best restaurants near me", "research"),
    ("task: look up the weather for tomorrow", "research"),
    ("task: what is the capital of France", "research"),
    ("task: explain how diffusion models work", "research"),
    ("task: summarize this article for me", "research"),
    ("task: check the news about WWDC", "research"),
    # unknown
    ("task: do that thing", "unknown"),
    ("task: ok", "unknown"),
]


def test_classify_task_category_all_cases():
    for body, expected in CATEGORY_CASES:
        result = classify_task_category(body)
        assert result == expected, f"classify_task_category({body!r}) = {result!r}, want {expected!r}"


def test_classify_ignores_header_fields():
    """source: code in headers must not trigger code classification."""
    raw = "id: task-99\nsource: discord\naccess_tier: owner\ntask: check my gmail"
    body = extract_task_body(raw)
    assert classify_task_category(body) == "email"


# ---------------------------------------------------------------------------
# has_serial_dependency
# ---------------------------------------------------------------------------

SERIAL_CASES = [
    "task: after you finish the research, send the email",
    "task: once it's done, open a PR",
    "task: depends on the previous task completing",
    "task: wait for the build to finish then notify me",
    "task: when you're done with the email, do this",
    "task: when you are finished with the search, add a reminder",
]

INDEPENDENT_CASES = [
    "task: search for news about AI",
    "task: check my gmail",
    "task: what time is it",
    "task: fix the login bug",
]


def test_has_serial_dependency_detects_all():
    for body in SERIAL_CASES:
        assert has_serial_dependency(body), f"expected serial dep in: {body!r}"


def test_has_serial_dependency_no_false_positives():
    for body in INDEPENDENT_CASES:
        assert not has_serial_dependency(body), f"unexpected serial dep in: {body!r}"


# ---------------------------------------------------------------------------
# are_safe_to_overlap — full task-file strings
# ---------------------------------------------------------------------------

def _make_task(task_id: str, body: str, source: str = "voice") -> str:
    return "\n".join([
        f"id: task-{task_id}",
        "timestamp: 2026-06-11T00:00:00.000Z",
        f"source: {source}",
        "channel_id: local-voice",
        "user_id: voice-local",
        "access_tier: owner",
        "priority: normal",
        f"task: {body}",
    ])


def test_research_email_safe():
    t1 = _make_task("1", "search for sushi restaurants near me")
    t2 = _make_task("2", "check my gmail inbox")
    assert are_safe_to_overlap(t1, t2)


def test_research_calendar_safe():
    t1 = _make_task("1", "look up the weather for tomorrow")
    t2 = _make_task("2", "schedule a meeting at 3pm")
    assert are_safe_to_overlap(t1, t2)


def test_research_research_not_safe():
    t1 = _make_task("1", "search for best coffee shops")
    t2 = _make_task("2", "look up the weather in NYC")
    assert not are_safe_to_overlap(t1, t2)


def test_email_email_not_safe():
    t1 = _make_task("1", "draft email to the team")
    t2 = _make_task("2", "reply to unread email from Sarah")
    assert not are_safe_to_overlap(t1, t2)


def test_code_research_not_safe():
    t1 = _make_task("1", "fix the bug in health-check.py")
    t2 = _make_task("2", "search for the latest Node.js docs")
    assert not are_safe_to_overlap(t1, t2)


def test_code_email_not_safe():
    t1 = _make_task("1", "run tests and commit the changes")
    t2 = _make_task("2", "send email to John")
    assert not are_safe_to_overlap(t1, t2)


def test_code_code_not_safe():
    t1 = _make_task("1", "fix the bug in task-bridge.ts")
    t2 = _make_task("2", "create a PR for the new feature")
    assert not are_safe_to_overlap(t1, t2)


def test_cancel_anything_not_safe():
    t1 = _make_task("1", "CANCEL_INSTRUCTION: stop processing task-5")
    t2 = _make_task("2", "search for news about WWDC")
    assert not are_safe_to_overlap(t1, t2)
    assert not are_safe_to_overlap(t2, t1)


def test_serial_dependency_not_safe():
    t1 = _make_task("1", "search for the best sushi near me")
    t2 = _make_task("2", "after you finish the search, schedule a dinner reservation")
    assert not are_safe_to_overlap(t1, t2)


def test_unknown_research_safe():
    """unknown ≠ research → different categories → safe (matches TS behavior)."""
    t1 = _make_task("1", "do the thing")
    t2 = _make_task("2", "look up the weather")
    assert are_safe_to_overlap(t1, t2)


def test_unknown_unknown_not_safe():
    t1 = _make_task("1", "do that thing")
    t2 = _make_task("2", "ok")
    assert not are_safe_to_overlap(t1, t2)


def test_header_fields_do_not_skew_classification():
    """A task with source: discord in headers should not be classified as code."""
    t1 = "\n".join([
        "id: task-99",
        "source: discord",
        "access_tier: owner",
        "task: check my gmail",
    ])
    t2 = _make_task("2", "schedule a team meeting")
    assert are_safe_to_overlap(t1, t2)  # email + calendar = safe


def test_symmetric_safe_to_overlap():
    """areSafeToOverlap is symmetric for non-cancel tasks."""
    t1 = _make_task("1", "search for the latest Python docs")
    t2 = _make_task("2", "schedule a meeting tomorrow at 3pm")
    assert are_safe_to_overlap(t1, t2) == are_safe_to_overlap(t2, t1)


def main():
    test_extract_task_body_returns_from_task_line()
    test_extract_task_body_returns_full_when_no_task_line()
    test_extract_task_body_stops_at_first_occurrence()
    test_classify_task_category_all_cases()
    test_classify_ignores_header_fields()
    test_has_serial_dependency_detects_all()
    test_has_serial_dependency_no_false_positives()
    test_research_email_safe()
    test_research_calendar_safe()
    test_research_research_not_safe()
    test_email_email_not_safe()
    test_code_research_not_safe()
    test_code_email_not_safe()
    test_code_code_not_safe()
    test_cancel_anything_not_safe()
    test_serial_dependency_not_safe()
    test_unknown_research_safe()
    test_unknown_unknown_not_safe()
    test_header_fields_do_not_skew_classification()
    test_symmetric_safe_to_overlap()
    print("All task_overlap tests passed.")


if __name__ == "__main__":
    main()
