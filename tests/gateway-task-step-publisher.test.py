"""Owner ledger step publication: rate, filtering, envelope correlation and real HTTP transport."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import
from _helpers.gateway_relay import Bridges, Relay

gw, _ = isolate_then_import()
from ag2_sparrow.task_step_publisher import StepPublisher, progress_arguments

MCP = "/mcp"


def minting(answer):
    """A relay `on_post` that mints an MCP token at the mint path and serves the MCP endpoint as the
    hosted facade does: `answer(action payload)` gives (HTTP status, Action envelope); 200 with a
    `code` is a tool error, 200 without one a success (a sentence, then the JSON, plus structuredContent)."""
    def on_post(path, body):
        if path == gw.AGENT_ACCESS_MINT_PATH:
            return 200, {"access_token": "mcp-token", "expires_in": 900}
        status, envelope = answer(body["params"]["arguments"])
        if status != 200:
            return status, envelope
        if "code" in envelope:
            return 200, {"jsonrpc": "2.0", "id": 1, "result": {
                "isError": True, "content": [{"type": "text", "text": json.dumps(envelope)}]}}
        return 200, {"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": "room.task.progress completed."},
                        {"type": "text", "text": json.dumps(envelope)}], "structuredContent": envelope}}
    return on_post


def actions(relay):
    return [(p, b["params"]["arguments"]) for p, b in relay.post_paths if p == MCP]


class Publisher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # A fresh secret registry per test: the AgentAccess fixtures mint one-letter tokens, which the
        # bridge registers as secrets, and a one-letter secret makes every error code a piece of one.
        fresh = gw.gateway_credentials.SecretRegistry()
        self._registry_patches = [patch.object(gw.diagnostics, "REGISTRY", fresh)]
        if getattr(gw, "_SECRETS", None) is not None:
            self._registry_patches.append(patch.object(gw, "_SECRETS", fresh))
        for p in self._registry_patches:
            p.start()
            self.addCleanup(p.stop)
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
        relay.on_post = minting(lambda body: (200, {"status": "completed"}))
        tasks_dir, results = self.root / "tasks", self.root / "results"
        tasks_dir.mkdir(); results.mkdir()
        for instance in ("dev", "prod"):
            tid = f"task-{instance}~task-a"
            (tasks_dir / f"{tid}.txt").write_text(
                f"id: {tid}\nchannel_id: !r:test\ntask_id: task-a\nexecution_generation: 3\ntask: test\n")
        self.row()
        self.pub.send = gw._send_task_step
        with patch.multiple(gw, URL=relay.url + "/relay", TOKEN="fixture-token", GATEWAY_INSTANCE="dev",
                            TASKS_DIR=tasks_dir, RESULTS_DIR=results, _ATTEMPTS=None, _STEP_PUBLISHER=self.pub,
                            _lane_mcp_url=lambda: relay.url + MCP):
            gw._publish_task_steps_snapshot({"task-dev~task-a", "task-prod~task-a"})
            self.assertEqual([p for p, _ in relay.post_paths], [gw.AGENT_ACCESS_MINT_PATH, MCP])
            self.assertEqual(relay.post_paths[1][1]["params"]["name"], "room.action.execute")
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
        def slow(*_):
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
        relay.on_post = minting(lambda body: (200, {"code": "NOT_FOUND", "message": "Action is not available"}))
        self.row()
        self.pub.send = gw._send_task_step
        with patch.multiple(gw, URL=relay.url + "/relay", TOKEN="fixture-token", _lane_mcp_url=lambda: relay.url + MCP):
            self.pub.publish(self.tasks)
            self.assertEqual(len(actions(relay)), 1)
            self.assertIn("room.task.progress not deployed; skipping", self.logs[0])
            self.assertNotIn("task-dev~task-a", self.pub.sent)
            self.pub.publish(self.tasks)
            self.assertEqual(len(actions(relay)), 1)
            self.clock += 10
            relay.on_post = minting(lambda body: (200, {"status": "completed"}))
            self.pub.publish(self.tasks)
            self.assertEqual(len(actions(relay)), 2)
            self.assertEqual(actions(relay)[0][1], actions(relay)[1][1])
            self.assertIn("task-dev~task-a", self.pub.sent)

    def test_non_404_transport_error_is_logged_as_failure(self):
        relay = Relay(); self.addCleanup(relay.close)
        relay.on_post = minting(lambda body: (503, {"error_code": "UPSTREAM_UNAVAILABLE", "message": "private response content"}))
        self.row()
        self.pub.send = gw._send_task_step
        with patch.multiple(gw, URL=relay.url + "/relay", TOKEN="fixture-token", _lane_mcp_url=lambda: relay.url + MCP):
            self.pub.publish(self.tasks)
        self.assertIn("HTTP 503", self.logs[0])
        self.assertIn("error_code=UPSTREAM_UNAVAILABLE", self.logs[0])
        self.assertNotIn("private response content", self.logs[0])
        self.assertEqual(self.pub.sent, {})


    def test_error_codes_never_log_registered_secrets_or_free_text(self):
        gw.diagnostics.register_secret("fixture-private-credential")
        for code in ("fixture-private-credential", "private response body with spaces", None):
            with self.subTest(code=code):
                relay = Relay(); self.addCleanup(relay.close)
                relay.on_post = minting(lambda body: (401, {"error_code": code}))
                self.row()
                self.pub.send = gw._send_task_step
                with patch.multiple(gw, URL=relay.url + "/relay", TOKEN="fixture-private-credential",
                                    _lane_mcp_url=lambda: relay.url + MCP):
                    self.pub.publish(self.tasks)
                self.assertIn("HTTP 401", self.logs[-1])
                self.assertIn("error_code=unavailable", self.logs[-1])
                self.assertNotIn("fixture-private-credential", self.logs[-1])
                self.assertNotIn("private response body", self.logs[-1])
                self.clock += 10


class AgentAccess(unittest.TestCase):
    """Room Actions go through the lane's MCP endpoint with a delegation minted from its relay bearer."""

    def setUp(self):
        gw._AGENT_ACCESS.update(token="", until=0.0, penalty_until=0.0)
        self.mints, self.calls = [], []

    def mint(self, *tokens):
        tokens = list(tokens)

        def req(method, path, payload=None, timeout=None, origin=False, **kw):
            self.mints.append(path)
            out = tokens.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        return req

    def endpoint(self, *answers):
        answers = list(answers)

        class Resp:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, size=-1):
                return json.dumps(self.body).encode()

        def urlopen(req, timeout=None):
            self.calls.append((req.full_url, req.get_header("Authorization"), json.loads(req.data)))
            out = answers.pop(0)
            if isinstance(out, Exception):
                raise out
            return Resp(out)
        return urlopen

    @staticmethod
    def ok(envelope):
        return {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": envelope, "content": []}}

    @staticmethod
    def tool_error(code):
        return {"jsonrpc": "2.0", "id": 1, "result": {"isError": True,
                                                       "content": [{"type": "text", "text": json.dumps({"code": code})}]}}

    def http(self, code):
        import io
        import urllib.error
        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b""))

    def run_step(self, mint, endpoint, url="https://mcp.test/mcp"):
        with patch.object(gw, "_req", mint), patch.object(gw.urllib.request, "urlopen", endpoint), \
                patch.object(gw, "_lane_mcp_url", lambda: url):
            return gw._send_task_step({"action": "room.task.progress", "operation_id": "step-1"})

    def test_the_call_carries_the_minted_token_and_the_token_is_reused(self):
        mint, endpoint = self.mint({"access_token": "mcp-1", "expires_in": 900}), \
            self.endpoint(self.ok({"status": "completed"}), self.ok({"status": "completed"}))
        self.assertEqual(self.run_step(mint, endpoint), {"status": "completed"})
        self.assertEqual(self.run_step(mint, endpoint), {"status": "completed"})
        self.assertEqual(self.mints, [gw.AGENT_ACCESS_MINT_PATH], "one mint, reused")
        url, auth, rpc = self.calls[0]
        self.assertEqual((url, auth), ("https://mcp.test/mcp", "Bearer mcp-1"))
        self.assertEqual((rpc["method"], rpc["params"]["name"]), ("tools/call", "room.action.execute"))
        self.assertEqual(rpc["params"]["arguments"]["action"], "room.task.progress")

    def test_a_rejected_token_is_minted_once_more_for_a_401_or_an_auth_envelope(self):
        mint = self.mint({"access_token": "old", "expires_in": 900}, {"access_token": "new", "expires_in": "junk"})
        endpoint = self.endpoint(self.http(401), self.ok({"status": "completed"}))
        self.assertEqual(self.run_step(mint, endpoint), {"status": "completed"})
        self.assertEqual([c[1] for c in self.calls], ["Bearer old", "Bearer new"])
        gw._AGENT_ACCESS.update(token="", until=0.0)
        self.calls.clear()
        mint = self.mint({"access_token": "a"}, {"access_token": "b"})
        endpoint = self.endpoint(self.tool_error("AUTHENTICATION_FAILED"), self.ok({"status": "completed"}))
        self.assertEqual(self.run_step(mint, endpoint), {"status": "completed"})
        self.assertEqual([c[1] for c in self.calls], ["Bearer a", "Bearer b"])

    def test_repeated_rejections_and_other_failures(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError):
            self.run_step(self.mint({"access_token": "a"}, {"access_token": "b"}),
                          self.endpoint(self.http(401), self.http(401)))
        gw._AGENT_ACCESS.update(token="", until=0.0)
        out = self.run_step(self.mint({"access_token": "a"}, {"access_token": "b"}),
                            self.endpoint(self.tool_error("AUTHENTICATION_FAILED"),
                                          self.tool_error("AUTHENTICATION_FAILED")))
        self.assertEqual((out.get("is_error"), out.get("code")), (True, "AUTHENTICATION_FAILED"))
        gw._AGENT_ACCESS.update(token="", until=0.0)
        out = self.run_step(self.mint({"access_token": "a"}), self.endpoint(self.tool_error("UNAUTHORIZED")))
        self.assertEqual(out.get("code"), "UNAUTHORIZED", "only the proxy's AUTHENTICATION_FAILED mints again")
        gw._AGENT_ACCESS.update(token="", until=0.0)
        with self.assertRaises(urllib.error.HTTPError):
            self.run_step(self.mint({"access_token": "a"}), self.endpoint(self.http(500)))
        gw._AGENT_ACCESS.update(token="", until=0.0)
        with self.assertRaises(ValueError):
            self.run_step(self.mint({"access_token": "a"}), self.endpoint({"jsonrpc": "2.0", "id": 1, "error": {}}))
        out = self.run_step(self.mint(), self.endpoint(self.tool_error("RATE_LIMITED")))
        self.assertEqual(out, {"code": "RATE_LIMITED", "is_error": True})

    def test_a_rate_limited_mint_backs_off_for_a_minute(self):
        with self.assertRaises(gw.MintRateLimited):
            self.run_step(self.mint(self.http(429)), self.endpoint())
        with self.assertRaises(gw.MintRateLimited):
            self.run_step(self.mint(), self.endpoint())  # no mint is attempted inside the penalty
        self.assertEqual(self.mints, [gw.AGENT_ACCESS_MINT_PATH])
        with patch.object(gw.time, "monotonic", return_value=gw.time.monotonic() + gw.MINT_RATE_LIMIT_S + 1):
            self.assertEqual(self.run_step(self.mint({"access_token": "c"}), self.endpoint(self.ok({"status": "completed"}))),
                             {"status": "completed"})
        self.assertEqual(len(self.mints), 2, "after the penalty it mints again")

    def test_the_token_is_withheld_from_logs_and_its_lifetime_bounded(self):
        registered = []
        with patch.object(gw._SECRETS, "register", side_effect=lambda v, *a: registered.append(v)):
            self.run_step(self.mint({"access_token": "tok-secret", "expires_in": float("inf")}),
                          self.endpoint(self.ok({"status": "completed"})))
        self.assertEqual(registered, ["tok-secret"])
        left = gw._AGENT_ACCESS["until"] - gw.time.monotonic()
        self.assertLessEqual(left, gw.TOKEN_TTL_MAX_S, "an infinite expires_in is not cached forever")
        gw._AGENT_ACCESS.update(token="", until=0.0)
        self.run_step(self.mint({"access_token": "t", "expires_in": 10 ** 9}), self.endpoint(self.ok({"status": "completed"})))
        self.assertLessEqual(gw._AGENT_ACCESS["until"] - gw.time.monotonic(), gw.TOKEN_TTL_MAX_S)

    def test_not_deployed_cases_are_a_skip(self):
        # A 404 carries its safe diagnostics beside the skip (#6116): the status and HTTP code, and an
        # error code when the body named one (none here).
        self.assertEqual(self.run_step(self.mint(self.http(404)), self.endpoint()),
                         {"status": "not_deployed", "http_status": 404, "error_code": None})
        self.assertEqual(self.run_step(self.mint(), self.endpoint(), url=""), {"status": "not_deployed"})
        gw._AGENT_ACCESS.update(token="", until=0.0)
        self.assertEqual(self.run_step(self.mint({"access_token": "a"}), self.endpoint(self.http(404))),
                         {"status": "not_deployed", "http_status": 404, "error_code": None})
        # A body that cannot be read (the response already closed) is still a skip, with no code.
        closed = self.http(404)
        closed.fp.close()
        self.assertEqual(self.run_step(self.mint(), self.endpoint(closed)),
                         {"status": "not_deployed", "http_status": 404, "error_code": None})
        self.assertEqual(self.run_step(self.mint(), self.endpoint(self.tool_error("NOT_FOUND"))),
                         {"status": "not_deployed"})
        gw._AGENT_ACCESS.update(token="", until=0.0)
        with self.assertRaises(ValueError):
            self.run_step(self.mint({"nope": 1}), self.endpoint())

    def test_the_envelope_is_read_from_structured_content_or_the_last_json_text(self):
        self.assertEqual(gw._mcp_envelope({"content": [{"text": "done."}, {"text": "[1]"}, {"text": '{"a": 1}'},
                                                       {"text": "not json"}, {}]}), {"a": 1})
        self.assertEqual(gw._mcp_envelope({"content": [{"text": "only words"}]}), {})
        self.assertEqual(gw._mcp_envelope({"structuredContent": {"b": 2}, "content": []}), {"b": 2})

    def test_the_lane_endpoint_comes_from_its_gateways_dotspace(self):
        with patch.object(gw, "URL", "https://dev-chat.s.space/relay"):
            self.assertEqual(gw._lane_mcp_url(), "https://dev-mcp.s.space/mcp")


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

    def test_a_published_step_is_logged_with_its_phase_and_never_its_text(self):
        self.status["task-dev~task-a"] = {"phase": "Waiting", "summary": "Queued · next", "since": 1.0,
                                          "waiting": {"reason": "queued", "position": 1}}
        self.pub.publish(self.tasks)
        self.assertEqual(self.logs, ["task step task-dev~task-a: published Waiting (queued)"])
        self.clock += 10
        self.status["task-dev~task-a"] = {"phase": "Running tests", "summary": "Running tests", "since": 2.0}
        self.pub.publish(self.tasks)
        self.assertEqual(self.logs[-1], "task step task-dev~task-a: published Running tests")

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
        self.assertIn("Action answered denied, not completed", self.logs[2])
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

    def test_steps_from_another_runtime_record_rules_phases_without_text(self):
        repo = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(repo / "skills" / "agent-activity" / "scripts"))
        import phase_line
        ws = Path(tempfile.mkdtemp())
        (ws / "state").mkdir()
        # Three good steps (a missing or non-dict input is an empty one); the rest are skipped.
        batch = [{"task": "task-a", "tool": "Bash", "input": {"command": "pytest -q"}},
                 {"task": "task-a", "tool": "Monitor"},
                 {"task": "task-a", "tool": "Bash", "input": "not a dict"},
                 {"task": "../evil", "tool": "Bash", "input": {}},
                 {"task": "task-a", "tool": "", "input": {}},
                 {"task": 7, "tool": "Bash"}, "junk", None]
        self.assertEqual(phase_line.record_steps(ws, json.dumps(batch)), 3)
        recs = [json.loads(l) for l in (ws / "state" / "agent-activity.phase.jsonl").read_text().splitlines()]
        self.assertEqual([(r["task_id"], r["phase"], r["step"]) for r in recs],
                         [("task-a", "Running tests", None), ("task-a", "Waiting", None), ("task-a", "Running a command", None)])
        self.assertNotIn("pytest", (ws / "state" / "agent-activity.phase.jsonl").read_text(), "no command text is stored")
        self.assertEqual(phase_line.status_for(ws, "task-a")["phase"], "Running a command")
        self.assertEqual(phase_line.record_steps(ws, "{not json"), -1)
        self.assertEqual(phase_line.record_steps(ws, '{"not": "a list"}'), -1)
        self.assertEqual(phase_line.record_steps(ws, "[]"), 0)
        many = [{"task": "task-b", "tool": "Read", "input": {}}] * (phase_line.STEPS_MAX + 5)
        self.assertEqual(phase_line.record_steps(ws, json.dumps(many)), phase_line.STEPS_MAX, "a batch is capped")

    def test_the_steps_command_reads_stdin_and_needs_no_task(self):
        repo = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(repo / "skills" / "agent-activity" / "scripts"))
        import phase_line
        import io
        ws = Path(tempfile.mkdtemp())
        (ws / "state").mkdir()
        with patch.object(sys, "stdin", io.StringIO('[{"task": "task-c", "tool": "Edit", "input": {}}]')):
            self.assertEqual(phase_line.main(["steps", "--workspace", str(ws)]), 0)
        self.assertEqual(phase_line.status_for(ws, "task-c")["phase"], "Editing code")
        with patch.object(sys, "stdin", io.StringIO("{}")):
            self.assertEqual(phase_line.main(["steps", "--workspace", str(ws)]), 1)
        with self.assertRaises(SystemExit):
            phase_line.main(["status", "--workspace", str(ws)])  # every other command still needs --task

    def test_a_refusal_logs_its_code_and_never_payload_text(self):
        self.status["task-dev~task-a"] = {"phase": "Thinking", "summary": "Thinking"}
        self.pub.send = lambda body: {"is_error": True, "code": "task_already_terminal", "message": "secret text"}
        self.pub.publish(self.tasks)
        self.clock += 10
        self.pub.send = lambda body: {"is_error": True, "code": "has spaces and secret text"}
        self.pub.publish(self.tasks)
        self.assertEqual(self.logs, ["task step task-dev~task-a: Action refused (task_already_terminal); continuing",
                                     "task step task-dev~task-a: Action refused (unknown); continuing"])
        self.assertEqual(self.pub.sent, {})

    def test_a_task_closed_meanwhile_is_not_stepped(self):
        self.status["task-dev~task-a"] = {"phase": "Thinking", "summary": "Thinking"}
        self.pub.publish(self.tasks, open_now=lambda tid: False)
        self.assertEqual(self.calls, [])
        self.pub.publish(self.tasks, open_now=lambda tid: True)
        self.assertEqual(len(self.calls), 1)

    def test_the_bridge_rechecks_the_live_set_and_results_before_sending(self):
        results = Path(tempfile.mkdtemp())
        seen = {}

        class Spy(StepPublisher):
            def publish(spy, tasks, open_now=None):
                seen.update(tasks=tasks, open_now=open_now)
        spy = Spy(Path(tempfile.mkdtemp()), self.send)
        live = {"task-dev~task-a"}
        with patch.multiple(gw, TASK_STATUS=None, _STEP_PUBLISHER=spy, RESULTS_DIR=results,
                            TASKS_DIR=Path(tempfile.mkdtemp())):
            gw._publish_task_steps_snapshot(set(), live)
            self.assertTrue(seen["open_now"]("task-dev~task-a"))
            live.clear()
            self.assertFalse(seen["open_now"]("task-dev~task-a"), "retired from the outbound worker meanwhile")
            live.add("task-dev~task-a")
            (results / "task-dev~task-a.txt").write_text("done")
            self.assertFalse(seen["open_now"]("task-dev~task-a"), "its result written meanwhile")

    def test_the_bridge_hands_the_host_hook_to_the_publisher(self):
        hook = lambda tid: None  # noqa: E731
        with patch.multiple(gw, TASK_STATUS=hook, _STEP_PUBLISHER=self.pub, TASKS_DIR=Path(tempfile.mkdtemp())):
            gw._publish_task_steps_snapshot(set())
        self.assertIs(self.pub.status_of, hook)


class PickedUp(unittest.TestCase):
    """With the engine's status hook, "Queued" never outlives the wait: once the core picks the task up
    (its file still in the queue), the next published step is not waiting."""

    def test_queued_then_picked_up_publishes_a_non_waiting_step(self):
        repo = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(repo / "skills" / "agent-activity" / "scripts"))
        import phase_line
        ws = Path(tempfile.mkdtemp())
        (ws / "tasks").mkdir()
        (ws / "tasks" / "task-dev~task-a.txt").write_text("id: task-dev~task-a\ntask: hi\n")
        calls, clock = [], [100.0]
        pub = StepPublisher(ws, lambda body: calls.append(body) or {"status": "completed"}, lambda m: None,
                            lambda: clock[0], status_of=lambda tid: phase_line.published_status(ws, tid))
        tasks = {"task-dev~task-a": {"task_id": "task-a", "execution_generation": 1, "room_id": "!r:test"}}
        pub.publish(tasks)
        self.assertEqual((calls[-1]["arguments"]["phase"], calls[-1]["arguments"].get("waiting")), ("Waiting", "queued"))
        index = ws / "state" / "agent-activity.index.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(json.dumps({"task-dev~task-a": {"started": time.time()}}))  # the core picked it up
        clock[0] += 10
        pub.publish(tasks)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1]["arguments"]["phase"], "Understanding the request")
        self.assertNotIn("waiting", calls[-1]["arguments"])


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
