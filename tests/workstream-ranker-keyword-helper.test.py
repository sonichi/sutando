#!/usr/bin/env python3
"""Regression pin: ranker keywords must be split on every non-letter.

`best_match` takes two arguments and both fail the same silent way. The
candidates side already has a helper and a pin; the keywords side did not, so
the extraction regex was hand-written per call site. Keeping `-` in the token
class merges `morning-briefing.py` into one token that matches no workstream
label, the correct workstream scores on neither word, and the refusal that
follows is indistinguishable from a careful low-confidence omission.

Measured on a live snapshot: hyphens kept scored the right workstream 2 with a
margin of 1 (refuse); split on non-letters scored it 7 with a margin of 3.

Run: python3 tests/workstream-ranker-keyword-helper.test.py
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
        {"id": "w-brief", "name": "Daily morning briefing",
         "summary": "briefing pipeline: calendar cache, delivery, the insight that feeds it"},
        {"id": "w-cal", "name": "Calendar access and retrieval fixes",
         "summary": "missing and moved calendar events, the calendar retrieval mechanism"},
        {"id": "w-misc", "name": "Recurring ops crons", "summary": "scheduled jobs"},
    ]
}

# `morning` and `briefing` must occur ONLY inside the compound: a fixture where
# either also stands alone cannot reproduce the defect that check (d) pins.
TASK = ("FIRST, write today's calendar cache -- morning-briefing.py cannot reach the calendar "
        "itself, and its local calendar fallback needs 49s while the code allows only 10s. "
        "THEN run morning-briefing.py and deliver the result.")

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        if detail:
            print(f"       {detail}")
        failures.append(label)


kws = rw.keywords_from_text(TASK)
cands = rw.candidates_from_snapshot(SNAPSHOT)

check("a) a hyphenated compound yields BOTH of its words",
      "morning" in kws and "briefing" in kws,
      f"got {kws}")

check("b) no token retains a hyphen or a dot",
      all("-" not in k and "." not in k for k in kws), f"got {kws}")

check("c) the reuse decision resolves to the semantically right workstream",
      rw.best_match(cands, kws) == "w-brief",
      f"got {rw.best_match(cands, kws)!r}; scores "
      f"{sorted(((rw.score(t, kws), c) for c, t in cands), reverse=True)}")

# Control: the bug this pins. Keeping `-` in the class is what used to happen.
import re  # noqa: E402
hyphenated = sorted({w for w in re.findall(r"[a-zA-Z][a-zA-Z-]{3,}", TASK.lower())})
check("d) CONTROL: the old hyphen-keeping extraction does collapse to a refusal",
      "morning" not in hyphenated and rw.best_match(cands, hyphenated) is None,
      "the control did not reproduce the defect, so (c) proves nothing; "
      f"kws={hyphenated} verdict={rw.best_match(cands, hyphenated)!r}")

check("e) output is deduped and sorted",
      kws == sorted(set(kws)) and len(kws) == len(set(kws)), f"got {kws}")

check("f) words shorter than the floor are dropped, and the floor is honoured",
      all(len(k) >= 4 for k in kws)
      and rw.keywords_from_text("alpha bravo charlie", min_length=6) == ["charlie"],
      f"got {kws}")

check("g) empty, None and letterless input yield no keywords",
      rw.keywords_from_text("") == []
      and rw.keywords_from_text(None) == []
      and rw.keywords_from_text("123 -- 4.5 /9/ __") == [],
      f'got {rw.keywords_from_text("123 -- 4.5 /9/ __")!r}')

print(f"\nworkstream-ranker-keyword-helper: {7 - len(failures)}/7 passed")
if failures:
    print("FAIL — " + "; ".join(failures))
    raise SystemExit(1)
print("all passed")
