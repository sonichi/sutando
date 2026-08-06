#!/usr/bin/env python3
"""Contract test for the schedule-crons EXAMPLE file (PR #2030).

The digest-delivery contract (SKILL.md "Digest cron delivery"): a cron whose
prompt produces long research output (>280 chars) MUST deliver via
`results/proactive-*.txt` and MUST NOT hand its final result to
`task-progress/notify.py`, which hard-rejects anything over 280 chars — a
progress-ping tool, not a delivery channel. The canonical template lives in
`crons.example.json`; if it contradicts the contract, a user copying it gets a
silently-missing digest (exactly qingyun-wu's CR on #2030).

This test pins the example file to the contract so the regression can't return.

Run: python3 tests/schedule-crons-example-digest-delivery.test.py  (exit 0/1)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "skills" / "schedule-crons" / "crons.example.json"

_passed = 0
_failed = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok  {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


# The example file must parse and be a non-empty list.
entries = json.loads(EXAMPLE.read_text())
ok("crons.example.json parses to a non-empty list", isinstance(entries, list) and entries, str(type(entries)))

# A "digest-style" entry is one whose prompt advertises a payload larger than
# notify.py's 280-char ceiling ("under ~<N> chars", N > 280). Those are exactly
# the entries that must NOT use notify.py for final delivery.
_cap = re.compile(r"under\s*~?\s*([0-9,]+)\s*chars", re.I)


def declared_cap(prompt: str) -> int | None:
    m = _cap.search(prompt or "")
    return int(m.group(1).replace(",", "")) if m else None


NOTIFY_FINAL = re.compile(r"notify\.py[^\n]*--message", re.I)

digest_entries = [e for e in entries if (declared_cap(e.get("prompt", "")) or 0) > 280]
ok("at least one digest-style entry exists to validate", bool(digest_entries),
   "no entry declares a >280-char payload")

for e in digest_entries:
    name = e.get("name", "?")
    prompt = e.get("prompt", "")
    ok(f"digest '{name}': does NOT route final output through notify.py",
       not NOTIFY_FINAL.search(prompt), "still uses notify.py --message for a >280-char payload")
    ok(f"digest '{name}': writes results/proactive-*.txt (cross-surface delivery)",
       "results/proactive-" in prompt, "missing the proactive-* delivery path")

# Doc-drift guard (qingyun-wu CR on #2578): a feature-specific cron that is NOT
# in the default template must not have its skill docs claim the default template
# installs it. obsidian-dream was removed from crons.example.json here; the
# obsidian-vault SKILL.md must not tell users the default template adds it — else
# a new user enabling the mirror believes nightly relinking is scheduled when
# nothing runs.
_names = {e.get("name") for e in entries}
_obsidian_skill = REPO / "skills" / "obsidian-vault" / "SKILL.md"
if "obsidian-dream" not in _names and _obsidian_skill.is_file():
    _txt = _obsidian_skill.read_text()
    ok("obsidian SKILL.md doesn't claim the default template adds obsidian-dream",
       not re.search(r"default template adds\s*`?obsidian-dream", _txt, re.I),
       "SKILL.md still says the default template installs obsidian-dream, but it "
       "was removed from crons.example.json — update the setup instruction")

print()
if _failed:
    print(f"FAIL — {_failed} of {_passed + _failed}")
    sys.exit(1)
print(f"PASS — {_passed}/{_passed} schedule-crons example digest-delivery checks")
sys.exit(0)
