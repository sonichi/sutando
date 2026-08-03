#!/usr/bin/env python3
"""Docs must not claim `$SUTANDO_WORKSPACE` still affects workspace resolution.

`$SUTANDO_WORKSPACE` was removed from the resolution order in v0.8 (#1440); the
resolver ignores its value and a set value only fires a one-time deprecation
warning (plus possible one-time auto-migration).

This guard exists because a *partially* corrected doc is worse than an
uncorrected one. On 2026-08-01 the sweep that fixed the resolution-order list in
`src/workspace_default.py` left two bullets in the SAME docstring still saying
users with the env var "keep their old location" and that "only the env var in
the process environment matters". A reader hitting the stale half sets the
variable, sees no error, and writes into the real workspace — the exact failure
the sweep was meant to prevent.

Text-level guard, deliberately: the claim being wrong is a documentation defect,
so the assertion belongs on the text. Behavioural coverage of the resolver lives
in the sutando-config suites.

Run: python3 tests/workspace-env-var-not-honored-docs.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGETS = [
    REPO / "src" / "workspace_default.py",
    REPO / "docs" / "workspace-contract.md",
]

# Phrases that assert the env var IS consulted. Each is a real phrasing that
# shipped, not a hypothetical.
STALE = [
    (r"with\s+`?\$SUTANDO_WORKSPACE`?\s+set\s+keep\s+their\s+old\s+location",
     'claims a set env var preserves the old location'),
    (r"only\s+the\s+env\s+var\s+in\s+the\s+process\s+environment\s+matters",
     'implies the process env var is consulted for resolution'),
    (r"^\s*1\.\s*`?\$?SUTANDO_WORKSPACE",
     'lists the env var as step 1 of the resolution order'),
]

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


# A missing target would make every assertion below vacuously pass.
for t in TARGETS:
    check(f"target exists: {t.relative_to(REPO)}", t.is_file())

for t in TARGETS:
    if not t.is_file():
        continue
    text = t.read_text()
    rel = t.relative_to(REPO)
    for pat, why in STALE:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        line = text[: m.start()].count("\n") + 1 if m else None
        check(f"{rel}: no stale claim — {why}", m is None,
              f"matched at line {line}: {m.group(0)[:70]!r}" if m else "")

# Positive control: the corrective statement must actually be present, so this
# file cannot pass merely because someone deleted the documentation.
wd = (REPO / "src" / "workspace_default.py")
if wd.is_file():
    body = wd.read_text()
    check("workspace_default.py still documents that the env var is NOT honored",
          bool(re.search(r"(not\s+in\s+that\s+order|no\s+longer\s+honored|ignores\s+its\s+value|"
                         r"Neither\s+`?\$SUTANDO_WORKSPACE)", body, re.IGNORECASE)))

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — no doc claims the removed env var affects resolution")
