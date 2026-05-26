#!/usr/bin/env python3
"""
Tests for src/friction-detector.py

Covers the three pure file-based checkers:
  check_pending_questions() — section parser
  check_stale_tasks()       — task file mtime gate
  check_notes_without_follow_up() — TODO marker + mtime gate

Subprocess-backed checkers (check_github_issues, check_overdue_reminders)
are excluded: they require live network / macOS tool access. The
check_stale_results stub always returns [] and needs no test.

Run: python3 tests/friction-detector.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "friction_detector", REPO / "src" / "friction-detector.py"
)
fd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fd)

PASS = 0
FAIL = 0


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS  {label}")


def fail(label: str, msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL  {label}: {msg}")


# ── check_pending_questions ──────────────────────────────────────────────────

def test_pq_empty_file():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / "pending-questions.md").write_text("")
        with patch.object(fd, "WORKSPACE", ws), \
             patch("builtins.open", side_effect=lambda *a, **k: open(*a, **k)):
            result = _run_pq(ws, "")
        if result == []:
            ok("(pq-a) empty file → []")
        else:
            fail("(pq-a) empty file → []", f"got {result}")


def test_pq_no_pending_marker():
    content = "(No pending questions)"
    result = _run_pq_content(content)
    if result == []:
        ok("(pq-b) '(No pending questions)' → []")
    else:
        fail("(pq-b) '(No pending questions)' → []", f"got {result}")


def test_pq_unanswered_section_found():
    content = """## Big decision about X

- **Asked:** 2026-01-01
- **Status:** unanswered
"""
    result = _run_pq_content(content)
    if len(result) == 1 and "Big decision about X" in result[0]:
        ok("(pq-c) unanswered section → found")
    else:
        fail("(pq-c) unanswered section → found", f"got {result}")


def test_pq_resolved_section_skipped():
    content = """## Old resolved thing

- **Asked:** 2026-01-01
- **Status:** resolved
"""
    result = _run_pq_content(content)
    if result == []:
        ok("(pq-d) resolved section → []")
    else:
        fail("(pq-d) resolved section → []", f"got {result}")


def test_pq_mixed_sections():
    content = """## Resolved stuff

- **Asked:** 2026-01-01
- **Status:** resolved

## Open question

- **Asked:** 2026-01-02
- **Status:** unanswered

## Another resolved

- **Asked:** 2026-01-03
- **Status:** resolved
"""
    result = _run_pq_content(content)
    if len(result) == 1 and "Open question" in result[0]:
        ok("(pq-e) mixed sections → only unanswered")
    else:
        fail("(pq-e) mixed sections → only unanswered", f"got {result}")


def test_pq_no_status_field():
    content = """## Section with no status field

Some text but no Status line.
"""
    result = _run_pq_content(content)
    if result == []:
        ok("(pq-f) section with no Status field → []")
    else:
        fail("(pq-f) section with no Status field → []", f"got {result}")


def test_pq_missing_file():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        # pending-questions.md does not exist in ws
        with patch.object(fd, "WORKSPACE", ws):
            result = fd.check_pending_questions()
        if result == []:
            ok("(pq-g) missing file → []")
        else:
            fail("(pq-g) missing file → []", f"got {result}")


# ── check_stale_tasks ────────────────────────────────────────────────────────

def test_stale_tasks_no_dir():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        # tasks/ subdir does not exist
        with patch.object(fd, "WORKSPACE", ws):
            result = fd.check_stale_tasks()
        if result == []:
            ok("(st-a) no tasks dir → []")
        else:
            fail("(st-a) no tasks dir → []", f"got {result}")


def test_stale_tasks_fresh_file():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        tasks = ws / "tasks"
        tasks.mkdir()
        tf = tasks / "task-123.txt"
        tf.write_text("some task")
        # mtime = now (within threshold)
        with patch.object(fd, "WORKSPACE", ws):
            result = fd.check_stale_tasks()
        if result == []:
            ok("(st-b) fresh task file → []")
        else:
            fail("(st-b) fresh task file → []", f"got {result}")


def test_stale_tasks_old_file():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        tasks = ws / "tasks"
        tasks.mkdir()
        tf = tasks / "task-456.txt"
        tf.write_text("old task")
        # Set mtime to 2h ago
        old_time = time.time() - 7200
        os.utime(tf, (old_time, old_time))
        with patch.object(fd, "WORKSPACE", ws):
            result = fd.check_stale_tasks()
        if len(result) == 1 and "task-456.txt" in result[0]:
            ok("(st-c) old task file (2h) → found")
        else:
            fail("(st-c) old task file (2h) → found", f"got {result}")


def test_stale_tasks_non_task_files_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        tasks = ws / "tasks"
        tasks.mkdir()
        # Non-matching filename — should be ignored even if old
        nf = tasks / "result-123.txt"
        nf.write_text("not a task")
        old_time = time.time() - 7200
        os.utime(nf, (old_time, old_time))
        with patch.object(fd, "WORKSPACE", ws):
            result = fd.check_stale_tasks()
        if result == []:
            ok("(st-d) non-task-*.txt file → []")
        else:
            fail("(st-d) non-task-*.txt file → []", f"got {result}")


# ── check_notes_without_follow_up ───────────────────────────────────────────

def test_notes_old_checkbox_todo():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        notes = ws / "notes"
        notes.mkdir()
        note = notes / "my-plan.md"
        note.write_text("# Plan\n\n- [ ] Finish this\n")
        old_time = time.time() - (8 * 86400)  # 8 days ago
        os.utime(note, (old_time, old_time))
        with patch.object(fd, "WORKSPACE", ws):
            result = fd.check_notes_without_follow_up()
        if len(result) == 1 and "My Plan" in result[0]:
            ok("(nf-a) old note with '- [ ]' → found")
        else:
            fail("(nf-a) old note with '- [ ]' → found", f"got {result}")


def test_notes_fresh_checkbox_not_flagged():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        notes = ws / "notes"
        notes.mkdir()
        note = notes / "recent-plan.md"
        note.write_text("# Recent\n\n- [ ] Do this now\n")
        # mtime = now (within 7d threshold)
        with patch.object(fd, "WORKSPACE", ws):
            result = fd.check_notes_without_follow_up()
        if result == []:
            ok("(nf-b) fresh note with '- [ ]' → []")
        else:
            fail("(nf-b) fresh note with '- [ ]' → []", f"got {result}")


def test_notes_todo_marker():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        notes = ws / "notes"
        notes.mkdir()
        note = notes / "old-task.md"
        note.write_text("# Old Task\n\ntodo: call Alice\n")
        old_time = time.time() - (10 * 86400)
        os.utime(note, (old_time, old_time))
        with patch.object(fd, "WORKSPACE", ws):
            result = fd.check_notes_without_follow_up()
        if len(result) == 1:
            ok("(nf-c) old note with 'todo:' → found")
        else:
            fail("(nf-c) old note with 'todo:' → found", f"got {result}")


def test_notes_no_markers_not_flagged():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        notes = ws / "notes"
        notes.mkdir()
        note = notes / "clean-note.md"
        note.write_text("# Clean\n\nJust some thoughts, nothing actionable.\n")
        old_time = time.time() - (30 * 86400)
        os.utime(note, (old_time, old_time))
        with patch.object(fd, "WORKSPACE", ws):
            result = fd.check_notes_without_follow_up()
        if result == []:
            ok("(nf-d) old note without markers → []")
        else:
            fail("(nf-d) old note without markers → []", f"got {result}")


def test_notes_no_dir():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        # notes/ dir does not exist
        with patch.object(fd, "WORKSPACE", ws):
            result = fd.check_notes_without_follow_up()
        if result == []:
            ok("(nf-e) no notes dir → []")
        else:
            fail("(nf-e) no notes dir → []", f"got {result}")


# ── helpers ──────────────────────────────────────────────────────────────────

def _run_pq_content(content: str) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / "pending-questions.md").write_text(content)
        with patch.object(fd, "WORKSPACE", ws):
            return fd.check_pending_questions()


def _run_pq(ws: Path, content: str) -> list[str]:
    (ws / "pending-questions.md").write_text(content)
    with patch.object(fd, "WORKSPACE", ws):
        return fd.check_pending_questions()


if __name__ == "__main__":
    print("friction-detector tests")
    print("=" * 40)
    test_pq_empty_file()
    test_pq_no_pending_marker()
    test_pq_unanswered_section_found()
    test_pq_resolved_section_skipped()
    test_pq_mixed_sections()
    test_pq_no_status_field()
    test_pq_missing_file()
    test_stale_tasks_no_dir()
    test_stale_tasks_fresh_file()
    test_stale_tasks_old_file()
    test_stale_tasks_non_task_files_skipped()
    test_notes_old_checkbox_todo()
    test_notes_fresh_checkbox_not_flagged()
    test_notes_todo_marker()
    test_notes_no_markers_not_flagged()
    test_notes_no_dir()
    print("=" * 40)
    print(f"Results: {PASS} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)
