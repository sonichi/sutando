"""Owner ledger step publication: rate, filtering, envelope correlation and real HTTP transport."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import
from _helpers.gateway_relay import Bridges, Relay

gw, _ = isolate_then_import()
from ag2_sparrow.task_step_publisher import StepPublisher


class Publisher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls, self.logs = [], []
        self.clock = 100.0
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

    def test_bridge_scope_and_held_transport(self):
        relay = Relay(); self.addCleanup(relay.close)
        relay.token = "fixture-token"
        relay.on_post = lambda path, body: (200, {"status": "completed"})
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
            self.assertEqual(relay.post_paths, [])
            self.assertIn("Action failed", self.logs[0])
            self.pub.send = self.send
            self.clock += 10
            gw._publish_task_steps_snapshot({"task-dev~task-a", "task-prod~task-a"})
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.calls[0]["arguments"], {"summary": "Step one"})
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
