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
    r"[Aa]ll PR edits[^.]*(?:solicit|notif)",
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

# The OWNER is bolded; item 9 only QUOTES it. Counting raw text found the quote
# and missed the owner, which wraps a physical newline — so a needle in prose lies.
_flat = re.sub(r"\s+", " ", text)
check(_flat.count("**" + TRIGGER_OWNER) == 1,
      "exactly ONE section OWNS the messaging trigger (bolded, whitespace-normalised)")
check(_flat.count(TRIGGER_OWNER) >= 2,
      "item 9 still QUOTES the owner (a bare mention is a reference, not ownership)")

# Control: delete the owner and the gate must go RED. The raw-count check this
# replaces stayed GREEN here — it was counting item 9's quote, not the owner.
_without_owner = _flat.replace("**" + TRIGGER_OWNER, "", 1)
check(_without_owner.count("**" + TRIGGER_OWNER) == 0,
      "control: removing the bolded owner is detectable (the check can FAIL)")
check(_without_owner.count(TRIGGER_OWNER) >= 1,
      "control: item 9's quote SURVIVES that removal — which is why raw count was blind")

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
check(any(re.search(p, "All PR edits require reviewer solicitation and notification")
          for p in UNCONDITIONAL),
      "control: alternate unconditional wording is rejected")
check(any(re.search(p, pre_fix) for p in UNCONDITIONAL),
      "control: the unconditional scan DOES match the pre-fix wording")

# The actual owner is a section, not item 9's reference to its opening sentence.
owner_start = text.index("**The trigger to message")
owner_end = text.find("\n## ", owner_start)
without_section = text[:owner_start] + (text[owner_end:] if owner_end != -1 else "")
check(("**" + TRIGGER_OWNER) not in re.sub(r"\s+", " ", without_section),
      "control: deleting the owning section fails the ownership requirement")

route_section = text.split("### Provider-specific awareness delivery\n", 1)[-1].split("\n## ", 1)[0]
route_section = re.sub(r"\s+", " ", route_section)
check("For Matrix routes" in item9 and "#provider-specific-awareness-delivery" in item9,
      "item 9 branches away from the Matrix-only helper")
check("**Matrix:**" in route_section and "scripts/notify_reviewers.py" in route_section,
      "Matrix control retains the roster-aware notifier")
check('**Discord, including `exclusive: true`:**' in route_section
      and '<@STAND_ID>' in route_section
      and 'python3 src/discord-bridge.py send CHANNEL_ID --body-file BODY_FILE' in route_section,
      "exclusive Discord uses the existing addressed provider sender")
check("awareness is still unsatisfied" in route_section
      and "Hand the exact artifact" in route_section,
      "unsupported/refused delivery requires immediate handoff, never satisfied awareness")

print(f"\n{'OK' if not failures else 'FAILED'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
