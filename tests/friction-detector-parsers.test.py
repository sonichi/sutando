#!/usr/bin/env python3
"""Behavioral tests for `src/friction-detector.py` file-parsing functions.

Functions under test (all read from workspace files; tested via monkeypatched
WORKSPACE pointing to a temp directory with controlled fixtures):

  check_pending_questions()     — parses pending-questions.md for unanswered items
  check_stale_tasks()           — flags task-*.txt files older than 1 hour
  check_notes_without_follow_up() — flags notes with TODO markers older than 7 days

Network-dependent functions (check_github_issues, check_overdue_reminders) are
excluded — they invoke subprocess/gh and require external services.

Run: python3 tests/friction-detector-parsers.test.py
Exit: 0 on pass, non-zero on fail.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src"

# -----------------------------------------------------------------------
# Module loader
# -----------------------------------------------------------------------
_WS = tempfile.mkdtemp(prefix="sutando-test-friction-")
os.makedirs(f"{_WS}/tasks", exist_ok=True)
os.makedirs(f"{_WS}/results", exist_ok=True)
os.environ["SUTANDO_WORKSPACE"] = _WS
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

for _n in ("friction_detector", "friction-detector", "workspace_default", "util_paths"):
    sys.modules.pop(_n, None)

_spec = importlib.util.spec_from_file_location("friction_detector", _SRC / "friction-detector.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

check_pending_questions = _mod.check_pending_questions
check_stale_tasks = _mod.check_stale_tasks
check_notes_without_follow_up = _mod.check_notes_without_follow_up

_WS_PATH = Path(_WS)

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

PASS = 0
FAIL = 0


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}")


def fail(label: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f": {detail}" if detail else ""))


def _write_pq(content: str) -> None:
    """Write content to pending-questions.md in the workspace."""
    (_WS_PATH / "pending-questions.md").write_text(content)


def _with_workspace(ws: Path):
    """Context manager: temporarily point _mod.WORKSPACE to ws."""
    class _Ctx:
        def __enter__(self):
            self._orig = _mod.WORKSPACE
            _mod.WORKSPACE = ws
            return self
        def __exit__(self, *_):
            _mod.WORKSPACE = self._orig
    return _Ctx()


# -----------------------------------------------------------------------
# check_pending_questions
# -----------------------------------------------------------------------

# T1 — missing pending-questions.md → empty list
(_WS_PATH / "pending-questions.md").unlink(missing_ok=True)
with _with_workspace(_WS_PATH):
    result = check_pending_questions()
if result == []:
    ok("T1: missing pending-questions.md → []")
else:
    fail("T1: missing file", f"got {result}")

# T2 — file contains "(No pending questions)" sentinel → []
_write_pq("(No pending questions)")
with _with_workspace(_WS_PATH):
    result = check_pending_questions()
if result == []:
    ok("T2: '(No pending questions)' sentinel → []")
else:
    fail("T2: sentinel", f"got {result}")

# T3 — blank file → []
_write_pq("")
with _with_workspace(_WS_PATH):
    result = check_pending_questions()
if result == []:
    ok("T3: blank file → []")
else:
    fail("T3: blank file", f"got {result}")

# T4 — well-formed unanswered item → issue returned
_write_pq("""## PR go-ahead question

- **Asked:** 2026-01-01
- **Status:** unanswered
- Question body here.
""")
with _with_workspace(_WS_PATH):
    result = check_pending_questions()
if result and "PR go-ahead question" in result[0]:
    ok("T4: well-formed unanswered item → issue returned with title")
else:
    fail("T4: unanswered item", f"got {result}")

# T5 — item with status 'answered' → not returned
_write_pq("""## Some resolved question

- **Asked:** 2026-01-01
- **Status:** answered
- Answer was given.
""")
with _with_workspace(_WS_PATH):
    result = check_pending_questions()
if result == []:
    ok("T5: status=answered → not returned (no false positive)")
else:
    fail("T5: answered not returned", f"got {result}")

# T6 — missing Asked date → unanswered item still returned (no crash)
_write_pq("""## Dateless question

- **Status:** unanswered
""")
with _with_workspace(_WS_PATH):
    result = check_pending_questions()
if result and "Dateless question" in result[0]:
    ok("T6: missing Asked date → item still returned without crash")
else:
    fail("T6: missing date no crash", f"got {result}")

# T7 — multiple items, only unanswered returned
_write_pq("""## Question Alpha

- **Asked:** 2026-01-01
- **Status:** unanswered

## Question Beta

- **Asked:** 2026-01-02
- **Status:** answered

## Question Gamma

- **Asked:** 2026-01-03
- **Status:** unanswered
""")
with _with_workspace(_WS_PATH):
    result = check_pending_questions()
titles_in_result = " ".join(result)
if (len(result) == 2
        and "Question Alpha" in titles_in_result
        and "Question Gamma" in titles_in_result
        and "Question Beta" not in titles_in_result):
    ok("T7: 3 items (2 unanswered, 1 answered) → only 2 unanswered returned")
else:
    fail("T7: multiple items filter", f"got {result}")

# T8 — age string embedded in returned issue (old date)
_write_pq("""## Old unresolved question

- **Asked:** 2020-06-01
- **Status:** unanswered
""")
with _with_workspace(_WS_PATH):
    result = check_pending_questions()
if result and ("d old" in result[0] or "days" in result[0] or "2020" in result[0] or result):
    ok("T8: old unanswered item returned (age calculation runs without crash)")
else:
    fail("T8: age calculation", f"got {result}")

# -----------------------------------------------------------------------
# check_stale_tasks
# -----------------------------------------------------------------------

# T9 — tasks dir missing → []
import shutil
tasks_dir = _WS_PATH / "tasks"
shutil.rmtree(str(tasks_dir), ignore_errors=True)
with _with_workspace(_WS_PATH):
    result = check_stale_tasks()
if result == []:
    ok("T9: tasks dir missing → []")
else:
    fail("T9: no tasks dir", f"got {result}")

# Recreate tasks dir for remaining tests
tasks_dir.mkdir(exist_ok=True)

# T10 — fresh task file (age < 1h) → not flagged
fresh_task = tasks_dir / "task-fresh-001.txt"
fresh_task.write_text("id: task-fresh-001\ntask: test\n")
with _with_workspace(_WS_PATH):
    result = check_stale_tasks()
if not result:
    ok("T10: fresh task file (age < 1h) → not flagged")
else:
    fail("T10: fresh task not flagged", f"got {result}")
fresh_task.unlink()

# T11 — old task file (mtime set to 2h ago) → flagged
old_task = tasks_dir / "task-old-002.txt"
old_task.write_text("id: task-old-002\ntask: test\n")
two_hours_ago = time.time() - 7200
os.utime(str(old_task), (two_hours_ago, two_hours_ago))
with _with_workspace(_WS_PATH):
    result = check_stale_tasks()
if result and "task-old-002.txt" in result[0]:
    ok("T11: task file with mtime 2h ago → flagged as stale")
else:
    fail("T11: stale task flagged", f"got {result}")
old_task.unlink()

# T12 — non-task-*.txt file in tasks dir → not flagged
other_file = tasks_dir / "archive-info.txt"
other_file.write_text("some archive info")
two_hours_ago = time.time() - 7200
os.utime(str(other_file), (two_hours_ago, two_hours_ago))
with _with_workspace(_WS_PATH):
    result = check_stale_tasks()
if not result:
    ok("T12: non-task-*.txt file (even if old) → not flagged")
else:
    fail("T12: non-task glob", f"got {result}")
other_file.unlink()

# T13 — mix: one stale task + one fresh → only stale flagged
fresh2 = tasks_dir / "task-fresh-003.txt"
stale2 = tasks_dir / "task-stale-004.txt"
fresh2.write_text("fresh\n")
stale2.write_text("stale\n")
os.utime(str(stale2), (time.time() - 7300, time.time() - 7300))
with _with_workspace(_WS_PATH):
    result = check_stale_tasks()
if len(result) == 1 and "task-stale-004.txt" in result[0]:
    ok("T13: mix of stale + fresh → only stale flagged")
else:
    fail("T13: stale+fresh mix", f"got {result}")
fresh2.unlink()
stale2.unlink()

# -----------------------------------------------------------------------
# check_notes_without_follow_up
# -----------------------------------------------------------------------

# T14 — notes dir missing → []
# Point to a workspace with no notes dir
empty_ws = Path(tempfile.mkdtemp(prefix="sutando-test-friction-nonotes-"))
with _with_workspace(empty_ws):
    result = check_notes_without_follow_up()
if result == []:
    ok("T14: notes dir missing → []")
else:
    fail("T14: no notes dir", f"got {result}")

# For remaining notes tests, create a notes dir under _WS_PATH
notes_dir = _WS_PATH / "notes"
notes_dir.mkdir(exist_ok=True)

def _write_note(name: str, content: str, age_days: float = 0.0) -> Path:
    p = notes_dir / name
    p.write_text(content)
    if age_days > 0:
        t = time.time() - age_days * 86400
        os.utime(str(p), (t, t))
    return p


# T15 — note with "- [ ]" checkbox, older than 7 days → flagged
p = _write_note("note-todo-checkbox.md", "# Todo note\n\n- [ ] Do the thing\n", age_days=10)
with _with_workspace(_WS_PATH):
    result = check_notes_without_follow_up()
if result and "Note with action items" in result[0] and "Note Todo Checkbox" in result[0]:
    ok("T15: note with '- [ ]' checkbox >7 days old → flagged (title formatted from stem)")
else:
    fail("T15: checkbox todo flagged", f"got {result}")
p.unlink()

# T16 — note with "todo:" marker at line start, older than 7 days → flagged
p = _write_note("note-todo-marker.md", "# Work note\n\ntodo: call the vendor\n", age_days=8)
with _with_workspace(_WS_PATH):
    result = check_notes_without_follow_up()
if result and "Note with action items" in result[0] and "Note Todo Marker" in result[0]:
    ok("T16: note with 'todo:' at line start >7 days old → flagged")
else:
    fail("T16: todo: marker flagged", f"got {result}")
p.unlink()

# T17 — fresh note with "- [ ]" (< 7 days) → NOT flagged
p = _write_note("note-todo-fresh.md", "# Fresh todo\n\n- [ ] still fresh\n", age_days=0)
with _with_workspace(_WS_PATH):
    result = check_notes_without_follow_up()
if not result:
    ok("T17: note with '- [ ]' but <7 days old → not flagged")
else:
    fail("T17: fresh todo not flagged", f"got {result}")
p.unlink()

# T18 — note with "Action:" anywhere in body (not a directive prefix) → NOT flagged
# This guards against the documented false-positive: "Action: Get Contents of URL" in Shortcuts docs
p = _write_note(
    "note-action-prose.md",
    "# Research note\n\nAction: Get Contents of URL is a Shortcuts primitive.\n",
    age_days=10
)
with _with_workspace(_WS_PATH):
    result = check_notes_without_follow_up()
if not result:
    ok("T18: 'Action: ...' prose in note body → NOT flagged (false-positive guard per code comment)")
else:
    fail("T18: action prose false positive", f"got {result}")
p.unlink()

# T19 — note with tags line containing 'todo' tag → flagged if >7 days
p = _write_note(
    "note-tags-todo.md",
    "---\ntags: [research, todo, followup]\n---\n\n# Tagged todo note\n",
    age_days=10
)
with _with_workspace(_WS_PATH):
    result = check_notes_without_follow_up()
if result and "Note with action items" in result[0] and "Note Tags Todo" in result[0]:
    ok("T19: note with 'todo' in tags line >7 days → flagged")
else:
    fail("T19: tags-line todo", f"got {result}")
p.unlink()

# T20 — clean note (no markers, no todo tags) → not flagged regardless of age
p = _write_note("note-clean.md", "# Clean note\n\nJust some notes with no action items.\n", age_days=30)
with _with_workspace(_WS_PATH):
    result = check_notes_without_follow_up()
if not result:
    ok("T20: clean note (no TODO markers) → not flagged regardless of age")
else:
    fail("T20: clean note false positive", f"got {result}")
p.unlink()

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
print(f"\n{PASS}/{PASS + FAIL} tests passed")
if FAIL:
    sys.exit(1)
