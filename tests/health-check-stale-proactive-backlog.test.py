#!/usr/bin/env python3
"""Regression coverage for check_stale_proactive_backlog.

A proactive body is the message nobody is waiting for, so an undelivered one
leaves no gap: the conversation it would have started simply never happens, and
that is indistinguishable from a quiet system. The two probes either side of it
both decline the case on purpose — `check_orphaned_results` excludes the
`proactive-*` family by name, and `check_proactive_quarantine` reads
`results/undelivered/`, which is where a body lands after a consumer took it and
a transport refused. A file no consumer ever claimed is in neither.

Both directions are covered. A detector that only ever warns carries no
information, so the negatives — fresh bodies, claimed bodies, other namespaces —
matter as much as the positive, and the registration case matters because a
probe that is never appended to `checks` passes its own unit test forever.

Run: python3 tests/health-check-stale-proactive-backlog.test.py
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "health_check", REPO / "src" / "health-check.py"
)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

TWO_HOURS = 7200


class StaleProactiveBacklogTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "results").mkdir()
        self._saved = hc.WORKSPACE_DIR
        hc.WORKSPACE_DIR = self.ws

    def tearDown(self) -> None:
        hc.WORKSPACE_DIR = self._saved
        self._tmp.cleanup()

    def _write(self, rel: str, age_sec: int = 0) -> Path:
        path = self.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("body")
        if age_sec:
            stamp = time.time() - age_sec
            os.utime(path, (stamp, stamp))
        return path

    # --- positive -------------------------------------------------------

    def test_stale_proactive_body_is_reported(self) -> None:
        self._write("results/proactive-1786480017.txt", TWO_HOURS)
        verdict = hc.check_stale_proactive_backlog()
        self.assertEqual(verdict["status"], "warn")
        self.assertIn("proactive-1786480017.txt", verdict["detail"])

    def test_detail_names_the_count_and_the_oldest(self) -> None:
        self._write("results/proactive-new.txt", TWO_HOURS)
        self._write("results/proactive-old.txt", TWO_HOURS * 12)
        verdict = hc.check_stale_proactive_backlog()
        self.assertIn("2 proactive", verdict["detail"])
        # The oldest is the one that tells the reader how long this has run.
        self.assertIn("proactive-old.txt", verdict["detail"])
        self.assertIn("24h", verdict["detail"])

    # --- negative -------------------------------------------------------

    def test_fresh_body_is_not_reported(self) -> None:
        # Between the write and the consumer's claim the file legitimately sits.
        self._write("results/proactive-fresh.txt")
        self.assertEqual(hc.check_stale_proactive_backlog()["status"], "ok")

    def test_claimed_body_is_not_reported(self) -> None:
        # Claim-by-rename carries the claiming pid, so match the marker only.
        self._write("results/proactive-x.sending.4242.txt", TWO_HOURS)
        self.assertEqual(hc.check_stale_proactive_backlog()["status"], "ok")

    def test_task_results_are_left_to_their_own_probe(self) -> None:
        self._write("results/task-123.txt", TWO_HOURS)
        self.assertEqual(hc.check_stale_proactive_backlog()["status"], "ok")

    def test_quarantined_body_is_left_to_its_own_probe(self) -> None:
        # results/undelivered/ is check_proactive_quarantine's territory: a
        # consumer already took this one. Counting it here double-reports it.
        self._write("results/undelivered/proactive-rejected.txt", TWO_HOURS)
        self.assertEqual(hc.check_stale_proactive_backlog()["status"], "ok")

    def test_a_directory_named_like_a_body_is_not_reported(self) -> None:
        # stat() succeeds on a directory, so without the is_file guard this is
        # reported as a body — an age, a name, and nothing to deliver.
        (self.ws / "results" / "proactive-adir.txt").mkdir()
        stamp = time.time() - TWO_HOURS
        os.utime(self.ws / "results" / "proactive-adir.txt", (stamp, stamp))
        self.assertEqual(hc.check_stale_proactive_backlog()["status"], "ok")

    def test_missing_results_dir_is_clean(self) -> None:
        hc.WORKSPACE_DIR = self.ws / "nope"
        self.assertEqual(hc.check_stale_proactive_backlog()["status"], "ok")

    # --- coverage honesty ------------------------------------------------

    def test_unreadable_entry_is_not_rounded_down_to_clean(self) -> None:
        path = self._write("results/proactive-unreadable.txt", TWO_HOURS)
        real_stat = Path.stat

        def boom(self, *a, **kw):
            if self.name == path.name:
                raise OSError("EIO")
            return real_stat(self, *a, **kw)

        Path.stat = boom
        try:
            verdict = hc.check_stale_proactive_backlog()
        finally:
            Path.stat = real_stat
        self.assertEqual(verdict["status"], "warn")
        self.assertIn("unreadable", verdict["detail"])

    # --- wiring ----------------------------------------------------------

    def test_probe_is_registered_in_the_check_list(self) -> None:
        # A probe that is never appended passes every unit test above and still
        # reports nothing on a real run.
        # assertIn would echo the whole module into the failure message.
        src = inspect.getsource(hc)
        self.assertTrue(
            "checks.append(check_stale_proactive_backlog())" in src,
            "check_stale_proactive_backlog() is never appended to `checks`, so "
            "it cannot report on a real run",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
