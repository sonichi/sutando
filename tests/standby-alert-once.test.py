#!/usr/bin/env python3
"""src/standby_alert_once.py: at most one owner alert per outage of a seat's watcher.

The standby supervisor retries an alert that was not published. These pin what makes a retry
safe: one fixed name per message, "already published" decided by the result-side owners (live,
every archive layout, the undelivered quarantine), the check and the publish under one lock so
racing publishers send one, a durable outage record shared across supervisors and restarts, and
one notification claim per outage.

Run: python3 tests/standby-alert-once.test.py
"""
from __future__ import annotations

import io
import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import standby_alert_once as m  # noqa: E402

BODY = "Sutando: the core has had no task watcher for 5 min.\n"


def _race_publish(ws: str, stem: str, q) -> None:
    q.put(m.publish(ws, stem, BODY))


def _race_claim(ws: str, inbox: str, q) -> None:
    q.put(m.claim_notify(ws, inbox, 1000))


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name) / "ws"
        (self.ws / "results").mkdir(parents=True)
        self.inbox = self.ws / "tasks"
        self.inbox.mkdir()
        self.stem = m.stem(self.inbox, 1000)

    def copies(self):
        return sorted(p.relative_to(self.ws / "results").as_posix()
                      for p in (self.ws / "results").rglob(f"{self.stem}*.txt"))


class Names(Base):
    def test_one_name_per_message_of_an_episode_and_no_path_in_it(self):
        self.assertEqual(m.stem(self.inbox, 1000), m.stem(str(self.inbox) + "/", 1000))
        self.assertEqual(m.stem(self.inbox, 1000, 2), self.stem + "-r2")
        self.assertNotEqual(m.stem(self.inbox, 1001), self.stem)
        self.assertNotEqual(m.stem(self.ws / "other", 1000), self.stem)
        self.assertNotIn(str(self.ws), self.stem)
        self.assertRegex(self.stem, r"^proactive-standby-unwatched-[0-9a-f]{12}-1000$")


class Publish(Base):
    def test_published_once_then_already_published(self):
        self.assertEqual(m.publish(self.ws, self.stem, BODY), m.PUBLISHED)
        self.assertEqual(m.publish(self.ws, self.stem, BODY), m.ALREADY)
        self.assertEqual(self.copies(), [f"{self.stem}.txt"])

    def test_a_copy_the_bridge_moved_anywhere_counts_as_published(self):
        for where in (f"archive/{self.stem}.txt", f"archive/2026-11/{self.stem}.txt",
                      f"archive/2026-11/{self.stem}-1763500000.txt", f"archive/{self.stem}-1763500000.txt",
                      f"undelivered/{self.stem}-1763500000.txt"):
            with self.subTest(where=where):
                for p in (self.ws / "results").rglob("*.txt"):
                    p.unlink()
                target = self.ws / "results" / where
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(BODY)
                self.assertEqual(m.publish(self.ws, self.stem, BODY), m.ALREADY)
                self.assertFalse((self.ws / "results" / f"{self.stem}.txt").exists())

    def test_another_messages_copy_does_not_count(self):
        (self.ws / "results" / f"{self.stem}-r1.txt").write_text(BODY)
        (self.ws / "results" / f"{m.stem(self.inbox, 999)}.txt").write_text(BODY)
        self.assertEqual(m.publish(self.ws, self.stem, BODY), m.PUBLISHED)

    def test_a_failure_before_the_rename_writes_nothing_and_a_retry_publishes(self):
        with mock.patch.object(m, "publish_text", side_effect=OSError("disk full")):
            self.assertEqual(m.publish(self.ws, self.stem, BODY), m.FAILED)
        self.assertEqual(self.copies(), [])
        self.assertEqual(m.publish(self.ws, self.stem, BODY), m.PUBLISHED)

    def test_an_unsynced_publish_is_reported_and_a_retry_finds_it(self):
        def renamed_then_unsynced(path, body):
            Path(path).write_text(body)
            raise m.PublishedUnconfirmed(5, "directory sync failed")
        with mock.patch.object(m, "publish_text", side_effect=renamed_then_unsynced):
            self.assertEqual(m.publish(self.ws, self.stem, BODY), m.UNCONFIRMED)
        self.assertEqual(m.publish(self.ws, self.stem, BODY), m.ALREADY)
        self.assertEqual(self.copies(), [f"{self.stem}.txt"])

    def test_no_results_folder_is_a_failure(self):
        (self.ws / "results").rmdir()
        self.assertEqual(m.publish(self.ws, self.stem, BODY), m.FAILED)

    def test_racing_publishers_send_one(self):
        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        procs = [ctx.Process(target=_race_publish, args=(str(self.ws), self.stem, q)) for _ in range(8)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        outcomes = sorted(q.get(timeout=10) for _ in procs)
        self.assertEqual(outcomes, [m.PUBLISHED] + [m.ALREADY] * 7)
        self.assertEqual(self.copies(), [f"{self.stem}.txt"])

    def test_the_check_and_the_publish_are_one_locked_step(self):
        # A slowed check: without the lock every thread passes it before any publishes.
        import threading
        import time
        real = m.already_published

        def slow(results, stem):
            seen = real(results, stem)
            time.sleep(0.2)
            return seen
        outcomes = []
        with mock.patch.object(m, "already_published", side_effect=slow):
            threads = [threading.Thread(target=lambda: outcomes.append(m.publish(self.ws, self.stem, BODY)))
                       for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(sorted(outcomes), [m.PUBLISHED] + [m.ALREADY] * 3)

    def test_the_cli_refuses_a_name_that_is_not_a_standby_alert(self):
        with mock.patch.object(sys, "stdin", io.StringIO(BODY)), mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                m.main(["publish", "--workspace", str(self.ws), "--stem", "task-1"])
        self.assertEqual(self.copies(), [])


class Episodes(Base):
    def test_an_outage_is_shared_refreshed_and_ended(self):
        self.assertEqual(m.observe(self.ws, self.inbox, 1000, 300), 1000)
        self.assertEqual(m.observe(self.ws, self.inbox, 1200, 300), 1000, "a second supervisor or a restart joins it")
        self.assertEqual(m.observe(self.ws, self.inbox, 1450, 300), 1000, "each poll refreshes it")
        m.end(self.ws, self.inbox)
        self.assertEqual(m.observe(self.ws, self.inbox, 1500, 300), 1500, "after the watcher returns, a new outage")

    def test_an_outage_nobody_refreshed_for_longer_than_the_grace_is_over(self):
        self.assertEqual(m.observe(self.ws, self.inbox, 1000, 300), 1000)
        self.assertEqual(m.observe(self.ws, self.inbox, 1301, 300), 1301)

    def test_a_damaged_record_starts_a_new_outage(self):
        m.observe(self.ws, self.inbox, 1000, 300)
        (m._state(self.ws) / f"{m.inbox_key(self.inbox)}.json").write_text("{not json")
        self.assertEqual(m.observe(self.ws, self.inbox, 1100, 300), 1100)

    def test_inboxes_are_independent(self):
        other = self.ws / "deliveries" / "w1"
        m.observe(self.ws, self.inbox, 1000, 300)
        self.assertEqual(m.observe(self.ws, other, 1100, 300), 1100)
        m.end(self.ws, other)
        self.assertEqual(m.observe(self.ws, self.inbox, 1200, 300), 1000)

    def test_the_notification_is_claimed_once_per_outage(self):
        self.assertTrue(m.claim_notify(self.ws, self.inbox, 1000))
        self.assertFalse(m.claim_notify(self.ws, self.inbox, 1000))
        self.assertTrue(m.claim_notify(self.ws, self.inbox, 2000))
        m.end(self.ws, self.inbox)
        self.assertTrue(m.claim_notify(self.ws, self.inbox, 1000), "a new outage notifies again")

    def test_racing_claims_notify_once(self):
        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        procs = [ctx.Process(target=_race_claim, args=(str(self.ws), str(self.inbox), q)) for _ in range(8)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        self.assertEqual(sorted(q.get(timeout=10) for _ in procs), [False] * 7 + [True])

    def test_ending_an_outage_clears_its_message_locks(self):
        m.observe(self.ws, self.inbox, 1000, 300)
        m.publish(self.ws, self.stem, BODY)
        m.end(self.ws, self.inbox)
        left = sorted(p.name for p in (m._state(self.ws)).iterdir())
        self.assertEqual(left, [f"{m.inbox_key(self.inbox)}.lock"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
