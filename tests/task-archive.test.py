"""Tests for src/task_archive.py — find_task_file() helper (closes #933)."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import os
from datetime import datetime

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from task_archive import archive_file, find_task_file, task_id_from_filename


def exdev_from(src):
    """EXDEV is a property of the PAIR — a same-directory link never raises it,
    so an unconditional os.link mock also breaks the temp-publish step."""
    real = os.link

    def fake(a, b, **kw):
        if Path(a) == Path(src):
            raise OSError(18, "Cross-device link")
        return real(a, b, **kw)

    return mock.patch("os.link", side_effect=fake)


class TestArchiveNeverOverwrites(unittest.TestCase):
    """The success path must not destroy an existing archived record.

    Before the fix these called shutil.move, which REPLACES the destination on
    POSIX, so a repeated task id silently overwrote the earlier archive.
    """

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        self.tasks_dir = root / "archive-tasks"
        self.results_dir = root / "archive-results"
        self.live = root / "live"
        self.live.mkdir()
        self.month = datetime.now().strftime("%Y-%m")

    def _archive(self, src: Path, task_id: str) -> bool:
        return archive_file(src, "tasks", task_id, tasks_dir=self.tasks_dir,
                            results_dir=self.results_dir, log=lambda _m: None)

    def test_existing_archive_record_survives_a_repeated_id(self) -> None:
        (self.tasks_dir / self.month).mkdir(parents=True)
        prior = self.tasks_dir / self.month / "task-x.txt"
        prior.write_text("OLD-RECORD")
        src = self.live / "task-x.txt"
        src.write_text("NEW-RECORD")

        self.assertTrue(self._archive(src, "task-x"))
        self.assertEqual(prior.read_text(), "OLD-RECORD")
        self.assertFalse(src.exists(), "source must leave the live queue")
        self.assertEqual((self.tasks_dir / self.month / "task-x.txt.1").read_text(),
                         "NEW-RECORD")

    def test_normal_archive_still_lands_under_the_plain_name(self) -> None:
        src = self.live / "task-y.txt"
        src.write_text("BODY")
        self.assertTrue(self._archive(src, "task-y"))
        self.assertEqual((self.tasks_dir / self.month / "task-y.txt").read_text(),
                         "BODY")

    def test_cross_device_archive_copies_instead_of_linking(self) -> None:
        """os.link cannot span filesystems and the archive can be another mount:
        the temp-then-publish fallback must preserve the bytes and the source."""
        import task_archive
        src = self.live / "task-c.txt"
        src.write_text("PAYLOAD")
        with exdev_from(src):
            self.assertTrue(self._archive(src, "task-c"))
        landed = self.tasks_dir / self.month / "task-c.txt"
        self.assertEqual(landed.read_text(), "PAYLOAD")
        self.assertFalse(src.exists(), "source must still leave the live queue")

    def test_cross_device_fallback_also_refuses_to_clobber(self) -> None:
        (self.tasks_dir / self.month).mkdir(parents=True)
        (self.tasks_dir / self.month / "task-d.txt").write_text("OLD")
        src = self.live / "task-d.txt"
        src.write_text("NEW")
        with exdev_from(src):
            self.assertTrue(self._archive(src, "task-d"))
        self.assertEqual((self.tasks_dir / self.month / "task-d.txt").read_text(), "OLD")
        self.assertEqual((self.tasks_dir / self.month / "task-d.txt.1").read_text(), "NEW")

    def test_a_failed_cross_device_copy_leaves_no_partial_and_keeps_the_source(self) -> None:
        """A half-written archive that reads as complete is worse than none."""
        src = self.live / "task-e.txt"
        src.write_text("PAYLOAD")
        with exdev_from(src), \
             mock.patch("shutil.copyfileobj", side_effect=OSError(28, "No space left")):
            self._archive(src, "task-e")
        self.assertFalse((self.tasks_dir / self.month / "task-e.txt").exists(),
                         "a partial copy must be removed, not left looking archived")
        self.assertTrue(src.exists() or
                        (self.live / "task-e.txt.archive-failed").exists(),
                        "the bytes must survive somewhere in the live queue")

    def test_quarantine_does_not_clobber_an_earlier_quarantine(self) -> None:
        self.tasks_dir.mkdir()
        os.chmod(self.tasks_dir, 0o500)
        self.addCleanup(os.chmod, self.tasks_dir, 0o700)
        (self.live / "task-z.txt.archive-failed").write_text("FIRST")
        src = self.live / "task-z.txt"
        src.write_text("SECOND")

        self.assertTrue(self._archive(src, "task-z"))
        self.assertEqual((self.live / "task-z.txt.archive-failed").read_text(), "FIRST")
        self.assertEqual((self.live / "task-z.txt.archive-failed.1").read_text(), "SECOND")
        self.assertFalse(src.exists())


class TestFindTaskFile(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tasks_dir = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def _write(self, name: str, content: str = "task body") -> Path:
        p = self.tasks_dir / name
        p.write_text(content)
        return p

    def test_bare_file_returned(self) -> None:
        self._write("task-123.txt")
        result = find_task_file(self.tasks_dir, "task-123")
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "task-123.txt")

    def test_claimed_file_returned_when_bare_missing(self) -> None:
        self._write("task-456.claimed-worker-2.txt")
        result = find_task_file(self.tasks_dir, "task-456")
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "task-456.claimed-worker-2.txt")

    def test_bare_preferred_over_claimed(self) -> None:
        self._write("task-789.txt")
        self._write("task-789.claimed-worker-1.txt")
        result = find_task_file(self.tasks_dir, "task-789")
        self.assertEqual(result.name, "task-789.txt")

    def test_returns_none_when_no_file(self) -> None:
        result = find_task_file(self.tasks_dir, "task-nonexistent")
        self.assertIsNone(result)

    def test_multiple_claimed_returns_first_lexicographic(self) -> None:
        self._write("task-000.claimed-worker-2.txt")
        self._write("task-000.claimed-worker-3.txt")
        result = find_task_file(self.tasks_dir, "task-000")
        self.assertIsNotNone(result)
        self.assertIn("claimed-worker-", result.name)



class CollisionRecordsStayReachable(unittest.TestCase):
    """A collision that the production reader cannot see is still data loss:
    the record exists and every lookup returns the superseded one."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        self.tasks = root / "tasks"
        (self.tasks / "archive").mkdir(parents=True)
        self.results = root / "results"
        self.results.mkdir()

    def test_the_production_reader_returns_the_NEWEST_colliding_record(self) -> None:
        import task_archive
        import local_task_protocol
        tid = "task-1786600000000"
        for chan in ("OLD", "NEW"):
            src = self.tasks / f"{tid}.txt"
            src.write_text(f"id: {tid}\nchannel_id: {chan}\ntask: x\n")
            task_archive.archive_file(src, "tasks", tid, tasks_dir=self.tasks / "archive",
                                      results_dir=self.results, log=lambda *a: None)
        got = local_task_protocol.find_archived_task(self.tasks, tid)
        self.assertIsNotNone(got)
        self.assertIn("channel_id: NEW", got.read_text(),
                      "reader returned the superseded record — routing would use a stale channel")

    def test_selection_is_numeric_so_txt_10_beats_txt_2(self) -> None:
        import local_task_protocol
        tid = "task-1786600000001"
        d = self.tasks / "archive"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}.txt").write_text(f"id: {tid}\ntask: x\n")
        for i in range(1, 11):
            (d / f"{tid}.txt.{i}").write_text(f"id: {tid}\nchannel_id: N{i}\ntask: x\n")
        got = local_task_protocol.find_archived_task(self.tasks, tid)
        self.assertTrue(got.name.endswith(".10"), f"string order picked {got.name}")

    def test_archive_failed_quarantine_is_not_mistaken_for_a_collision(self) -> None:
        import local_task_protocol
        tid = "task-1786600000002"
        d = self.tasks / "archive"
        (d / f"{tid}.txt").write_text(f"id: {tid}\nchannel_id: REAL\ntask: x\n")
        (d / f"{tid}.txt.archive-failed-1").write_text("junk")
        got = local_task_protocol.find_archived_task(self.tasks, tid)
        self.assertIn("channel_id: REAL", got.read_text())


class ArchiveLookupNeverScansTheDirectory(unittest.TestCase):
    """find_archived_task runs in agent-api's per-poll loop over an archive that
    reached 5,716 entries; a glob there measured 442x an exists()."""

    def test_newest_archived_probes_exact_names_and_never_globs(self) -> None:
        import local_task_protocol
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "task-x.txt").write_text("a")
            (d / "task-x.txt.1").write_text("b")
            def boom(self, *a, **kw):
                raise AssertionError("newest_archived scanned the directory")
            with mock.patch.object(Path, "glob", boom), \
                 mock.patch.object(Path, "iterdir", boom), \
                 mock.patch("os.scandir", boom):
                got = local_task_protocol.newest_archived(d, "task-x")
            self.assertEqual(got.name, "task-x.txt.1")

    def test_an_absent_id_costs_one_probe_and_returns_none(self) -> None:
        import local_task_protocol
        with tempfile.TemporaryDirectory() as td:
            def boom(self, *a, **kw):
                raise AssertionError("scanned the directory for an absent id")
            with mock.patch.object(Path, "glob", boom):
                self.assertIsNone(local_task_protocol.newest_archived(Path(td), "task-nope"))


class CrashDuringCrossDeviceCopy(unittest.TestCase):
    """A mocked exception exercises cleanup; only a real kill proves the
    authoritative name is never published half-written."""

    SCRIPT = """
import os, shutil, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import task_archive as TA
src = Path(sys.argv[2]) / "live.txt"
dest = Path(sys.argv[2]) / "dest"
real_link = os.link
def fake_link(a, b, **k):
    if Path(a) == src: raise OSError(18, "EXDEV")
    return real_link(a, b, **k)
os.link = fake_link
def crashing(inp, out, *a, **k):
    out.write(b"PART"); out.flush(); os._exit(23)
shutil.copyfileobj = crashing
TA._move_without_clobbering(src, dest / "task-crash.txt")
"""

    def test_a_kill_mid_copy_publishes_no_authoritative_record(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "live.txt").write_text("COMPLETE\n" * 200)
            (root / "dest").mkdir()
            src_dir = str(Path(__file__).resolve().parent.parent / "src")
            proc = subprocess.run([sys.executable, "-c", self.SCRIPT, src_dir, str(root)],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 23, proc.stderr)
            self.assertFalse((root / "dest" / "task-crash.txt").exists(),
                             "a truncated record was published under the authoritative name")
            self.assertTrue((root / "live.txt").exists(), "the live source must survive")


class TaskIdFromFilename(unittest.TestCase):
    """`.stem` and a greedy `^task-(.+)\\.txt$` both return the compound name for a
    CLAIMED file, so the caller looks for a reply under an id nothing writes."""

    def test_every_real_filename_form_yields_one_canonical_id(self):
        for name in ("task-abc123.txt",
                     "task-abc123.claimed-worker-2.txt",
                     "task-abc123.claimed-worker-11.txt",
                     "task-abc123.assigned-worker-3.txt",
                     "task-abc123.assigned-follower-7.txt",
                     "task-abc123.txt.1",
                     "task-abc123.txt.archive-failed-9"):
            with self.subTest(name=name):
                self.assertEqual(task_id_from_filename(name), "task-abc123")

    def test_claimed_is_the_regression_stem_gets_wrong(self):
        name = "task-abc123.claimed-worker-2.txt"
        self.assertEqual(Path(name).stem, "task-abc123.claimed-worker-2")   # the old behaviour
        self.assertEqual(task_id_from_filename(name), "task-abc123")      # the fixed one

    def test_instance_label_is_opaque(self):
        """pool_lead interpolates the instance with re.escape, so the label is
        arbitrary — a reader must not assume `core-<digits>`."""
        for name in ("task-abc123.claimed-worker-x.txt",
                     "task-abc123.assigned-follower-7.txt",
                     "task-abc123.claimed-worker-2.local.txt"):
            with self.subTest(name=name):
                self.assertEqual(task_id_from_filename(name), "task-abc123")

    def test_a_dot_inside_an_id_is_legal(self):
        """pool_lead allows [A-Za-z0-9._~-] in an id and excludes the state
        suffixes by lookahead, so banning dots would reject a valid name."""
        self.assertEqual(task_id_from_filename("task-a.b.txt"), "task-a.b")
        self.assertEqual(
            task_id_from_filename("task-a.b.claimed-worker-2.txt"), "task-a.b")

    def test_hyphenated_ids_survive(self):
        self.assertEqual(
            task_id_from_filename("task-cron-pending-questions-1787641302891.txt"),
            "task-cron-pending-questions-1787641302891")

    def test_non_task_and_malformed_names_are_rejected_not_guessed(self):
        for name in ("proactive-123.txt", "notes.txt", "reply-1.txt"):
            with self.subTest(name=name):
                self.assertIsNone(task_id_from_filename(name))

    def test_find_task_file_accepts_every_state_the_id_parser_does(self):
        """One filename grammar for both functions: a narrower glob here handles
        one state, misses its sibling, and the archive silently strands the file."""
        cases = {
            "task-A.txt": "task-A",
            "task-B.claimed-worker-1.txt": "task-B",
            "task-C.assigned-worker-2.txt": "task-C",
            "task-D.claimed-worker-7.txt": "task-D",
            "task-E.claimed-core1.txt": "task-E",
            "task-F.a.claimed-worker-3.txt": "task-F.a",
        }
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp)
            for name in cases:
                (tasks / name).write_text("x")
            for name, task_id in cases.items():
                with self.subTest(name=name):
                    self.assertEqual(task_id_from_filename(name), task_id)
                    found = find_task_file(tasks, task_id)
                    self.assertIsNotNone(found, f"{name} not located by {task_id}")
                    self.assertEqual(found.name, name)

    def test_a_shorter_id_does_not_grab_a_dotted_siblings_file(self):
        """The glob is a prefix match, so the grammar must confirm the id itself."""
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp)
            (tasks / "task-F.a.claimed-worker-3.txt").write_text("x")
            self.assertIsNone(find_task_file(tasks, "task-F"))

    def test_round_trips_with_find_task_file(self):
        """The two directions must agree, or a locator and a reader disagree."""
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp)
            claimed = tasks / "task-abc123.claimed-worker-2.txt"
            claimed.write_text("id: task-abc123\n")
            task_id = task_id_from_filename(claimed.name)
            self.assertEqual(task_id, "task-abc123")
            self.assertEqual(find_task_file(tasks, task_id), claimed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
