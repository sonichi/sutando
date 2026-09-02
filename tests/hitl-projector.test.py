"""Projector contract: CREATE then EDIT, idempotent on the revision ledger,
retry-whole on a rejected send, dedupe keys stable per (requirement, revision).
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hitl.manager import HitlManager, HitlStore  # noqa: E402
from hitl.projector import (  # noqa: E402
    fallback_body,
    project,
)
from hitl.schema import Action, HumanRequirement, WIRE_FIELD  # noqa: E402

ROOM = "!room:ag2.space"


def make_req(**kw):
    defaults = dict(
        kind="auth",
        runtime="claude",
        message="Claude Code needs to sign in again",
        guard="g1",
        device={"id": "d1", "name": "Qingyun's Air"},
        actions=[Action(id="reauth", kind="authenticate", label="Re-authenticate")],
    )
    defaults.update(kw)
    return HumanRequirement(**defaults)


class RecordingSender:
    def __init__(self):
        self.sent = []
        self.fail_next = False
        self.counter = 0

    def __call__(self, payload):
        self.sent.append(payload)
        if self.fail_next:
            self.fail_next = False
            return {"ok": False}
        self.counter += 1
        return {"ok": True, "event_id": f"$ev{self.counter}"}


class ProjectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = HitlManager(HitlStore(Path(self.tmp.name)))
        self.send = RecordingSender()

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_then_edit(self):
        req = self.mgr.create(make_req())
        done = project(self.mgr, self.send, ROOM)
        self.assertEqual(done, [(req.id, "$ev1")])
        first = self.send.sent[0]
        self.assertEqual(first["op"], "message")
        self.assertEqual(first["room_id"], ROOM)
        self.assertEqual(first["dedupe_key"], f"hitl:{req.id}:1")
        wire = first["extra_content"][WIRE_FIELD]
        self.assertEqual(wire["status"], "pending")
        self.assertEqual(wire["revision"], 1)
        self.assertIn("Sutando needs your attention", first["body"])
        self.assertIn("Qingyun's Air", first["body"])

        self.mgr.resolve(req.id)
        project(self.mgr, self.send, ROOM)
        edit = self.send.sent[1]
        self.assertEqual(edit["op"], "edit")
        self.assertEqual(edit["event_id"], "$ev1")  # EDIT targets the CREATE
        self.assertEqual(edit["dedupe_key"], f"hitl:{req.id}:2")
        self.assertEqual(edit["extra_content"][WIRE_FIELD]["status"], "resolved")
        self.assertIn("Resolved", edit["body"])

    def test_idempotent_between_changes(self):
        req = self.mgr.create(make_req())
        project(self.mgr, self.send, ROOM)
        self.assertEqual(project(self.mgr, self.send, ROOM), [])
        self.assertEqual(len(self.send.sent), 1)  # nothing re-sent

    def test_rejected_send_retries_whole(self):
        req = self.mgr.create(make_req())
        self.send.fail_next = True
        self.assertEqual(project(self.mgr, self.send, ROOM), [])
        self.assertTrue(self.mgr.needs_projection(req.id))  # nothing recorded
        done = project(self.mgr, self.send, ROOM)
        self.assertEqual(done, [(req.id, "$ev1")])
        # Same dedupe key on both attempts: the gateway absorbs a send that
        # failed after landing. (The fake numbers successes only, hence $ev1.)
        self.assertEqual(self.send.sent[0]["dedupe_key"], self.send.sent[1]["dedupe_key"])

    def test_edit_target_survives_multiple_transitions(self):
        req = self.mgr.create(make_req())
        project(self.mgr, self.send, ROOM)
        loaded = self.mgr.get(req.id)
        loaded.refresh_guard("g2")
        self.mgr.store.save(loaded)
        project(self.mgr, self.send, ROOM)
        self.mgr.resolve(req.id)
        project(self.mgr, self.send, ROOM)
        ops = [(p["op"], p.get("event_id")) for p in self.send.sent]
        self.assertEqual(ops, [("message", None), ("edit", "$ev1"), ("edit", "$ev1")])

    def test_fallback_body_terminal_states(self):
        req = make_req()
        req.transition("cancelled")
        self.assertIn("Cancelled", fallback_body(req))


if __name__ == "__main__":
    unittest.main()
