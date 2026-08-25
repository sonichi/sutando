#!/usr/bin/env python3
"""The launcher: pre-assign the id, CAS the seat, and refuse rather than race.

Covers the four startup cases end to end against the real store — ordinary
restart, resume failure, a probe that also fails, and a stale core whose
profile was re-seated while it was gone.

Run: python3 tests/pool-session-start.test.py   (stdlib only)
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from pool_profiles import ProfileStore  # noqa: E402
from pool_resume import BACKOFF, NEW, PROBE, RESUME  # noqa: E402

_START = REPO / "scripts" / "pool-session-start.py"
_spec = importlib.util.spec_from_file_location("pool_session_start", _START)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
launch_plan, report, seated_profile = (_mod.launch_plan, _mod.report,
                                       _mod.seated_profile)

LEAD = "pool-lead"
ROOMS = {"!room-a:ag2.space": {"read": True, "write": "scoped"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.store = ProfileStore(self.ws / "state" / "pool" / "profiles.json",
                                  lead_label=LEAD)
        self.ids = iter(f"uuid-{n}" for n in range(1, 50))
        self.addCleanup(self.tmp.cleanup)

    def make_seated(self, core="core-1", pid="p-0001"):
        self.store.create(pid, ROOMS, writer=LEAD)
        return pid, self.store.seat(pid, core, writer=LEAD)

    def plan(self, core="core-1", runtime="claude", probe_ok=None):
        return launch_plan(self.store, core, runtime, probe_ok,
                           new_id_fn=lambda: next(self.ids))


class FirstStartTests(Base):
    def test_an_unseated_core_starts_unmanaged_rather_than_failing(self):
        p = self.plan()
        self.assertEqual((p["action"], p["profile_id"]), (NEW, None))

    def test_a_first_start_preassigns_the_id_before_exec(self):
        pid, _ = self.make_seated()
        p = self.plan()
        self.assertEqual(p["args"], ["--session-id", "uuid-1"])
        # written BEFORE the process exists — that is the whole point
        self.assertEqual(self.store.head(pid)["session_id"], "uuid-1")

    def test_a_recorded_session_is_resumed_next_time(self):
        self.make_seated()
        self.plan()
        p = self.plan()
        self.assertEqual((p["action"], p["args"]),
                         (RESUME, ["--resume", "uuid-1"]))

    def test_a_runtime_without_preassign_defers_the_id(self):
        pid, _ = self.make_seated()
        p = self.plan(runtime="codex")
        self.assertEqual((p["action"], p["args"]), (NEW, []))
        self.assertIsNone(self.store.head(pid))
        self.assertIn("read back after start", p["note"])


class FailurePathTests(Base):
    def _fail_twice(self):
        pid, epoch = self.make_seated()
        self.plan()  # creates uuid-1 and promotes it
        for _ in range(2):
            report(self.store, "core-1", pid, epoch, "uuid-1", ok=False)
        return pid, epoch

    def test_one_failure_still_resumes_the_same_session(self):
        pid, epoch = self.make_seated()
        self.plan()
        report(self.store, "core-1", pid, epoch, "uuid-1", ok=False)
        self.assertEqual(self.plan()["args"], ["--resume", "uuid-1"])

    def test_a_reproduced_failure_asks_for_a_probe(self):
        self._fail_twice()
        p = self.plan()
        self.assertEqual((p["action"], p["args"], p["probe"]), (PROBE, [], True))

    def test_a_healthy_probe_starts_a_new_generation(self):
        pid, _ = self._fail_twice()
        p = self.plan(probe_ok=True)
        self.assertEqual((p["action"], p["args"]),
                         (NEW, ["--session-id", "uuid-2"]))
        self.assertEqual(self.store.head(pid)["session_id"], "uuid-2")
        self.assertEqual(self.store.ancestry(pid), ["g2", "g1"])

    def test_a_failing_probe_backs_off_and_creates_nothing(self):
        pid, _ = self._fail_twice()
        before = len(self.store.get(pid)["generations"])
        p = self.plan(probe_ok=False)
        self.assertEqual((p["action"], p["args"]), (BACKOFF, []))
        self.assertEqual(len(self.store.get(pid)["generations"]), before,
                         "a backoff must not manufacture a generation")

    def test_a_success_after_failures_returns_to_plain_resume(self):
        pid, epoch = self._fail_twice()
        report(self.store, "core-1", pid, epoch, "uuid-1", ok=True)
        self.assertEqual(self.plan()["action"], RESUME)


class FencingTests(Base):
    def test_a_reseated_profile_is_invisible_to_the_old_core(self):
        pid, _ = self.make_seated(core="core-1")
        self.store.reseat(pid, "core-2", writer=LEAD)
        p = self.plan(core="core-1")
        self.assertIsNone(p["profile_id"], "core-1 must not still see it")

    def test_the_new_core_gets_the_profile_and_its_session(self):
        pid, _ = self.make_seated(core="core-1")
        self.plan(core="core-1")
        self.store.reseat(pid, "core-2", writer=LEAD)
        p = self.plan(core="core-2")
        self.assertEqual((p["action"], p["args"]),
                         (RESUME, ["--resume", "uuid-1"]))

    def test_reporting_with_a_stale_epoch_is_refused(self):
        pid, stale = self.make_seated()
        self.store.reseat(pid, "core-1", writer=LEAD)
        from pool_profiles import SeatFenced
        with self.assertRaises(SeatFenced):
            report(self.store, "core-1", pid, stale, "uuid-1", ok=False)

    def test_attempts_are_bounded(self):
        pid, epoch = self.make_seated()
        for _ in range(30):
            report(self.store, "core-1", pid, epoch, "s", ok=False)
        self.assertEqual(len(self.store.get(pid)["attempts"]), 20)


class CliTests(Base):
    def _run(self, *args):
        r = subprocess.run(
            [sys.executable, str(_START), "--workspace", str(self.ws),
             "--core", "core-1", *args],
            capture_output=True, text=True)
        return r.returncode, json.loads(r.stdout or "{}")

    def test_the_cli_emits_a_plan_as_json(self):
        self.make_seated()
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out["action"], NEW)
        self.assertEqual(out["args"][0], "--session-id")

    def test_a_stale_epoch_exits_nonzero_and_says_refused(self):
        pid, stale = self.make_seated()
        self.store.reseat(pid, "core-1", writer=LEAD)
        rc, out = self._run("--outcome", "fail", "--profile-id", pid,
                            "--seat-epoch", str(stale))
        self.assertEqual(rc, 3)
        self.assertEqual(out["action"], "refused")

    def test_a_corrupt_store_refuses_rather_than_launching_blind(self):
        self.make_seated()
        self.store.path.write_text("{not json")
        rc, out = self._run()
        self.assertEqual((rc, out["action"]), (0, NEW))
        self.assertIsNone(out["profile_id"],
                          "a corrupt store must not resolve a profile")


if __name__ == "__main__":
    unittest.main(verbosity=2)
