"""Detector contract: three probe verdicts, unknown creates nothing, the
full not-ready -> ready cycle creates then resolves and returns blocked work.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hitl.detector import drive, probe_claude_auth  # noqa: E402
from hitl.manager import HitlManager, HitlStore  # noqa: E402

LOGGED_IN = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "a@b.c"})
LOGGED_OUT = json.dumps({"loggedIn": False, "authMethod": "claude.ai", "email": "a@b.c"})


def runner_returning(rc, out):
    return lambda cmd: (rc, out)


def runner_raising(cmd):
    raise OSError("no such binary")


class ProbeTests(unittest.TestCase):
    def test_ready(self):
        r = probe_claude_auth(runner_returning(0, LOGGED_IN))
        self.assertIs(r.ready, True)
        self.assertTrue(r.guard.startswith("auth:"))

    def test_not_ready(self):
        r = probe_claude_auth(runner_returning(0, LOGGED_OUT))
        self.assertIs(r.ready, False)

    def test_guard_changes_with_auth_state(self):
        a = probe_claude_auth(runner_returning(0, LOGGED_OUT)).guard
        b = probe_claude_auth(runner_returning(0, LOGGED_IN)).guard
        self.assertNotEqual(a, b)

    def test_unknown_on_garbage(self):
        self.assertIsNone(probe_claude_auth(runner_returning(0, "not json")).ready)

    def test_unknown_on_missing_field(self):
        self.assertIsNone(probe_claude_auth(runner_returning(0, '{"x": 1}')).ready)

    def test_unknown_on_missing_binary(self):
        self.assertIsNone(probe_claude_auth(runner_raising).ready)


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = HitlManager(HitlStore(Path(self.tmp.name)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_cycle_creates_then_resolves_and_resumes(self):
        out = drive(self.mgr, device={"name": "Air"}, runner=runner_returning(0, LOGGED_OUT))
        self.assertIsNotNone(out.created)
        req = self.mgr.get(out.created)
        self.assertEqual(req.kind, "auth")
        self.assertEqual(req.device["name"], "Air")

        self.mgr.link_blocked_task(req.id, "task-77")
        out2 = drive(self.mgr, runner=runner_returning(0, LOGGED_IN))
        self.assertEqual(out2.resolved, [req.id])
        self.assertEqual(out2.resumed_tasks, ["task-77"])
        self.assertEqual(self.mgr.get(req.id).status, "resolved")

    def test_repeat_not_ready_dedups(self):
        a = drive(self.mgr, runner=runner_returning(0, LOGGED_OUT))
        b = drive(self.mgr, runner=runner_returning(0, LOGGED_OUT))
        self.assertEqual(a.created, b.created)
        self.assertEqual(len(self.mgr.active()), 1)

    def test_unknown_probe_touches_nothing(self):
        drive(self.mgr, runner=runner_returning(0, LOGGED_OUT))
        out = drive(self.mgr, runner=runner_returning(1, ""))
        self.assertIsNone(out.created)
        self.assertEqual(out.resolved, [])
        self.assertEqual(len(self.mgr.active()), 1)  # still pending, not resolved

    def test_ready_with_no_active_is_noop(self):
        out = drive(self.mgr, runner=runner_returning(0, LOGGED_IN))
        self.assertEqual(out.resolved, [])
        self.assertEqual(len(self.mgr.store.all()), 0)


if __name__ == "__main__":
    unittest.main()
