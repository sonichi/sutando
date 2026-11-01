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
import io
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from delivery import turn_failure as tf  # noqa: E402
from util_paths import turn_failure_path  # noqa: E402

HOOK = REPO / "src" / "stop-failure.sh"
STOP_HOOK = REPO / "src" / "check-pending-tasks.sh"
TURN_START = REPO / "src" / "turn-start.sh"
MODULE = REPO / "src" / "delivery" / "turn_failure.py"


class DecideRetryTests(unittest.TestCase):
    def rec(self, at, error="server_error", recovered=None, task="t"):
        return {"failed_at": at, "error": error, "recovered_at": recovered, "task": task}

    def test_no_failure_or_one_older_than_the_submit_never_retries(self):
        self.assertIsNone(tf.decide_retry(None, "t", 100, 0, 0, 10_000))
        self.assertIsNone(tf.decide_retry(self.rec(99), "t", 100, 0, 0, 10_000))
        self.assertIsNone(tf.decide_retry(self.rec(100), "t", 100, 0, 0, 10_000))

    def test_a_failure_after_the_submit_is_due_once_the_first_backoff_elapses(self):
        self.assertIsNone(tf.decide_retry(self.rec(110), "t", 100, 0, 0, 110 + 29))
        self.assertIn("backoff 30s", tf.decide_retry(self.rec(110), "t", 100, 0, 0, 110 + 30))

    def test_a_successful_turn_after_the_failure_is_due_immediately(self):
        reason = tf.decide_retry(self.rec(110, recovered=111), "t", 100, 0, 0, 111)
        self.assertIn("later turn succeeded", reason)
        # A recovery stamp OLDER than the failure is a previous episode's, not this one's.
        self.assertIsNone(tf.decide_retry(self.rec(110, recovered=105), "t", 100, 0, 0, 111))

    def test_a_failure_in_a_turn_another_prompt_started_never_retries(self):
        for task in (None, "task-other.txt"):
            with self.subTest(task=task):
                self.assertIsNone(tf.decide_retry(self.rec(110, task=task, recovered=111), "t", 100, 0, 0, 10_000))

    def test_task_of_prompt_reads_only_the_notifier_prompt(self):
        self.assertEqual(tf.task_of_prompt("Sutando task ready: task-1.txt. Read /w/tasks/task-1.txt, ..."),
                         "task-1.txt")
        for p in (None, "", "hello", "Re: Sutando task ready: task-1.txt.", "Sutando task ready: ../x.",
                  "Sutando task ready: a/b.txt."):
            with self.subTest(p=p):
                self.assertIsNone(tf.task_of_prompt(p))

    def test_non_transient_errors_never_retry(self):
        for err in ("authentication_failed", "billing_error", "oauth_org_not_allowed", "account_on_hold",
                    "invalid_request", "model_not_found", "cloud_credential_error", "max_output_tokens"):
            with self.subTest(err=err):
                self.assertIsNone(tf.decide_retry(self.rec(110, err, recovered=200), "t", 100, 0, 0, 10_000))

    def test_backoff_schedule_grows_then_caps_at_600(self):
        self.assertEqual([tf.backoff_delay(n) for n in range(8)], [30, 60, 120, 300, 600, 600, 600, 600])

    def test_re_delivery_stops_after_max_attempts(self):
        self.assertEqual(tf.MAX_ATTEMPTS, 3)
        f = self.rec(110, error="rate_limit", recovered=111)
        self.assertIsNotNone(tf.decide_retry(f, "t", 100, 2, 0, 10_000))
        self.assertIsNone(tf.decide_retry(f, "t", 100, 3, 0, 10_000))
        self.assertTrue(tf.gives_up(f, "t", 100, 3))
        self.assertFalse(tf.gives_up(f, "t", 100, 2))
        self.assertFalse(tf.gives_up(self.rec(110, task=None), "t", 100, 3))

    def test_backoff_counts_from_the_later_of_failure_and_last_retry(self):
        f = self.rec(110)
        self.assertIsNone(tf.decide_retry(f, "t", 100, 2, 200, 200 + 119))
        self.assertIsNotNone(tf.decide_retry(f, "t", 100, 2, 200, 200 + 120))


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
        tf.record_failure(self.state, "overloaded", "s", now=1010, task=self.name)
        self.assertIsNotNone(self.due(now=1040))
        self.assertEqual(tf.note_retry(self.state, self.name, now=1040), 1)
        self.assertIsNone(self.due(now=1040 + 59))
        self.assertIsNotNone(self.due(now=1040 + 60))

    def test_recovery_makes_it_due_at_once(self):
        tf.record_failure(self.state, "server_error", "s", now=1010, task=self.name)
        self.assertIsNone(self.due(now=1011))
        self.assertTrue(tf.record_recovery(self.state, now=1011))
        self.assertIsNotNone(self.due(now=1011))
        self.assertFalse(tf.record_recovery(self.state, now=1012), "a second Stop re-stamped the recovery")

    def test_a_later_unrelated_turn_failing_is_not_due(self):
        tf.record_failure(self.state, "server_error", "s", now=1010, task=None)
        self.assertTrue(tf.record_recovery(self.state, now=1011))
        self.assertIsNone(self.due(now=5000), "a 502 in a turn our prompt did not start re-typed the task")

    def test_auth_error_is_not_recorded_and_never_due(self):
        self.assertFalse(tf.record_failure(self.state, "authentication_failed", "s", now=1010))
        self.assertIsNone(tf.read_failure(self.state))
        self.assertIsNone(self.due())

    def test_guards_result_archived_worker_held_no_marker(self):
        tf.record_failure(self.state, "server_error", "s", now=1010, task=self.name)
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

    def test_the_cap_is_reported_once_then_stays_quiet(self):
        tf.record_failure(self.state, "unknown", "s", now=1010, task=self.name)
        for n in range(tf.MAX_ATTEMPTS):
            self.assertIsNotNone(self.due(now=100_000 + n), f"attempt {n + 1} was not due")
            tf.note_retry(self.state, self.name, now=1011)
        args = (self.state, self.inflight, self.results, self.tasks / self.name, self.name)
        kind, why = tf.retry_verdict(*args, now=200_000)
        self.assertEqual(kind, "give_up")
        self.assertIn(f"{tf.MAX_ATTEMPTS} re-deliveries", why)
        self.assertIsNone(tf.retry_verdict(*args, now=300_000), "the give-up was reported twice")
        self.assertIsNone(self.due(now=400_000))

    def test_retry_clear_resets_attempts(self):
        tf.note_retry(self.state, self.name, now=1)
        tf.clear_retry(self.state, self.name)
        self.assertEqual(tf.read_retry(self.state, self.name), (0, 0.0))

    def test_cli_retry_due_exit_codes(self):
        args = [sys.executable, str(MODULE), "retry-due", "--state", str(self.state),
                "--inflight-dir", str(self.inflight), "--results-dir", str(self.results),
                "--payload", str(self.tasks / self.name), self.name]
        self.assertEqual(subprocess.run(args, capture_output=True).returncode, 1)
        tf.record_failure(self.state, "server_error", "s", now=time.time() - 60, task=self.name)
        out = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("server_error", out.stdout)
        for _ in range(tf.MAX_ATTEMPTS):
            tf.note_retry(self.state, self.name, now=1)
        capped = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(capped.returncode, 3, capped.stderr)
        self.assertIn("not re-delivering", capped.stdout)
        self.assertEqual(subprocess.run(args, capture_output=True).returncode, 1)
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

    def turn_start(self, prompt, session="abc", identity=None):
        r = self.run_hook(json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": session,
                                      "prompt": prompt}), script=TURN_START, identity=identity)
        self.assertEqual(r.returncode, 0, r.stderr)

    def stop_failure(self, session="abc"):
        r = self.run_hook(json.dumps({"hook_event_name": "StopFailure", "session_id": session,
                                      "error": "server_error"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(self.marker.read_text())

    def test_the_failure_is_blamed_on_the_task_whose_prompt_started_the_turn(self):
        self.turn_start("Sutando task ready: task-a.txt. Read /w/tasks/task-a.txt, follow CLAUDE.md")
        self.assertEqual(self.stop_failure()["task"], "task-a.txt")

    def test_a_failure_in_a_turn_another_prompt_started_blames_no_task(self):
        self.turn_start("Sutando task ready: task-a.txt. Read it")
        self.turn_start("<task-notification> monitor event </task-notification>")
        self.assertIsNone(self.stop_failure()["task"])

    def test_a_failure_after_a_successful_stop_blames_no_task(self):
        self.turn_start("Sutando task ready: task-a.txt. Read it")
        (self.ws / "tasks").mkdir()
        (self.ws / "results").mkdir()
        r = self.run_hook(json.dumps({"hook_event_name": "Stop"}), script=STOP_HOOK,
                          extra={"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(self.ws)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.stop_failure()["task"], "a later turn's 502 was blamed on a finished prompt")

    def test_a_failure_in_another_session_blames_no_task(self):
        self.turn_start("Sutando task ready: task-a.txt. Read it", session="core")
        self.assertIsNone(self.stop_failure(session="other")["task"])

    def test_a_guest_prompt_records_no_turn(self):
        self.turn_start("Sutando task ready: task-a.txt. Read it", identity={})
        self.assertIsNone(tf.read_turn(self.ws / "state"))

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


class InProcessCliTests(unittest.TestCase):
    """`_main` and the hook handlers called directly, so coverage measures them (subprocesses are not)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.state = root / "state"
        self.inflight = self.state / "task-notifier-inflight"
        self.results = root / "results"
        self.tasks = root / "tasks"
        for d in (self.inflight, self.results, self.tasks):
            d.mkdir(parents=True)
        self.name = "task-y.txt"
        (self.tasks / self.name).write_text("task: y\n")
        marker = self.inflight / self.name
        marker.write_text("1\n")
        os.utime(marker, (1000, 1000))
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in ("SUTANDO_INSTANCE_ID", "SUTANDO_AGENT", "SUTANDO_CORE_SESSION"):
            os.environ.pop(k, None)

    def main(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            rc = tf._main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def st(self):
        return ["--state", str(self.state)]

    def due_args(self, *extra):
        return ["retry-due", *self.st(), "--inflight-dir", str(self.inflight), "--results-dir",
                str(self.results), "--payload", str(self.tasks / self.name), *extra, self.name]

    def hook(self, cmd, payload):
        return self.main(cmd, *self.st(), stdin=payload if isinstance(payload, str) else json.dumps(payload))

    def test_no_arguments_prints_usage_and_exits_2(self):
        rc, out, err = self.main()
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("usage:", err)

    def test_missing_state_fails_a_command_but_never_a_hook(self):
        for cmd in ("hook-turn-start", "hook-stop-failure", "record-recovery"):
            with self.subTest(cmd=cmd):
                rc, _, err = self.main(cmd)
                self.assertEqual(rc, 0)
                self.assertIn("usage:", err)
        for argv in (("retry-note", "x"), ("retry-due",), ("--state",), ("bogus", "--state")):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv)[0], 2)

    def test_unknown_command_and_stray_arguments_exit_2_or_0_for_hooks(self):
        self.assertEqual(self.main("bogus", *self.st())[0], 2)
        self.assertEqual(self.main("retry-note", *self.st())[0], 2)
        self.assertEqual(self.main("retry-clear", *self.st(), "a", "b")[0], 2)
        for cmd in ("hook-turn-start", "hook-stop-failure", "record-recovery"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.main(cmd, *self.st(), "extra")[0], 0)

    def test_retry_due_exit_codes_in_process(self):
        rc, out, _ = self.main(*self.due_args())
        self.assertEqual((rc, out), (1, ""))
        tf.record_failure(self.state, "overloaded", "s", now=time.time() - 60, task=self.name)
        rc, out, _ = self.main(*self.due_args())
        self.assertEqual(rc, 0)
        self.assertIn("overloaded", out)
        for n in range(1, tf.MAX_ATTEMPTS + 1):
            rc, out, _ = self.main("retry-note", *self.st(), self.name)
            self.assertEqual((rc, out.strip()), (0, str(n)))
        rc, out, _ = self.main(*self.due_args())
        self.assertEqual(rc, 3)
        self.assertIn("not re-delivering", out)
        self.assertEqual(self.main(*self.due_args())[0], 1, "the give-up was reported twice")
        self.assertEqual(self.main("retry-clear", *self.st(), self.name)[0], 0)
        self.assertEqual(tf.read_retry(self.state, self.name), (0, 0.0))
        self.assertEqual(self.main(*self.due_args())[0], 0)

    def test_retry_due_usage_errors_exit_2(self):
        base = ["retry-due", *self.st()]
        self.assertEqual(self.main(*base, self.name)[0], 2)
        self.assertEqual(self.main(*self.due_args("extra"))[0], 2)
        rc, _, err = self.main(*base, "--inflight-dir")
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_retry_due_with_a_deliveries_dir(self):
        tf.record_failure(self.state, "server_error", "s", now=time.time() - 60, task=self.name)
        pool = Path(self._tmp.name) / "deliveries"
        self.assertEqual(self.main(*self.due_args("--deliveries-dir", str(pool)))[0], 0)
        (pool / "w").mkdir(parents=True)
        (pool / "w" / self.name).write_text("")
        self.assertEqual(self.main(*self.due_args("--deliveries-dir", str(pool)))[0], 1)

    def test_an_unreadable_pool_is_not_due(self):
        tf.record_failure(self.state, "server_error", "s", now=1010, task=self.name)
        with mock.patch("delivery.task_dispatch.worker_holds", side_effect=OSError("denied")):
            self.assertIsNone(tf.retry_due(self.state, self.inflight, self.results, self.tasks / self.name,
                                           self.name, deliveries_dir=self.tasks, now=5000))

    def test_a_bad_filename_is_rejected(self):
        for name in ("../x", "a/b", ""):
            with self.subTest(name=name):
                rc, _, err = self.main("retry-note", *self.st(), name)
                self.assertEqual(rc, 2)
                self.assertIn("not a task filename", err)

    def test_a_failed_write_leaves_no_temp_file(self):
        path = self.state / "x" / "rec.json"
        with self.assertRaises(TypeError):
            tf._write_json(path, {"bad": object()})
        self.assertEqual(list(path.parent.iterdir()), [])

    def test_turn_start_then_stop_failure_blames_the_task(self):
        prompt = f"Sutando task ready: {self.name}. Read it"
        self.assertEqual(self.hook("hook-turn-start", {"hook_event_name": "UserPromptSubmit",
                                                        "session_id": "s1", "prompt": prompt})[0], 0)
        self.assertEqual(tf.read_turn(self.state)["task"], self.name)
        self.assertEqual(self.hook("hook-stop-failure", {"hook_event_name": "StopFailure",
                                                          "session_id": "s1", "error": "server_error"})[0], 0)
        self.assertIsNone(tf.read_turn(self.state), "the failed turn was not ended")
        rec = tf.read_failure(self.state)
        self.assertEqual((rec["task"], rec["session_id"], rec["error"]), (self.name, "s1", "server_error"))

    def test_turn_start_with_a_non_string_session_and_empty_stdin(self):
        self.hook("hook-turn-start", {"session_id": 7, "prompt": f"Sutando task ready: {self.name}."})
        self.assertEqual(tf.read_turn(self.state)["session_id"], "")
        self.assertEqual(self.main("hook-turn-start", *self.st(), stdin="")[0], 0)
        self.assertIsNone(tf.read_turn(self.state)["task"])

    def test_stop_failure_in_another_session_blames_no_task(self):
        tf.record_turn_start(self.state, f"Sutando task ready: {self.name}.", "core")
        self.hook("hook-stop-failure", {"hook_event_name": "StopFailure", "session_id": "other",
                                        "error": "overloaded"})
        self.assertIsNone(tf.read_failure(self.state)["task"])

    def test_stop_failure_without_a_session_or_with_a_bad_one_keeps_the_task(self):
        for session in (None, 7):
            with self.subTest(session=session):
                tf.record_turn_start(self.state, f"Sutando task ready: {self.name}.", "core")
                self.hook("hook-stop-failure", {"hook_event_name": "StopFailure", "session_id": session,
                                                "error": "rate_limit"})
                self.assertEqual(tf.read_failure(self.state)["task"], self.name)

    def test_stop_failure_with_a_non_string_error_records_nothing_but_ends_the_turn(self):
        tf.record_turn_start(self.state, "hello", "s")
        self.hook("hook-stop-failure", {"hook_event_name": "StopFailure", "error": 7})
        self.assertIsNone(tf.read_failure(self.state))
        self.assertIsNone(tf.read_turn(self.state))

    def test_hooks_ignore_bad_or_foreign_payloads(self):
        for stdin in ("not json", "[1, 2]", json.dumps({"hook_event_name": "Stop", "error": "server_error",
                                                         "prompt": "Sutando task ready: t."})):
            for cmd in ("hook-turn-start", "hook-stop-failure"):
                with self.subTest(cmd=cmd, stdin=stdin):
                    rc, out, _ = self.hook(cmd, stdin)
                    self.assertEqual((rc, out), (0, ""))
                    self.assertIsNone(tf.read_turn(self.state))
                    self.assertIsNone(tf.read_failure(self.state))

    def test_record_recovery_ends_the_turn_and_stamps_once(self):
        tf.record_turn_start(self.state, "hello", "s")
        self.assertEqual(self.main("record-recovery", *self.st())[0], 0)
        self.assertIsNone(tf.read_turn(self.state))
        self.assertIsNone(tf.read_failure(self.state))
        tf.record_failure(self.state, "server_error", "s", now=10, task=self.name)
        self.assertEqual(self.main("record-recovery", *self.st())[0], 0)
        self.assertIsNotNone(tf.read_failure(self.state)["recovered_at"])

    def test_a_hook_error_is_reported_but_exits_0(self):
        blocked = Path(self._tmp.name) / "file-not-dir"
        blocked.write_text("x")
        rc, _, err = self.main("hook-stop-failure", "--state", str(blocked),
                               stdin=json.dumps({"hook_event_name": "StopFailure", "error": "overloaded"}))
        self.assertEqual(rc, 0)
        self.assertIn("turn_failure.py hook-stop-failure:", err)


if __name__ == "__main__":
    unittest.main()
