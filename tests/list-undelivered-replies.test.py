#!/usr/bin/env python3
"""The lister must name a reply nothing is coming for, AND stay silent about
one a consumer can still reach. A run that finds nothing proves neither."""
import importlib.util
import io
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "lur", REPO / "scripts" / "list-undelivered-replies.py")
lur = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lur)


class Lister(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        for d in ("tasks", "tasks/archive", "results"):
            (self.ws / d).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _orphan(self, tid, dest="!ROOM:ag2.space", tier="owner", where="archive"):
        hdr = (f"id: {tid}\nsource: ag2space\nchannel_id: {dest}\n"
               f"user_id: @someone:ag2.space\naccess_tier: {tier}\ntask: t\n")
        (self.ws / "tasks" / where / f"{tid}.txt").write_text(hdr) if where == "archive" \
            else (self.ws / "tasks" / f"{tid}.txt").write_text(hdr)
        (self.ws / "results" / f"{tid}.txt").write_text("the reply body\n")

    def test_a_task_that_exists_nowhere_yields_an_empty_header(self):
        """The `return {}` fallback: a result whose task file is gone from every
        location must still be listed, with UNKNOWN rather than a crash."""
        (self.ws / "results" / "task-vanished.txt").write_text("body\n")
        rows = lur.undelivered(self.ws)
        self.assertEqual([r[0] for r in rows], ["task-vanished"])
        self.assertEqual(rows[0][2], {})

    def test_an_unreadable_candidate_falls_through_to_the_next(self):
        """A directory where a task file should be raises OSError on read; the
        loop must continue rather than abort on the first candidate."""
        (self.ws / "tasks" / "task-dir.txt").mkdir()
        (self.ws / "tasks" / "archive" / "task-dir.txt").write_text(
            "id: task-dir\nchannel_id: !R:ag2.space\n")
        self.assertEqual(lur._task_header(self.ws, "task-dir")["channel_id"],
                         "!R:ag2.space")

    def test_resolve_workspace_goes_through_the_loader(self):
        """Covers the bootstrap: it must return the SAME path the shared
        resolver does, not a repo-relative guess of its own."""
        saved = list(sys.path)
        try:
            got = lur._resolve_workspace(REPO)
        finally:
            sys.path[:] = saved
        sys.path.insert(0, str(REPO / "src"))
        try:
            from workspace_default import resolve_workspace
            self.assertEqual(got, Path(resolve_workspace()))
        finally:
            sys.path[:] = saved

    def test_names_the_orphan_and_its_destination(self):
        self._orphan("task-aaa")
        rows = lur.undelivered(self.ws)
        self.assertEqual([r[0] for r in rows], ["task-aaa"])
        self.assertEqual(rows[0][2]["channel_id"], "!ROOM:ag2.space")
        self.assertEqual(rows[0][2]["access_tier"], "owner")

    def test_a_task_still_queued_is_not_listed(self):
        """Negative control: a consumer can still reach that pair, so
        reporting it would send an operator to deliver a live reply twice."""
        self._orphan("task-bbb", where="tasks")
        self.assertEqual(lur.undelivered(self.ws), [])

    def test_a_claimed_task_is_not_listed(self):
        """A task being worked right now lives under its claimed name, not the
        plain one. Listing it sends an operator to deliver a reply the worker
        is about to deliver itself."""
        self._orphan("task-ddd", where="tasks")
        (self.ws / "tasks" / "task-ddd.txt").rename(
            self.ws / "tasks" / "task-ddd.claimed-core-2.txt")
        self.assertEqual(lur.undelivered(self.ws), [])

    def test_an_assigned_task_is_not_listed(self):
        """Same class one step earlier: the lead has handed it to a core that
        has not claimed it yet."""
        self._orphan("task-eee", where="tasks")
        (self.ws / "tasks" / "task-eee.txt").rename(
            self.ws / "tasks" / "task-eee.assigned-core-3.txt")
        self.assertEqual(lur.undelivered(self.ws), [])

    def test_a_longer_task_id_does_not_suppress_a_real_orphan(self):
        """The dot in `{id}.*.txt` is load-bearing. Without it the live
        task-fff0 would prefix-match and hide task-fff, which IS orphaned."""
        self._orphan("task-fff0", where="tasks")
        self._orphan("task-fff")
        self.assertEqual([r[0] for r in lur.undelivered(self.ws)], ["task-fff"])

    def test_a_header_without_a_room_is_flagged_not_guessed(self):
        (self.ws / "tasks" / "archive" / "task-ccc.txt").write_text(
            "id: task-ccc\nsource: local\ntask: t\n")
        (self.ws / "results" / "task-ccc.txt").write_text("body\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            lur.main(["--workspace", str(self.ws)])
        out = buf.getvalue()
        self.assertIn("dest   UNKNOWN", out)
        self.assertIn("do NOT guess a room", out)

    def test_empty_workspace_says_so(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            lur.main(["--workspace", str(self.ws)])
        self.assertIn("no replies awaiting delivery", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
