#!/usr/bin/env python3
"""`sync-conflicts-unmerged` warns on preserved peer content nobody merged back.

The count already exists: `sync-workspace.sh` prints it at exit 0, and the cron
running it is told to report only failures, so the number lands where nothing
reads it. This probe moves it to a surface that is read every pass. It never
fails — a count is not a sync failure, and an alarm that cries is the first one
silenced.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
sys.modules["hc"] = hc
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass

COUNT = "sync-conflicts: {n} file(s) hold peer content not in the live copy"


def _run_returning(stdout: str, rc: int = 0):
    def _run(*a, **k):
        return subprocess.CompletedProcess(a[0] if a else [], rc, stdout, "")
    return _run


class SyncConflictsUnmerged(unittest.TestCase):
    def _probe(self, stdout=None, run=None, repo=None):
        with tempfile.TemporaryDirectory() as td:
            return hc.check_sync_conflicts_unmerged(
                Path(td), repo_root=repo or REPO,
                run=run if run is not None else _run_returning(stdout or ""))

    def test_a_nonzero_count_warns_and_names_the_number(self):
        r = self._probe(COUNT.format(n=21))
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("21", r["detail"])
        self.assertIn("sync-conflicts-report.py", r["detail"])

    def test_the_clean_case_is_ok(self):
        self.assertEqual(self._probe("sync-conflicts: no unmerged peer content")["status"], "ok")

    def test_an_explicit_zero_is_ok(self):
        self.assertEqual(self._probe(COUNT.format(n=0))["status"], "ok")

    def test_an_unparseable_report_is_unobserved_never_zero(self):
        r = self._probe("sync-conflicts: some future wording")
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("not asserting a count", r["detail"])

    def test_a_reporter_that_cannot_run_never_fails_the_probe(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(["python3"], 60)
        r = self._probe(run=boom)
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("not asserting a count", r["detail"])

    def test_a_checkout_without_the_reporter_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            r = hc.check_sync_conflicts_unmerged(Path(td), repo_root=Path(td))
            self.assertEqual(r["status"], "ok", r)

    def test_the_default_repo_root_resolves(self):
        # Every arm above passes repo_root=, so the default branch was never
        # exercised and shipped a NameError that only a full run caught.
        with tempfile.TemporaryDirectory() as td:
            r = hc.check_sync_conflicts_unmerged(Path(td), run=_run_returning(COUNT.format(n=2)))
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("2", r["detail"])


    def test_the_probe_is_registered_in_the_run(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertEqual(src.count("checks.append(check_sync_conflicts_unmerged())"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
