#!/usr/bin/env python3
"""pool_delivery.pending() lists its folder without a lock, so a sentinel can be
withdrawn between the listing and the stat that orders it. A vanished entry is
simply no longer pending; any other stat failure is not an answer and must raise.

The disappearance is produced inside the stat call itself, not by a racing thread.
"""
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills/worker-pool/scripts"))
import pool_delivery as pd

W = "17c6c322"
REAL_STAT = Path.stat


def gone(path) -> None:
    # os.unlink, not Path.exists()/unlink(): those stat, and stat is what is patched.
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


class PendingListing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pool-pending-vanish-")
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)
        self.inbox = self.ws / "deliveries" / W
        self.inbox.mkdir(parents=True)
        # Delivery order is the reverse of name order, so a name sort cannot pass for an mtime sort.
        self.paths = {}
        for age, tid in enumerate(("task-c", "task-b", "task-a")):
            p = self.inbox / f"{tid}.txt"
            os.close(os.open(p, os.O_CREAT | os.O_EXCL))
            os.utime(p, (1_000_000 + age, 1_000_000 + age))
            self.paths[tid] = p

    def stat_that(self, victim: Path, effect):
        def fake(path, *a, **kw):
            if Path(path) == victim:
                effect(path)
            return REAL_STAT(path, *a, **kw)
        return mock.patch.object(Path, "stat", fake)

    def test_the_baseline_is_delivery_order(self):
        self.assertEqual([p.name for p in pd.pending(self.ws, W)],
                         ["task-c.txt", "task-b.txt", "task-a.txt"])

    def test_an_entry_withdrawn_after_the_listing_is_dropped_and_the_rest_keep_their_order(self):
        victim = self.paths["task-b"]
        seen = []

        def withdraw(path):
            seen.append(path)
            gone(victim)

        with self.stat_that(victim, withdraw):
            got = pd.pending(self.ws, W)
        self.assertTrue(seen, "the victim was never stat'ed: this test exercised nothing")
        self.assertEqual([p.name for p in got], ["task-c.txt", "task-a.txt"])

    def test_every_entry_withdrawn_is_an_empty_listing(self):
        def withdraw_all(_path):
            for p in self.paths.values():
                gone(p)

        with self.stat_that(self.paths["task-c"], withdraw_all):
            got = pd.pending(self.ws, W)
        # task-c is stat'ed at some point whatever the listing order; nothing survives it.
        self.assertEqual(got, [])

    def test_a_stat_that_is_refused_still_raises(self):
        """EACCES says nothing about whether the sentinel is pending: dropping it
        would report a worker's owed task as not there."""
        victim = self.paths["task-b"]

        def refuse(path):
            raise PermissionError(13, "Permission denied", str(path))

        with self.stat_that(victim, refuse), self.assertRaises(PermissionError):
            pd.pending(self.ws, W)

    def test_any_other_oserror_still_raises(self):
        victim = self.paths["task-a"]

        def eio(path):
            raise OSError(5, "Input/output error", str(path))

        with self.stat_that(victim, eio), self.assertRaises(OSError) as ctx:
            pd.pending(self.ws, W)
        self.assertEqual(ctx.exception.errno, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
