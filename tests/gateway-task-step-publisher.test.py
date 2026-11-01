"""Owner ledger step publication: rate, filtering, envelope correlation and real HTTP transport."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import
from _helpers.gateway_relay import Bridges, Relay

gw, _ = isolate_then_import()
from ag2_sparrow.task_step_publisher import StepPublisher, progress_arguments

EXECUTE = "/api/capabilities/v1/actions/execute"


def minting(answer):
    """A relay `on_post` that mints an MCP token at the mint path and answers Actions with `answer`."""
    def on_post(path, body):
        if path == gw.AGENT_ACCESS_MINT_PATH:
            return 200, {"access_token": "mcp-token", "expires_in": 900}
        return answer(path, body)
    return on_post


def actions(relay):
    return [p for p in relay.post_paths if p[0] == EXECUTE]


class Publisher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls, self.logs = [], []
        self.clock = 100.0
        gw._AGENT_ACCESS.update(token="", until=0.0)
        self.pub = StepPublisher(self.root, self.send, self.logs.append, lambda: self.clock)
        self.tasks = {"task-dev~task-a": {"task_id": "task-a", "execution_generation": 3, "room_id": "!r:test"}}

    def send(self, body):
        self.calls.append(body)
        return {"status": "completed"}

    def row(self, line="Step one", **kw):
        row = {"ts": self.clock, "kind": "working", "line": line, "room": "!r:test",
               "task": {"id": "task-dev~task-a"}, **kw}
        with self.pub.path.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def test_change_only_and_ten_seconds_coalesce_latest(self):
        self.row(); self.pub.publish(self.tasks)
        self.clock += 20; self.row(); self.pub.publish(self.tasks)
        self.assertEqual(len(self.calls), 1)
        self.row("Step two"); self.pub.publish(self.tasks)
        self.clock += 9.99; self.row("Step three"); self.pub.publish(self.tasks)
        self.assertEqual(len(self.calls), 2)
        self.clock += .01; self.pub.publish(self.tasks)
        self.assertEqual([c["arguments"]["summary"] for c in self.calls], ["Step one", "Step two", "Step three"])
        self.assertEqual(self.calls[0]["task_id"], "task-a")
        self.assertEqual(self.calls[0]["execution_generation"], 3)

    def test_scope_missing_context_terminal_and_non_work(self):
        for changes in ({"kind": "notice"}, {"kind": "health"}, {"kind": "processing"},
                        {"task": None}, {"task": {"id": "task-prod~task-a"}}, {"room": None},
                        {"room": "!other:test"}, {"audience": "owner"}, {"projection": "status"}):
            self.row(**changes)
        self.pub.publish(self.tasks)
        self.assertEqual(self.calls, [])
        self.row(kind="thinking"); self.row(kind="done")
        self.pub.publish(self.tasks)
        self.assertEqual(self.calls, [])

    def test_missing_or_invalid_generation(self):
        self.row()
        for gen in (None, True, "3", 0):
            self.tasks["task-dev~task-a"]["execution_generation"] = gen
            self.pub.publish(self.tasks)
        self.assertEqual(self.calls, [])

    def test_failure_logs_continues_and_retry_is_throttled_and_idempotent(self):
        def fail(body):
            self.calls.append(body)
            raise OSError("credential must not appear in logs")
        self.pub.send = fail
        self.row(); self.pub.publish(self.tasks)
        self.pub.publish(self.tasks)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("credential", self.logs[0])
        self.clock += 10; self.pub.send = self.send; self.pub.publish(self.tasks)
        self.assertEqual(self.calls[0], self.calls[1])
        self.clock += 10; self.pub.publish(self.tasks)
        self.assertEqual(len(self.calls), 2)

    def test_summary_limit_and_action_refusal(self):
        self.row("x" * 200, kind="thinking")
        self.pub.send = lambda body: self.calls.append(body) or {"status": "denied"}
        self.pub.publish(self.tasks)
        self.assertEqual(len(self.calls[0]["arguments"]["summary"]), 140)
        self.assertTrue(self.logs)

    def test_bridge_scope_and_progress_action_transport(self):
        relay = Relay(); self.addCleanup(relay.close)
        relay.on_post = minting(lambda path, body: (200, {"status": "completed"}))
        tasks_dir, results = self.root / "tasks", self.root / "results"
        tasks_dir.mkdir(); results.mkdir()
        for instance in ("dev", "prod"):
            tid = f"task-{instance}~task-a"
            (tasks_dir / f"{tid}.txt").write_text(
                f"id: {tid}\nchannel_id: !r:test\ntask_id: task-a\nexecution_generation: 3\ntask: test\n")
        self.row()
        self.pub.send = gw._send_task_step
        with patch.multiple(gw, URL=relay.url + "/relay", TOKEN="fixture-token", GATEWAY_INSTANCE="dev",
                            TASKS_DIR=tasks_dir, RESULTS_DIR=results, _ATTEMPTS=None, _STEP_PUBLISHER=self.pub):
            gw._publish_task_steps_snapshot({"task-dev~task-a", "task-prod~task-a"})
            self.assertEqual([p for p, _ in relay.post_paths], [gw.AGENT_ACCESS_MINT_PATH, EXECUTE])
            path, payload = actions(relay)[0]
            self.assertEqual(payload["action"], "room.task.progress")
            self.assertEqual(payload["arguments"], {"summary": "Step one"})
            self.assertEqual(payload["task_id"], "task-a")
            self.assertEqual(payload["execution_generation"], 3)
            self.assertNotIn("task_status", payload)
            self.row("Step two")
            self.pub.send = self.send
            self.clock += 10
            gw._publish_task_steps_snapshot({"task-dev~task-a", "task-prod~task-a"})
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.calls[0]["arguments"], {"summary": "Step two"})
            (results / "task-dev~task-a.txt").write_text("done")
            self.clock += 10; self.row("Later")
            gw._publish_task_steps_snapshot({"task-dev~task-a"})
            self.assertEqual(len(self.calls), 1)


    def test_secret_redaction_precedes_length_cap(self):
        secret = "sk-ant-" + "a" * 60
        self.row("Inspect " + secret + " " + "z" * 180)
        self.pub.publish(self.tasks)
        summary = self.calls[0]["arguments"]["summary"]
        self.assertNotIn(secret, summary)
        self.assertNotIn("sk-ant-", summary)
        self.assertEqual(len(summary), 140)

    def test_empty_tasks_do_not_read_activity(self):
        with patch.object(Path, "open", side_effect=AssertionError("must not read")):
            self.pub.publish({})

    def test_default_off_and_enabled_worker_never_blocks_poll(self):
        entered, release = threading.Event(), threading.Event()
        def slow(_):
            entered.set()
            release.wait(5)
        with patch.multiple(gw, TASK_STEP_PUBLICATION_ENABLED=None, _STEP_PUSH_THREAD=None), \
                patch.object(gw, "_publish_task_steps_now", side_effect=slow):
            gw._publish_task_steps(set(self.tasks))
            self.assertIsNone(gw._STEP_PUSH_THREAD)
            with patch.object(gw, "TASK_STEP_PUBLICATION_ENABLED", lambda: True):
                try:
                    gw._publish_task_steps(set(self.tasks))
                    self.assertTrue(entered.wait(1))
                    worker = gw._STEP_PUSH_THREAD
                    gw._publish_task_steps(set(self.tasks))
                    self.assertIs(gw._STEP_PUSH_THREAD, worker)
                    self.assertTrue(worker.is_alive())
                finally:
                    release.set()
                    gw._STEP_PUSH_THREAD.join(2)

    def test_switch_declared_off_in_manifest(self):
        repo = Path(__file__).resolve().parents[1]
        manifest = json.loads((repo / "skills/agent-activity/manifest.json").read_text())
        self.assertIs(manifest["config"]["LEDGER_STEP_PUBLISH_ENABLED"], False)


    def test_truncated_malformed_or_missing_activity_is_best_effort(self):
        self.pub.publish(self.tasks)
        self.pub.path.write_bytes(b"junk" * 100 + b"\n{bad json\n")
        self.row()
        with patch("ag2_sparrow.task_step_publisher.TAIL_BYTES", 250):
            self.pub.publish(self.tasks)
        self.assertEqual(len(self.calls), 1)

    def test_failed_switch_and_worker_are_contained(self):
        with patch.multiple(gw, TASK_STEP_PUBLICATION_ENABLED=Mock(side_effect=OSError("broken")),
                            _STEP_PUSH_THREAD=None):
            gw._publish_task_steps(set(self.tasks))
            self.assertIsNone(gw._STEP_PUSH_THREAD)
        with patch.object(gw, "_publish_task_steps_snapshot", side_effect=OSError("private")), \
                patch.object(gw, "_log") as log:
            gw._publish_task_steps_now(set(self.tasks))
            self.assertIn("OSError", log.call_args.args[0])
            self.assertNotIn("private", log.call_args.args[0])

    def test_attempt_and_envelope_boundaries(self):
        tasks_dir, results = self.root / "tasks", self.root / "results"
        tasks_dir.mkdir(); results.mkdir()
        tid = "task-dev~task-a"
        envelope = tasks_dir / f"{tid}.txt"
        attempts = Mock()
        self.row()
        with patch.multiple(gw, GATEWAY_INSTANCE="dev", TASKS_DIR=tasks_dir, RESULTS_DIR=results,
                            _ATTEMPTS=attempts, _STEP_PUBLISHER=self.pub):
            attempts.entry.return_value = None
            gw._publish_task_steps_now({tid})
            for body in ("id: " + tid + "\ntask: missing generation\n",
                         f"id: {tid}\ntask_id: task-other\nexecution_generation: 3\ntask: bad id\n"):
                envelope.write_text(body)
                gw._publish_task_steps_now({tid})
            envelope.write_text(f"id: {tid}\ntask_id: task-a\nexecution_generation: 3\n"
                                "channel_id: !r:test\ntask: live\n")
            for rec in (gw.task_attempts.UNKNOWN, {"finished": True}, {"fenced": "cancelled"},
                        {"run": tid, "execution_generation": 4}):
                attempts.entry.return_value = rec
                gw._publish_task_steps_now({tid})
            self.assertEqual(self.calls, [])
            attempts.entry.return_value = {"run": tid, "execution_generation": 3}
            gw._publish_task_steps_now({tid})
            self.assertEqual(len(self.calls), 1)

    def test_real_writer_preserves_broker_generation(self):
        with patch.multiple(gw, TASKS_DIR=self.root / "tasks", RESULTS_DIR=self.root / "results",
                            GATEWAY_INSTANCE="", _ATTEMPTS=None):
            gw.RESULTS_DIR.mkdir()
            gw._write_task({"id": "task-step-gen", "task": "test", "channel_id": "!r:test",
                            "execution_generation": 9})
            headers = gw.local_task_protocol.parse_task_headers(
                (gw.TASKS_DIR / "task-step-gen.txt").read_text()).headers
            self.assertEqual(headers["execution_generation"], "9")
            self.assertEqual(headers["task_id"], "task-step-gen")


    def test_undeployed_action_is_skipped_then_retried_after_rate_limit(self):
        relay = Relay(); self.addCleanup(relay.close)
        relay.on_post = minting(lambda path, body: (404, {"error_code": "ACTION_NOT_FOUND"}))
        self.row()
        self.pub.send = gw._send_task_step
        with patch.multiple(gw, URL=relay.url + "/relay", TOKEN="fixture-token"):
            self.pub.publish(self.tasks)
            self.assertEqual(len(actions(relay)), 1)
            self.assertIn("room.task.progress not deployed; skipping", self.logs[0])
            self.assertNotIn("task-dev~task-a", self.pub.sent)
            self.pub.publish(self.tasks)
            self.assertEqual(len(actions(relay)), 1)
            self.clock += 10
            relay.on_post = minting(lambda path, body: (200, {"status": "completed"}))
            self.pub.publish(self.tasks)
            self.assertEqual(len(actions(relay)), 2)
            self.assertEqual(actions(relay)[0][1], actions(relay)[1][1])
            self.assertIn("task-dev~task-a", self.pub.sent)

    def test_non_404_transport_error_is_logged_as_failure(self):
        relay = Relay(); self.addCleanup(relay.close)
        relay.on_post = minting(lambda path, body: (503, {"error": "unavailable"}))
        self.row()
        self.pub.send = gw._send_task_step
        with patch.multiple(gw, URL=relay.url + "/relay", TOKEN="fixture-token"):
            self.pub.publish(self.tasks)
        self.assertIn("Action failed (HTTPError)", self.logs[0])
        self.assertEqual(self.pub.sent, {})


class AgentAccess(unittest.TestCase):
    """Actions take the lane's MCP delegation, minted from its relay bearer, never the bearer itself."""

    def setUp(self):
        gw._AGENT_ACCESS.update(token="", until=0.0)
        self.calls = []

    def fake_req(self, answers):
        answers = list(answers)

        def req(method, path, payload=None, timeout=None, operation_id=None, origin=False, bearer=None, **kw):
            self.calls.append((path, bearer))
            out = answers.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        return req

    def http(self, code):
        import io
        import urllib.error
        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b""))

    def test_the_action_carries_the_minted_token_and_the_token_is_reused(self):
        step = {"operation_id": "step-1", "action": "room.task.progress"}
        req = self.fake_req([{"access_token": "mcp-1", "expires_in": 900}, {"status": "completed"},
                             {"status": "completed"}])
        with patch.object(gw, "_req", req):
            self.assertEqual(gw._send_task_step(step), {"status": "completed"})
            gw._send_task_step(step)
        self.assertEqual(self.calls, [(gw.AGENT_ACCESS_MINT_PATH, None), (EXECUTE, "mcp-1"), (EXECUTE, "mcp-1")])

    def test_a_rejected_token_is_minted_once_more(self):
        req = self.fake_req([{"access_token": "old", "expires_in": 900}, self.http(401),
                             {"access_token": "new", "expires_in": "junk"}, {"status": "completed"}])
        with patch.object(gw, "_req", req):
            self.assertEqual(gw._send_task_step({"operation_id": "s"}), {"status": "completed"})
        self.assertEqual([b for _, b in self.calls], [None, "old", None, "new"])

    def test_a_second_rejection_or_another_error_raises(self):
        import urllib.error
        req = self.fake_req([{"access_token": "a"}, self.http(401), {"access_token": "b"}, self.http(401)])
        with patch.object(gw, "_req", req), self.assertRaises(urllib.error.HTTPError):
            gw._send_task_step({"operation_id": "s"})
        gw._AGENT_ACCESS.update(token="", until=0.0)
        with patch.object(gw, "_req", self.fake_req([{"access_token": "a"}, self.http(500)])), \
                self.assertRaises(urllib.error.HTTPError):
            gw._send_task_step({"operation_id": "s"})

    def test_a_mint_without_a_token_or_not_deployed(self):
        with patch.object(gw, "_req", self.fake_req([{"nope": 1}])), self.assertRaises(ValueError):
            gw._send_task_step({"operation_id": "s"})
        with patch.object(gw, "_req", self.fake_req([self.http(404)])):
            self.assertEqual(gw._send_task_step({"operation_id": "s"}), {"status": "not_deployed"})


class Typed(unittest.TestCase):
    """With the host's status hook, a step carries only the typed fields of its published status."""

    def setUp(self):
        self.calls, self.logs, self.clock = [], [], 100.0
        self.status = {}
        self.pub = StepPublisher(Path(tempfile.mkdtemp()), self.send, self.logs.append, lambda: self.clock,
                                 status_of=lambda tid: self.status.get(tid))
        self.tasks = {"task-dev~task-a": {"task_id": "task-a", "execution_generation": 3, "room_id": "!r:test"}}

    def send(self, body):
        self.calls.append(body)
        return {"status": "completed"}

    def test_running_queued_and_delegated_tasks(self):
        self.status["task-dev~task-a"] = {"phase": "Running tests", "summary": "Running tests", "since": 1793491200.0}
        self.pub.publish(self.tasks)
        self.assertEqual(self.calls[-1]["arguments"], {"summary": "Running tests", "phase": "Running tests",
                                                       "since": "2026-11-01T00:00:00+00:00"})
        self.clock += 10
        self.status["task-dev~task-a"] = {"phase": "Waiting", "summary": "Queued · 2 ahead", "since": 1793491260.0,
                                          "waiting": {"reason": "queued", "position": 3}}
        self.pub.publish(self.tasks)
        self.assertEqual(self.calls[-1]["arguments"], {"summary": "Queued · 2 ahead", "phase": "Waiting",
                                                       "line": "Queued · 2 ahead", "since": "2026-11-01T00:01:00+00:00",
                                                       "waiting": "queued", "queue_ahead": 2})
        self.clock += 10
        self.status["task-dev~task-a"] = {"phase": "Delegated", "summary": "Delegated", "since": 1793491200.0,
                                          "waiting": {"reason": "delegated"}}
        self.pub.publish(self.tasks)
        self.assertEqual(self.calls[-1]["arguments"]["waiting"], "delegated")
        self.assertNotIn("queue_ahead", self.calls[-1]["arguments"])

    def test_unchanged_status_is_not_resent_and_nothing_for_a_finished_task(self):
        self.status["task-dev~task-a"] = {"phase": "Reading files", "summary": "Reading files", "since": 1.0}
        self.pub.publish(self.tasks)
        self.clock += 20
        self.pub.publish(self.tasks)
        self.assertEqual(len(self.calls), 1)
        self.status.clear()
        self.clock += 20
        self.pub.publish(self.tasks)
        self.assertEqual(len(self.calls), 1)

    def test_bad_statuses_and_envelopes_publish_nothing(self):
        for bad in (None, {"phase": "Juggling", "summary": "x"}, {"phase": "Thinking", "summary": ""}, "x"):
            self.assertIsNone(progress_arguments(bad), bad)
        args = progress_arguments({"phase": "Thinking", "summary": "Thinking", "since": float("nan"),
                                   "waiting": {"reason": "queued", "position": 0}})
        self.assertEqual(args, {"summary": "Thinking", "phase": "Thinking", "waiting": "queued"})
        long = progress_arguments({"phase": "Thinking", "summary": "x" * 90})
        self.assertNotIn("line", long, "a summary over the line cap is not a line")
        self.pub.status_of = lambda tid: 1 / 0
        self.pub.publish(self.tasks)
        self.tasks["task-dev~task-a"]["execution_generation"] = 0
        self.pub.status_of = lambda tid: {"phase": "Thinking", "summary": "Thinking"}
        self.pub.publish(self.tasks)
        self.assertEqual(self.calls, [])

    def test_failures_and_undeployed_are_logged_and_retried_later(self):
        self.status["task-dev~task-a"] = {"phase": "Thinking", "summary": "Thinking"}
        self.pub.send = lambda body: {"status": "not_deployed"}
        self.pub.publish(self.tasks)
        self.pub.send = lambda body: (_ for _ in ()).throw(OSError("down"))
        self.clock += 10
        self.pub.publish(self.tasks)
        self.pub.send = lambda body: {"status": "denied"}
        self.clock += 10
        self.pub.publish(self.tasks)
        self.assertIn("not deployed", self.logs[0])
        self.assertIn("Action failed (OSError)", self.logs[1])
        self.assertIn("Action failed (ValueError)", self.logs[2])
        self.assertEqual(self.pub.sent, {})

    def test_the_publishers_phases_are_phase_lines_words_plus_delegated(self):
        repo = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(repo / "skills" / "agent-activity" / "scripts"))
        import phase_line
        from ag2_sparrow import task_step_publisher as tsp
        self.assertEqual(tsp.PHASES, tuple(phase_line.PHASES) + ("Delegated",))
        tools = [("Read", {}), ("Grep", {}), ("WebSearch", {}), ("Edit", {}), ("Write", {}), ("Agent", {}),
                 ("SendMessage", {}), ("Bash", {"command": "pytest -q"}), ("Bash", {"command": "git diff"}),
                 ("Bash", {"command": "sleep 5"}), ("Bash", {"command": "ls"}), ("Bash", {"command": "make"}),
                 ("Unknown", {})]
        for tool, inp in tools:
            self.assertIn(phase_line.rules_phase(tool, inp), tsp.PHASES, tool)
        self.assertTrue(set(phase_line.MODEL_PHASES) <= set(tsp.PHASES))

    def test_the_bridge_hands_the_host_hook_to_the_publisher(self):
        hook = lambda tid: None  # noqa: E731
        with patch.multiple(gw, TASK_STATUS=hook, _STEP_PUBLISHER=self.pub, TASKS_DIR=Path(tempfile.mkdtemp())):
            gw._publish_task_steps_snapshot(set())
        self.assertIs(self.pub.status_of, hook)


class BridgeLoop(unittest.TestCase):
    def test_served_generation_is_stamped_but_publisher_defaults_off(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        relay = Relay()
        self.addCleanup(relay.close)
        bridges = Bridges(self, Path(directory.name), relay)
        actions = []
        def post(path, body):
            if path == "/api/capabilities/v1/actions/execute":
                actions.append(body)
                return 200, {"status": "completed"}
            return None
        relay.on_post = post
        relay.queue.append({"id": "task-step1", "task": "test step", "source": "ag2space",
                            "channel_id": "!r:test", "execution_generation": 4})
        proc = bridges.start()
        bridges.wait_for(lambda: relay.acks, proc, "task ack")
        envelope = (bridges.ws / "tasks" / "task-step1.txt").read_text()
        self.assertIn("task_id: task-step1\nexecution_generation: 4\n", envelope)
        with (bridges.ws / "state" / "agent-activity.jsonl").open("a") as stream:
            stream.write(json.dumps({"ts": 1, "kind": "working", "room": "!r:test",
                                     "task": {"id": "task-step1"}, "line": "Checking the fix"}) + "\n")
        polls = len(relay.polls)
        bridges.wait_for(lambda: len(relay.polls) > polls + 1, proc, "further intake polls")
        self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()
