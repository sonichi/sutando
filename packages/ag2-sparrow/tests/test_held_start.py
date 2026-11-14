"""held_start: a connector run --held enters the held root and starts only once attached.

Run:
    python3 packages/ag2-sparrow/tests/test_held_start.py
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ag2_sparrow import held_start, task_io  # noqa: E402


class HeldStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.here = os.getcwd()
        self.fd = os.open(self.tmp.name, os.O_RDONLY)

    def tearDown(self):
        os.chdir(self.here)
        os.close(self.fd)
        self.tmp.cleanup()

    def enter(self, record, fd=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = held_start.enter(self.fd if fd is None else fd, 0.1, lambda root: record)
        return rc, err.getvalue()

    def test_attached_and_verified_starts_inside_the_held_root(self):
        me = {"pid": os.getpid(), "live": True, "start": task_io.start_time(os.getpid())}
        self.assertEqual(self.enter(me), (None, ""))
        self.assertTrue(os.path.samefile(os.getcwd(), self.tmp.name))

    def test_every_unverified_record_is_one_refusal_line(self):
        mine = task_io.start_time(os.getpid())
        for record, word in (({"start": mine, "live": False}, "liveness false"),
                             ({"start": mine, "live": None}, "liveness unknown"),
                             ({"start": "bogus", "live": True}, "a start time that is not this process's")):
            rc, err = self.enter(dict(record, pid=os.getpid()))
            self.assertEqual(rc, 2)
            self.assertIn("names this pid with %s, not verified" % word, err)

    def test_no_holder_or_another_process_or_a_bad_descriptor_is_refused(self):
        for record in (None, {"pid": os.getpid() + 1, "live": True}):
            rc, err = self.enter(record)
            self.assertEqual(rc, 2)
            self.assertIn("attached this process", err)
        rc, err = self.enter(None, fd=987654)
        self.assertEqual(rc, 2)
        self.assertIn("--root-fd 987654 is not the held root's directory", err)

    def test_a_stop_request_ends_the_wait(self):
        with mock.patch.object(held_start, "stop_requested", lambda: True):
            with mock.patch.object(held_start, "WAIT_S", 60):
                rc, _ = self.enter(None)
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
