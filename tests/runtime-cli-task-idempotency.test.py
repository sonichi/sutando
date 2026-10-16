#!/usr/bin/env python3
"""Task submission retries through the real CLI and Unix-socket daemon.

Run: python3 tests/runtime-cli-task-idempotency.test.py
Temporary state follows TMPDIR; no task consumer or external service is used.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "src" / "runtime-api" / "server.py"
CLI = ROOT / "src" / "runtime-cli" / "sutando-runtime.py"
TEXT = "Summarize this week's synthetic meeting notes"

PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={ROOT / '.coveragerc'}"]


def read_frame(sock):
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(65536)
        if not chunk:
            raise EOFError("connection closed before a complete RPC frame")
        data += chunk
        if len(data) > 65536:
            raise ValueError("oversized RPC frame")
    return data


class TaskIdempotencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="cli-idem-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.workspace = self.base / "workspace"
        self.tasks = self.workspace / "tasks"
        self.results = self.workspace / "results"
        state = self.workspace / "state"
        config = self.base / "config"
        for path in (self.tasks, self.results, state, config):
            path.mkdir(parents=True)
        self.socket = self.base / "rt.sock"
        self.env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "SUTANDO_TEST_MODE": "1",
            "SUTANDO_PACKAGED": "",
            "SUTANDO_APP_SUPPORT": "",
            "SUTANDO_WORKSPACE": str(self.workspace),
            "SUTANDO_WORKSPACE_DIR": str(self.workspace),
            "SUTANDO_DEFAULT_WORKSPACE": str(self.workspace),
            "SUTANDO_TASKS_DIR": str(self.tasks),
            "SUTANDO_RESULTS_DIR": str(self.results),
            "SUTANDO_RUNTIME_STATE": str(state),
            "SUTANDO_RUNTIME_DB": str(state / "runtime.sqlite"),
            "SUTANDO_HA_DIR": str(state / "ha"),
            "SUTANDO_RUNTIME_SOCKET": str(self.socket),
            "SUTANDO_RUN_DIR": str(self.base / "run"),
            "SUTANDO_INSTANCE_REGISTRY": str(self.base / "registry"),
            "SUTANDO_AGENT_ID": "cli-idempotency-test",
            "SUTANDO_HOST_LABEL": "cli-idempotency-test",
            "SUTANDO_INSTANCE_ID": "",
            "CLAUDE_CONFIG_DIR": str(config),
            "SUTANDO_TMUX_SOCKET": str(self.base / "unused-tmux.sock"),
            "SUTANDO_SCP_WSS_URL": "",
        }
        self.daemon = None
        self.generation = 0
        self.addCleanup(self.stop_daemon)
        self.start_daemon()

    def start_daemon(self):
        self.generation += 1
        self.log = self.base / f"daemon-{self.generation}.log"
        command = [*PYBASE, str(SERVER)]
        with self.log.open("wb") as output:
            self.daemon = subprocess.Popen(
                command, cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 8
        last_error = None
        while time.monotonic() < deadline and self.daemon.poll() is None:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(0.5)
                    sock.connect(str(self.socket))
                    sock.sendall(b'{"jsonrpc":"2.0","id":"ready",'
                                 b'"method":"task.list","params":{}}\n')
                    response = json.loads(read_frame(sock))
                self.assertEqual(response["id"], "ready")
                self.assertIn("tasks", response["result"])
                print(json.dumps({"event": "daemon-ready", "at": time.time(),
                                  "test": self.id(), "pid": self.daemon.pid,
                                  "argv": command, "socket": str(self.socket),
                                  "response": response}), flush=True)
                return
            except (OSError, EOFError) as exc:
                last_error = exc
                time.sleep(0.03)
        self.fail(f"daemon did not become ready: {last_error}; "
                  f"log: {self.log.read_text()[-8192:]}")

    def stop_daemon(self):
        if self.daemon is None:
            return
        daemon, self.daemon = self.daemon, None
        if daemon.poll() is None:
            daemon.terminate()
        try:
            daemon.wait(timeout=3)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=3)
        with self.log.open("rb") as output:
            output.seek(max(0, self.log.stat().st_size - 8192))
            log = output.read().decode("utf-8", errors="replace")
        print(json.dumps({"event": "daemon-reaped", "at": time.time(),
                          "test": self.id(), "pid": daemon.pid,
                          "returncode": daemon.returncode, "log": log}), flush=True)

    def restart_daemon(self):
        self.stop_daemon()
        self.start_daemon()

    def cli(self, *args, socket_path=None):
        env = dict(self.env)
        if socket_path is not None:
            env["SUTANDO_RUNTIME_SOCKET"] = str(socket_path)
        return subprocess.run(
            [*PYBASE, str(CLI), *args], cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)

    def submit(self, key=None, *, text=TEXT, priority="normal", socket_path=None):
        args = ["task", "submit", text, "--priority", priority]
        if key is not None:
            args += ["--idempotency-key", key]
        return self.cli(*args, socket_path=socket_path)

    def success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def backend_error(self, result, message):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr), {"error": f"-32602: {message}"})

    def snapshot(self):
        return {
            str(path.relative_to(self.workspace)):
                (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
            for directory in (self.tasks, self.results)
            for path in directory.rglob("*") if path.is_file()
        }

    def assert_queue(self, *task_ids):
        self.assertEqual({path.stem for path in self.tasks.glob("task-*.txt")},
                         set(task_ids))

    @contextlib.contextmanager
    def relay(self, *, discard_response=False):
        """Forward one real RPC unchanged, optionally withholding its response."""
        relay_path = self.base / "relay.sock"
        captured = {}
        errors = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(relay_path))
            listener.listen(1)
            listener.settimeout(3)

            def forward():
                try:
                    connection, _ = listener.accept()
                    with connection, socket.socket(
                            socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
                        connection.settimeout(3)
                        upstream.settimeout(3)
                        request = read_frame(connection)
                        captured["request"] = json.loads(request)
                        upstream.connect(str(self.socket))
                        upstream.sendall(request)
                        response = read_frame(upstream)
                        captured["response"] = json.loads(response)
                        captured["published"] = self.snapshot()
                        if not discard_response:
                            connection.sendall(response)
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=forward, daemon=True)
            thread.start()
            try:
                yield relay_path, captured
            finally:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), "relay thread did not stop")
                relay_path.unlink(missing_ok=True)
                self.assertEqual(errors, [], f"relay failed: {errors}")

    def test_same_key_separate_invocations_and_verbatim_forwarding(self):
        key = " weekly-summary-é-42 "
        with self.relay() as (relay_path, captured):
            first = self.success(self.submit(key, priority="urgent",
                                             socket_path=relay_path))
        self.assertEqual(captured["request"]["method"], "task.submit")
        self.assertEqual(captured["request"]["params"], {
            "task": TEXT, "priority": "urgent", "idempotencyKey": key})
        self.assertEqual(first["state"], "pending")
        before = self.snapshot()
        for _ in range(2):
            self.assertEqual(self.success(self.submit(key, priority="urgent")), first)
            self.assert_queue(first["taskId"])
            self.assertEqual(self.snapshot(), before)

    def lost_response(self, *, restart):
        key = "weekly-summary-lost-response"
        with self.relay(discard_response=True) as (relay_path, captured):
            lost = self.submit(key, socket_path=relay_path)
        self.assertEqual(lost.returncode, 1, lost.stderr)
        self.assertEqual(lost.stdout, "")
        self.assertIn("error", json.loads(lost.stderr))
        response = captured["response"]
        self.assertNotIn("error", response)
        original = response["result"]
        self.assertEqual(original["state"], "pending")
        self.assert_queue(original["taskId"])
        public = self.tasks / f"{original['taskId']}.txt"
        self.assertIn(str(public.relative_to(self.workspace)), captured["published"])
        self.assertEqual(self.snapshot(), captured["published"])
        if restart:
            self.restart_daemon()
        recovered = self.success(self.submit(key))
        self.assertEqual(recovered, original)
        self.assert_queue(original["taskId"])
        self.assertEqual(self.snapshot(), captured["published"])
        print(json.dumps({"event": "lost-response-recovered", "at": time.time(),
                          "restart": restart, "discarded": original,
                          "recovered": recovered, "publications": 1}), flush=True)

    def test_lost_response_retry_same_daemon(self):
        self.lost_response(restart=False)

    def test_lost_response_retry_after_daemon_restart(self):
        self.lost_response(restart=True)

    def finished_retry(self, *, archived):
        key = "finished-summary"
        original = self.success(self.submit(key))
        task_id = original["taskId"]
        public = self.tasks / f"{task_id}.txt"
        # These are lifecycle fixtures, not execution by a task consumer.
        if archived:
            archive = self.tasks / "archive"
            archive.mkdir()
            public.rename(archive / public.name)
        else:
            (self.results / public.name).write_text("Synthetic summary complete")
            public.unlink()
        before = self.snapshot()
        self.restart_daemon()
        expected = {"taskId": task_id, "state": "done"}
        self.assertEqual(self.success(self.cli("task", "status", task_id)), expected)
        self.assertEqual(self.success(self.submit(key)), expected)
        self.assert_queue()
        self.assertEqual(self.snapshot(), before)

    def test_completed_retry_does_not_requeue(self):
        self.finished_retry(archived=False)

    def test_archived_retry_does_not_requeue(self):
        self.finished_retry(archived=True)

    def test_unkeyed_and_different_keys_create_distinct_tasks(self):
        with self.relay() as (relay_path, captured):
            first = self.success(self.submit(socket_path=relay_path))
        self.assertEqual(captured["request"]["params"], {
            "task": TEXT, "priority": "normal"})
        submissions = [first, self.success(self.submit()),
                       self.success(self.submit("summary-a")),
                       self.success(self.submit("summary-b"))]
        task_ids = {result["taskId"] for result in submissions}
        self.assertEqual(len(task_ids), 4)
        self.assert_queue(*task_ids)

    def test_key_conflicts_preserve_original_submission(self):
        key = "summary-conflict"
        first = self.success(self.submit(key))
        before = self.snapshot()
        for change in ({"text": "Different synthetic task"}, {"priority": "low"}):
            with self.subTest(change=change):
                self.backend_error(self.submit(key, **change),
                                   "idempotency key is already bound to a "
                                   "different task submission")
                self.assert_queue(first["taskId"])
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.success(self.submit(key)), first)

    def test_invalid_keys_rejected_by_backend_without_publication(self):
        before = self.snapshot()
        for key in ("", " \t\n ", "x" * 1025, "é" * 513):
            with self.subTest(key=repr(key)[:40]):
                self.backend_error(self.submit(key),
                                   "idempotency key is required" if not key.strip()
                                   else "idempotency key is too long")
                self.assert_queue()
                self.assertEqual(self.snapshot(), before)

    def test_maximum_utf8_key_is_accepted(self):
        key = "é" * 512
        first = self.success(self.submit(key))
        before = self.snapshot()
        self.assertEqual(self.success(self.submit(key)), first)
        self.assert_queue(first["taskId"])
        self.assertEqual(self.snapshot(), before)

    def test_help_explains_explicit_retry_semantics(self):
        result = self.cli("task", "submit", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        help_text = " ".join(result.stdout.split())
        for phrase in ("--idempotency-key KEY", "same key, text and priority",
                       "explicit retry", "original taskId", "without requeueing",
                       "completion", "daemon restart", "without a key",
                       "new task", "no automatic retries"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, help_text)
        self.assert_queue()


if __name__ == "__main__":
    unittest.main(verbosity=2)
