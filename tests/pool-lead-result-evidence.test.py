#!/usr/bin/env python3
"""Result evidence through the production PoolLead, against the shared owners.

The lead decides two destructive things from "does a result exist": whether a
pending task may be assigned, and whether a dead core's claim was delivered or
must be repooled. Both answers have to agree with the repository's result
locator and readiness owner, or the lead requeues answers that already reached
the owner and suppresses tasks whose answer never landed.

Run: python3 tests/pool-lead-result-evidence.test.py   (stdlib only)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from local_task_protocol import archive_month_dir  # noqa: E402
from pool_lead import PoolLead  # noqa: E402

TASK = "task-evidence"


class EvidenceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.state = root / "state"
        self.results = root / "results"
        for path in (self.tasks, self.state, self.results):
            path.mkdir()
        self.lead = PoolLead(
            self.tasks, self.state,
            followers_fn=lambda: ["core-1"],
            alive_fn=lambda instance: instance == "core-1",
            now_fn=lambda: 1_000.0,
            mono_fn=lambda: 500.0,
            results_dir=self.results)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, path: Path, body: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def _claim_of_a_dead_core(self):
        claim = self.tasks / f"{TASK}.claimed-core-9.txt"
        claim.write_text("task body")
        return claim

    def _reclaim(self):
        return self.lead.reclaim_claimed()


class LocatorCoversEveryArchiveLayout(EvidenceBase):
    """`find_result` owns the layouts. A lead that only knew the flat name read
    a month-partitioned answer as "never delivered" and requeued it."""

    def test_live_result_is_delivered(self):
        self._write(self.results / f"{TASK}.txt", "the answer")
        self._claim_of_a_dead_core()
        self.assertEqual(self._reclaim(),
                         [(f"{TASK}.claimed-core-9.txt", "delivered")])

    def test_month_partitioned_archive_is_delivered(self):
        # Path built by the owner of the layout, not spelled out here.
        month = archive_month_dir(self.results, "2026-08-27T00:00:00Z")
        self._write(month / f"{TASK}.txt", "already delivered answer")
        self._claim_of_a_dead_core()
        self.assertEqual(self._reclaim(),
                         [(f"{TASK}.claimed-core-9.txt", "delivered")])

    def test_flat_gateway_archive_is_delivered(self):
        self._write(self.results / "archive" / f"{TASK}-1785976425.txt",
                    "already delivered answer")
        self._claim_of_a_dead_core()
        self.assertEqual(self._reclaim(),
                         [(f"{TASK}.claimed-core-9.txt", "delivered")])

    def test_quarantined_result_keeps_its_own_disposition(self):
        self._write(self.results / "undelivered" / f"{TASK}.txt", "produced")
        self._claim_of_a_dead_core()
        # Evidence the work RAN — so it is not repooled — but it never
        # reached anyone, so calling it "delivered" would be a lie.
        self.assertEqual(self._reclaim(),
                         [(f"{TASK}.claimed-core-9.txt", "undelivered")])

    def test_no_result_anywhere_is_repooled(self):
        self._claim_of_a_dead_core()
        self.assertEqual(self._reclaim(),
                         [(f"{TASK}.claimed-core-9.txt", "repooled")])


class ReadinessIsNotExistence(EvidenceBase):
    """`read_ready_result` owns readiness. A path exists before it holds an
    answer, and treating existence as done strands the real reply."""

    def test_empty_result_is_not_evidence(self):
        self._write(self.results / f"{TASK}.txt", "")
        self._claim_of_a_dead_core()
        self.assertEqual(self._reclaim(),
                         [(f"{TASK}.claimed-core-9.txt", "repooled")])

    def test_whitespace_only_result_is_not_evidence(self):
        self._write(self.results / f"{TASK}.txt", "   \n\t\n")
        self._claim_of_a_dead_core()
        self.assertEqual(self._reclaim(),
                         [(f"{TASK}.claimed-core-9.txt", "repooled")])

    def test_torn_utf8_result_is_not_evidence(self):
        # A write observed mid-character. Readable again on a later pass.
        (self.results / f"{TASK}.txt").write_bytes(b"answer \xe4\xb8")
        self._claim_of_a_dead_core()
        self.assertEqual(self._reclaim(),
                         [(f"{TASK}.claimed-core-9.txt", "repooled")])

    def test_empty_quarantined_result_is_not_evidence(self):
        self._write(self.results / "undelivered" / f"{TASK}.txt", "")
        self._claim_of_a_dead_core()
        self.assertEqual(self._reclaim(),
                         [(f"{TASK}.claimed-core-9.txt", "repooled")])


class SweepAgreesWithTheSameOwners(EvidenceBase):
    """The assignment side reads the same evidence: a task whose answer exists
    must not be handed out again, and one whose answer does not must be."""

    def _pending(self):
        (self.tasks / f"{TASK}.txt").write_text("task body")

    def test_month_partitioned_answer_is_not_reassigned(self):
        month = archive_month_dir(self.results, "2026-08-27T00:00:00Z")
        self._write(month / f"{TASK}.txt", "already delivered answer")
        self._pending()
        self.assertEqual(self.lead.sweep(), [])
        self.assertTrue((self.tasks / f"{TASK}.txt").exists())

    def test_unready_answer_does_not_suppress_the_task(self):
        self._write(self.results / f"{TASK}.txt", "  ")
        self._pending()
        self.assertEqual(self.lead.sweep(), [(f"{TASK}.txt", "core-1")])
        self.assertTrue(
            (self.tasks / f"{TASK}.assigned-core-1.txt").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
