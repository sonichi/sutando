#!/usr/bin/env python3
"""Assignment-time attribution: the store the bridge reads by path.

The bridge cannot import this skill, so the contract that matters is the PATH.
These fixtures go through `attribution_path()` itself, so if the writer's
convention moves, this fails instead of the bridge silently losing attribution.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "worker-pool" / "scripts"))
import pool_attribution as a  # noqa: E402

W = "02e4302f00844397bac09533fc398248"


class AttributionStore(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())

    def test_records_and_reads_back(self):
        self.assertTrue(a.record(self.ws, "task-1", W))
        self.assertEqual(a.worker_for_task(self.ws, "task-1"), W)

    def test_written_where_the_bridge_looks(self):
        a.record(self.ws, "task-1", W)
        # the path the bridge hard-codes by convention, built independently here
        self.assertEqual(
            (self.ws / "state" / "attribution" / "task-1").read_text(), W)

    def test_first_write_wins_so_a_fanout_cannot_be_overwritten(self):
        other = "212e8040d38d48b5aadab0db295dc33a"
        self.assertTrue(a.record(self.ws, "task-1", W))
        self.assertFalse(a.record(self.ws, "task-1", other))
        self.assertEqual(a.worker_for_task(self.ws, "task-1"), W)

    def test_unattributed_is_None_not_the_core(self):
        self.assertIsNone(a.worker_for_task(self.ws, "task-never-assigned"))

    def test_refuses_a_non_worker_id(self):
        # "core" is not a worker; recording it would make the bridge stamp a
        # worker that never ran.
        for bad in ("core", "", "not-hex-" + "0" * 24, W[:31], W + "0"):
            self.assertFalse(a.record(self.ws, "task-x", bad), bad)
        self.assertIsNone(a.worker_for_task(self.ws, "task-x"))

    def test_corrupt_value_reads_as_unattributed(self):
        p = a.attribution_path(self.ws, "task-2")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("garbage")
        self.assertIsNone(a.worker_for_task(self.ws, "task-2"))


    def test_empty_task_id_is_not_attributed(self):
        """No id, no attribution — and never a guess."""
        with tempfile.TemporaryDirectory() as ws:
            self.assertIsNone(a.worker_for_task(ws, ""))
            self.assertFalse(a.record(ws, "", W))


class RecordingFollowsDelivery(unittest.TestCase):
    """qingyun-001 on #4359: recording before deliver_one() attributed refusals.
    A no-payload target left a record, and a later real delivery to a DIFFERENT
    worker inherited it — the bridge then stamps a reply with a worker that
    never ran, which is the failure the reader's fail-closed rule exists to
    prevent."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]
                               / "skills" / "worker-pool" / "scripts"))
        import pool_router
        self.router = pool_router
        self.ws = tempfile.mkdtemp()

    def test_refusal_is_not_recorded(self):
        """No payload -> nothing delivered -> nothing attributed."""
        tid = "task-99aabbccddeeff0011"
        self.assertEqual(self.router.deliver_one(self.ws, W, tid), "no-payload")
        self.assertIsNone(a.worker_for_task(self.ws, tid))

    def test_a_later_real_delivery_is_attributed_to_its_own_worker(self):
        """THE REGRESSION GUARD. The refused target must not poison the task."""
        W2 = "212e8040d38d48b5aadab0db295dc33a"
        tid = "task-aabbccddeeff001122"
        self.assertEqual(self.router.deliver_one(self.ws, W, tid), "no-payload")
        payload = Path(self.ws) / "tasks" / f"{tid}.txt"
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_text("task: hi\n")
        self.assertEqual(self.router.deliver_one(self.ws, W2, tid), "delivered")
        self.assertEqual(a.worker_for_task(self.ws, tid), W2)

    def test_record_is_never_half_written(self):
        """link() publishes the NAME only once the content exists, so no reader
        can see an empty record and no writer is blocked by one."""
        tid = "task-bbccddeeff00112233"
        self.assertTrue(a.record(self.ws, tid, W))
        self.assertEqual(a.attribution_path(self.ws, tid).read_text(), W)
        leftovers = [p.name for p in a.attribution_dir(self.ws).iterdir()
                     if p.name.startswith(".")]
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
