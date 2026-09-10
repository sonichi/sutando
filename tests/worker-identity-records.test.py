#!/usr/bin/env python3
"""A worker's identity records: lineage and run history are separate things.

The rules under test, each of which loses information if collapsed:

  * resume keeps the session and adds only a run; fork adds a session AND a run;
  * a new session has NO parent — inventing one falsifies the lineage;
  * a watcher restart is not a run, so nothing may record it as one;
  * a worker maps to MANY sessions over its life (measured: one core's own
    done-flags resolved to three different sessions), so none of this is
    derivable afterwards.

Run: python3 tests/worker-identity-records.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import worker_identity as wi  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)


class TestIds(Base):
    def test_worker_id_fits_the_recipient_bound(self):
        for _ in range(20):
            self.assertRegex(wi.new_worker_id(), wi.WORKER_ID_RE.pattern)

    def test_ids_are_not_reused(self):
        ids = {wi.new_worker_id() for _ in range(500)}
        self.assertEqual(len(ids), 500)

    def test_a_malformed_worker_id_is_refused(self):
        for bad in ("../escape", "Worker-1", "", "a" * 33):
            with self.assertRaises(wi.IdentityError, msg=bad):
                wi.worker_dir(self.ws, bad)


class TestWorkspaceResolution(Base):
    def test_an_explicit_workspace_is_honoured(self):
        w = wi.new_worker_id()
        self.assertTrue(str(wi.worker_dir(self.ws, w)).startswith(str(self.ws)))

    def test_no_workspace_uses_the_canonical_resolver(self):
        """A second resolution path is how a reader and a writer end up in
        different trees; there must be exactly one."""
        from unittest.mock import patch
        w = wi.new_worker_id()
        with patch.object(wi, "resolve_workspace", return_value=self.ws / "resolved"):
            got = wi.worker_dir(None, w)
        self.assertEqual(got, self.ws / "resolved" / "state" / "workers" / w)


class TestLineage(Base):
    def test_a_new_session_has_no_parent(self):
        w = wi.new_worker_id()
        row = wi.record_session(self.ws, w, "s1", runtime="claude", relation="new")
        self.assertIsNone(row["parent_session_id"])

    def test_inventing_a_parent_for_a_new_session_is_refused(self):
        w = wi.new_worker_id()
        with self.assertRaises(wi.IdentityError):
            wi.record_session(self.ws, w, "s3", runtime="claude",
                              relation="new", parent_session_id="s1")

    def test_a_fork_must_name_its_origin(self):
        w = wi.new_worker_id()
        with self.assertRaises(wi.IdentityError):
            wi.record_session(self.ws, w, "s2", runtime="claude", relation="forked")

    def test_a_fork_records_the_relation_not_just_an_ancestor_list(self):
        w = wi.new_worker_id()
        wi.record_session(self.ws, w, "s1", runtime="claude", relation="new")
        wi.record_session(self.ws, w, "s2", runtime="claude",
                          relation="forked", parent_session_id="s1")
        rows = {r["session_id"]: r for r in wi.sessions(self.ws, w)}
        self.assertEqual(rows["s2"]["relation"], "forked")
        self.assertEqual(rows["s2"]["parent_session_id"], "s1")
        self.assertIsNone(rows["s1"]["parent_session_id"])

    def test_the_transcript_locator_is_recorded_not_derived(self):
        w = wi.new_worker_id()
        wi.record_session(self.ws, w, "s1", runtime="claude", relation="new",
                          host="mac-1", cwd="/dev/proj", transcript_path="/t/s1.jsonl")
        t = wi.sessions(self.ws, w)[0]["transcript"]
        self.assertEqual((t["host"], t["cwd"], t["path"]),
                         ("mac-1", "/dev/proj", "/t/s1.jsonl"))

    def test_recording_the_same_session_twice_is_idempotent(self):
        w = wi.new_worker_id()
        wi.record_session(self.ws, w, "s1", runtime="claude", relation="new")
        wi.record_session(self.ws, w, "s1", runtime="claude", relation="new")
        self.assertEqual(len(wi.sessions(self.ws, w)), 1)


class TestIncarnations(Base):
    def _worker(self):
        w = wi.new_worker_id()
        wi.record_session(self.ws, w, "s1", runtime="claude", relation="new")
        return w

    def test_resume_adds_a_RUN_and_leaves_lineage_untouched(self):
        """The row the whole split exists for."""
        w = self._worker()
        i1 = wi.start_incarnation(self.ws, w, "s1")
        wi.end_incarnation(self.ws, w, i1["incarnation_id"], "crashed")
        before = wi.sessions(self.ws, w)
        i2 = wi.start_incarnation(self.ws, w, "s1")
        self.assertEqual(wi.sessions(self.ws, w), before, "resume must not touch lineage")
        self.assertEqual(len(wi.incarnations(self.ws, w)), 2)
        self.assertNotEqual(i1["incarnation_id"], i2["incarnation_id"])
        self.assertEqual(i2["session_id"], "s1")

    def test_an_incarnation_needs_a_session_in_the_lineage(self):
        w = self._worker()
        with self.assertRaises(wi.IdentityError):
            wi.start_incarnation(self.ws, w, "unknown-session")

    def test_ending_records_why(self):
        w = self._worker()
        i = wi.start_incarnation(self.ws, w, "s1")
        row = wi.end_incarnation(self.ws, w, i["incarnation_id"], "killed")
        self.assertEqual(row["end_reason"], "killed")
        self.assertIsNotNone(row["ended_at"])

    def test_an_unknown_end_reason_is_refused(self):
        w = self._worker()
        i = wi.start_incarnation(self.ws, w, "s1")
        with self.assertRaises(wi.IdentityError):
            wi.end_incarnation(self.ws, w, i["incarnation_id"], "vibes")

    def test_current_tracks_the_live_run_and_clears_on_end(self):
        w = self._worker()
        i = wi.start_incarnation(self.ws, w, "s1")
        self.assertEqual(wi.current(self.ws, w)["incarnation_id"], i["incarnation_id"])
        wi.end_incarnation(self.ws, w, i["incarnation_id"], "exited")
        self.assertIsNone(wi.current(self.ws, w)["incarnation_id"])

    def test_a_watcher_restart_is_not_a_run(self):
        """Nothing here is called on a restart; the records must be unchanged."""
        w = self._worker()
        wi.start_incarnation(self.ws, w, "s1")
        snap = (wi.sessions(self.ws, w), wi.incarnations(self.ws, w), wi.current(self.ws, w))
        # a watcher restart touches no identity API at all
        self.assertEqual((wi.sessions(self.ws, w), wi.incarnations(self.ws, w),
                          wi.current(self.ws, w)), snap)


class TestTmuxLocator(Base):
    def _worker(self):
        w = wi.new_worker_id()
        wi.record_session(self.ws, w, "s1", runtime="claude", relation="new")
        return w

    def test_the_name_is_derived_from_the_id_not_the_label(self):
        """Renaming a worker is a one-field edit, never a process migration."""
        w = wi.new_worker_id()
        self.assertEqual(wi.tmux_session_name(w), f"sutando-worker-{w}")

    def test_the_locator_lives_on_the_RUN_not_the_worker(self):
        w = self._worker()
        i = wi.start_incarnation(self.ws, w, "s1", tmux_socket="/tmp/s.sock",
                                 tmux_session=wi.tmux_session_name(w))
        self.assertEqual(i["tmux"]["socket"], "/tmp/s.sock")
        self.assertEqual(i["tmux"]["session_name"], f"sutando-worker-{w}")
        self.assertNotIn("tmux", wi.sessions(self.ws, w)[0])

    def test_a_later_run_may_land_in_a_different_tmux_session(self):
        """The whole reason it is per-incarnation: worker_id stays, tmux moves."""
        w = self._worker()
        i1 = wi.start_incarnation(self.ws, w, "s1", tmux_socket="/tmp/a.sock",
                                  tmux_session="sutando-worker-old")
        wi.end_incarnation(self.ws, w, i1["incarnation_id"], "crashed")
        i2 = wi.start_incarnation(self.ws, w, "s1", tmux_socket="/tmp/b.sock",
                                  tmux_session="sutando-worker-new")
        self.assertNotEqual(i1["tmux"], i2["tmux"])
        self.assertEqual(len({r["session_id"] for r in wi.incarnations(self.ws, w)}), 1)

    def test_create_worker_records_the_derived_name(self):
        got = wi.create_worker(self.ws, runtime="claude", tmux_socket="/tmp/s.sock")
        self.assertEqual(got["tmux_session"], f"sutando-worker-{got['worker_id']}")
        inc = wi.incarnations(self.ws, got["worker_id"])[0]
        self.assertEqual(inc["tmux"]["socket"], "/tmp/s.sock")


class TestCreateWorker(Base):
    def test_new_mints_three_distinct_ids_and_a_root_session(self):
        got = wi.create_worker(self.ws, runtime="claude", cwd="/dev/proj")
        self.assertEqual(got["relation"], "new")
        self.assertEqual(len({got["worker_id"], got["runtime_session_id"],
                              got["incarnation_id"]}), 3)
        row = wi.sessions(self.ws, got["worker_id"])[0]
        self.assertIsNone(row["parent_session_id"])

    def test_resume_keeps_the_session_id_and_mints_a_new_worker(self):
        got = wi.create_worker(self.ws, runtime="claude", session_id="S1", resume=True)
        self.assertEqual(got["runtime_session_id"], "S1")
        self.assertEqual(got["relation"], "resumed")
        self.assertNotEqual(got["worker_id"], "S1")

    def test_fork_records_the_origin(self):
        got = wi.create_worker(self.ws, runtime="claude",
                               session_id="S2", fork_from="S1")
        row = wi.sessions(self.ws, got["worker_id"])[0]
        self.assertEqual((row["relation"], row["parent_session_id"]), ("forked", "S1"))

    def test_resume_and_fork_are_different_operations(self):
        with self.assertRaises(wi.IdentityError):
            wi.create_worker(self.ws, runtime="claude", session_id="S1",
                             resume=True, fork_from="S0")

    def test_resuming_without_a_session_id_is_refused(self):
        with self.assertRaises(wi.IdentityError):
            wi.create_worker(self.ws, runtime="claude", resume=True)

    def test_two_workers_may_share_one_working_directory(self):
        a = wi.create_worker(self.ws, runtime="claude", cwd="/dev/proj")
        b = wi.create_worker(self.ws, runtime="claude", cwd="/dev/proj")
        self.assertNotEqual(a["worker_id"], b["worker_id"])
        self.assertNotEqual(a["runtime_session_id"], b["runtime_session_id"])


class TestDurability(Base):
    def test_records_survive_a_reread(self):
        got = wi.create_worker(self.ws, runtime="claude")
        w = got["worker_id"]
        self.assertEqual(len(wi.sessions(self.ws, w)), 1)
        self.assertEqual(len(wi.incarnations(self.ws, w)), 1)

    def test_a_write_is_atomic_no_partial_file(self):
        got = wi.create_worker(self.ws, runtime="claude")
        p = wi.sessions_path(self.ws, got["worker_id"])
        json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(list(p.parent.glob(".*tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
