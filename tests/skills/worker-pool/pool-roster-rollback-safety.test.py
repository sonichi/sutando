#!/usr/bin/env python3
"""A roster this build writes, read by the build that came before it.

Rolling the code back does not roll the workspace back: `state/roster.json` is
already on disk in the new shape when the older router starts reading it. A
bound SET persisted as a LIST is accepted by that older reader as a fan-out, so
both members receive the same task, run it, and race one result path — the exact
outcome a set is supposed to make impossible.

So the persisted set is compiled to ONE name under a prefix `WORKER_ID_RE` can
never produce. The older reader resolves it to nothing, its own rule sends the
task to the core, and no worker runs anything twice.

The reader below is the one this build replaces, copied verbatim so the claim is
tested against it rather than against a description of it; the first test is its
positive control, and it fails if the copy is not the fan-out reader.

Run: python3 tests/skills/worker-pool/pool-roster-rollback-safety.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import pool_roster as pr  # noqa: E402

W1 = "a3f91c2d4e5b6a7c8d9e0f1a2b3c4d5e"
W2 = "b4e02d3c5f6a7b8c9d0e1f2a3b4c5d6e"
SRC = "!abc:ag2.space"


def _parent_resolve_label(roster, name):
    workers = roster.get("workers") or {}
    if name in workers or name == "core":
        return name
    hits = [wid for wid, row in workers.items() if (row or {}).get("label") == name]
    return hits[0] if len(hits) == 1 else name


def _parent_members(bound):
    """The parent's own destructuring of a binding value, shared by its compile
    guard and its router."""
    return list(bound) if isinstance(bound, list) else [bound]


def _parent_targets_for(roster, source, requested_worker=None):
    if requested_worker:
        return [_parent_resolve_label(roster, requested_worker)]
    bound = (roster.get("bindings") or {}).get(source)
    if bound is None:
        return ["core"]
    return list(bound) if isinstance(bound, list) else [bound]


def _parent_unknown_targets(roster, targets):
    known = set(roster.get("workers") or {}) | {"core"}
    return [t for t in targets if t not in known]


def _parent_decision(roster, source):
    """The parent route handler's classify, reduced to its branch: DECLINE hands
    the task to the core, 0 admits every target as a recipient."""
    targets = _parent_targets_for(roster, source)
    if targets == ["core"] or _parent_unknown_targets(roster, targets):
        return "decline", targets
    return "deliver", targets


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        pr.compile_roster(self.ws, {W1: {"label": "one", "state": "live"},
                                    W2: {"label": "two", "state": "live"}}, {})

    def head_roster(self):
        """The file the production writer leaves on disk, read back as bytes —
        not the dict it returned, which is what a rolled-back build would read."""
        pr.bind_room(self.ws, SRC, [W1, W2])
        return json.loads(pr.roster_path(self.ws).read_text(encoding="utf-8"))


class TestTheParentReader(Base):
    def test_control_it_fans_out_on_the_shape_this_change_replaced(self):
        """Without this the assertions below could not fail: the copied reader
        really does deliver a list to every member."""
        legacy = {"workers": {W1: {"state": "live"}, W2: {"state": "live"}},
                  "bindings": {SRC: [W1, W2]}}
        self.assertEqual(_parent_decision(legacy, SRC), ("deliver", [W1, W2]))

    def test_a_head_produced_set_leaves_it_with_no_worker_to_deliver_to(self):
        decision, targets = _parent_decision(self.head_roster(), SRC)
        self.assertEqual(decision, "decline")
        self.assertEqual(len(targets), 1, "the parent reader saw a fan-out")
        self.assertNotIn(W1, targets)
        self.assertNotIn(W2, targets)

    def test_an_addressed_member_still_reaches_that_member_alone(self):
        """Rollback must fail CLOSED, not break: addressing by name predates
        sets and keeps working, and it names one recipient."""
        r = self.head_roster()
        self.assertEqual(_parent_targets_for(r, SRC, requested_worker=W2), [W2])

    def test_the_parent_compile_guard_refuses_the_declaration_too(self):
        """bindings.json keeps the owner's ordered list, which the parent's
        fan-out guard (`len(members) > 1`) refuses rather than recompiles."""
        self.head_roster()
        declared = pr.load_bindings(self.ws)[SRC]
        self.assertGreater(len(_parent_members(declared)), 1)

    def test_the_parent_compile_guard_also_refuses_the_compiled_name(self):
        """And if it reaches compile by the roster instead, the one name it
        destructures to is not a worker, so its `missing` check refuses."""
        r = self.head_roster()
        members = _parent_members(r["bindings"][SRC])
        self.assertEqual(len(members), 1)
        self.assertNotIn(members[0], set(r["workers"]) | {"core"})


class TestTheRepresentation(Base):
    def test_a_compiled_set_is_not_a_list_and_not_a_name_any_reader_knows(self):
        value = self.head_roster()["bindings"][SRC]
        self.assertNotIsInstance(value, list)
        self.assertIsNone(pr.WORKER_ID_RE.match(value),
                          "a set compiled to something a reader can mistake for a worker id")
        self.assertTrue(value.startswith(pr.SET_PREFIX))

    def test_a_singleton_binding_is_unchanged_by_this_encoding(self):
        pr.bind_room(self.ws, SRC, W1)
        r = json.loads(pr.roster_path(self.ws).read_text(encoding="utf-8"))
        self.assertEqual(r["bindings"][SRC], W1)
        self.assertEqual(_parent_decision(r, SRC), ("deliver", [W1]))

    def test_this_build_reads_its_own_set_back_off_disk(self):
        r = self.head_roster()
        self.assertEqual(pr.members_of(r["bindings"][SRC]), [W1, W2])
        self.assertEqual(pr.targets_for(r, SRC), [W1])
        self.assertEqual(pr.targets_for(r, SRC, requested_worker=W2), [W2])


if __name__ == "__main__":
    unittest.main(verbosity=0)
