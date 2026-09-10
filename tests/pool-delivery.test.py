#!/usr/bin/env python3
"""Contract tests for src/pool_delivery.py — a real filesystem, no mocks."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import pool_delivery as pd


class Workspace:
    def __init__(self, root):
        self.root = Path(root)
        for d in ("tasks", "tasks/archive", "results", "deliveries", "state/workers"):
            (self.root / d).mkdir(parents=True, exist_ok=True)

    def payload(self, task_id, body="do a thing"):
        p = self.root / "tasks" / f"{task_id}.json"
        p.write_text(f'{{"id": "{task_id}", "task": "{body}"}}', encoding="utf-8")
        return p

    def deliver(self, recipient, task_id):
        d = self.root / "deliveries" / recipient
        d.mkdir(parents=True, exist_ok=True)
        p = d / task_id
        os.open(p, os.O_CREAT | os.O_EXCL)
        return p

    def result(self, task_id, text="done"):
        p = self.root / "results" / f"{task_id}.txt"
        p.write_text(text, encoding="utf-8")
        return p

    def flag(self, recipient, task_id):
        p = pd.done_flag(self.root, recipient, task_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
        return p


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(self._tmp.name)
        self.root = self.ws.root
        self.addCleanup(self._tmp.cleanup)


class TestSentinelNames(Base):
    def test_unclaimed_parses(self):
        self.assertEqual(pd.parse_sentinel("task-abc"), ("task-abc", False))

    def test_claimed_parses(self):
        self.assertEqual(pd.parse_sentinel("task-abc.claimed"), ("task-abc", True))

    def test_non_sentinel_rejected(self):
        for name in ("roster.json", "task-abc.json", ".DS_Store", "notes.txt"):
            self.assertIsNone(pd.parse_sentinel(name), name)

    def test_suffix_substitutes_never_appends(self):
        """A claimed sentinel must not still read as unclaimed."""
        claimed = pd.parse_sentinel("task-abc.claimed")
        self.assertTrue(claimed[1])
        self.assertNotIn(".claimed", claimed[0])

    def test_recipient_id_is_validated(self):
        for bad in ("../core", "core/../x", "Core", "", "a" * 40):
            with self.assertRaises(ValueError, msg=bad):
                pd.deliveries_dir(self.root, bad)


class TestPending(Base):
    def test_empty_folder_is_not_an_error(self):
        self.assertEqual(pd.pending(self.root, "core"), [])

    def test_absent_folder_is_not_an_error(self):
        self.assertEqual(pd.pending(self.root, "worker-nope"), [])

    def test_claimed_is_not_pending(self):
        s = self.ws.deliver("core", "task-1")
        pd.claim(s)
        self.assertEqual(pd.pending(self.root, "core"), [])

    def test_ordered_by_delivery_time(self):
        for i, tid in enumerate(("task-c", "task-a", "task-b")):
            p = self.ws.deliver("core", tid)
            os.utime(p, (1000 + i, 1000 + i))
        got = [pd.parse_sentinel(p.name)[0] for p in pd.pending(self.root, "core")]
        self.assertEqual(got, ["task-c", "task-a", "task-b"])

    def test_reader_sees_only_its_own_folder(self):
        self.ws.deliver("core", "task-mine")
        self.ws.deliver("worker-1", "task-theirs")
        got = [pd.parse_sentinel(p.name)[0] for p in pd.pending(self.root, "core")]
        self.assertEqual(got, ["task-mine"])

    def test_a_worker_reads_its_own_folder_not_the_cores(self):
        """Scoping must follow the recipient argument, not a hardcoded folder."""
        self.ws.deliver("core", "task-for-core")
        self.ws.deliver("worker-1", "task-for-worker")
        got = [pd.parse_sentinel(p.name)[0] for p in pd.pending(self.root, "worker-1")]
        self.assertEqual(got, ["task-for-worker"])

    def test_claim_by_a_worker_lands_in_the_workers_folder(self):
        self.ws.payload("task-1")
        s = self.ws.deliver("worker-1", "task-1")
        c = pd.claim(s)
        self.assertEqual(c.parent.name, "worker-1")
        self.assertIsNone(pd.find(self.root, "core", "task-1"))


class TestClaim(Base):
    def test_claim_renames_in_place(self):
        s = self.ws.deliver("core", "task-1")
        c = pd.claim(s)
        self.assertFalse(s.exists())
        self.assertTrue(c.exists())
        self.assertEqual(c.name, "task-1.claimed")

    def test_claim_is_exclusive(self):
        s = self.ws.deliver("core", "task-1")
        pd.claim(s)
        with self.assertRaises(OSError):
            pd.claim(s)

    def test_claiming_a_claimed_sentinel_is_refused(self):
        s = self.ws.deliver("core", "task-1")
        c = pd.claim(s)
        with self.assertRaises(pd.NotDelivered):
            pd.claim(c)

    def test_claim_does_not_touch_the_payload(self):
        p = self.ws.payload("task-1")
        before = p.read_text()
        pd.claim(self.ws.deliver("core", "task-1"))
        self.assertTrue(p.exists())
        self.assertEqual(p.read_text(), before)

    def test_release_returns_it_to_pending(self):
        s = self.ws.deliver("core", "task-1")
        pd.release(pd.claim(s))
        got = [pd.parse_sentinel(p.name)[0] for p in pd.pending(self.root, "core")]
        self.assertEqual(got, ["task-1"])

    def test_release_refuses_an_unclaimed_sentinel(self):
        s = self.ws.deliver("core", "task-1")
        with self.assertRaises(pd.NotDelivered):
            pd.release(s)

    def test_find_locates_under_either_name(self):
        s = self.ws.deliver("core", "task-1")
        self.assertEqual(pd.find(self.root, "core", "task-1"), s)
        c = pd.claim(s)
        self.assertEqual(pd.find(self.root, "core", "task-1"), c)

    def test_find_returns_none_when_undelivered(self):
        self.assertIsNone(pd.find(self.root, "core", "task-absent"))


class TestResidue(Base):
    def test_result_without_flag_is_completed(self):
        self.ws.payload("task-1")
        pd.claim(self.ws.deliver("core", "task-1"))
        self.ws.result("task-1")
        self.assertEqual(pd.residue(self.root, "core", "task-1"), "completed")

    def test_result_plus_flag_is_finished(self):
        self.ws.payload("task-1")
        self.ws.result("task-1")
        self.ws.flag("core", "task-1")
        self.assertEqual(pd.residue(self.root, "core", "task-1"), "finished")

    def test_delivered_unclaimed_work_is_named_not_a_fallback(self):
        """It is work waiting, so it must not share a name with 'nothing here'."""
        self.ws.payload("task-1")
        self.ws.deliver("core", "task-1")
        self.assertEqual(pd.residue(self.root, "core", "task-1"), "unclaimed")

    def test_nothing_anywhere_is_clean(self):
        self.assertEqual(pd.residue(self.root, "core", "task-absent"), "clean")

    def test_claimed_without_result_died_mid_work(self):
        self.ws.payload("task-1")
        pd.claim(self.ws.deliver("core", "task-1"))
        self.assertEqual(pd.residue(self.root, "core", "task-1"), "died-mid-work")

    def test_sentinel_without_payload_is_stale(self):
        self.ws.deliver("core", "task-1")
        self.assertEqual(pd.residue(self.root, "core", "task-1"), "stale-sentinel")

    def test_payload_without_sentinel_is_undelivered(self):
        self.ws.payload("task-1")
        self.assertEqual(pd.residue(self.root, "core", "task-1"), "undelivered")

    def test_archived_payload_is_not_undelivered(self):
        self.ws.payload("task-1")
        pd.archived_payload(self.root, "task-1").write_text("{}", encoding="utf-8")
        self.assertEqual(pd.residue(self.root, "core", "task-1"), "clean")


class TestSweep(Base):
    def test_releases_work_a_crash_left_claimed(self):
        self.ws.payload("task-1")
        pd.claim(self.ws.deliver("core", "task-1"))
        out = pd.sweep(self.root, "core")
        self.assertEqual(out["released"], ["task-1"])
        self.assertTrue((self.root / "deliveries/core/task-1").exists())

    def test_does_not_release_finished_work(self):
        self.ws.payload("task-1")
        pd.claim(self.ws.deliver("core", "task-1"))
        self.ws.result("task-1")
        out = pd.sweep(self.root, "core")
        self.assertEqual(out["completed"], ["task-1"])
        self.assertEqual(out["released"], [])
        self.assertTrue((self.root / "deliveries/core/task-1.claimed").exists())

    def test_removes_a_sentinel_whose_payload_is_gone(self):
        self.ws.deliver("core", "task-1")
        out = pd.sweep(self.root, "core")
        self.assertEqual(out["stale"], ["task-1"])
        self.assertIsNone(pd.find(self.root, "core", "task-1"))

    def test_reports_untouched_work_as_ready(self):
        self.ws.payload("task-1")
        self.ws.deliver("core", "task-1")
        self.assertEqual(pd.sweep(self.root, "core")["ready"], ["task-1"])

    def test_is_idempotent(self):
        self.ws.payload("task-1")
        pd.claim(self.ws.deliver("core", "task-1"))
        pd.sweep(self.root, "core")
        second = pd.sweep(self.root, "core")
        self.assertEqual(second["released"], [])
        self.assertEqual(second["ready"], ["task-1"])

    def test_finished_work_is_retired_not_handed_back(self):
        """Result + flag + a lingering sentinel must never re-enter the queue."""
        self.ws.payload("task-1")
        pd.claim(self.ws.deliver("core", "task-1"))
        self.ws.result("task-1")
        self.ws.flag("core", "task-1")
        out = pd.sweep(self.root, "core")
        self.assertEqual(out["retired"], ["task-1"])
        self.assertEqual(out["ready"], [])
        self.assertIsNone(pd.find(self.root, "core", "task-1"))

    def test_every_residue_state_has_a_sweep_branch(self):
        """A state with no branch must fail loudly, not fall through to ready."""
        states = ("unclaimed", "died-mid-work", "completed", "finished",
                  "stale-sentinel", "undelivered", "clean")
        seen = set()
        for name, build in (
            ("unclaimed", lambda: (self.ws.payload("task-1"), self.ws.deliver("core", "task-1"))),
            ("died-mid-work", lambda: (self.ws.payload("task-1"), pd.claim(self.ws.deliver("core", "task-1")))),
            ("completed", lambda: (self.ws.payload("task-1"), pd.claim(self.ws.deliver("core", "task-1")), self.ws.result("task-1"))),
            ("finished", lambda: (self.ws.payload("task-1"), pd.claim(self.ws.deliver("core", "task-1")), self.ws.result("task-1"), self.ws.flag("core", "task-1"))),
            ("stale-sentinel", lambda: (self.ws.deliver("core", "task-1"),)),
        ):
            with tempfile.TemporaryDirectory() as d:
                self.ws = Workspace(d); self.root = self.ws.root
                build()
                self.assertEqual(pd.residue(self.root, "core", "task-1"), name)
                pd.sweep(self.root, "core")   # raises if the state has no branch
                seen.add(name)
        # the two remaining states have no sentinel, so sweep never sees them
        self.assertEqual(set(states) - seen, {"undelivered", "clean"})

    def test_sweep_never_touches_a_sibling(self):
        self.ws.payload("task-2")
        other = self.ws.deliver("worker-1", "task-2")
        claimed = pd.claim(other)
        pd.sweep(self.root, "core")
        self.assertTrue(claimed.exists())


class TestPayload(Base):
    def test_reads_delivered_payload(self):
        self.ws.payload("task-1", "write the docs")
        self.assertEqual(pd.read_payload(self.root, "task-1")["task"], "write the docs")

    def test_missing_payload_is_none(self):
        self.assertIsNone(pd.read_payload(self.root, "task-absent"))

    def test_corrupt_payload_is_none_not_a_crash(self):
        (self.root / "tasks" / "task-1.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(pd.read_payload(self.root, "task-1"))


class TestCLI(Base):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "src" / "pool_delivery.py"),
             "--workspace", str(self.root), *args],
            capture_output=True, text=True, timeout=30)

    def test_pending_lists_task_ids(self):
        self.ws.payload("task-1")
        self.ws.deliver("core", "task-1")
        r = self.run_cli("pending")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "task-1")

    def test_residue_prints_the_state(self):
        self.ws.payload("task-1")
        r = self.run_cli("residue", "--task-id", "task-1")
        self.assertEqual(r.stdout.strip(), "undelivered")

    def test_sweep_emits_json(self):
        self.ws.payload("task-1")
        self.ws.deliver("core", "task-1")
        r = self.run_cli("sweep")
        self.assertIn('"ready"', r.stdout)
        self.assertIn("task-1", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
