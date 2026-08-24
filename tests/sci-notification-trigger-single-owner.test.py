#!/usr/bin/env python3
"""collaboration-intelligence: the PR messaging trigger has ONE owner.

Operating-contract item 9 used to restate the trigger ("every PR create or
update"), which is strictly wider than the procedure's own tests. A cosmetic
edit therefore commanded a duplicate solicitation under one rule and no action
under the other. This pins the STRUCTURE that makes that divergence impossible
— one section states when to message, item 9 states only what addressing is —
rather than the wording of either.

Run: python3 tests/sci-notification-trigger-single-owner.test.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "collaboration-intelligence" / "SKILL.md"

# Formulations that make ANY PR edit a trigger. Item 9 carrying one of these is
# the defect: it outranks the procedure's tests without naming them.
UNCONDITIONAL = [
    r"every PR create or update ends with",
    r"every PR (?:create|update)\b[^.]*\bends with\b",
    r"on every (?:PR )?update[^.]*(?:solicit|notif)",
]
TRIGGER_OWNER = "The trigger to message is a state change the other party would want to know about"

failures = []


def check(cond, label):
    print(f"{'ok' if cond else 'FAIL'}: {label}")
    if not cond:
        failures.append(label)


text = SKILL.read_text(encoding="utf-8")
item9 = next((ln for ln in text.splitlines() if ln.startswith("9. ")), "")

check(bool(item9), "operating-contract item 9 is present")

for pat in UNCONDITIONAL:
    check(re.search(pat, item9) is None,
          f"item 9 states no unconditional trigger: /{pat}/")

check(text.count(TRIGGER_OWNER) == 1,
      "exactly ONE section states the messaging trigger")

check("trigger is owned solely by" in item9,
      "item 9 defers the trigger instead of restating it")

# The narrowing must name the negative case, or 'review-relevant' reads as
# advice rather than as a rule with an excluded case.
check(re.search(r"cosmetic[^.]*fires nothing", item9) is not None,
      "item 9 names the cosmetic-edit negative case explicitly")

check("fires once" in item9,
      "item 9 preserves once-per-trigger")

# Positive control: the probe must be able to FAIL. Re-run the unconditional
# scan against the pre-fix wording; a probe that cannot fire proves nothing.
pre_fix = ("9. **PR notification contract: every PR create or update ends with "
           "reviewers SOLICITED and NOTIFIED.**")
check(any(re.search(p, pre_fix) for p in UNCONDITIONAL),
      "control: the unconditional scan DOES match the pre-fix wording")

print(f"\n{'OK' if not failures else 'FAILED'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
