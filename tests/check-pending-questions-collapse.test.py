#!/usr/bin/env python3
"""Tests for src/check-pending-questions.py — stable reminder filenames.

Discord-DM reminder files (`proactive-pending-q-*.txt`) are named from
`questions_key()`, a hash of the sorted pending-question set. Covers: the key
is order-independent and stable for a given set, changes when a question is
added or answered, and that repeated reminders for the same set reuse one file
while a changed set produces a new one.

Run: python3 tests/check-pending-questions-collapse.test.py
"""

from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_pending_questions", REPO / "src" / "check-pending-questions.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_passed = 0
_failed = 0


def ok(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


Q_AB = [{"title": "Q1: Apply stash@{0} to 0625"}, {"title": "Q2: PR #1753 CLA"}]
Q_AB_REORDERED = [{"title": "Q2: PR #1753 CLA"}, {"title": "Q1: Apply stash@{0} to 0625"}]
Q_B = [{"title": "Q2: PR #1753 CLA"}]          # Q1 answered
Q_ABC = Q_AB + [{"title": "Q3: new question"}]  # one added

# T1: key is deterministic AND order-independent (it's a SET key).
k_ab = _mod.questions_key(Q_AB)
ok("key is order-independent", k_ab == _mod.questions_key(Q_AB_REORDERED))
ok("key is deterministic", k_ab == _mod.questions_key(Q_AB))

# T2: set changes -> different key (so a genuinely-changed reminder re-surfaces).
ok("answering one question -> new key", _mod.questions_key(Q_B) != k_ab)
ok("adding one question -> new key", _mod.questions_key(Q_ABC) != k_ab)

# T3: key is a 16-char hex hash (stable), not a timestamp.
ok("key is 16 hex chars", re.fullmatch(r"[0-9a-f]{16}", k_ab) is not None)

# T4: repeated reminders for the same set reuse one file.
with tempfile.TemporaryDirectory() as td:
    _mod.RESULTS_DIR = Path(td)
    _mod.notify_discord_dm(Q_AB)
    _mod.notify_discord_dm(Q_AB)
    _mod.notify_discord_dm(Q_AB)
    files = list(Path(td).glob("proactive-pending-q-*.txt"))
    ok("3 fires, same set -> 1 proactive-pending-q file", len(files) == 1)
    body = files[0].read_text() if files else ""
    ok("proactive content lists the question set", "Q1" in body and "Q2" in body)

# T5: a changed set produces a distinct file.
with tempfile.TemporaryDirectory() as td:
    _mod.RESULTS_DIR = Path(td)
    _mod.notify_discord_dm(Q_AB)
    _mod.notify_discord_dm(Q_B)  # Q1 answered -> different set
    files = list(Path(td).glob("proactive-pending-q-*.txt"))
    ok("set change -> 2 distinct files", len(files) == 2)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(0 if _failed == 0 else 1)
