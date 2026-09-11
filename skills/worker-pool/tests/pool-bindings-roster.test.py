#!/usr/bin/env python3
"""Bindings are owner-authored; the roster is compiled; the router only reads.

The rules under test are the ones whose violation routes work somewhere the
owner did not ask for:

  * an absent or unreadable roster REFUSES the pass — never defaults to core,
    because a silent default aims every task at one recipient the moment the
    file is unwritable;
  * a target not in the roster fails the task by name — never substituted;
  * a target that is not `live` holds the work — never re-aimed;
  * a binding naming a nonexistent worker is refused at COMPILE time, where one
    error is visible, rather than at routing time once per task.

Run: python3 skills/worker-pool/tests/pool-bindings-roster.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pool_roster as pr  # noqa: E402

W1 = "a3f91c2d4e5b6a7c8d9e0f1a2b3c4d5e"
W2 = "b4e02d3c5f6a7b8c9d0e1f2a3b4c5d6e"


def live(*ids):
    return {i: {"label": i[:6], "state": "live"} for i in ids}


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)


class TestCompile(Base):
    def test_version_increments_so_an_assignment_can_cite_one(self):
        a = pr.compile_roster(self.ws, live(W1))
        b = pr.compile_roster(self.ws, live(W1))
        self.assertEqual(b["version"], a["version"] + 1)

    def test_a_binding_to_a_nonexistent_worker_is_refused_at_compile(self):
        """One visible error, instead of every task from that source failing."""
        with self.assertRaises(pr.RosterError) as e:
            pr.compile_roster(self.ws, live(W1), {"room:!x:ag2.space": W2})
        self.assertIn(W2, str(e.exception))

    def test_an_empty_binding_is_refused(self):
        with self.assertRaises(pr.RosterError):
            pr.compile_roster(self.ws, live(W1), {"room:!x:ag2.space": []})

    def test_an_unknown_state_is_refused(self):
        with self.assertRaises(pr.RosterError):
            pr.compile_roster(self.ws, {W1: {"state": "alive"}})

    def test_a_malformed_worker_id_is_refused(self):
        with self.assertRaises(pr.RosterError):
            pr.compile_roster(self.ws, {"../escape": {"state": "live"}})

    def test_binding_to_the_core_is_allowed(self):
        got = pr.compile_roster(self.ws, live(W1), {"room:!x:ag2.space": "core"})
        self.assertEqual(got["bindings"]["room:!x:ag2.space"], "core")

    def test_a_refused_compile_leaves_the_previous_roster_intact(self):
        first = pr.compile_roster(self.ws, live(W1))
        with self.assertRaises(pr.RosterError):
            pr.compile_roster(self.ws, live(W1), {"room:!x:ag2.space": "nope"})
        self.assertEqual(pr.load_roster(self.ws)["version"], first["version"])

    def test_the_written_roster_is_valid_json(self):
        pr.compile_roster(self.ws, live(W1))
        json.loads(pr.roster_path(self.ws).read_text(encoding="utf-8"))


class TestLoad(Base):
    def test_absent_roster_is_None_not_a_default(self):
        self.assertIsNone(pr.load_roster(self.ws))

    def test_unreadable_roster_is_None(self):
        p = pr.roster_path(self.ws)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        self.assertIsNone(pr.load_roster(self.ws))

    def test_a_roster_without_workers_is_not_a_roster(self):
        p = pr.roster_path(self.ws)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"version": 1}', encoding="utf-8")
        self.assertIsNone(pr.load_roster(self.ws))


class TestTargets(Base):
    def test_no_binding_resolves_to_the_core(self):
        r = pr.compile_roster(self.ws, live(W1))
        self.assertEqual(pr.targets_for(r, "room:!unbound:ag2.space"), ["core"])

    def test_a_binding_resolves_to_its_worker(self):
        r = pr.compile_roster(self.ws, live(W1), {"room:!x:ag2.space": W1})
        self.assertEqual(pr.targets_for(r, "room:!x:ag2.space"), [W1])

    def test_a_set_is_refused_until_members_have_their_own_result(self):
        with self.assertRaises(pr.RosterError) as e:
            pr.compile_roster(self.ws, live(W1, W2), {"room:!x:ag2.space": [W1, W2]})
        self.assertIn("fan-out", str(e.exception))

    def test_a_one_member_list_is_still_one_target(self):
        r = pr.compile_roster(self.ws, live(W1), {"room:!x:ag2.space": [W1]})
        self.assertEqual(pr.targets_for(r, "room:!x:ag2.space"), [W1])
    def test_requested_worker_outranks_the_binding(self):
        r = pr.compile_roster(self.ws, live(W1, W2), {"room:!x:ag2.space": W1})
        self.assertEqual(pr.targets_for(r, "room:!x:ag2.space", requested_worker=W2), [W2])

    def test_the_binding_key_is_the_SOURCE_not_a_task_property(self):
        """The owner declares it before any task exists, so it cannot key on one."""
        r = pr.compile_roster(self.ws, live(W1), {"room:!x:ag2.space": W1})
        self.assertEqual(pr.targets_for(r, "room:!x:ag2.space"), [W1])
        self.assertEqual(pr.targets_for(r, "discord:1234"), ["core"])


class TestValidation(Base):
    def test_an_unknown_target_is_named_not_substituted(self):
        r = pr.compile_roster(self.ws, live(W1))
        self.assertEqual(pr.unknown_targets(r, [W1, "ghost"]), ["ghost"])

    def test_the_core_is_always_known(self):
        r = pr.compile_roster(self.ws, live(W1))
        self.assertEqual(pr.unknown_targets(r, ["core"]), [])

class TestRouterInputIsClosed(Base):
    def test_a_snapshot_routes_after_the_file_is_gone(self):
        """The router's whole input is the roster it was handed, so a pass is
        replayable: delete the file and the same snapshot still resolves."""
        r = pr.compile_roster(self.ws, {W1: {"state": "live"}}, {"!x:ag2.space": W1})
        snapshot = json.loads(json.dumps(r))
        pr.roster_path(self.ws).unlink()          # the file is gone
        self.assertIsNone(pr.load_roster(self.ws))
        self.assertEqual(pr.targets_for(snapshot, "!x:ag2.space", None), [W1])
        self.assertEqual(pr.unknown_targets(snapshot, [W1]), [])


class TestLabelResolution(unittest.TestCase):
    """An envelope names a worker the way a person does — by label."""

    ROSTER = {"workers": {"a" * 32: {"state": "live", "label": "worker-1"},
                          "b" * 32: {"state": "live", "label": "worker-2"}},
              "bindings": {}}

    def test_a_label_resolves_to_its_id(self):
        self.assertEqual(pr.targets_for(self.ROSTER, "", "worker-1"), ["a" * 32])

    def test_an_id_passes_through_unchanged(self):
        self.assertEqual(pr.targets_for(self.ROSTER, "", "b" * 32), ["b" * 32])

    def test_an_unknown_label_is_left_to_fail_by_name(self):
        self.assertEqual(pr.targets_for(self.ROSTER, "", "worker-9"), ["worker-9"])
        self.assertEqual(pr.unknown_targets(self.ROSTER, ["worker-9"]), ["worker-9"])

    def test_an_ambiguous_label_never_picks_one(self):
        """Two workers sharing a label must fail the task, not silently choose."""
        r = {"workers": {"a" * 32: {"state": "live", "label": "dup"},
                         "b" * 32: {"state": "live", "label": "dup"}},
             "bindings": {}}
        self.assertEqual(pr.targets_for(r, "", "dup"), ["dup"])
        self.assertEqual(pr.unknown_targets(r, ["dup"]), ["dup"])

    def test_a_requested_worker_overrides_the_binding(self):
        r = dict(self.ROSTER, bindings={"!room:x": "b" * 32})
        self.assertEqual(pr.targets_for(r, "!room:x", "worker-1"), ["a" * 32])



class TestCorruptDeclarations(Base):
    def declare(self, text):
        p = pr.bindings_path(self.ws); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)

    def test_a_missing_file_is_a_valid_empty_declaration(self):
        self.assertEqual(pr.load_bindings(self.ws), {})
        r = pr.compile_roster(self.ws, live(W1))
        self.assertEqual(pr.targets_for(r, "!x:ag2.space"), [pr.CORE])

    def test_a_valid_empty_declaration_compiles(self):
        self.declare(json.dumps({"bindings": {}}))
        self.assertEqual(pr.compile_roster(self.ws, live(W1))["bindings"], {})

    def test_a_corrupt_file_is_refused_and_the_last_roster_survives(self):
        """Read as empty, a corrupt declaration compiled into a roster that sent
        every bound task to the core, one version up, with no error anywhere."""
        self.declare(json.dumps({"bindings": {"!x:ag2.space": W1}}))
        good = pr.compile_roster(self.ws, live(W1))
        self.assertEqual(pr.targets_for(good, "!x:ag2.space"), [W1])
        self.declare("{broken")
        with self.assertRaises(pr.RosterError):
            pr.compile_roster(self.ws, live(W1))
        kept = pr.load_roster(self.ws)
        self.assertEqual(kept["version"], good["version"])
        self.assertEqual(pr.targets_for(kept, "!x:ag2.space"), [W1])

    def test_a_mis_shaped_declaration_is_refused(self):
        for bad in ("[]", '{"bindings": []}', '"text"'):
            self.declare(bad)
            with self.assertRaises(pr.RosterError, msg=bad):
                pr.load_bindings(self.ws)

    def test_a_saved_declaration_reloads_and_compiles(self):
        pr.save_bindings(self.ws, {"!x:ag2.space": W1})
        self.assertEqual(pr.load_bindings(self.ws), {"!x:ag2.space": W1})
        self.assertEqual(json.loads(pr.bindings_path(self.ws).read_text()),
                         {"bindings": {"!x:ag2.space": W1}})
        r = pr.compile_roster(self.ws, live(W1))
        self.assertEqual(pr.targets_for(r, "!x:ag2.space"), [W1])


if __name__ == "__main__":
    unittest.main(verbosity=0)
