#!/usr/bin/env python3
"""runtime_observation: schema validation, ordering, lease, atomic publication and the write CLI."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import runtime_observation as ro  # noqa: E402

NOW = 1_790_000_000.0
WID = "40659240fd884f63bcd19fa684b451f1"


def rec(**over):
    base = {"schema": 1, "observer": "obs", "observer_version": "0.3.0", "observer_id": "a" * 16,
            "observer_started_at": NOW - 100, "seat": "core", "session": "sutando-core",
            "claude_session_id": None, "seq": 1, "changed_at": NOW - 5, "condition_since": None,
            "last_success_at": None, "heartbeat_at": NOW, "phase": "idle", "motion": "idle",
            "condition": "healthy", "reason": None}
    return {**base, **over}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)


class ValidateTests(Base):
    def test_valid_record_round_trips_and_drops_unknown_keys(self):
        out = ro.validate({**rec(), "extra": "x"})
        self.assertEqual(out, rec(**{k: float(v) if isinstance(v, int) and k.endswith("_at") else v
                                     for k, v in rec().items()}))
        self.assertNotIn("extra", out)

    def test_bad_values_reject_the_whole_record(self):
        for over in ({"schema": 2}, {"seat": "worker"}, {"seat": WID.upper()}, {"seat": WID[:-1]},
                     {"phase": "sleeping"}, {"motion": "fast"}, {"condition": "ok"}, {"reason": "boom"},
                     {"seq": -1}, {"seq": True}, {"seq": 1.5}, {"heartbeat_at": "now"},
                     {"heartbeat_at": float("nan")}, {"heartbeat_at": 0}, {"observer": ""},
                     {"observer": "x" * 41}, {"observer_id": "x" * 65}, {"session": "x" * 81},
                     {"claude_session_id": "x" * 81}, {"changed_at": None}, {"observer_version": None}):
            with self.assertRaises(ValueError, msg=str(over)):
                ro.validate(rec(**over))

    def test_nullable_and_worker_seat(self):
        out = ro.validate(rec(seat=WID, claude_session_id="s", condition_since=NOW, last_success_at=NOW,
                              condition="abnormal", reason="needs-login"))
        self.assertEqual((out["seat"], out["reason"]), (WID, "needs-login"))

    def test_reason_is_present_exactly_when_abnormal(self):
        for bad in (rec(condition="abnormal", reason=None), rec(condition="healthy", reason="api-error"),
                    rec(condition="unknown", reason="needs-login")):
            with self.assertRaises(ValueError):
                ro.validate(bad)
        self.assertEqual(ro.validate(rec(condition="abnormal", reason="api-error"))["reason"], "api-error")

    def test_non_object_rejected(self):
        for bad in (None, [], "x", 3):
            with self.assertRaises(ValueError):
                ro.validate(bad)


class WriteTests(Base):
    def test_write_then_load(self):
        self.assertTrue(ro.write(rec(), self.ws))
        got, why = ro.load(self.ws, "core", NOW)
        self.assertIsNone(why)
        self.assertEqual(got["phase"], "idle")
        self.assertEqual(ro.record_path(self.ws, "core"), self.ws / "state" / "runtime-observations" / "core.json")

    def test_ordering_within_one_observer(self):
        self.assertTrue(ro.write(rec(seq=5, heartbeat_at=NOW), self.ws))
        self.assertFalse(ro.write(rec(seq=4, heartbeat_at=NOW + 10, phase="failed"), self.ws))
        self.assertFalse(ro.write(rec(seq=5, heartbeat_at=NOW, phase="failed"), self.ws))
        self.assertFalse(ro.write(rec(seq=5, heartbeat_at=NOW - 1, phase="failed"), self.ws))
        self.assertTrue(ro.write(rec(seq=5, heartbeat_at=NOW + 1), self.ws))
        self.assertTrue(ro.write(rec(seq=6, heartbeat_at=NOW + 2, phase="tool"), self.ws))
        self.assertEqual(ro.load(self.ws, "core", NOW + 2)[0]["phase"], "tool")

    def test_a_new_observer_replaces_regardless_of_seq(self):
        ro.write(rec(seq=50), self.ws)
        self.assertTrue(ro.write(rec(seq=1, observer_id="b" * 16, heartbeat_at=NOW + 1), self.ws))
        self.assertEqual(ro.load(self.ws, "core", NOW + 1)[0]["observer_id"], "b" * 16)

    def test_invalid_record_leaves_the_stored_one(self):
        ro.write(rec(), self.ws)
        with self.assertRaises(ValueError):
            ro.write(rec(phase="nope"), self.ws)
        self.assertEqual(ro.load(self.ws, "core", NOW)[0]["phase"], "idle")

    def test_no_partial_file_under_concurrent_writers_and_readers(self):
        ro.write(rec(), self.ws)
        path = ro.record_path(self.ws, "core")
        stop, bad = threading.Event(), []

        def reader():
            while not stop.is_set():
                try:
                    ro.validate(json.loads(path.read_text()))
                except (ValueError, OSError) as exc:
                    bad.append(exc)

        t = threading.Thread(target=reader)
        t.start()
        for i in range(2, 80):
            ro.write(rec(seq=i, heartbeat_at=NOW + i), self.ws)
        stop.set()
        t.join()
        self.assertEqual(bad, [])
        self.assertEqual(ro.load(self.ws, "core", NOW + 79)[0]["seq"], 79)
        leftovers = [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class LoadTests(Base):
    def test_missing_invalid_expired_future(self):
        self.assertEqual(ro.load(self.ws, "core", NOW), (None, "missing"))
        path = ro.record_path(self.ws, "core")
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        self.assertEqual(ro.load(self.ws, "core", NOW), (None, "invalid"))
        path.write_text(json.dumps(rec(seat=WID)))
        self.assertEqual(ro.load(self.ws, "core", NOW), (None, "invalid"))
        path.write_text(json.dumps(rec(), indent=0) + " " * ro.MAX_BYTES)
        self.assertEqual(ro.load(self.ws, "core", NOW), (None, "invalid"))
        path.write_text(json.dumps(rec()))
        self.assertEqual(ro.load(self.ws, "core", NOW + ro.LEASE_S)[1], None)
        self.assertEqual(ro.load(self.ws, "core", NOW + ro.LEASE_S + 1), (None, "expired"))
        self.assertEqual(ro.load(self.ws, "core", NOW - 5)[1], None)
        self.assertEqual(ro.load(self.ws, "core", NOW - 6), (None, "future"))


class CliTests(Base):
    def run_cli(self, stdin):
        return subprocess.run([sys.executable, str(REPO / "src" / "runtime_observation.py"), "write",
                               "--workspace", str(self.ws)], input=stdin, capture_output=True, text=True,
                              timeout=30, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_round_trip_stale_and_invalid(self):
        ok = self.run_cli(json.dumps(rec(seq=3)))
        self.assertEqual((ok.returncode, ok.stderr), (0, ""))
        self.assertEqual(ro.load(self.ws, "core", NOW)[0]["seq"], 3)
        stale = self.run_cli(json.dumps(rec(seq=2, phase="failed")))
        self.assertEqual(stale.returncode, 0)
        self.assertIn("stale", stale.stderr)
        self.assertEqual(ro.load(self.ws, "core", NOW)[0]["phase"], "idle")
        for bad in ("not json", json.dumps(rec(reason="boom")), "x" * (ro.MAX_BYTES + 10)):
            res = self.run_cli(bad)
            self.assertEqual(res.returncode, 2, bad[:20])
            self.assertIn("rejected", res.stderr)


class CliInProcessTests(Base):
    def main(self, stdin):
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), redirect_stderr(err):
            code = ro.main(["write", "--workspace", str(self.ws)])
        return code, err.getvalue()

    def test_accept_stale_and_reject(self):
        self.assertEqual(self.main(json.dumps(rec(seq=3))), (0, ""))
        code, err = self.main(json.dumps(rec(seq=2)))
        self.assertEqual(code, 0)
        self.assertIn("stale", err)
        for bad in ("not json", json.dumps(rec(reason="boom")), "x" * (ro.MAX_BYTES + 10)):
            code, err = self.main(bad)
            self.assertEqual(code, 2, bad[:20])
            self.assertIn("rejected", err)


if __name__ == "__main__":
    unittest.main()
