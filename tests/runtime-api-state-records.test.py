#!/usr/bin/env python3
"""One owner for the workspace state records the runtime-API views project.

The views read files other processes write. `[]` is valid JSON, so an
exception guard around json.loads does not stop `.get` from AttributeError-ing
— every public diagnostic RPC failed on one malformed-but-parseable record.

Run: python3 tests/runtime-api-state-records.test.py
"""
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))
sys.path.insert(0, str(ROOT / "src"))

from state_records import read_beat, read_record  # noqa: E402
from agents_view import AgentsView  # noqa: E402

from runtime_view import RuntimeView  # noqa: E402
from identity_view import IdentityView  # noqa: E402

HOST = "h1"


class ReadRecordContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.f = Path(self.tmp.name) / "rec.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_object_is_returned(self):
        self.f.write_text(json.dumps({"status": "idle"}))
        self.assertEqual(read_record(self.f), {"status": "idle"})

    def test_empty_object_is_not_the_same_as_unknown(self):
        # {} means "read it, nothing in it"; None means "cannot project".
        self.f.write_text("{}")
        self.assertEqual(read_record(self.f), {})

    def test_valid_but_non_object_json_is_unknown(self):
        for raw in ("[]", '"a string"', "3", "null", "true"):
            with self.subTest(raw=raw):
                self.f.write_text(raw)
                self.assertIsNone(read_record(self.f))

    def test_absent_and_malformed_are_unknown(self):
        self.assertIsNone(read_record(self.f))          # never written
        self.f.write_text("{not json")
        self.assertIsNone(read_record(self.f))

    def test_beat_of_a_non_object_carries_no_age(self):
        self.f.write_text("[]")
        self.assertEqual(read_beat(self.f), {})

    def test_beat_of_an_object_carries_the_age(self):
        self.f.write_text(json.dumps({"host": HOST}))
        beat = read_beat(self.f, now_fn=lambda: Path(self.f).stat().st_mtime + 5)
        self.assertEqual(beat["host"], HOST)
        self.assertEqual(beat["beatAgeS"], 5.0)


class ViewsSurviveMalformedRecords(unittest.TestCase):
    """The reviewer's exact inputs, driven through the production views."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        (self.state / "cores").mkdir(parents=True)
        self.beat = self.state / "cores" / f"{HOST}.alive"
        self.cs = self.state / "core-status.json"
        self.beat.write_text(json.dumps({"host": HOST}))
        self.cs.write_text(json.dumps({"status": "idle"}))

    def tearDown(self):
        self.tmp.cleanup()

    def _drive(self):
        AgentsView(self.state).list_agents()
        IdentityView(self.state, HOST).status()
        rv = RuntimeView(self.state, HOST)
        rv.health()
        rv.details()

    def test_positive_control_well_formed_records_project(self):
        # Without this the cases below could pass on views that do nothing.
        agents = AgentsView(self.state).list_agents()
        self.assertIn(HOST, json.dumps(agents))
        self.assertEqual(IdentityView(self.state, HOST).status()["status"], "idle")

    def test_non_object_heartbeat_does_not_break_any_projection(self):
        self.beat.write_text("[]")
        self._drive()

    def test_non_object_core_status_does_not_break_any_projection(self):
        self.cs.write_text("[]")
        self._drive()
        self.assertEqual(IdentityView(self.state, HOST).status()["status"], "unknown")

    def test_scalar_records_do_not_break_any_projection(self):
        self.beat.write_text('"beat"')
        self.cs.write_text("42")
        self._drive()


class OneOwnerNotThreeCopies(unittest.TestCase):
    """Delegation asserted on the BINDING, not on source text. A grep for
    `json.loads` fires on the other records identity_view still reads itself,
    and patching state_records does not reach a name already imported by a
    view — the binding identity is what actually says "one owner"."""

    def test_every_view_binds_the_owner_not_its_own_copy(self):
        import state_records
        import agents_view
        import identity_view
        import runtime_view
        for mod in (agents_view, identity_view, runtime_view):
            with self.subTest(view=mod.__name__):
                self.assertIs(mod.read_record, state_records.read_record)
        for mod in (identity_view, runtime_view):
            with self.subTest(view=mod.__name__, fn="read_beat"):
                self.assertIs(mod.read_beat, state_records.read_beat)


if __name__ == "__main__":
    unittest.main(verbosity=2)
