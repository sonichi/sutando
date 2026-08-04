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

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
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




class ContentionPathsInProcess(unittest.TestCase):
    """Exercise the CONTENDED branches in-process.

    Why these were uncovered: every other test in this suite contends from a
    CHILD process, which is the honest topology (hook and reporter really are
    separate processes) — but coverage only measures the parent, and the parent
    always WINS the lock. So the `except OSError` / retry / give-up branches in
    usage_lock, and the "busy, skip" branches in the reporter and hook, never
    executed under instrumentation.

    They can be reached in-process because `flock` locks an OPEN FILE
    DESCRIPTION, not a file or a process: two separate `open()` calls in one
    process yield two descriptions, and LOCK_EX|LOCK_NB on the second is
    refused. That is the same refusal a second process would get, so this is
    the real branch and not a stub.
    """

    def _hold(self, path: Path):
        """Take the raw lock the way a competing process would."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(fh.close)
        return fh

    def test_claim_lock_gives_up_when_contended(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "skill-usage-log.jsonl"
            self._hold(lock_path(log))
            with claim_lock(log) as ok:
                self.assertFalse(ok, "claim_lock reported success while contended")

    def test_claim_lock_blocking_polls_then_gives_up(self):
        """The blocking path: retries until the deadline, then returns False
        rather than waiting forever — a hook must never hang on a tool call."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "skill-usage-log.jsonl"
            self._hold(lock_path(log))
            t0 = time.monotonic()
            with claim_lock(log, timeout=0.05, blocking=True) as ok:
                self.assertFalse(ok)
            self.assertLess(time.monotonic() - t0, 2.0, "blocking wait did not honour its timeout")

    def test_run_lock_gives_up_when_contended(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "skill-usage-log.jsonl"
            self._hold(run_lock_path(log))
            with reporter_run_lock(log) as owned:
                self.assertFalse(owned, "reporter_run_lock reported ownership while contended")

    def test_run_lock_degrades_on_unwritable_lock_dir(self):
        """Cron context: an unusable lock location yields False, never raises."""
        with tempfile.TemporaryDirectory() as td:
            ro = Path(td) / "ro"
            ro.mkdir()
            os.chmod(ro, 0o500)
            try:
                with reporter_run_lock(ro / "sub" / "skill-usage-log.jsonl") as owned:
                    self.assertFalse(owned)
            finally:
                os.chmod(ro, 0o700)


class SkipBranchesRunInProcess(unittest.TestCase):
    """The reporter's and hook's "busy — skip" branches.

    These are the user-visible half of the lock: what actually happens when the
    lock is NOT available. They were uncovered for the same reason as the
    contention branches — the losing side always lived in a subprocess.
    """

    def _hold(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(fh.close)
        return fh

    def _reporter(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "report_usage_mod", SKILL / "scripts" / "report-usage.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    # NOTE: main()'s own run-lock skip branch is deliberately NOT unit-tested
    # here. main() resolves the workspace itself, so there is no seam to inject
    # a temp dir, and the version of this test I first wrote asserted `rc == 0`
    # against a value it had just assigned — a test that cannot fail. That path
    # is covered end-to-end instead (documented in the PR: with the run lock
    # held, the reporter prints "another report is in flight" and exits 0).

    def test_report_leaves_the_log_intact_when_it_cannot_proceed(self):
        """Honest name, because the first one over-claimed.

        This does NOT reach _report()'s claim-lock branch: _report returns at
        the earlier vault check ("no AG2_CLOUD_TOKEN"), so the assertions below
        hold via that path, not via contention. Caught by reading the captured
        output rather than trusting the green.

        What it does still prove, and what the invariant actually is: whatever
        makes the reporter decline to run, it exits 0 and leaves the active log
        in place having claimed nothing. Lines 148-149 (claim busy) remain
        uncovered by unit test; the run-lock skip is covered end-to-end instead.
        """
        rep = self._reporter()
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td); (ws / "state").mkdir()
            log = ws / "state" / "skill-usage-log.jsonl"
            pending = log.with_suffix(".jsonl.reporting")
            log.write_text('{"slug":"probe","ts":1}\n')
            os.environ["AGENT_MXID"] = "@t:test"
            self._hold(lock_path(log))
            rc = rep._report(ws, log, pending)
            self.assertEqual(rc, 0, "busy claim must exit 0")
            self.assertTrue(log.exists(), "the active log must be left in place when busy")
            self.assertFalse(pending.exists(), "nothing should have been claimed")

    def test_hook_drops_the_record_rather_than_blocking(self):
        """log-usage: cannot acquire promptly -> exit 0, write nothing."""
        import importlib.util
        import io
        spec = importlib.util.spec_from_file_location(
            "log_usage_mod", SKILL / "hooks" / "log-usage.py")
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td); (ws / "state").mkdir()
            log = ws / "state" / "skill-usage-log.jsonl"
            self._hold(lock_path(log))
            real_stdin, real_ws = sys.stdin, hook.workspace
            sys.stdin = io.StringIO(json.dumps(
                {"tool_name": "Skill", "tool_input": {"skill": "probe"}}))
            hook.workspace = lambda: ws
            try:
                rc = hook.main()
            finally:
                sys.stdin, hook.workspace = real_stdin, real_ws
            self.assertEqual(rc, 0, "the hook must never block or fail a tool call")
            self.assertFalse(log.exists(), "the hook must not write unsynchronised")



if __name__ == "__main__":
    unittest.main(verbosity=2)
