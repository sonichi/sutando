#!/usr/bin/env python3
"""Tests for src/check-pending-questions.py — stable reminder filenames.

Discord-DM reminder files (`proactive-pending-q-*.txt`) are named from
`questions_key()`, a hash of the sorted pending-question set. Covers: the key
is order-independent and stable for a given set, changes when a question is
added or answered, and that a reminder supersedes any earlier undelivered one
— ours only, never a `.sending` NAME, and never one written after we looked.

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

# T8: the sweep must not take a body that appeared AFTER we enumerated. Two
# overlapping runs that each glob post-write delete each other's file, and zero
# notifications is worse than the stale one this supersede exists to remove.
with tempfile.TemporaryDirectory() as td:
    _mod.RESULTS_DIR = Path(td)
    rival = Path(td) / f"proactive-pending-q-{_mod.questions_key(Q_ABC)}.txt"
    _real_write = Path.write_text
    _real_fdopen = _mod.os.fdopen

    def _spawn_rival():
        # Stands in for an overlapping run finishing while we write. Hooked on
        # both body-write forms so it fires whichever one the writer uses.
        if not rival.exists():
            _real_write(rival, "the other run's snapshot")

    def _racing_write(self, *a, **kw):
        _spawn_rival()
        return _real_write(self, *a, **kw)

    def _racing_fdopen(*a, **kw):
        _spawn_rival()
        return _real_fdopen(*a, **kw)

    Path.write_text = _racing_write
    _mod.os.fdopen = _racing_fdopen
    try:
        _mod.notify_discord_dm(Q_AB)
    finally:
        Path.write_text = _real_write
        _mod.os.fdopen = _real_fdopen
    ok("a body written after the enumeration survives the sweep", rival.exists())
    ok("our own reminder is still written",
       (Path(td) / f"proactive-pending-q-{_mod.questions_key(Q_AB)}.txt").exists())

# T9: the body must appear at its deliverable name in one step. Bridges claim
# proactive-*.txt by rename the moment they see it, so a truncate-then-write
# leaves a window where the claimable file is empty — a DM with no body.
with tempfile.TemporaryDirectory() as td:
    _mod.RESULTS_DIR = Path(td)
    seen_incomplete = []
    _real_open = Path.open
    _real_fdopen = _mod.os.fdopen
    _real_replace = _mod.os.replace
    _looking = False

    def _poll():
        # What a bridge iterating results/ would see at this instant.
        global _looking
        if _looking:
            return
        _looking = True
        try:
            for p in Path(td).iterdir():
                if p.name.startswith("proactive-") and p.suffix == ".txt":
                    with _real_open(p) as g:
                        if not g.read().startswith("⚠️"):
                            seen_incomplete.append(p.name)
        finally:
            _looking = False

    def _observing_open(self, *a, **kw):
        # Runs the bridge's poll inside the real gap, not a simulated one.
        fh = _real_open(self, *a, **kw)
        if "w" in kw.get("mode", a[0] if a else ""):
            _poll()
        return fh

    def _observing_fdopen(*a, **kw):
        fh = _real_fdopen(*a, **kw)
        _poll()  # body still unwritten: the deliverable name must not exist yet
        return fh

    def _observing_replace(src, dst):
        _poll()  # last instant before the deliverable name appears
        return _real_replace(src, dst)

    Path.open = _observing_open
    _mod.os.fdopen = _observing_fdopen
    _mod.os.replace = _observing_replace
    try:
        _mod.notify_discord_dm(Q_AB)
    finally:
        Path.open = _real_open
        _mod.os.fdopen = _real_fdopen
        _mod.os.replace = _real_replace
    ok("a claimable body is never observed incomplete", not seen_incomplete)
    ok("no scratch file is left behind",
       {f.name for f in Path(td).iterdir()} ==
       {f"proactive-pending-q-{_mod.questions_key(Q_AB)}.txt"})

# T10: the scratch name must be unique per writer. A name derived from the
# deliverable is the SAME for two runs of the same question set — the shape this
# PR defends against — so one can truncate the body the other is about to publish.
with tempfile.TemporaryDirectory() as td:
    _mod.RESULTS_DIR = Path(td)
    scratch = []
    _real_replace = _mod.os.replace

    def _recording_replace(src, dst):
        scratch.append(Path(src).name)
        return _real_replace(src, dst)

    _mod.os.replace = _recording_replace
    try:
        _mod.notify_discord_dm(Q_AB)
        _mod.notify_discord_dm(Q_AB)  # same set -> same deliverable name
    finally:
        _mod.os.replace = _real_replace
    ok("two runs of the same set use distinct scratch names",
       len(scratch) == 2 and len(set(scratch)) == 2)
    ok("the scratch name cannot be claimed as a deliverable",
       all(n.startswith(".") and not n.endswith(".txt") for n in scratch))

# T10b: a unique scratch name accumulates unless the writer cleans up after a
# failed publish. A stray .tmp per crashed run is the cost of dropping the
# deterministic name; removing it on the failure path is what pays it.
with tempfile.TemporaryDirectory() as td:
    _mod.RESULTS_DIR = Path(td)
    _real_replace = _mod.os.replace

    def _failing_replace(src, dst):
        raise OSError("publish failed")

    _mod.os.replace = _failing_replace
    try:
        _mod.notify_discord_dm(Q_AB)
    except OSError:
        pass
    finally:
        _mod.os.replace = _real_replace
    ok("a failed publish leaves no scratch file behind",
       list(Path(td).iterdir()) == [])


# notify_key: the COOLDOWN discriminator, distinct from the collapse id. Every
# consumer renders an ORDERED prefix, which the order-free set hash cannot see.

def _qs(*titles):
    return [{"title": t} for t in titles]


_BASE = _qs("a", "b", "c", "d", "e", "f", "g")
_PROMOTED = _qs("a", "b", "z", "c", "d", "e", "f")
_SWAP_TOP = _qs("a", "c", "b", "d", "e", "f", "g")
_SWAP_DEEP = _qs("a", "b", "c", "d", "e", "g", "f")
_PLUS_ONE = _qs("a", "b", "c", "d", "e", "f", "g", "h")

ok("collapse id stays order-independent (the proactive filename must not move)",
   _mod.questions_key(_BASE) == _mod.questions_key(_SWAP_TOP))

ok("reorder INSIDE the rendered prefix changes notify_key",
   _mod.notify_key(_BASE) != _mod.notify_key(_SWAP_TOP))

# The control: "hash the whole ordered list" also passes the case above, then
# re-notifies on shuffles the owner cannot see. This is what rejects it.
ok("reorder BELOW the rendered prefix does NOT change notify_key",
   _mod.notify_key(_BASE) == _mod.notify_key(_SWAP_DEEP))

ok("a membership change still changes notify_key (this can only widen)",
   _mod.notify_key(_BASE) != _mod.notify_key(_PLUS_ONE))

ok("notify_key is deterministic",
   _mod.notify_key(_BASE) == _mod.notify_key(_BASE))

ok("promotion into the prefix is caught even when the set also changed",
   _mod.notify_key(_BASE) != _mod.notify_key(_PROMOTED))


print(f"\n{_passed} passed, {_failed} failed")
sys.exit(0 if _failed == 0 else 1)