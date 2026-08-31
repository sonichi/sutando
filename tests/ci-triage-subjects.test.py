#!/usr/bin/env python3
"""Regression pin: a red check's SUBJECT is what finds the issue, not its name.

The check that goes red is named for its detector ("diff coverage >= 95%
(python)"); the open issue is titled after the file ("outbox-race.test.py").
Searching the detector name finds nothing, so the failure reads as novel and
gets diagnosed from scratch. `subjects_from_text` extracts the file, and ranks
lines that ACCUSE a file above the many that merely mention one.

Run: python3 tests/ci-triage-subjects.test.py
Exit code: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ci_triage", REPO / "scripts" / "ci-triage.py")
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

failures: "list[str]" = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        if detail:
            print(f"       {detail}")
        failures.append(label)


# Shaped like a real coverage-gate log: one accusing line, many benign mentions.
LOG = """
  ✖ test TIMED OUT under instrumentation (>120s): tests/outbox-race.test.py
      - tests/browser-persistent.test.py (1/11 skipped)
      - tests/workspace-default.test.py (4/44 skipped)
  running scripts/coverage-gate.sh
"""

subs = ct.subjects_from_text(LOG)

check("a) the accused file is found at all", "tests/outbox-race.test.py" in subs, f"got {subs}")

check("b) the accused file RANKS FIRST, ahead of merely-mentioned ones",
      subs and subs[0] == "tests/outbox-race.test.py", f"got {subs}")

# Changed deliberately: benign mentions are now EXCLUDED, not merely ranked
# lower. A confident pointer at an unrelated file is worse than reporting none.
check("c) files mentioned WITHOUT a failure marker are excluded entirely",
      "tests/browser-persistent.test.py" not in subs
      and "tests/workspace-default.test.py" not in subs, f"got {subs}")

# CONTROL: without blame-ranking, log order would put the accused file first only
# by luck. Put it LAST in the text and confirm ranking still promotes it.
LOG_REORDERED = """
      - tests/browser-persistent.test.py (1/11 skipped)
      - tests/workspace-default.test.py (4/44 skipped)
  ✖ test TIMED OUT under instrumentation (>120s): tests/outbox-race.test.py
"""
subs2 = ct.subjects_from_text(LOG_REORDERED)
check("d) CONTROL: ranking is by blame, not by position in the text",
      subs2 and subs2[0] == "tests/outbox-race.test.py",
      f"got {subs2} — first-in-text would have returned browser-persistent")

# A coverage miss names a SOURCE file on the accusing line.
COV = "src/review-preflight.py (90.3%): Missing lines 147-148,153-154"
check("e) a source file on an accusing line is a subject too",
      ct.subjects_from_text(COV)[:1] == ["src/review-preflight.py"],
      f"got {ct.subjects_from_text(COV)}")

check("f2) a log with NO accusing line yields nothing, not a listing",
      ct.subjects_from_text("  - tests/a.test.py (skipped)\n  - tests/b.test.py (ok)") == [],
      "whole-text fallback would return both")

# Verbatim annotation text from job 99387881539 (#3588). `::error::` output
# lands in annotations; the job LOG holds the workflow's echoed script instead.
check("e2) a real CI annotation line yields the accused test file",
      ct.subjects_from_text("FAILED: tests/runtime-tui-reference-client.test.py (exit 1)")
      == ["tests/runtime-tui-reference-client.test.py"])

check("f) empty and None inputs yield no subjects, not a crash",
      ct.subjects_from_text("") == [] and ct.subjects_from_text(None) == [])

check("g) duplicates collapse", ct.subjects_from_text(LOG + LOG).count("tests/outbox-race.test.py") == 1)


class _R:
    def __init__(self, rc, out=""):
        self.returncode, self.stdout = rc, out


check("h) a failed gh call reads as UNKNOWN (None), never as 'nothing filed'",
      ct.open_issues_for("x", lambda a: _R(1), "o/r") is None
      and ct.failing_checks("1", lambda a: _R(1), "o/r") is None)

check("i) unparseable stdout is also UNKNOWN",
      ct.open_issues_for("x", lambda a: _R(0, "not json"), "o/r") is None)

_ROLLUP = json.dumps({"statusCheckRollup": [
    {"name": "real", "conclusion": "FAILURE"},
    {"name": "superseded", "conclusion": "CANCELLED"},
    {"name": "fine", "conclusion": "SUCCESS"}]})
check("j2) CANCELLED is capacity, not a failing check — only FAILURE is triaged",
      ct.failing_checks("1", lambda a: _R(0, _ROLLUP), "o/r") == ["real"],
      f'got {ct.failing_checks("1", lambda a: _R(0, _ROLLUP), "o/r")}')

check("j) an empty issue list is a real zero, distinct from a failed call",
      ct.open_issues_for("x", lambda a: _R(0, "[]"), "o/r") == [])

# gh's --search RANKS; it does not match. An issue that merely scores for a path
# is a wrong pointer carrying the tool's authority, so it must be dropped.
_RANKED = json.dumps([{"number": 1, "title": "unrelated flake", "body": "nothing here"},
                      {"number": 2, "title": "tests/x.test.py times out", "body": ""}])
check("k) an issue that only RANKS for the subject is dropped; a literal match is kept",
      [h["number"] for h in ct.open_issues_for("tests/x.test.py", lambda a: _R(0, _RANKED), "o/r")] == [2],
      f'got {ct.open_issues_for("tests/x.test.py", lambda a: _R(0, _RANKED), "o/r")}')

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("A red check's subject is extracted and blame-ranked.")
