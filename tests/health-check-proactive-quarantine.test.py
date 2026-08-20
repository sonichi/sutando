#!/usr/bin/env python3
"""Tests for health-check.py's check_proactive_quarantine.

#2626 stops `poll_proactive` from deleting a proactive DM that Discord refused,
moving the body to `results/undelivered/` instead. That is strictly better than
destroying it, but left the body with no consumer: at that change's head the only
code touching the directory is the writer. This probe is the reader — so what it
reports is that nothing drains the directory, never that nobody has been told.

The controls that matter here are the ones that would let the probe report a
clean host while a message sits unread:

  * a NON-EMPTY quarantine must warn      <- the whole point; must fail if the
                                             probe is neutered to always-ok
  * an ABSENT directory must be ok        <- silent before #2626 lands, so it
                                             cannot invent a problem
  * an EMPTY directory must be ok         <- without this, "warns on non-empty"
                                             is satisfied by warning always
  * an unreadable entry must be COUNTED, not rounded down into a clean verdict
  * a sub-DIRECTORY is not a message      <- it must not inflate the count

Hermetic: WORKSPACE_DIR is rebound to a tmpdir for every case, and the last
test asserts the operator's real workspace was never touched.

Run: python3 tests/health-check-proactive-quarantine.test.py
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src", "health-check.py")
_spec = importlib.util.spec_from_file_location("health_check", _SRC)
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)


class TestProactiveQuarantine(unittest.TestCase):
    def _run(self, td):
        with mock.patch.object(hc, "WORKSPACE_DIR", pathlib.Path(td)):
            return hc.check_proactive_quarantine()

    def _quarantine(self, td):
        q = pathlib.Path(td) / "results" / "undelivered"
        q.mkdir(parents=True, exist_ok=True)
        return q

    # --- the point ------------------------------------------------------
    def test_a_kept_body_warns_and_is_named(self):
        with tempfile.TemporaryDirectory() as td:
            q = self._quarantine(td)
            body = q / "proactive-1785870055.txt"
            body.write_text("[file: /tmp/sutando-oversize.bin]")
            old = time.time() - 2 * 3600 - 15 * 60
            os.utime(body, (old, old))
            r = self._run(td)
            self.assertEqual(r["status"], "warn", r)
            self.assertIn("proactive-1785870055.txt", r["detail"])
            self.assertIn("2h15m", r["detail"])
            # The verdict must say WHY it matters, not just that a file exists.
            self.assertIn("no consumer drains this directory", r["detail"])
            # ...and must not say nobody was told. Emitting this line IS telling;
            # the claim was quoted as an independent finding twice.
            self.assertNotIn("nobody has been told", r["detail"])

    def test_every_kept_body_is_counted(self):
        with tempfile.TemporaryDirectory() as td:
            q = self._quarantine(td)
            for i in range(3):
                (q / f"proactive-{i}.txt").write_text("x")
            r = self._run(td)
            self.assertEqual(r["status"], "warn", r)
            self.assertIn("3 proactive message(s)", r["detail"])

    # --- the controls that stop "warn" from being free -------------------
    def test_absent_directory_is_ok(self):
        """Silent before #2626 lands. A probe that warns on a directory nothing
        creates yet is noise that trains its reader to ignore it."""
        with tempfile.TemporaryDirectory() as td:
            r = self._run(td)
            self.assertEqual(r["status"], "ok", r)
            self.assertIn("absent", r["detail"])

    def test_empty_directory_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            self._quarantine(td)
            r = self._run(td)
            self.assertEqual(r["status"], "ok", r)

    def test_a_subdirectory_is_not_a_message(self):
        with tempfile.TemporaryDirectory() as td:
            q = self._quarantine(td)
            (q / "somedir").mkdir()
            r = self._run(td)
            self.assertEqual(r["status"], "ok", r)

    # --- coverage is part of the verdict ---------------------------------
    def test_an_unreadable_entry_is_reported_not_rounded_down(self):
        """An entry we cannot stat must appear in the detail. Rounding it into
        'no quarantined bodies' is how a probe reports clean while blind."""
        with tempfile.TemporaryDirectory() as td:
            q = self._quarantine(td)
            boom = q / "unstattable.txt"
            boom.write_text("x")
            real_stat = pathlib.Path.stat
            real_is_file = pathlib.Path.is_file

            def _is_file(self, *a, **k):
                if self.name == "unstattable.txt":
                    return True
                return real_is_file(self, *a, **k)

            def _stat(self, *a, **k):
                if self.name == "unstattable.txt":
                    raise OSError(5, "I/O error")
                return real_stat(self, *a, **k)

            # is_file() must be patched too: pathlib swallows OSError inside it
            # and returns False, so patching stat alone would skip the entry one
            # line earlier and the "unreadable" branch would never execute while
            # the assertion still passed.
            with mock.patch.object(pathlib.Path, "is_file", _is_file), \
                 mock.patch.object(pathlib.Path, "stat", _stat):
                r = self._run(td)
            self.assertEqual(r["status"], "warn", r)
            self.assertIn("1 entry unreadable", r["detail"])

    def test_a_scan_failure_warns_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as td:
            self._quarantine(td)
            with mock.patch.object(pathlib.Path, "iterdir",
                                   side_effect=OSError(13, "denied")):
                r = self._run(td)
            self.assertEqual(r["status"], "warn", r)
            self.assertIn("could not scan", r["detail"])

    # --- hermetic ---------------------------------------------------------
    def test_the_operators_real_workspace_is_never_touched(self):
        before = None
        real = hc.WORKSPACE_DIR / "results" / "undelivered"
        if real.is_dir():
            before = sorted(p.name for p in real.iterdir())
        with tempfile.TemporaryDirectory() as td:
            self._quarantine(td)
            (pathlib.Path(td) / "results" / "undelivered" / "x.txt").write_text("x")
            self._run(td)
        after = sorted(p.name for p in real.iterdir()) if real.is_dir() else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
