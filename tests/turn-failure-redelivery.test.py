#!/usr/bin/env python3
"""delivery/turn_failure.py: the policy, its on-disk guards, and the StopFailure / Stop hooks.

Run: python3 tests/turn-failure-redelivery.test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from delivery import turn_failure as tf  # noqa: E402
from util_paths import turn_failure_path  # noqa: E402

HOOK = REPO / "src" / "stop-failure.sh"
STOP_HOOK = REPO / "src" / "check-pending-tasks.sh"
MODULE = REPO / "src" / "delivery" / "turn_failure.py"


class DecideRetryTests(unittest.TestCase):
    def rec(self, at, error="server_error", recovered=None):
        return {"failed_at": at, "error": error, "recovered_at": recovered}

    def test_no_failure_or_one_older_than_the_submit_never_retries(self):
        self.assertIsNone(tf.decide_retry(None, 100, 0, 0, 10_000))
        self.assertIsNone(tf.decide_retry(self.rec(99), 100, 0, 0, 10_000))
        self.assertIsNone(tf.decide_retry(self.rec(100), 100, 0, 0, 10_000))

    def test_a_failure_after_the_submit_is_due_once_the_first_backoff_elapses(self):
        self.assertIsNone(tf.decide_retry(self.rec(110), 100, 0, 0, 110 + 29))
        self.assertIn("backoff 30s", tf.decide_retry(self.rec(110), 100, 0, 0, 110 + 30))

    def test_a_successful_turn_after_the_failure_is_due_immediately(self):
        reason = tf.decide_retry(self.rec(110, recovered=111), 100, 0, 0, 111)
        self.assertIn("later turn succeeded", reason)
        # A recovery stamp OLDER than the failure is a previous episode's, not this one's.
        self.assertIsNone(tf.decide_retry(self.rec(110, recovered=105), 100, 0, 0, 111))

    def test_non_transient_errors_never_retry(self):
        for err in ("authentication_failed", "billing_error", "oauth_org_not_allowed", "account_on_hold",
                    "invalid_request", "model_not_found", "cloud_credential_error", "max_output_tokens"):
            with self.subTest(err=err):
                self.assertIsNone(tf.decide_retry(self.rec(110, err, recovered=200), 100, 0, 0, 10_000))

    def test_backoff_schedule_grows_then_caps_at_600(self):
        self.assertEqual([tf.backoff_delay(n) for n in range(8)], [30, 60, 120, 300, 600, 600, 600, 600])

    def test_backoff_counts_from_the_later_of_failure_and_last_retry(self):
        f = self.rec(110)
        self.assertIsNone(tf.decide_retry(f, 100, 2, 200, 200 + 119))
        self.assertIsNotNone(tf.decide_retry(f, 100, 2, 200, 200 + 120))


class OnDiskTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.state = root / "state"
        self.inflight = self.state / "task-notifier-inflight"
        self.results = root / "results"
        self.tasks = root / "tasks"
        self.deliveries = root / "deliveries"
        for d in (self.inflight, self.results, self.tasks):
            d.mkdir(parents=True)
        self.name = "task-x.txt"
        (self.tasks / self.name).write_text("task: x\n")
        marker = self.inflight / self.name
        marker.write_text("4242\n")
        os.utime(marker, (1000, 1000))

    def due(self, now=5000, **kw):
        return tf.retry_due(self.state, self.inflight, self.results, self.tasks / self.name,
                            self.name, now=now, **kw)

    def test_failure_newer_than_submission_is_due_and_retry_note_pushes_backoff(self):
        tf.record_failure(self.state, "overloaded", "s", now=1010)
        self.assertIsNotNone(self.due(now=1040))
        self.assertEqual(tf.note_retry(self.state, self.name, now=1040), 1)
        self.assertIsNone(self.due(now=1040 + 59))
        self.assertIsNotNone(self.due(now=1040 + 60))

    def test_recovery_makes_it_due_at_once(self):
        tf.record_failure(self.state, "server_error", "s", now=1010)
        self.assertIsNone(self.due(now=1011))
        self.assertTrue(tf.record_recovery(self.state, now=1011))
        self.assertIsNotNone(self.due(now=1011))
        self.assertFalse(tf.record_recovery(self.state, now=1012), "a second Stop re-stamped the recovery")

    def test_auth_error_is_not_recorded_and_never_due(self):
        self.assertFalse(tf.record_failure(self.state, "authentication_failed", "s", now=1010))
        self.assertIsNone(tf.read_failure(self.state))
        self.assertIsNone(self.due())

    def test_guards_result_archived_worker_held_no_marker(self):
        tf.record_failure(self.state, "server_error", "s", now=1010)
        self.assertIsNotNone(self.due())
        (self.results / self.name).write_text("done\n")
        self.assertIsNone(self.due(), "re-sent a task that has a result")
        (self.results / self.name).unlink()
        (self.deliveries / "worker-a").mkdir(parents=True)
        (self.deliveries / "worker-a" / "task-x.txt").write_text("")
        self.assertIsNone(self.due(deliveries_dir=self.deliveries), "re-sent a worker-held task")
        self.assertIsNotNone(self.due(deliveries_dir=Path(self._tmp.name) / "no-pool"))
        (self.tasks / self.name).unlink()
        self.assertIsNone(self.due(), "re-sent an archived task")
        (self.tasks / self.name).write_text("task: x\n")
        (self.inflight / self.name).unlink()
        self.assertIsNone(self.due(), "re-sent a task that was never submitted")

    def test_retry_clear_resets_attempts(self):
        tf.note_retry(self.state, self.name, now=1)
        tf.clear_retry(self.state, self.name)
        self.assertEqual(tf.read_retry(self.state, self.name), (0, 0.0))

    def test_cli_retry_due_exit_codes(self):
        args = [sys.executable, str(MODULE), "retry-due", "--state", str(self.state),
                "--inflight-dir", str(self.inflight), "--results-dir", str(self.results),
                "--payload", str(self.tasks / self.name), self.name]
        self.assertEqual(subprocess.run(args, capture_output=True).returncode, 1)
        tf.record_failure(self.state, "server_error", "s", now=time.time() - 60)
        out = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("server_error", out.stdout)
        bad = subprocess.run([sys.executable, str(MODULE), "retry-due", "--state", str(self.state)],
                             capture_output=True)
        self.assertEqual(bad.returncode, 2)


class HookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "ws"
        self.ws.mkdir()
        self.marker = turn_failure_path(self.ws / "state")

    def run_hook(self, stdin, script=HOOK, extra=None, identity=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("SUTANDO_CORE_SESSION", "SUTANDO_INSTANCE_ID")}
        env.update({"SUTANDO_CORE_SESSION": "1"} if identity is None else identity)
        env.update(SUTANDO_WORKSPACE_DIR=str(self.ws), **(extra or {}))
        return subprocess.run(["bash", str(script)], input=stdin, env=env,
                              capture_output=True, text=True, timeout=60)

    def test_server_error_writes_the_marker(self):
        r = self.run_hook(json.dumps({"hook_event_name": "StopFailure", "session_id": "abc",
                                      "error": "server_error", "last_assistant_message": "API Error: 502"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = json.loads(self.marker.read_text())
        self.assertEqual((rec["error"], rec["session_id"], rec["recovered_at"]), ("server_error", "abc", None))

    def test_a_guest_session_records_nothing(self):
        stdin = json.dumps({"hook_event_name": "StopFailure", "session_id": "guest-review-session",
                            "error": "server_error"})
        for identity in ({}, {"SUTANDO_CORE_SESSION": "0"}):
            with self.subTest(identity=identity):
                r = self.run_hook(stdin, identity=identity)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertFalse(self.marker.exists(), "a guest session's API error was blamed on the core")

    def test_a_pool_worker_records_its_own_failure(self):
        r = self.run_hook(json.dumps({"hook_event_name": "StopFailure", "error": "overloaded"}),
                          identity={"SUTANDO_INSTANCE_ID": "w1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(list((self.ws / "state" / "core-turn-failure").glob("*.json")), r.stderr)

    def test_authentication_failed_is_ignored(self):
        r = self.run_hook(json.dumps({"hook_event_name": "StopFailure", "error": "authentication_failed"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.marker.exists())

    def test_bad_stdin_never_fails(self):
        for stdin in ("", "not json", "[1,2]", '{"error": 7}', '{"hook_event_name": "Stop", "error": "server_error"}'):
            with self.subTest(stdin=stdin):
                r = self.run_hook(stdin)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, "")
                self.assertFalse(self.marker.exists())

    def test_an_unwritable_state_dir_never_fails(self):
        (self.ws / "state").write_text("a file where the dir should be")
        r = self.run_hook(json.dumps({"hook_event_name": "StopFailure", "error": "overloaded"}))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_stop_hook_stamps_recovery_for_the_core(self):
        self.run_hook(json.dumps({"hook_event_name": "StopFailure", "error": "overloaded"}))
        (self.ws / "tasks").mkdir()
        (self.ws / "results").mkdir()
        r = self.run_hook(json.dumps({"hook_event_name": "Stop"}), script=STOP_HOOK,
                          extra={"SUTANDO_CORE_SESSION": "1", "SUTANDO_TEST_MODE": "1",
                                 "SUTANDO_WORKSPACE": str(self.ws)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(json.loads(self.marker.read_text())["recovered_at"], r.stderr)


if __name__ == "__main__":
    unittest.main()
