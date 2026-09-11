"""Supervisor ordering contract: detect before project, so a requirement
created and resolved across passes always projects the state the pass ends
in — and a steady state produces zero sends."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hitl.manager import HitlManager, HitlStore  # noqa: E402
from hitl.supervisor import supervise_once  # noqa: E402

LOGGED_IN = json.dumps({"loggedIn": True})
LOGGED_OUT = json.dumps({"loggedIn": False})
ROOM = "!r:ag2.space"


class Sender:
    def __init__(self):
        self.sent = []

    def __call__(self, payload):
        self.sent.append(payload)
        return {"ok": True, "event_id": f"$ev{len(self.sent)}"}


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = HitlManager(HitlStore(Path(self.tmp.name)))
        self.send = Sender()

    def tearDown(self):
        self.tmp.cleanup()

    def run_pass(self, probe_out):
        return supervise_once(self.mgr, self.send, ROOM, runner=lambda cmd: (0, probe_out))

    def test_not_ready_pass_creates_and_projects_in_one_turn(self):
        out = self.run_pass(LOGGED_OUT)
        self.assertIsNotNone(out.drove.created)
        self.assertEqual(len(out.projected), 1)
        self.assertEqual(self.send.sent[0]["op"], "message")

    def test_steady_state_sends_nothing(self):
        self.run_pass(LOGGED_OUT)
        out = self.run_pass(LOGGED_OUT)  # same guard: dedup, no revision bump
        self.assertEqual(out.projected, [])
        self.assertEqual(len(self.send.sent), 1)

    def test_recovery_pass_resolves_and_projects_resolved(self):
        first = self.run_pass(LOGGED_OUT)
        self.mgr.link_blocked_task(first.drove.created, "task-9")
        out = self.run_pass(LOGGED_IN)
        self.assertEqual(out.resumed_tasks, ["task-9"])
        edit = self.send.sent[-1]
        self.assertEqual(edit["op"], "edit")
        self.assertEqual(edit["extra_content"]["space.ag2.hitl"]["status"], "resolved")

    def test_flap_within_one_pass_projects_final_state_once(self):
        # Created and resolved between projector drives: only the final state
        # is ever sent (detect-then-project ordering).
        self.run_pass(LOGGED_OUT)
        self.run_pass(LOGGED_IN)
        ops = [p["op"] for p in self.send.sent]
        self.assertEqual(ops, ["message", "edit"])

    def test_probe_unknown_projects_pending_state_unchanged(self):
        self.run_pass(LOGGED_OUT)
        out = supervise_once(self.mgr, self.send, ROOM, runner=lambda cmd: (1, ""))
        self.assertEqual(out.projected, [])  # nothing new to say
        self.assertEqual(len(self.mgr.active()), 1)


if __name__ == "__main__":
    unittest.main()
