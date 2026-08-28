#!/usr/bin/env python3
"""An unready live placeholder must not mask a delivered archived answer.

`find_result` stops at the first path that EXISTS. Composing it with a
readiness test therefore tested only that first path: an empty or half-written
live file made a ready archived result invisible, and the lead repooled a task
whose answer had already gone out. `find_ready_result` owns both halves and
continues past an unready candidate.

Run: python3 tests/result-ready-locator.test.py   (stdlib only)
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from local_task_protocol import (find_ready_result, find_result,  # noqa: E402
                                 iter_result_candidates)
import pool_lead as pl  # noqa: E402

TASK = "task-evidence"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks, self.results, self.state = root / "tasks", root / "results", root / "state"
        for d in (self.tasks, self.results, self.state / "cores",
                  self.results / "archive" / "2026-08"):
            d.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _archived(self, body="the delivered answer\n"):
        (self.results / "archive" / "2026-08" / f"{TASK}.txt").write_text(body)

    def _live(self, body):
        (self.results / f"{TASK}.txt").write_text(body)

    def _lead(self):
        lead = pl.PoolLead(self.tasks, self.state, lambda: [], lambda inst: False)
        lead.results_dir = self.results
        return lead


class LocatorTests(Fixture):
    def test_empty_live_does_not_hide_a_ready_archived_result(self):
        self._archived()
        self._live("")
        self.assertEqual(find_result(self.results, TASK).name, f"{TASK}.txt")
        self.assertEqual(find_result(self.results, TASK).parent, self.results,
                         "find_result must still return the live path (existence)")
        found = find_ready_result(self.results, TASK)
        self.assertIsNotNone(found, "the archived answer was masked")
        self.assertEqual(found.parent.name, "2026-08")

    def test_ready_live_still_wins(self):
        self._archived("older\n")
        self._live("fresh answer\n")
        self.assertEqual(find_ready_result(self.results, TASK).parent, self.results)

    def test_no_ready_candidate_anywhere_is_none(self):
        self._live("")
        self._archived("")
        self.assertIsNone(find_ready_result(self.results, TASK))

    def test_candidates_are_enumerated_live_then_archive(self):
        self._archived()
        self._live("")
        names = [str(p.parent.name) for p in iter_result_candidates(self.results, TASK)]
        self.assertEqual(names[0], self.results.name)
        self.assertIn("2026-08", names)

    def test_malformed_id_yields_no_candidates(self):
        self.assertEqual(list(iter_result_candidates(self.results, "../etc/passwd")), [])
        self.assertIsNone(find_ready_result(self.results, "../etc/passwd"))


class LeadDispositionTests(Fixture):
    def test_reclaim_claimed_reads_delivered_despite_empty_live(self):
        (self.tasks / f"{TASK}.claimed-core-9.txt").write_text("id: %s\n" % TASK)
        self._archived()
        self._live("")
        self.assertEqual(self._lead().reclaim_claimed(),
                         [(f"{TASK}.claimed-core-9.txt", "delivered")])

    def test_reclaim_claimed_unchanged_when_live_absent(self):
        (self.tasks / f"{TASK}.claimed-core-9.txt").write_text("id: %s\n" % TASK)
        self._archived()
        self.assertEqual(self._lead().reclaim_claimed(),
                         [(f"{TASK}.claimed-core-9.txt", "delivered")])

    def test_sweep_does_not_reassign_a_task_whose_answer_was_delivered(self):
        # sweep() sees the canonical name reclaim_claimed restores; a delivered
        # task must not be handed to a follower for re-execution.
        (self.tasks / f"{TASK}.claimed-core-9.txt").write_text("id: %s\n" % TASK)
        self._archived()
        self._live("")
        lead = self._lead()
        lead.reclaim_claimed()
        assigned = [n for n, _ in lead.sweep()]
        self.assertNotIn(f"{TASK}.txt", assigned,
                         "re-executed a task after its answer was delivered")


class DelegationTests(unittest.TestCase):
    def test_watcher_uses_the_shared_locator_not_its_own_composition(self):
        s = (REPO / "src" / "watch-tasks-stream.sh").read_text()
        self.assertIn("find_ready_result", s)
        self.assertNotIn("from local_task_protocol import find_result", s)

    def test_pool_lead_uses_the_shared_locator(self):
        s = (REPO / "src" / "runtime-api" / "pool_lead.py").read_text()
        self.assertIn("find_ready_result", s)
        self.assertNotIn("from local_task_protocol import find_result\n", s)

    def test_watcher_is_valid_shell(self):
        r = subprocess.run(["bash", "-n", str(REPO / "src" / "watch-tasks-stream.sh")],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
