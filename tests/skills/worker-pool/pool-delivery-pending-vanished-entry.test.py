#!/usr/bin/env python3
"""pool_delivery.pending() lists its folder without a lock, so a sentinel can be
withdrawn between the listing and the stat that orders it. A vanished entry is
simply no longer pending; any other stat failure is not an answer and must raise.

The disappearance is produced inside the stat call itself, not by a racing thread,
and every case runs under each of the six orders the folder listing can come back in.
"""
import contextlib
import itertools
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
REAL_ITERDIR = Path.iterdir
# Delivery order is the reverse of name order, so a name sort cannot pass for an mtime sort.
DELIVERED = ("task-c", "task-b", "task-a")
ORDERS = list(itertools.permutations(DELIVERED))


def gone(path) -> None:
    # os.unlink, not Path.exists()/unlink(): those stat, and stat is what is patched.
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


class Inbox:
    """One recipient folder with three sentinels, listed in a forced order."""

    def __init__(self, test, order):
        tmp = tempfile.TemporaryDirectory(prefix="pool-pending-vanish-")
        test.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        self.dir = self.ws / "deliveries" / W
        self.dir.mkdir(parents=True)
        self.paths = {}
        for age, tid in enumerate(DELIVERED):
            p = self.dir / f"{tid}.txt"
            os.close(os.open(p, os.O_CREAT | os.O_EXCL))
            os.utime(p, (1_000_000 + age, 1_000_000 + age))
            self.paths[tid] = p
        self.order = [self.paths[tid] for tid in order]
        self.listed = 0
        self.statted = []

    def pending(self, on_stat=lambda inbox, path: None):
        """pd.pending() with the listing forced to self.order; `on_stat` runs before each sentinel's stat."""
        def iterdir(path):
            if Path(path) != self.dir:
                return REAL_ITERDIR(path)
            self.listed += 1
            return iter(list(self.order))

        def stat(path, *a, **kw):
            if Path(path).parent == self.dir:
                self.statted.append(Path(path).name)
                on_stat(self, Path(path))
            return REAL_STAT(path, *a, **kw)

        with mock.patch.object(Path, "iterdir", iterdir), mock.patch.object(Path, "stat", stat):
            return [p.name for p in pd.pending(self.ws, W)]


class PendingListing(unittest.TestCase):
    def exercised(self, inbox):
        self.assertEqual(inbox.listed, 1, "the forced listing was not the one pending() read")
        self.assertTrue(inbox.statted, "no sentinel was stat'ed: this case exercised nothing")

    def test_the_baseline_is_delivery_order_whatever_the_listing_order(self):
        for order in ORDERS:
            with self.subTest(listing=order):
                inbox = Inbox(self, order)
                self.assertEqual(inbox.pending(), ["task-c.txt", "task-b.txt", "task-a.txt"])
                self.exercised(inbox)

    def test_the_real_listing_is_read_in_delivery_order(self):
        inbox = Inbox(self, DELIVERED)
        self.assertEqual([p.name for p in pd.pending(inbox.ws, W)],
                         ["task-c.txt", "task-b.txt", "task-a.txt"])

    def test_an_entry_withdrawn_at_its_own_stat_is_dropped_and_the_rest_keep_their_order(self):
        """The victim goes at its own stat, so it is never collected, wherever the listing puts it."""
        survivors = {"task-c": ["task-b.txt", "task-a.txt"],
                     "task-b": ["task-c.txt", "task-a.txt"],
                     "task-a": ["task-c.txt", "task-b.txt"]}
        for (victim, want), order in itertools.product(survivors.items(), ORDERS):
            with self.subTest(victim=victim, listing=order):
                inbox = Inbox(self, order)
                target = inbox.paths[victim]
                got = inbox.pending(lambda _i, path, t=target: gone(t) if path == t else None)
                self.assertEqual(got, want)
                self.assertIn(target.name, inbox.statted)
                self.exercised(inbox)

    def test_every_entry_withdrawn_at_the_first_stat_is_an_empty_listing(self):
        """Keyed on the FIRST sentinel stat, not on a name: nothing has been collected yet."""
        def withdraw_all(inbox, _path):
            if len(inbox.statted) == 1:
                for p in inbox.paths.values():
                    gone(p)

        for order in ORDERS:
            with self.subTest(listing=order):
                inbox = Inbox(self, order)
                self.assertEqual(inbox.pending(withdraw_all), [])
                self.exercised(inbox)

    def test_a_stat_that_is_refused_still_raises(self):
        """EACCES says nothing about whether the sentinel is pending: dropping it
        would report a worker's owed task as not there."""
        for victim, order in itertools.product(DELIVERED, ORDERS):
            with self.subTest(victim=victim, listing=order):
                inbox = Inbox(self, order)
                target = inbox.paths[victim]

                def refuse(_i, path, t=target):
                    if path == t:
                        raise PermissionError(13, "Permission denied", str(path))

                with self.assertRaises(PermissionError):
                    inbox.pending(refuse)
                self.exercised(inbox)

    def test_any_other_oserror_still_raises(self):
        for victim, order in itertools.product(DELIVERED, ORDERS):
            with self.subTest(victim=victim, listing=order):
                inbox = Inbox(self, order)
                target = inbox.paths[victim]

                def eio(_i, path, t=target):
                    if path == t:
                        raise OSError(5, "Input/output error", str(path))

                with self.assertRaises(OSError) as ctx:
                    inbox.pending(eio)
                self.assertEqual(ctx.exception.errno, 5)
                self.exercised(inbox)


if __name__ == "__main__":
    unittest.main(verbosity=2)
