#!/usr/bin/env python3
"""Contract tests for skills/worker-pool/scripts/pool_delivery.py — a real filesystem, no mocks."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills/worker-pool/scripts"))
import pool_delivery as pd


class Workspace:
    def __init__(self, root):
        self.root = Path(root)
        for d in ("tasks", "tasks/archive", "results", "deliveries", "state/workers"):
            (self.root / d).mkdir(parents=True, exist_ok=True)

    def payload(self, task_id, body="do a thing"):
        p = self.root / "tasks" / f"{task_id}.txt"
        p.write_text(f"id: {task_id}\nsource: test\ntask: {body}\n", encoding="utf-8")
        return p

    def deliver(self, recipient, task_id):
        d = self.root / "deliveries" / recipient
        d.mkdir(parents=True, exist_ok=True)
        # Spelled literally, not via pd.PENDING_SUFFIX: a test that reads the
        # constant cannot catch the constant changing.
        p = d / f"{task_id}.txt"
        os.close(os.open(p, os.O_CREAT | os.O_EXCL))
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
    def test_pending_parses(self):
        self.assertEqual(pd.parse_sentinel("task-abc.txt"), ("task-abc", False))
        # Extensionless is NOT a sentinel: the watcher that wakes a worker
        # emits for no other extension, so such a file would never be seen.
        self.assertIsNone(pd.parse_sentinel("task-abc"))

    def test_accepted_parses(self):
        self.assertEqual(pd.parse_sentinel("task-abc.accepted"), ("task-abc", True))

    def test_non_sentinel_rejected(self):
        for name in ("roster.json", "task-abc.json", ".DS_Store", "notes.txt"):
            self.assertIsNone(pd.parse_sentinel(name), name)

    def test_suffix_substitutes_never_appends(self):
        """A accepted sentinel must not still read as pending."""
        accepted = pd.parse_sentinel("task-abc.accepted")
        self.assertTrue(accepted[1])
        self.assertNotIn(".accepted", accepted[0])

    def test_recipient_id_is_validated(self):
        for bad in ("../core", "core/../x", "Core", "", "a" * 40):
            with self.assertRaises(ValueError, msg=bad):
                pd.deliveries_dir(self.root, bad)


class TestLaneEncodedIds(Base):
    def test_a_lane_encoded_id_parses_and_is_pending(self):
        """local-hs ids look like task-local-hs~task-<hex>: the `~` is the
        bridge's instance separator. Seen live: a worker's pending() was empty
        while its sentinel sat in the folder."""
        got = pd.parse_sentinel("task-local-hs~task-4384e6c562dee80eba.txt")
        self.assertEqual(got, ("task-local-hs~task-4384e6c562dee80eba", False))
        self.ws.deliver("core", "task-local-hs~task-1")
        self.assertEqual([pd.parse_sentinel(p.name)[0] for p in pd.pending(self.root, "core")],
                         ["task-local-hs~task-1"])


class TestPending(Base):
    def test_empty_folder_is_not_an_error(self):
        self.assertEqual(pd.pending(self.root, "core"), [])

    def test_absent_folder_is_not_an_error(self):
        self.assertEqual(pd.pending(self.root, "worker-nope"), [])

    def test_accepted_is_not_pending(self):
        s = self.ws.deliver("core", "task-1")
        pd.accept(s)
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

    def test_accept_by_a_worker_lands_in_the_workers_folder(self):
        self.ws.payload("task-1")
        s = self.ws.deliver("worker-1", "task-1")
        c = pd.accept(s)
        self.assertEqual(c.parent.name, "worker-1")
        self.assertIsNone(pd.find(self.root, "core", "task-1"))


class TestLegacyAcceptedSuffix(Base):
    def test_a_pre_rename_sentinel_is_still_seen(self):
        """The rename landed mid-flight on a live pool. A `.claimed` sentinel the
        old code wrote must not become invisible — `find` missing it lets the
        router deliver already-accepted work a second time. Seen live 2026-09-10."""
        self.ws.payload("task-1")
        d = self.root / "deliveries" / "core"; d.mkdir(parents=True, exist_ok=True)
        legacy = d / "task-1.claimed"; legacy.touch()
        self.assertEqual(pd.parse_sentinel("task-1.claimed"), ("task-1", True))
        self.assertEqual(pd.find(self.root, "core", "task-1"), legacy)
        self.assertEqual([p.name for p in pd.accepted(self.root, "core")], ["task-1.claimed"])
        self.assertEqual(pd.pending(self.root, "core"), [], "never offered as new work")


class TestAccept(Base):
    def test_accept_renames_in_place(self):
        s = self.ws.deliver("core", "task-1")
        c = pd.accept(s)
        self.assertFalse(s.exists())
        self.assertTrue(c.exists())
        self.assertEqual(c.name, "task-1.accepted")

    def test_accept_is_exclusive(self):
        s = self.ws.deliver("core", "task-1")
        pd.accept(s)
        with self.assertRaises(OSError):
            pd.accept(s)

    def test_accepting_an_accepted_sentinel_is_refused(self):
        s = self.ws.deliver("core", "task-1")
        c = pd.accept(s)
        with self.assertRaises(pd.NotDelivered):
            pd.accept(c)

    def test_accept_does_not_touch_the_payload(self):
        p = self.ws.payload("task-1")
        before = p.read_text()
        pd.accept(self.ws.deliver("core", "task-1"))
        self.assertTrue(p.exists())
        self.assertEqual(p.read_text(), before)

    def test_release_returns_it_to_pending(self):
        s = self.ws.deliver("core", "task-1")
        pd.release(pd.accept(s))
        got = [pd.parse_sentinel(p.name)[0] for p in pd.pending(self.root, "core")]
        self.assertEqual(got, ["task-1"])

    def test_release_refuses_a_pending_sentinel(self):
        s = self.ws.deliver("core", "task-1")
        with self.assertRaises(pd.NotDelivered):
            pd.release(s)

    def test_find_locates_under_either_name(self):
        s = self.ws.deliver("core", "task-1")
        self.assertEqual(pd.find(self.root, "core", "task-1"), s)
        c = pd.accept(s)
        self.assertEqual(pd.find(self.root, "core", "task-1"), c)

    def test_find_returns_none_when_undelivered(self):
        self.assertIsNone(pd.find(self.root, "core", "task-absent"))


class TestResidue(Base):
    def test_result_without_flag_is_completed(self):
        self.ws.payload("task-1")
        pd.accept(self.ws.deliver("core", "task-1"))
        self.ws.result("task-1")
        self.assertEqual(pd.residue(self.root, "core", "task-1"), "completed")

    def test_result_plus_flag_is_finished(self):
        self.ws.payload("task-1")
        self.ws.result("task-1")
        self.ws.flag("core", "task-1")
        self.assertEqual(pd.residue(self.root, "core", "task-1"), "finished")

    def test_delivered_pending_work_is_named_not_a_fallback(self):
        """It is work waiting, so it must not share a name with 'nothing here'."""
        self.ws.payload("task-1")
        self.ws.deliver("core", "task-1")
        self.assertEqual(pd.residue(self.root, "core", "task-1"), "pending")

    def test_nothing_anywhere_is_clean(self):
        self.assertEqual(pd.residue(self.root, "core", "task-absent"), "clean")

    def test_accepted_without_result_died_mid_work(self):
        self.ws.payload("task-1")
        pd.accept(self.ws.deliver("core", "task-1"))
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


class TestFlagAfterDrain(Base):
    def test_a_done_flag_with_no_result_is_finished_not_died(self):
        """The bridge drains results/<id>.txt on delivery. A crash between the
        flag and the archive, then a drain, leaves .accepted + flag + payload and
        NO result. Reading that as died-mid-work re-runs finished work."""
        self.ws.payload("task-1")
        pd.accept(self.ws.deliver("core", "task-1"))
        self.ws.flag("core", "task-1")                  # result already drained
        self.assertEqual(pd.residue(self.root, "core", "task-1"), "finished")
        out = pd.sweep(self.root, "core")
        self.assertEqual(out["released"], [])
        self.assertIsNone(pd.find(self.root, "core", "task-1"))


class TestSweep(Base):
    def test_releases_work_a_crash_left_accepted(self):
        self.ws.payload("task-1")
        pd.accept(self.ws.deliver("core", "task-1"))
        out = pd.sweep(self.root, "core")
        self.assertEqual(out["released"], ["task-1"])
        self.assertTrue((self.root / "deliveries/core/task-1.txt").exists())

    def test_does_not_release_finished_work(self):
        self.ws.payload("task-1")
        pd.accept(self.ws.deliver("core", "task-1"))
        self.ws.result("task-1")
        out = pd.sweep(self.root, "core")
        self.assertEqual(out["completed"], ["task-1"])
        self.assertEqual(out["released"], [])
        self.assertTrue((self.root / "deliveries/core/task-1.accepted").exists())

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
        pd.accept(self.ws.deliver("core", "task-1"))
        pd.sweep(self.root, "core")
        second = pd.sweep(self.root, "core")
        self.assertEqual(second["released"], [])
        self.assertEqual(second["ready"], ["task-1"])

    def test_finished_work_is_retired_not_handed_back(self):
        """Result + flag + a lingering sentinel must never re-enter the queue."""
        self.ws.payload("task-1")
        pd.accept(self.ws.deliver("core", "task-1"))
        self.ws.result("task-1")
        self.ws.flag("core", "task-1")
        out = pd.sweep(self.root, "core")
        self.assertEqual(out["retired"], ["task-1"])
        self.assertEqual(out["ready"], [])
        self.assertIsNone(pd.find(self.root, "core", "task-1"))

    def test_every_residue_state_has_a_sweep_branch(self):
        """A state with no branch must fail loudly, not fall through to ready."""
        states = ("pending", "died-mid-work", "completed", "finished",
                  "stale-sentinel", "undelivered", "clean")
        seen = set()
        for name, build in (
            ("pending", lambda: (self.ws.payload("task-1"), self.ws.deliver("core", "task-1"))),
            ("died-mid-work", lambda: (self.ws.payload("task-1"), pd.accept(self.ws.deliver("core", "task-1")))),
            ("completed", lambda: (self.ws.payload("task-1"), pd.accept(self.ws.deliver("core", "task-1")), self.ws.result("task-1"))),
            ("finished", lambda: (self.ws.payload("task-1"), pd.accept(self.ws.deliver("core", "task-1")), self.ws.result("task-1"), self.ws.flag("core", "task-1"))),
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
        accepted = pd.accept(other)
        pd.sweep(self.root, "core")
        self.assertTrue(accepted.exists())


class TestAliasRefused(Base):
    """The writer refuses to publish through a recipient-named symlink: a record
    would otherwise land in the TARGET's folder under this recipient's name."""

    A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    def _root(self):
        return pd.done_flag(self.ws.root, self.B, "task-0aliased000000000").parent.parent.parent

    def _alias(self):
        root = self._root()
        (root / self.B / "done").mkdir(parents=True)
        (root / self.A).symlink_to(root / self.B)
        return root

    def test_mark_done_through_an_aliased_recipient_dir_writes_nothing(self):
        root = self._alias()
        for published in (False, True):
            with self.assertRaises(pd.RecipientAliasError):
                pd.mark_done(self.ws.root, self.A, "task-0aliased000000001", published=published)
        self.assertEqual(sorted(p.name for p in (root / self.B / "done").iterdir()), [],
                         "a record was published into B's folder under A's name")

    def test_an_aliased_done_dir_is_refused_too(self):
        root = self._root()
        (root / self.B / "done").mkdir(parents=True)
        (root / self.A).mkdir()
        (root / self.A / "done").symlink_to(root / self.B / "done")
        with self.assertRaises(OSError):
            pd.mark_done(self.ws.root, self.A, "task-0aliased000000002", published=True)
        self.assertEqual(list((root / self.B / "done").iterdir()), [])

    def test_control_a_real_recipient_dir_still_publishes(self):
        pd.mark_done(self.ws.root, self.B, "task-0aliased000000003", published=True)
        self.assertTrue(pd.done_flag(self.ws.root, self.B, "task-0aliased000000003").exists())


class TestPayload(Base):
    def test_reads_the_bridges_task_file_as_text(self):
        self.ws.payload("task-1", "write the docs")
        self.assertIn("task: write the docs", pd.read_payload(self.root, "task-1"))

    def test_missing_payload_is_none(self):
        self.assertIsNone(pd.read_payload(self.root, "task-absent"))

    def test_an_archived_payload_is_none(self):
        """finish moved it; a lingering sentinel must not resurrect it."""
        pd.archived_payload(self.root, "task-1").write_text("id: task-1\n", encoding="utf-8")
        self.assertIsNone(pd.read_payload(self.root, "task-1"))


class TestCLI(Base):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[3] / "skills/worker-pool/scripts" / "pool_delivery.py"),
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


class TestEmit(Base):
    def test_a_dead_consumer_ends_the_reader(self):
        """A reader that keeps emitting into a closed pipe piles events up
        unseen; exiting is what makes the failure visible."""
        from unittest.mock import patch
        with patch("builtins.print", side_effect=BrokenPipeError):
            with self.assertRaises(SystemExit) as e:
                pd._emit("task-1")
        self.assertEqual(e.exception.code, 0)


class TestCliDispatch(Base):
    def _main(self, *args):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pd.main(["--workspace", str(self.root), "--recipient", "core", *args])
        return rc, buf.getvalue()

    def test_pending_lists_ids(self):
        self.ws.payload("task-1")
        self.ws.deliver("core", "task-1")
        rc, out = self._main("pending")
        self.assertEqual((rc, out.strip()), (0, "task-1"))

    def test_residue_requires_a_task_id(self):
        with self.assertRaises(SystemExit):
            self._main("residue")

    def test_residue_prints_the_state(self):
        self.ws.payload("task-1")
        rc, out = self._main("residue", "--task-id", "task-1")
        self.assertEqual((rc, out.strip()), (0, "undelivered"))

    def test_sweep_emits_json_with_every_bucket(self):
        self.ws.payload("task-1")
        self.ws.deliver("core", "task-1")
        rc, out = self._main("sweep")
        got = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(set(got), {"ready", "released", "completed", "retired", "stale"})
        self.assertEqual(got["ready"], ["task-1"])


class TestWatch(Base):
    def test_watch_emits_the_boot_sweep_then_new_arrivals(self):
        """The boot sweep is not optional: a delivery written while the reader
        was down produces no event, so only the sweep finds it."""
        from unittest.mock import patch
        self.ws.payload("task-old")
        self.ws.deliver("core", "task-old")
        seen = []

        def fake_sleep(_):
            if len(seen) >= 1:          # after the boot sweep emitted
                raise KeyboardInterrupt
            self.ws.payload("task-new")
            self.ws.deliver("core", "task-new")

        with patch.object(pd, "_emit", side_effect=seen.append), \
             patch("time.sleep", side_effect=fake_sleep):
            with self.assertRaises(KeyboardInterrupt):
                pd.main(["--workspace", str(self.root), "--recipient", "core", "watch"])
        self.assertIn("task-old", seen)

    def test_watch_announces_a_delivery_that_arrives_while_running(self):
        """The loop's own job, distinct from the boot sweep."""
        from unittest.mock import patch
        seen, ticks = [], []

        def fake_sleep(_):
            ticks.append(1)
            if len(ticks) == 1:
                self.ws.payload("task-live")
                self.ws.deliver("core", "task-live")
            else:
                raise KeyboardInterrupt

        with patch.object(pd, "_emit", side_effect=seen.append), \
             patch("time.sleep", side_effect=fake_sleep):
            with self.assertRaises(KeyboardInterrupt):
                pd.main(["--workspace", str(self.root), "--recipient", "core", "watch"])
        self.assertIn("task-live", seen)


class TestSweepTotality(Base):
    def test_one_task_under_both_names_is_visited_once(self):
        """`accepted + pending` can list the same id twice; a second visit would
        act on a state the first pass already resolved."""
        self.ws.payload("task-1")
        pd.accept(self.ws.deliver("core", "task-1"))
        self.ws.deliver("core", "task-2")
        self.ws.payload("task-2")
        out = pd.sweep(self.root, "core")
        flat = [x for v in out.values() for x in v]
        self.assertEqual(len(flat), len(set(flat)), out)

    def test_an_unmapped_state_raises_rather_than_falling_through(self):
        from unittest.mock import patch
        self.ws.payload("task-1")
        self.ws.deliver("core", "task-1")
        with patch.object(pd, "residue", return_value="something-new"):
            with self.assertRaises(AssertionError):
                pd.sweep(self.root, "core")



class TestBootRecoveryIsAnnounced(Base):
    """The production `watch` path, not `sweep()` alone: sweep released the
    interrupted delivery, then `watch` seeded `announced` from it and emitted
    nothing -- the task sat pending with nobody told."""
    def watch_once(self):
        from unittest.mock import patch
        seen = []
        def stop(_):
            raise KeyboardInterrupt
        with patch.object(pd, "_emit", side_effect=seen.append), patch("time.sleep", side_effect=stop):
            with self.assertRaises(KeyboardInterrupt):
                pd.main(["--workspace", str(self.root), "--recipient", "core", "watch"])
        return seen

    def test_an_accepted_delivery_interrupted_before_boot_is_announced(self):
        self.ws.payload("task-x")
        pd.accept(self.ws.deliver("core", "task-x"))
        self.assertEqual(self.watch_once(), ["task-x"])
        self.assertTrue((self.root / "deliveries" / "core" / "task-x.txt").exists(), "released")

    def test_a_legacy_claimed_name_is_announced_too(self):
        self.ws.payload("task-y")
        (self.root / "deliveries" / "core").mkdir(parents=True)
        (self.root / "deliveries" / "core" / "task-y.claimed").touch()
        self.assertEqual(self.watch_once(), ["task-y"])


class TestResultReadiness(Base):
    def accepted(self, tid="task-1"):
        self.ws.payload(tid)
        pd.accept(self.ws.deliver("core", tid))

    def test_an_empty_or_whitespace_result_is_not_completion(self):
        for body in ("", "   \n\t"):
            self.accepted(f"task-{len(body)}")
            self.ws.result(f"task-{len(body)}", body)
            self.assertEqual(pd.residue(self.root, "core", f"task-{len(body)}"), "died-mid-work", repr(body))

    def test_a_substantive_result_is(self):
        self.accepted()
        self.ws.result("task-1", "the answer")
        self.assertEqual(pd.residue(self.root, "core", "task-1"), "completed")


class TestAcceptIsExclusive(Base):
    def test_a_second_pending_name_cannot_replace_the_accepted_one(self):
        """rename() overwrites silently; two accepts of one task were both
        'successful'. The accepted name is work in flight and must stay."""
        self.ws.payload("task-1")
        pd.accept(self.ws.deliver("core", "task-1"))
        stray = self.ws.deliver("core", "task-1")
        with self.assertRaises(pd.NotDelivered):
            pd.accept(stray)
        self.assertTrue(stray.exists())
        self.assertTrue((self.root / "deliveries" / "core" / "task-1.accepted").exists())

    def test_the_lock_file_is_not_a_sentinel(self):
        self.ws.payload("task-1")
        pd.accept(self.ws.deliver("core", "task-1"))
        self.assertTrue((self.root / "deliveries" / "core" / pd.LOCK_NAME).exists())
        self.assertEqual([p.name for p in pd.pending(self.root, "core")], [])
        self.assertEqual([p.name for p in pd.accepted(self.root, "core")], ["task-1.accepted"])
        pd.sweep(self.root, "core")
        self.assertTrue((self.root / "deliveries" / "core" / pd.LOCK_NAME).exists())


class TestDoneFlagWriter(Base):
    """The writer half. `residue`/`sweep` already branch on the flag; until this
    cluster existed nothing wrote it, so `finished` was unreachable.
    """

    def test_the_pending_stage_substitutes_the_suffix_rather_than_appending(self):
        pend = pd.pending_flag(self.root, "worker-3", "task-1")
        done = pd.done_flag(self.root, "worker-3", "task-1")
        self.assertEqual(pend.parent, done.parent)
        # Spelled literally: a reader that globs *.flag must not also see pending.
        self.assertEqual(pend.name, "task-1.pending")
        self.assertEqual(done.name, "task-1.flag")

    def test_pending_lays_the_first_stage_and_not_the_second(self):
        got = pd.mark_done(self.root, "worker-3", "task-1", published=False)
        self.assertEqual(got, pd.pending_flag(self.root, "worker-3", "task-1"))
        self.assertTrue(got.is_file())
        self.assertFalse(pd.done_flag(self.root, "worker-3", "task-1").exists())

    def test_publishing_promotes_and_removes_the_pending_stage(self):
        pd.mark_done(self.root, "worker-3", "task-1", published=False)
        got = pd.mark_done(self.root, "worker-3", "task-1", published=True)
        self.assertEqual(got, pd.done_flag(self.root, "worker-3", "task-1"))
        self.assertTrue(got.is_file())
        self.assertFalse(pd.pending_flag(self.root, "worker-3", "task-1").exists())

    def test_publishing_without_a_prior_pending_stage_still_records(self):
        got = pd.mark_done(self.root, "worker-3", "task-1", published=True)
        self.assertTrue(got.is_file())

    def test_a_published_record_is_never_demoted(self):
        pd.mark_done(self.root, "worker-3", "task-1", published=True)
        got = pd.mark_done(self.root, "worker-3", "task-1", published=False)
        # A late pending must not reopen retired work, so it returns the
        # published stage and writes nothing.
        self.assertEqual(got, pd.done_flag(self.root, "worker-3", "task-1"))
        self.assertFalse(pd.pending_flag(self.root, "worker-3", "task-1").exists())

    def test_publishing_twice_is_idempotent(self):
        a = pd.mark_done(self.root, "worker-3", "task-1", published=True)
        b = pd.mark_done(self.root, "worker-3", "task-1", published=True)
        self.assertEqual(a, b)
        self.assertTrue(b.is_file())

    def test_it_leaves_no_temporary_file_behind(self):
        pd.mark_done(self.root, "worker-3", "task-1", published=False)
        pd.mark_done(self.root, "worker-3", "task-1", published=True)
        names = sorted(q.name for q in pd.done_flag(self.root, "worker-3", "task-1").parent.iterdir())
        self.assertEqual(names, ["task-1.flag"])

    def test_a_recipient_id_that_is_not_one_is_refused(self):
        for bad in ("", "../escape", "Worker3", "a/b", "x" * 33):
            with self.assertRaises(ValueError, msg=bad):
                pd.mark_done(self.root, bad, "task-1", published=True)

    def test_a_task_id_that_is_not_one_is_refused(self):
        for bad in ("", "1", "task-", "task-a/b", "task-a.b", "../task-a"):
            with self.assertRaises(ValueError, msg=bad):
                pd.mark_done(self.root, "worker-3", bad, published=True)

    def test_a_refused_id_writes_nothing(self):
        with self.assertRaises(ValueError):
            pd.mark_done(self.root, "worker-3", "task-a/b", published=True)
        self.assertEqual(list((self.root / "state" / "workers").iterdir()), [])


class TestPublishRecordCleansUpOnFailure(Base):
    def test_a_directory_at_the_target_name_raises_and_leaves_no_temp_file(self):
        dst = pd.done_flag(self.root, "worker-3", "task-1")
        dst.mkdir(parents=True)
        with self.assertRaises(OSError):
            pd._publish_record(dst)
        # A refused publish must not litter: the next listing would otherwise
        # accumulate one orphan per attempt.
        self.assertEqual(sorted(q.name for q in dst.parent.iterdir()), ["task-1.flag"])


class TestIsDoneFlag(Base):
    """Completion evidence is a regular file. Anything else at that name is
    malformed state, and reading it as a finish invents a claimant.
    """

    def test_absent_is_not_evidence(self):
        self.assertFalse(pd.is_done_flag(self.root / "state" / "workers" / "w" / "done" / "task-1.flag"))

    def test_a_regular_file_is(self):
        got = pd.mark_done(self.root, "worker-3", "task-1", published=True)
        self.assertTrue(pd.is_done_flag(got))

    def test_a_directory_at_the_name_is_not(self):
        d = pd.done_flag(self.root, "worker-3", "task-1")
        d.mkdir(parents=True)
        self.assertFalse(pd.is_done_flag(d))

    def test_a_symlink_at_the_name_refuses_loudly_instead_of_reading_through(self):
        target = self.root / "real"
        target.write_text("", encoding="utf-8")
        link = pd.done_flag(self.root, "worker-3", "task-1")
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        # O_NOFOLLOW: a link to a regular file must not read as this worker's
        # own finish. Raising is the fail-closed answer, not False.
        with self.assertRaises(OSError):
            pd.is_done_flag(link)


class TestTheDestructiveReaderUsesThePredicate(Base):
    """`sweep` DELETES on residue's verdict, so the flag read there must be the
    fail-closed predicate and not `is_file()`, which follows a symlink."""

    def _completed(self, recipient="worker-3", task_id="task-1"):
        self.ws.payload(task_id)
        pd.accept(self.ws.deliver(recipient, task_id))
        self.ws.result(task_id)
        return task_id

    def test_a_symlink_at_the_flag_name_does_not_retire_the_delivery(self):
        task_id = self._completed()
        target = self.root / "planted"
        target.write_text("", encoding="utf-8")
        link = pd.done_flag(self.root, "worker-3", task_id)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        with self.assertRaises(OSError):
            pd.residue(self.root, "worker-3", task_id)
        with self.assertRaises(OSError):
            pd.sweep(self.root, "worker-3")
        # The work survives: a planted link must not discard an accepted delivery.
        self.assertEqual([q.name for q in pd.accepted(self.root, "worker-3")],
                         [f"{task_id}.accepted"])

    def test_a_real_flag_still_retires_it(self):
        task_id = self._completed()
        pd.mark_done(self.root, "worker-3", task_id, published=True)
        self.assertEqual(pd.residue(self.root, "worker-3", task_id), "finished")
        self.assertEqual(pd.sweep(self.root, "worker-3").get("retired"), [task_id])
        self.assertEqual([q.name for q in pd.accepted(self.root, "worker-3")], [])


class TestTheWriterMakesFinishedReachable(Base):
    """The reason the writer exists: `residue` has a `finished` state that no
    path could reach, so `sweep` never retired a completed delivery.
    """

    def _completed(self, recipient="worker-3", task_id="task-1"):
        self.ws.payload(task_id)
        pd.accept(self.ws.deliver(recipient, task_id))
        self.ws.result(task_id)
        return task_id

    def test_without_the_record_a_completed_delivery_is_never_retired(self):
        task_id = self._completed()
        self.assertEqual(pd.residue(self.root, "worker-3", task_id), "completed")
        pd.sweep(self.root, "worker-3")
        # Still there: `completed` is reported, never unlinked — so every boot
        # re-reports the same delivery.
        self.assertEqual([q.name for q in pd.accepted(self.root, "worker-3")],
                         [f"{task_id}.accepted"])

    def test_with_the_record_the_same_delivery_reads_finished_and_is_retired(self):
        task_id = self._completed()
        pd.mark_done(self.root, "worker-3", task_id, published=True)
        self.assertEqual(pd.residue(self.root, "worker-3", task_id), "finished")
        pd.sweep(self.root, "worker-3")
        self.assertEqual([q.name for q in pd.accepted(self.root, "worker-3")], [])

    def test_the_pending_stage_alone_does_not_retire_the_delivery(self):
        task_id = self._completed()
        pd.mark_done(self.root, "worker-3", task_id, published=False)
        # Only the published stage is a finish; a pending hold must not retire.
        self.assertEqual(pd.residue(self.root, "worker-3", task_id), "completed")


class TestMarkDoneCli(Base):
    """The CLI is the only surface a non-python caller can reach, and the core
    watcher reaches it that way.
    """

    def _main(self, *args):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        rc = None
        with redirect_stdout(buf):
            rc = pd.main(["--workspace", str(self.root), "--recipient", "worker-3", *args])
        return rc, buf.getvalue()

    def test_it_writes_the_pending_stage(self):
        rc, out = self._main("mark-done", "--task-id", "task-1", "--stage", "pending")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), str(pd.pending_flag(self.root, "worker-3", "task-1")))
        self.assertTrue(pd.pending_flag(self.root, "worker-3", "task-1").is_file())

    def test_it_promotes_to_the_published_stage(self):
        self._main("mark-done", "--task-id", "task-1", "--stage", "pending")
        rc, out = self._main("mark-done", "--task-id", "task-1", "--stage", "done")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), str(pd.done_flag(self.root, "worker-3", "task-1")))
        self.assertTrue(pd.is_done_flag(pd.done_flag(self.root, "worker-3", "task-1")))

    def test_a_missing_stage_is_refused_rather_than_defaulted(self):
        # Defaulting a stage would let a caller that forgot it publish a finish.
        with self.assertRaises(SystemExit):
            self._main("mark-done", "--task-id", "task-1")
        self.assertFalse((self.root / "state" / "workers" / "worker-3").exists())

    def test_a_missing_task_id_is_refused(self):
        with self.assertRaises(SystemExit):
            self._main("mark-done", "--stage", "done")

    def test_a_refused_id_raises_rather_than_writing(self):
        with self.assertRaises(ValueError):
            self._main("mark-done", "--task-id", "not-a-task", "--stage", "done")
        self.assertFalse((self.root / "state" / "workers" / "worker-3").exists())

    def test_the_shipped_executable_records_a_stage(self):
        """The watcher spawns this file as a program, so the argv path is proven
        through the real executable and not only through main()."""
        r = subprocess.run(
            [sys.executable, str(Path(pd.__file__)), "--workspace", str(self.root),
             "--recipient", "worker-3", "mark-done", "--task-id", "task-1", "--stage", "done"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(pd.is_done_flag(pd.done_flag(self.root, "worker-3", "task-1")))


class TestClearPending(Base):
    """A hold the worker will not finish is withdrawn; a finish is never undone."""

    def test_it_removes_the_pending_stage(self):
        pd.mark_done(self.root, "worker-3", "task-1", published=False)
        got = pd.clear_pending(self.root, "worker-3", "task-1")
        self.assertEqual(got, pd.pending_flag(self.root, "worker-3", "task-1"))
        self.assertFalse(got.exists())

    def test_it_never_touches_a_published_flag(self):
        pd.mark_done(self.root, "worker-3", "task-1", published=True)
        pd.clear_pending(self.root, "worker-3", "task-1")
        self.assertTrue(pd.is_done_flag(pd.done_flag(self.root, "worker-3", "task-1")))

    def test_it_is_idempotent_when_nothing_is_pending(self):
        got = pd.clear_pending(self.root, "worker-3", "task-1")
        self.assertFalse(got.exists())
        self.assertFalse((self.root / "state" / "workers" / "worker-3").exists())

    def test_it_refuses_bad_ids_without_writing(self):
        for rec, tid in (("../x", "task-1"), ("worker-3", "task-a/b"), ("", "task-1")):
            with self.assertRaises(ValueError, msg=(rec, tid)):
                pd.clear_pending(self.root, rec, tid)
        self.assertEqual(list((self.root / "state" / "workers").iterdir())
                         if (self.root / "state" / "workers").exists() else [], [])

    def test_the_cli_abandon_stage_withdraws_a_hold(self):
        import io
        from contextlib import redirect_stdout
        pd.mark_done(self.root, "worker-3", "task-1", published=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pd.main(["--workspace", str(self.root), "--recipient", "worker-3",
                          "mark-done", "--task-id", "task-1", "--stage", "abandon"])
        self.assertEqual(rc, 0)
        self.assertFalse(pd.pending_flag(self.root, "worker-3", "task-1").exists())

    def _accepted(self, recipient, task_id):
        # Spelled literally for the same reason Workspace.deliver spells `.txt`.
        p = self.root / "deliveries" / recipient / f"{task_id}.accepted"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
        return p

    def test_abandon_retires_the_sentinel_so_the_sweep_never_reports_it_again(self):
        # Both sentinel names: the hold is gone, and so is the entry the sweep would read.
        for name, make in (("pending", lambda: self.ws.deliver("worker-3", "task-1")),
                           ("accepted", lambda: self._accepted("worker-3", "task-1"))):
            make(); self.ws.payload("task-1")
            pd.mark_done(self.root, "worker-3", "task-1", published=False)
            pd.clear_pending(self.root, "worker-3", "task-1")
            self.assertIsNone(pd.find(self.root, "worker-3", "task-1"), name)
            # the live core publishes later: nothing of this recipient's is left to misread
            self.ws.result("task-1")
            self.assertEqual(pd.sweep(self.root, "worker-3")["completed"], [], name)
            (self.root / "results" / "task-1.txt").unlink()

    def test_the_sentinel_is_found_and_removed_under_the_lock_accept_and_release_take(self):
        # A rename between the check and the unlink would leave the sentinel alive under
        # its other name, with the hold already withdrawn. The check must run locked.
        import fcntl
        from unittest import mock
        self.ws.deliver("worker-3", "task-1"); self.ws.payload("task-1")
        pd.mark_done(self.root, "worker-3", "task-1", published=False)
        seen = {}
        real_find = pd.find

        def find_under_scrutiny(workspace, recipient, task_id):
            with open(pd.deliveries_dir(workspace, recipient) / pd.LOCK_NAME, "a+") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    seen["held"] = True
                else:
                    fcntl.flock(fh, fcntl.LOCK_UN)
                    seen["held"] = False
            return real_find(workspace, recipient, task_id)

        with mock.patch.object(pd, "find", find_under_scrutiny):
            pd.clear_pending(self.root, "worker-3", "task-1")
        self.assertIs(seen.get("held"), True, "find ran outside the arbitration lock")
        self.assertIsNone(pd.find(self.root, "worker-3", "task-1"))

    def test_abandon_leaves_a_finished_delivery_for_the_sweep(self):
        self._accepted("worker-3", "task-1")
        pd.mark_done(self.root, "worker-3", "task-1", published=True)
        pd.clear_pending(self.root, "worker-3", "task-1")
        self.assertIsNotNone(pd.find(self.root, "worker-3", "task-1"))
        self.assertTrue(pd.is_done_flag(pd.done_flag(self.root, "worker-3", "task-1")))

    def test_writer_path_is_this_file_absolute_and_the_cli_prints_it(self):
        import io
        from contextlib import redirect_stdout
        self.assertTrue(pd.writer_path().is_absolute())
        self.assertEqual(pd.writer_path(), Path(pd.__file__).resolve())
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pd.main(["--workspace", str(self.root), "writer-path"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), str(pd.writer_path()))


class TestParseEntry(Base):
    """The inverse of `deliveries_dir` + the sentinel grammar. It lives here
    because the forward builder does: two owners of one layout drift.
    """

    def test_a_pending_entry_yields_its_workspace_recipient_and_task(self):
        s = self.ws.deliver("core", "task-1")
        ws, recipient, task_id, was_accepted = pd.parse_entry(s)
        self.assertEqual((recipient, task_id, was_accepted), ("core", "task-1", False))
        self.assertEqual(Path(ws).resolve(), self.root.resolve())

    def test_an_accepted_entry_reports_that_it_was_accepted(self):
        c = pd.accept(self.ws.deliver("core", "task-1"))
        _ws, _r, task_id, was_accepted = pd.parse_entry(c)
        self.assertEqual((task_id, was_accepted), ("task-1", True))

    def test_it_round_trips_the_forward_builder(self):
        s = self.ws.deliver("core", "task-1")
        ws, recipient, task_id, _ = pd.parse_entry(s)
        self.assertEqual(pd.deliveries_dir(ws, recipient) / f"{task_id}.txt", s)

    def test_the_pending_name_still_parses_once_the_sentinel_was_accepted(self):
        """The caller announces the name it saw; the delivery is the same one,
        so a rename in between must not read as undelivered."""
        s = self.ws.deliver("core", "task-1")
        pd.accept(s)
        self.assertFalse(s.exists())
        _ws, _r, task_id, _a = pd.parse_entry(s)
        self.assertEqual(task_id, "task-1")

    def test_a_name_with_no_sentinel_behind_it_is_refused(self):
        # Well-formed and in the right folder, but nothing was delivered: the
        # name alone must not authorise work.
        entry = pd.deliveries_dir(self.root, "core") / "task-1.txt"
        entry.parent.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(pd.NotDelivered):
            pd.parse_entry(entry)

    def test_a_name_that_is_not_a_sentinel_is_refused(self):
        d = pd.deliveries_dir(self.root, "core")
        d.mkdir(parents=True, exist_ok=True)
        for bad in ("notes.md", "task-1", "task-1.flag", ".lock"):
            (d / bad).write_text("", encoding="utf-8")
            with self.assertRaises(pd.NotDelivered, msg=bad):
                pd.parse_entry(d / bad)

    def test_a_folder_that_is_not_a_recipient_id_is_refused(self):
        d = self.root / "deliveries" / "Core_1"
        d.mkdir(parents=True)
        (d / "task-1.txt").write_text("", encoding="utf-8")
        with self.assertRaises(pd.NotDelivered):
            pd.parse_entry(d / "task-1.txt")

    def test_an_entry_outside_a_delivery_folder_is_refused(self):
        p = self.ws.payload("task-1")
        with self.assertRaises(pd.NotDelivered):
            pd.parse_entry(p)

    def test_an_explicit_workspace_that_agrees_is_returned_as_given(self):
        s = self.ws.deliver("core", "task-1")
        ws, _r, _t, _a = pd.parse_entry(s, self.root)
        self.assertEqual(ws, Path(self.root))

    def test_an_explicit_workspace_that_disagrees_is_refused(self):
        """Not a redirect: a tree the entry does not live in cannot be the one
        that delivered it, and picking either silently would split the two."""
        s = self.ws.deliver("core", "task-1")
        other = self.root / "elsewhere"
        (other / "deliveries" / "core").mkdir(parents=True)
        with self.assertRaises(pd.NotDelivered):
            pd.parse_entry(s, other)




if __name__ == "__main__":
    unittest.main(verbosity=2)
