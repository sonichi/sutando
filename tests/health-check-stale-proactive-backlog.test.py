#!/usr/bin/env python3
"""A probe never appended to `checks` passes every test in this file and
still reports nothing on a real run — hence the registration test."""

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

# The literal claim the bridges produce: with_suffix REPLACES .txt, so the
# claimed name carries no .txt and no pid. Hand-writing it invents a shape.
CLAIMED = Path("proactive-1786480017.txt").with_suffix(".sending").name


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

    def test_an_abandoned_claim_is_reported(self) -> None:
        # The shape a *.txt glob cannot see: a consumer took it and died. Not
        # in results/undelivered/, and restored only at the next restart.
        self._write(f"results/{CLAIMED}", TWO_HOURS * 3)
        verdict = hc.check_stale_proactive_backlog()
        self.assertEqual(verdict["status"], "warn")
        self.assertIn(CLAIMED, verdict["detail"])
        self.assertIn("abandoned mid-send", verdict["detail"])

    def test_both_shapes_are_reported_together(self) -> None:
        self._write("results/proactive-unclaimed.txt", TWO_HOURS)
        self._write(f"results/{CLAIMED}", TWO_HOURS * 3)
        detail = hc.check_stale_proactive_backlog()["detail"]
        self.assertIn("proactive-unclaimed.txt", detail)
        self.assertIn(CLAIMED, detail)

    def test_detail_carries_the_remedy_not_only_the_consequence(self) -> None:
        # Nothing auto-clears either shape, so a warn without the remedy is one
        # the reader eventually filters out.
        self._write("results/proactive-unclaimed.txt", TWO_HOURS)
        self._write(f"results/{CLAIMED}", TWO_HOURS * 3)
        detail = hc.check_stale_proactive_backlog()["detail"]
        self.assertIn("deliver or remove them", detail)
        self.assertIn("restart a consumer", detail)

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

    def test_a_fresh_claim_is_a_consumer_mid_send(self) -> None:
        # Seconds old: something is delivering it right now.
        self._write(f"results/{CLAIMED}")
        self.assertEqual(hc.check_stale_proactive_backlog()["status"], "ok")

    def test_a_claim_inside_its_grace_is_not_reported(self) -> None:
        # The startup sweep restores a claim, so its grace is longer: an age
        # that warns for a *.txt must stay quiet for a claim.
        self._write(f"results/{CLAIMED}", TWO_HOURS - 60)
        self.assertEqual(hc.check_stale_proactive_backlog()["status"], "ok")

    def test_an_unknown_suffix_is_not_guessed_at(self) -> None:
        # Neither side of the protocol writes this; reporting it would invent
        # a state, and the glob is now wide enough to see it.
        self._write("results/proactive-notes.md", TWO_HOURS * 12)
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

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_an_unscannable_results_dir_is_not_read_as_clean(self) -> None:
        # The per-file guard cannot cover this: the failure is on the directory
        # itself, and glob() answers [] there — the same answer as "no backlog".
        self._write("results/proactive-hidden.txt", TWO_HOURS)
        os.chmod(self.ws / "results", 0o000)
        try:
            verdict = hc.check_stale_proactive_backlog()
        finally:
            os.chmod(self.ws / "results", 0o755)
        self.assertEqual(verdict["status"], "warn")
        self.assertIn("could not scan results/", verdict["detail"])

    # --- wiring ----------------------------------------------------------

    def test_probe_is_registered_in_the_check_list(self) -> None:
        # assertIn would echo the whole module into the failure message.
        src = inspect.getsource(hc)
        self.assertTrue(
            "checks.append(check_stale_proactive_backlog())" in src,
            "check_stale_proactive_backlog() is never appended to `checks`, so "
            "it cannot report on a real run",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
