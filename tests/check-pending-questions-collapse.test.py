#!/usr/bin/env python3
"""Tests for src/check-pending-questions.py — stable reminder filenames.

Discord-DM reminder files (`proactive-pending-q-*.txt`) are named from
`questions_key()`, a hash of the sorted pending-question set. Covers: the key
is order-independent and stable for a given set, changes when a question is
added or answered, and that a reminder supersedes any earlier undelivered one
— of ours only, and never a claimed `.sending` body.

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

# T5: a changed set supersedes the earlier undelivered reminder rather than
# queueing beside it — the old body states a count that is no longer true.
with tempfile.TemporaryDirectory() as td:
    _mod.RESULTS_DIR = Path(td)
    _mod.notify_discord_dm(Q_AB)
    _mod.notify_discord_dm(Q_B)  # Q1 answered -> different set
    names = {f.name for f in Path(td).glob("proactive-pending-q-*.txt")}
    # Compare the whole SET: asserting on files[0] passes on a 2-file directory
    # whenever the glob happens to yield the new one first.
    ok("set change -> only the NEW set's file remains",
       names == {f"proactive-pending-q-{_mod.questions_key(Q_B)}.txt"})
    ok("the old set's file is gone",
       f"proactive-pending-q-{_mod.questions_key(Q_AB)}.txt" not in names)
    survivor = Path(td) / f"proactive-pending-q-{_mod.questions_key(Q_B)}.txt"
    body = survivor.read_text() if survivor.exists() else ""
    ok("the surviving body states the new count", body.startswith("⚠️ 1 pending question "))

# T6: superseding is scoped to OUR reminders. results/ is a shared namespace —
# other proactive producers' bodies must survive, as must a claimed .sending.
with tempfile.TemporaryDirectory() as td:
    _mod.RESULTS_DIR = Path(td)
    foreign = Path(td) / "proactive-morning-123.txt"
    foreign.write_text("briefing")
    claimed = Path(td) / "proactive-pending-q-deadbeefdeadbeef.sending"
    claimed.write_text("mid-delivery")
    _mod.notify_discord_dm(Q_AB)
    _mod.notify_discord_dm(Q_B)
    ok("another producer's proactive body is untouched", foreign.exists())
    ok("a claimed .sending body is untouched", claimed.exists())

# T7: an empty results/ is the first-run case — no predecessor to remove.
with tempfile.TemporaryDirectory() as td:
    _mod.RESULTS_DIR = Path(td)
    _mod.notify_discord_dm(Q_AB)
    ok("first reminder is written when there is nothing to supersede",
       len(list(Path(td).glob("proactive-pending-q-*.txt"))) == 1)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(0 if _failed == 0 else 1)
