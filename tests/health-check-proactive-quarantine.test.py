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
_SFP = os.path.join(_HERE, "..", "src", "send_failure_policy.py")
_sfp_spec = importlib.util.spec_from_file_location("send_failure_policy", _SFP)
sfp = importlib.util.module_from_spec(_sfp_spec)
_sfp_spec.loader.exec_module(sfp)


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
    def test_the_detail_names_no_transport_it_did_not_measure(self):
        """A parked body records no transport, so the warn must not name one.

        The docstring's Discord case is one real incident (#2626, a 413 from
        discord.com). The emitted text used to generalise that transport to
        every parked body — and the live bodies that prompted this were parked
        by remote-gateway-bridge, not Discord at all. Asserted in BOTH
        directions: a positive-only check passes on "delivery failed via Discord".
        """
        with tempfile.TemporaryDirectory() as td:
            self._quarantine(td)
            (pathlib.Path(td) / "results" / "undelivered" / "task-1.txt").write_text("body")
            out = self._run(td)
        self.assertEqual(out["status"], "warn")
        detail = out["detail"]
        self.assertIn("results/undelivered/", detail)
        for transport in ("Discord", "Slack", "Telegram", "Matrix", "gateway"):
            self.assertNotIn(transport, detail,
                             f"detail names {transport}, which the probe never measured")
        self.assertNotIn("refused", detail,
                         "'refused' implies policy/permission; a transient network "
                         "failure parks a body too, and points at a different fix")
        # The list above pins two words already known wrong, not "states only what
        # was measured": "after delivery failed" cleared it and was still false.
        self.assertNotIn("delivery failed", detail)

    def test_the_detail_tallies_the_reasons_the_filenames_record(self):
        """`_quarantine_orphan` writes <tid>.<reason>.<ts>.txt for five reasons,
        of which only one is a delivery failure; the send-failure path records
        none. The summary reports what is written, not a cause for all of them."""
        with tempfile.TemporaryDirectory() as td:
            self._quarantine(td)
            d = pathlib.Path(td) / "results" / "undelivered"
            for n in ("t-1.no-task.1.txt", "t-2.no-task.2.txt",
                      "t-3.undeliverable-after-retries.3.txt",
                      "task-abc-4.txt"):
                (d / n).write_text("body")
            out = self._run(td)
        self.assertEqual(out["status"], "warn")
        self.assertIn("2 no-task", out["detail"])
        self.assertIn("1 undeliverable-after-retries", out["detail"])
        self.assertIn("1 unlabelled", out["detail"],
                      "a body with no reason token must not be given one")


    # --- recency: is this directory FILLING, or inert history? -----------

    # Arrival is ctime, which no test can assign: aged history is made by
    # advancing the probe's clock, "just arrived" by the production writer.
    def _park(self, q, stem, age_s):
        b = q / f"{stem}.txt"
        b.write_text("body")
        t = time.time() - age_s
        os.utime(b, (t, t))
        return b

    def _run_at(self, td, later_s):
        with mock.patch.object(hc.time, "time", return_value=time.time() + later_s):
            return self._run(td)

    def _park_through_the_writer(self, q, stem, body_age_s):
        """Drive `send_failure_policy.resolve_failed_send` -- the production
        writer, bundled verbatim into ag2_sparrow -- so the body reaches
        undelivered/ by ITS rename, with the mtime it already had."""
        # The bridge claims `proactive-<ts>.txt` by renaming it to `.sending`;
        # the writer derives the parked name back with `with_suffix(".txt")`.
        claim = q.parent / f"{stem}.sending"
        claim.write_text("body")
        t = time.time() - body_age_s
        os.utime(claim, (t, t))
        # A 413 never becomes a 200: a status-less, non-transient error parks.
        out = sfp.resolve_failed_send(claim, RuntimeError("413"), {},
                                      undelivered_dir=q)
        self.assertEqual(out, "parked")
        return q / f"{stem}.txt"

    def test_the_production_writer_keeps_mtime_and_the_probe_still_sees_arrival(self):
        """The blocker: `rename()` parks an existing inode with its OLD mtime,
        so an mtime-derived "arrived" reports the body's age, not the park."""
        with tempfile.TemporaryDirectory() as td:
            q = self._quarantine(td)
            body = self._park_through_the_writer(q, "proactive-day-old", 24 * 3600)
            self.assertGreater(time.time() - body.stat().st_mtime, 23 * 3600,
                               "the writer must not have refreshed mtime, or this "
                               "test proves nothing about the rename path")
            d = self._run(td)["detail"]
        self.assertIn("oldest proactive-day-old.txt (24h0m)", d)
        self.assertIn("newest arrived 0h0m ago", d)

    def test_an_inert_backlog_and_a_filling_one_do_not_read_the_same(self):
        """The control the change exists for, in the reviewer's shape: same
        names, same count, same oldest label, same file mtimes -- one directory
        untouched for seven days, the other parked by the writer just now."""
        details = {}
        with tempfile.TemporaryDirectory() as td:
            q = self._quarantine(td)
            self._park(q, "proactive-old", 100 * 3600)
            self._park(q, "proactive-new", 24 * 3600)
            details["inert"] = self._run_at(td, 168 * 3600)["detail"]
        with tempfile.TemporaryDirectory() as td:
            q = self._quarantine(td)
            self._park(q, "proactive-old", 268 * 3600)
            self._park_through_the_writer(q, "proactive-new", 192 * 3600)
            details["filling"] = self._run(td)["detail"]
        for d in details.values():
            self.assertIn("oldest proactive-old.txt (268h0m)", d)
        self.assertIn("newest arrived 168h0m ago", details["inert"])
        self.assertIn("newest arrived 0h0m ago", details["filling"])
        self.assertNotEqual(details["inert"], details["filling"],
                            "inert and actively-filling quarantines render identically")

    def test_a_single_body_does_not_repeat_its_own_age(self):
        """One body written and parked at the same instant: oldest IS newest."""
        with tempfile.TemporaryDirectory() as td:
            q = self._quarantine(td)
            (q / "proactive-solo.txt").write_text("body")
            d = self._run_at(td, 5 * 3600)["detail"]
            self.assertIn("5h0m", d)
            self.assertNotIn("newest arrived", d)

    def test_identical_rendered_durations_do_not_print_twice(self):
        """7201s and 7200s both render 2h0m; gating on raw seconds would print
        "oldest X (2h0m); newest arrived 2h0m ago" -- true, and redundant."""
        with tempfile.TemporaryDirectory() as td:
            q = self._quarantine(td)
            self._park(q, "proactive-a", 1)          # mtime one second older
            (q / "proactive-b.txt").write_text("body")
            d = self._run_at(td, 7200)["detail"]
            self.assertIn("2h0m", d)
            self.assertNotIn("newest arrived", d)
            # ...and a minute of spread is enough to bring the clause back.
            self._park(q, "proactive-c", 60)
            self.assertIn("newest arrived 2h0m ago", self._run_at(td, 7200)["detail"])

    def test_the_arrival_age_tracks_the_newest_not_the_count(self):
        """Adding OLDER files must not change the reported arrival age -- a
        clause keyed on len() or on the oldest would move here."""
        with tempfile.TemporaryDirectory() as td:
            q = self._quarantine(td)
            self._park(q, "proactive-a", 400 * 3600)
            self._park(q, "proactive-b", 100 * 3600)
            first = self._run_at(td, 100 * 3600)["detail"]
            self._park(q, "proactive-c", 900 * 3600)   # older than both
            self.assertIn("100h0m ago", first)
            self.assertIn("100h0m ago", self._run_at(td, 100 * 3600)["detail"])

    def test_a_clock_behind_the_files_is_labelled_not_rendered_as_ago(self):
        """A negative age is skew, not a recent arrival: never "-1h55m ago"."""
        with tempfile.TemporaryDirectory() as td:
            q = self._quarantine(td)
            (q / "proactive-ahead.txt").write_text("body")
            d = self._run_at(td, -630)["detail"]
        self.assertIn("future-dated by 0h10m", d)
        self.assertNotIn(" ago", d)
        self.assertNotRegex(d, r"-\d+h")

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
