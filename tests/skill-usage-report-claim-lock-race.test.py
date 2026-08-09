#!/usr/bin/env python3
"""Primary-claim race regression (sonichi#2180 review, Codex [P1]).

The reported race, reproduced verbatim by the reviewer:

  1. an async hook opens the ACTIVE log for append
  2. the reporter renames that log to `.reporting` (the claim)
  3. the hook's still-open fd — now pointing at the renamed inode — writes
  4. the reporter has already read to EOF, so the record is not posted
  5. `pending.unlink()` destroys it

Net: an event that arrived during a report is neither posted nor folded back,
breaking this feature's central contract. The existing recovery-race test covers
the FOLD-BACK window; nothing covered this primary `log.rename(pending)` window.

The fix is a shared advisory lock (`skills/skill-usage-report/usage_lock.py`)
held by the hook across open+write and by the reporter across the rename. This
test asserts the lock actually serialises those two, i.e. that step 1 followed by
step 2 cannot interleave.

Deliberately tests the SHIPPED module, not a re-implementation — a copy of the
protocol would pass while the real one is broken, and for a lock the two sides
agreeing is the entire point.

Runs on stock macOS Python 3.9 and on 3.12.
"""

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

from usage_lock import claim_lock, lock_path  # noqa: E402


class ClaimLockProtocol(unittest.TestCase):
    def test_lock_file_is_a_stable_sibling(self):
        """The lock must NOT be the log itself — the log gets renamed and
        unlinked out from under both sides, so locking it would have the two
        parties holding different inodes while believing they were synchronised.
        """
        log = Path("/tmp/x/state/skill-usage-log.jsonl")
        lp = lock_path(log)
        self.assertEqual(lp.parent, log.parent)
        self.assertNotEqual(lp, log)
        self.assertTrue(lp.name.endswith(".lock"))

    def test_second_holder_is_refused_while_first_holds(self):
        """The core property. If this ever yields True twice, the hook and the
        reporter can both be inside their critical sections and the race is back.
        """
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "skill-usage-log.jsonl"
            with claim_lock(log) as first:
                self.assertTrue(first, "first holder should acquire")
                # Same process cannot prove exclusion (flock is per-open-file
                # description), so contend from a CHILD process — which is the
                # real topology: hook and reporter are separate processes.
                code = (
                    "import sys;sys.path.insert(0,%r)\n"
                    "from usage_lock import claim_lock\n"
                    "from pathlib import Path\n"
                    "with claim_lock(Path(%r)) as ok:\n"
                    "    sys.exit(0 if ok else 3)\n" % (str(SKILL), str(log))
                )
                r = subprocess.run([sys.executable, "-c", code], capture_output=True)
            self.assertEqual(
                r.returncode, 3,
                "a second process acquired the lock while the first held it — "
                "hook and reporter can interleave and the claim race is open",
            )

    def test_lock_is_released_after_the_block(self):
        """A lock that is never released would wedge every subsequent hook —
        the failure mode that turns a durability fix into an outage."""
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
            r = subprocess.run([sys.executable, "-c", code], capture_output=True)
            self.assertEqual(r.returncode, 0, "lock was not released")

    def test_hook_side_does_not_block_indefinitely(self):
        """A PostToolUse hook must never wait on a tool call. Non-blocking
        acquisition has to give up promptly rather than queue behind a holder."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "skill-usage-log.jsonl"
            with claim_lock(log) as first:
                self.assertTrue(first)
                code = (
                    "import sys,time;sys.path.insert(0,%r)\n"
                    "from usage_lock import claim_lock\n"
                    "from pathlib import Path\n"
                    "t=time.monotonic()\n"
                    "with claim_lock(Path(%r)) as ok:\n"
                    "    pass\n"
                    "print(time.monotonic()-t)\n" % (str(SKILL), str(log))
                )
                t0 = time.monotonic()
                r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
                waited = time.monotonic() - t0
            self.assertLess(waited, 2.0, f"hook-side wait was {waited:.2f}s — too long for a hook")

    def test_unwritable_lock_dir_degrades_rather_than_raises(self):
        """Cron/hook context: an unusable lock location must yield False, not
        raise out of the caller."""
        with tempfile.TemporaryDirectory() as td:
            ro = Path(td) / "ro"
            ro.mkdir()
            os.chmod(ro, 0o500)
            try:
                with claim_lock(ro / "sub" / "skill-usage-log.jsonl") as ok:
                    self.assertFalse(ok, "expected a clean False on an unwritable lock dir")
            finally:
                os.chmod(ro, 0o700)


class BothSidesUseTheSameLock(unittest.TestCase):
    """Source-tied: the point of the fix is ONE protocol on both sides."""

    def test_hook_takes_the_lock_around_its_append(self):
        src = (SKILL / "hooks" / "log-usage.py").read_text(encoding="utf-8")
        self.assertIn("_claim_lock(log)", src, "hook does not take the claim lock")
        i_lock, i_open = src.index("_claim_lock(log)"), src.index('log.open("a"')
        self.assertLess(i_lock, i_open, "hook opens the log BEFORE taking the lock — that is the race")

    def test_reporter_takes_the_lock_around_the_rename(self):
        src = (SKILL / "scripts" / "report-usage.py").read_text(encoding="utf-8")
        self.assertIn("_claim_lock(log", src, "reporter does not take the claim lock")
        i_lock, i_ren = src.index("_claim_lock(log"), src.index("log.rename(pending)")
        self.assertLess(i_lock, i_ren, "reporter renames before locking — the claim is unsynchronised")

    def test_neither_side_reimplements_the_protocol(self):
        """A second flock call site outside usage_lock.py means the protocol has
        been forked, which is the failure this module exists to prevent."""
        for rel in ("hooks/log-usage.py", "scripts/report-usage.py"):
            src = (SKILL / rel).read_text(encoding="utf-8")
            self.assertNotIn("flock", src, f"{rel} calls flock directly instead of using usage_lock")


if __name__ == "__main__":
    unittest.main(verbosity=2)
