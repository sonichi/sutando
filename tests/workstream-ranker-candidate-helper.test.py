#!/usr/bin/env python3
"""Regression pin: ranker candidates must be built from the snapshot's own key.

The store labels a workstream `title`; every snapshot layer re-exports it as
`name` (`task_workstreams.py` build/export sites). A caller hand-assembling
(id, text) pairs and reaching for `title` against a real snapshot gets empty
text, not an error -- so every score is 0 and `best_match` refuses everything,
which reads exactly like a correct low-confidence refusal. Two prior fixes for
this class (#3545, #3470) were documentation; the guess recurred anyway.

Run: python3 tests/workstream-ranker-candidate-helper.test.py
Exit code: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "task-workstream-grouping" / "scripts"))

import rank_workstreams as rw  # noqa: E402

# Shaped like build_classifier_snapshot's output: labels arrive under `name`.
SNAPSHOT = {
    "existing_workstreams": [
        {"id": "w-pending", "name": "Pending questions monitoring",
         "summary": "recurring cron surfacing open owner decisions"},
        {"id": "w-people", "name": "People dossier maintenance",
         "summary": "contact dossiers"},
        {"id": "w-misc", "name": "Recurring ops crons", "summary": "scheduled jobs"},
    ]
}

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        if detail:
            print(f"       {detail}")
        failures.append(label)


cands = rw.candidates_from_snapshot(SNAPSHOT)

check("a) every snapshot row yields non-empty searchable text",
      len(cands) == 3 and all(t.strip() for _, t in cands),
      f"got {cands}")

# The whole point: the label must reach the scorer, not just the summary.
check("b) the label itself is searchable (not only the summary)",
      rw.score(dict(cands)["w-pending"], ["pending questions"]) > 0,
      f'text was {dict(cands).get("w-pending")!r}')

check("c) a real reuse decision resolves instead of tying at zero",
      rw.best_match(cands, ["pending", "questions", "cron"]) == "w-pending",
      f'got {rw.best_match(cands, ["pending", "questions", "cron"])!r}')

# Control: the bug this pins. Reading only `title` is what used to happen.
by_title = [(r["id"], f'{r.get("title") or ""} {r.get("summary") or ""}'.strip())
            for r in SNAPSHOT["existing_workstreams"]]
check("d) CONTROL: the old title-only assembly does collapse to a refusal",
      rw.best_match(by_title, ["pending", "questions", "cron"]) is None,
      "the control did not reproduce the defect, so (c) proves nothing")

check("e) legacy store-shaped rows using `title` still resolve",
      rw.candidates_from_snapshot(
          {"existing_workstreams": [{"id": "w1", "title": "Legacy", "summary": "s"}]}
      ) == [("w1", "Legacy s")])

check("f) malformed rows are skipped, not crashed on",
      rw.candidates_from_snapshot(
          {"existing_workstreams": ["junk", {"name": "no id"}, {"id": "w9"}]}
      ) == [("w9", "")])

check("g) a missing or empty snapshot yields no candidates",
      rw.candidates_from_snapshot({}) == [] and rw.candidates_from_snapshot(None) == [])

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("Ranker candidates are built from the snapshot's own key.")
