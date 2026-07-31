#!/usr/bin/env python3
"""Overlapping-reporter regression (sonichi#2180 review, second [P1]).

The reported failure, and it is a direct consequence of the FIRST fix: the claim
lock is released right after `log.rename(pending)` so the hook never waits on the
POST. That left reporter-vs-reporter unguarded, and the claim FILENAME is not an
ownership marker:

    A: rename -> pending, release claim lock, begin POST
    B: starts, sees `.reporting`, treats it as a CRASHED run, folds A's live
       claim back into the active log, re-POSTs the same events, unlinks it
    A: finishes POST, `pending.unlink()` -> FileNotFoundError -> exit 1

So: duplicate reports, a destroyed claim, and a reporter that no longer always
exits 0 — from a cron overlapping a manual run.

Fix under test: `reporter_run_lock`, a SEPARATE lock file held for the entire
reporter run. Separate is the whole point — it can be held across the POST
without a hook ever contending for it, because hooks only take `claim_lock`.

Tests the SHIPPED module. Runs on stock macOS 3.9 and on 3.12.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "skills" / "skill-usage-report"
sys.path.insert(0, str(SKILL))

from usage_lock import claim_lock, lock_path, reporter_run_lock, run_lock_path  # noqa: E402


def _child_tries_run_lock(log: Path) -> int:
    """Second reporter, in a real second process (flock is per-open-file-description)."""
    code = (
        "import sys;sys.path.insert(0,%r)\n"
        "from usage_lock import reporter_run_lock\n"
        "from pathlib import Path\n"
        "with reporter_run_lock(Path(%r)) as owned:\n"
        "    sys.exit(0 if owned else 3)\n" % (str(SKILL), str(log))
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True).returncode


class ReporterOverlap(unittest.TestCase):
    def test_second_reporter_is_excluded(self):
        """The core property: while A holds the run lock, B cannot start."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "skill-usage-log.jsonl"
            with reporter_run_lock(log) as owned:
                self.assertTrue(owned, "first reporter should own the run")
                rc = _child_tries_run_lock(log)
            self.assertEqual(
                rc, 3,
                "a second reporter acquired the run lock while the first held it — "
                "it can fold the live claim back and double-post",
            )

    def test_lock_released_so_the_next_run_proceeds(self):
        """A run lock that outlives its run would wedge the cron permanently."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "skill-usage-log.jsonl"
            with reporter_run_lock(log) as owned:
                self.assertTrue(owned)
            self.assertEqual(_child_tries_run_lock(log), 0, "run lock was not released")

    def test_run_lock_is_a_DIFFERENT_file_from_the_claim_lock(self):
        """If these collided, holding the run lock across the POST would block
        every hook — reintroducing the latency bug the first fix avoided."""
        log = Path("/tmp/x/state/skill-usage-log.jsonl")
        self.assertNotEqual(run_lock_path(log), lock_path(log))
        self.assertTrue(run_lock_path(log).name.endswith(".runlock"))

    def test_holding_the_run_lock_does_not_block_a_hook(self):
        """The property that makes it safe to hold across a 20s POST: a hook
        contends only for claim_lock, so it proceeds normally."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "skill-usage-log.jsonl"
            with reporter_run_lock(log) as owned:
                self.assertTrue(owned)
                code = (
                    "import sys;sys.path.insert(0,%r)\n"
                    "from usage_lock import claim_lock\n"
                    "from pathlib import Path\n"
                    "with claim_lock(Path(%r)) as ok:\n"
                    "    sys.exit(0 if ok else 3)\n" % (str(SKILL), str(log))
                )
                rc = subprocess.run([sys.executable, "-c", code], capture_output=True).returncode
            self.assertEqual(
                rc, 0,
                "a hook was blocked by the reporter-run lock — the two locks are "
                "not actually independent, so holding one across the POST adds "
                "HTTP latency to a PostToolUse hook",
            )

    def test_claim_lock_still_excludes_independently(self):
        """Control: the first fix must still hold. If this passes trivially the
        suite would be blind to a regression in the ORIGINAL lock."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "skill-usage-log.jsonl"
            with claim_lock(log) as ok:
                self.assertTrue(ok)
                code = (
                    "import sys;sys.path.insert(0,%r)\n"
                    "from usage_lock import claim_lock\n"
                    "from pathlib import Path\n"
                    "with claim_lock(Path(%r)) as ok:\n"
                    "    sys.exit(0 if ok else 3)\n" % (str(SKILL), str(log))
                )
                rc = subprocess.run([sys.executable, "-c", code], capture_output=True).returncode
            self.assertEqual(rc, 3, "claim lock no longer excludes — first fix regressed")


class SourceTied(unittest.TestCase):
    def test_reporter_takes_the_run_lock_before_doing_any_work(self):
        src = (SKILL / "scripts" / "report-usage.py").read_text(encoding="utf-8")
        self.assertIn("reporter_run_lock(log)", src, "reporter does not take the run lock")
        i_run = src.index("reporter_run_lock(log)")
        i_report = src.index("return _report(")
        self.assertLess(i_run, i_report, "reporter starts work before taking the run lock")

    def test_second_reporter_exits_zero_rather_than_failing(self):
        """A cron overlapping a manual run is ordinary, not an error."""
        src = (SKILL / "scripts" / "report-usage.py").read_text(encoding="utf-8")
        self.assertIn("another report is in flight", src)
        seg = src[src.index("another report is in flight"):]
        self.assertIn("return 0", seg[:400], "the skip path does not exit 0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
