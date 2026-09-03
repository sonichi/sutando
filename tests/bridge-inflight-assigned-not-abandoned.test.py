#!/usr/bin/env python3
"""An in-flight id whose task file sits in the pool's ASSIGNED state is live.

The abandoned-id cleaner once hand-rolled its liveness check and missed
`.assigned-<core>`: a task waiting on a busy core was dropped from the
in-flight ledger, so its result later delivered via the orphan sweep with
the "(recovered result)" label instead of the normal path.
Run: python3 tests/bridge-inflight-assigned-not-abandoned.test.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.TemporaryDirectory()
_ROOT = Path(_TMP.name)
for d in ("tasks", "results", "state"):
    (_ROOT / d).mkdir()
os.environ["AGENT_CONNECT_TASK_DIR"] = str(_ROOT / "tasks")
os.environ["AGENT_CONNECT_RESULTS_DIR"] = str(_ROOT / "results")
os.environ["AGENT_CONNECT_STATE_DIR"] = str(_ROOT / "state")
os.environ["REMOTE_TASK_TOKEN"] = "https://unit.test/relay|not-a-real-secret"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "ag2-sparrow"))
from ag2_sparrow import remote_gateway_bridge as rgb  # noqa: E402


class AssignedIsNotAbandoned(unittest.TestCase):
    def setUp(self):
        for f in rgb.TASKS_DIR.iterdir():
            if f.is_file():
                f.unlink()

    def _two_passes(self, inflight):
        suspects = rgb._reconcile_abandoned(inflight, set())
        rgb._reconcile_abandoned(inflight, suspects)
        return inflight

    def test_assigned_state_keeps_the_inflight_entry(self):
        (rgb.TASKS_DIR / "task-a1.assigned-worker-1.txt").write_text("x")
        inflight = {"task-a1"}
        self.assertEqual(self._two_passes(inflight), {"task-a1"})

    def test_claimed_state_keeps_the_inflight_entry(self):
        (rgb.TASKS_DIR / "task-a2.claimed-worker-2.txt").write_text("x")
        self.assertEqual(self._two_passes({"task-a2"}), {"task-a2"})

    def test_truly_gone_id_is_dropped_after_two_sightings(self):
        inflight = {"task-a3"}
        self.assertEqual(self._two_passes(inflight), set())


if __name__ == "__main__":
    unittest.main(verbosity=1)
