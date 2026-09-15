#!/usr/bin/env python3
"""The core records which conversation it is having, not just that it is alive.

`state/cores/<host>.alive` answers "is a core running?" and nothing else: its
`session` field is the TMUX session NAME, not the runtime conversation id. So
when a host reboots, the workers can be brought back on their own history --
their `sessions.json` names it -- and the core cannot, because nothing ever
wrote down which conversation it was having. That asymmetry is what makes a
reboot cost the core's continuity.

This pins the core's half: the same lineage the worker keeps (sessions, runs,
and which run is current), written where the core's own state lives.

The load-bearing rule is that an UNKNOWN session is never invented. The
heartbeat runs as a detached process and may not inherit CLAUDE_CODE_SESSION_ID,
so "I don't know" must stay distinguishable from "there is no session" -- a
fabricated id would resume the wrong conversation, which is worse than
resuming none.

Run: python3 tests/core-lineage-records.test.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
import time
import fcntl
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import core_lineage as cl  # noqa: E402


class CoreLineage(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.host = "test-host"
        self.sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def test_a_session_is_recorded_and_becomes_current(self):
        cl.record_run(self.ws, self.host, self.sid, runtime="claude",
                      cwd="/repo", tmux_session="sutando-core")
        self.assertEqual(cl.current(self.ws, self.host)["session_id"], self.sid)
        self.assertEqual(len(cl.sessions(self.ws, self.host)), 1)
        self.assertEqual(len(cl.runs(self.ws, self.host)), 1)

    def test_the_same_session_beating_again_is_not_a_new_run(self):
        """The heartbeat calls this every 30s. If each beat opened a run, the
        record would be a log of beats rather than of restarts."""
        for _ in range(3):
            cl.record_run(self.ws, self.host, self.sid, runtime="claude", cwd="/repo")
        self.assertEqual(len(cl.runs(self.ws, self.host)), 1,
                         "a repeated heartbeat opened extra runs")
        self.assertEqual(len(cl.sessions(self.ws, self.host)), 1)

    def test_a_restart_on_a_new_session_appends_both(self):
        cl.record_run(self.ws, self.host, self.sid, runtime="claude", cwd="/repo")
        second = "ffffffff-1111-2222-3333-444444444444"
        cl.record_run(self.ws, self.host, second, runtime="claude", cwd="/repo")
        self.assertEqual(len(cl.sessions(self.ws, self.host)), 2)
        self.assertEqual(len(cl.runs(self.ws, self.host)), 2)
        self.assertEqual(cl.current(self.ws, self.host)["session_id"], second,
                         "current must name the session running NOW")

    def test_an_unknown_session_is_never_invented(self):
        """The heartbeat may not inherit the session id. Recording nothing is
        correct; recording a placeholder would later resume the wrong thing."""
        cl.record_run(self.ws, self.host, "", runtime="claude", cwd="/repo")
        self.assertEqual(cl.sessions(self.ws, self.host), [])
        self.assertEqual(cl.current(self.ws, self.host)["session_id"], None)

    def test_the_transcript_is_located_when_it_exists(self):
        proj = self.ws / ".claude-sutando" / "projects" / "-repo"
        proj.mkdir(parents=True)
        (proj / f"{self.sid}.jsonl").write_text("{}\n")
        cl.record_run(self.ws, self.host, self.sid, runtime="claude", cwd="/repo")
        row = cl.sessions(self.ws, self.host)[0]
        self.assertTrue(row["transcript"]["path"].endswith(f"{self.sid}.jsonl"))

    def test_a_missing_transcript_leaves_the_locator_empty(self):
        cl.record_run(self.ws, self.host, self.sid, runtime="claude", cwd="/repo")
        self.assertEqual(cl.sessions(self.ws, self.host)[0]["transcript"]["path"], "",
                         "a transcript that does not exist was recorded as if it did")

    def test_a_failed_write_removes_its_temp_file_and_reraises(self):
        """The atomic writer must not leave a .tmp behind for a reader to find."""
        d = Path(self.ws) / "state" / "cores"
        with mock.patch.object(cl.json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cl._write(d / "x.json", {"a": 1})
        leftovers = [q.name for q in d.glob(".x.json.*")] if d.is_dir() else []
        self.assertEqual(leftovers, [], f"temp file survived a failed write: {leftovers}")

    def test_two_callers_past_the_early_check_open_one_run_not_two(self):
        """The early return is outside the lock; the newest row must settle it."""
        with mock.patch.object(cl, "current", return_value={}):
            cl.record_run(self.ws, self.host, self.sid, runtime="claude", cwd="/x")
            cl.record_run(self.ws, self.host, self.sid, runtime="claude", cwd="/x")
        rows = cl.runs(self.ws, self.host)
        self.assertEqual([r["session_id"] for r in rows], [self.sid],
                         f"one session opened {len(rows)} runs: {rows}")

    def test_a_failing_cleanup_does_not_replace_the_write_error(self):
        """The unlink error must not become the exception the caller sees."""
        d = Path(self.ws) / "state" / "cores"
        with mock.patch.object(cl.json, "dump", side_effect=OSError("disk full")), \
             mock.patch.object(cl.os, "unlink", side_effect=OSError("unlink denied")):
            with self.assertRaises(OSError) as ctx:
                cl._write(d / "y.json", {"a": 1})
        self.assertIn("disk full", str(ctx.exception),
                      f"cleanup error masked the real one: {ctx.exception}")

    def test_an_empty_cwd_or_session_locates_nothing(self):
        """Empty means "no file", and must not compose a path out of blanks."""
        self.assertEqual(cl.transcript_path_for(self.ws, "", "abc"), "")
        self.assertEqual(cl.transcript_path_for(self.ws, "/some/dir", ""), "")
        self.assertEqual(cl.transcript_path_for(self.ws, "", ""), "")

    def test_each_host_keeps_its_own_lineage(self):
        cl.record_run(self.ws, "host-a", self.sid, runtime="claude", cwd="/repo")
        cl.record_run(self.ws, "host-b", "99999999-0000-0000-0000-000000000000",
                      runtime="claude", cwd="/repo")
        self.assertEqual(len(cl.sessions(self.ws, "host-a")), 1)
        self.assertNotEqual(cl.current(self.ws, "host-a")["session_id"],
                            cl.current(self.ws, "host-b")["session_id"])


class LineageLockIsBounded(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def test_a_held_lock_raises_instead_of_stalling_the_beat(self):
        """The heartbeat catches a raise but cannot catch a hang, and a beat that
        stops advancing reads to every peer as a dead core."""
        p = cl._path(self.ws, "h", "runs.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        lock = p.with_name(p.name + ".lock")
        with open(lock, "a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            t0 = time.monotonic()
            with self.assertRaises(TimeoutError):
                cl.record_run(self.ws, "h", "s1")
            waited = time.monotonic() - t0
        self.assertLess(waited, cl._LOCK_WAIT_S * 3,
                        "the wait was not bounded by _LOCK_WAIT_S")

    def test_control_an_unheld_lock_still_records(self):
        """Mutation control: if the acquire were simply broken, the test above
        would pass for the wrong reason."""
        cl.record_run(self.ws, "h", "s1")
        self.assertEqual(len(cl.runs(self.ws, "h")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
