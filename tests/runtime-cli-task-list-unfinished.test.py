#!/usr/bin/env python3
"""Unfinished task discovery through bin/sutando and the real Unix-socket daemon.

Run: python3 tests/runtime-cli-task-list-unfinished.test.py
Only disposable state and synthetic results are used; cancellation is a signal.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env


scratch_file = (Path(os.environ["SUTANDO_RUNTIME_SOCKET"]).parent / "environment.json"
                if os.environ.get("SUTANDO_RUNTIME_SOCKET") else None)
scratch = json.loads(scratch_file.read_text()) if scratch_file and scratch_file.is_file() else {}
isolate_os_environ(**scratch)

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "src" / "runtime-api" / "server.py"
CLI = ROOT / "bin" / "sutando"
sys.path.insert(0, str(ROOT / "src"))
from activity_bus import cancel_target
from local_task_protocol import parse_task_headers_lenient
from task_priority import parse_priority_from_file


class UnfinishedTaskListTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="unfinished-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.workspace = self.base / "workspace"
        self.tasks = self.workspace / "tasks"
        self.results = self.workspace / "results"
        state = self.workspace / "state"
        for path in (self.tasks, self.results, state, self.base / "config",
                     self.base / "tmp"):
            path.mkdir(parents=True)
        self.socket = self.base / "rt.sock"
        self.env = isolated_child_env(
            PYTHONDONTWRITEBYTECODE="1", SUTANDO_TEST_MODE="1", SUTANDO_TELEMETRY="0",
            SUTANDO_PACKAGED="", SUTANDO_APP_SUPPORT="", SUTANDO_SCP_WSS_URL="",
            TMPDIR=str(self.base / "tmp"), TMP=str(self.base / "tmp"),
            TEMP=str(self.base / "tmp"), SUTANDO_WORKSPACE=str(self.workspace),
            SUTANDO_WORKSPACE_DIR=str(self.workspace),
            SUTANDO_DEFAULT_WORKSPACE=str(self.workspace),
            SUTANDO_TASKS_DIR=str(self.tasks), SUTANDO_RESULTS_DIR=str(self.results),
            SUTANDO_RUNTIME_STATE=str(state), SUTANDO_RUNTIME_DB=str(state / "runtime.sqlite"),
            SUTANDO_HA_DIR=str(state / "ha"), SUTANDO_RUNTIME_SOCKET=str(self.socket),
            SUTANDO_RUN_DIR=str(self.base / "run"),
            SUTANDO_INSTANCE_REGISTRY=str(self.base / "registry"),
            SUTANDO_AGENT_ID="unfinished-list-test", SUTANDO_HOST_LABEL="unfinished-list-test",
            SUTANDO_INSTANCE_ID="", CLAUDE_CONFIG_DIR=str(self.base / "config"),
            SUTANDO_TMUX_SOCKET=str(self.base / "unused-tmux.sock"),
            SUTANDO_PY=sys.executable)
        self.pybase = [sys.executable]
        if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
            self.pybase += ["-m", "coverage", "run", f"--rcfile={ROOT / '.coveragerc'}"]
            launcher = self.base / "coverage-python"
            launcher.write_text("#!/usr/bin/env bash\nexec " + shlex.join(self.pybase)
                                + ' "$@"\n')
            launcher.chmod(0o700)
            self.env["SUTANDO_PY"] = str(launcher)
        self.diagnostics = ((scratch_file.parent / "diagnostics") if scratch
                            else self.base / "diagnostics")
        self.diagnostics.mkdir(parents=True, exist_ok=True)
        self.events = []
        self.processes = []
        self.sequence = 0
        self.addCleanup(self.stop_processes)
        self.record("environment", scratch=str(scratch_file) if scratch else None,
                    workspace=str(self.workspace), socket=str(self.socket),
                    path_preserved=self.env["PATH"] == os.environ["PATH"])
        if scratch:
            private_root = scratch_file.parent.resolve()
            self.assertTrue(self.base.resolve().is_relative_to(private_root))
            self.assertEqual(self.env["PATH"], scratch["PATH"])
            self.assertTrue(all(p in self.env["PYTHONPATH"].split(os.pathsep)
                                for p in scratch.get("PYTHONPATH", "").split(os.pathsep) if p))
        self.start_daemon()

    def record(self, event, **fields):
        row = {"event": event, "at": time.time(), **fields}
        self.events.append(row)
        print(json.dumps(row), flush=True)
        (self.diagnostics / "unfinished-task-list-events.json").write_text(
            json.dumps(self.events, indent=2))

    def rpc(self, method, params=None, timeout=3):
        self.sequence += 1
        request = {"jsonrpc": "2.0", "id": self.sequence,
                   "method": method, "params": params or {}}
        deadline = time.monotonic() + timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(self.socket))
            sock.sendall(json.dumps(request).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("RPC response deadline exceeded")
                sock.settimeout(remaining)
                chunk = sock.recv(65536)
                if not chunk:
                    raise EOFError("daemon closed before a complete RPC response")
                data += chunk
                self.assertLessEqual(len(data), 256 * 1024)
        response = json.loads(data)
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], request["id"])
        return response

    def result(self, method, params=None):
        response = self.rpc(method, params)
        self.assertNotIn("error", response, response)
        return response["result"]

    def start_daemon(self):
        command = [*self.pybase, str(SERVER)]
        log = tempfile.TemporaryFile(dir=self.base)
        try:
            self.daemon = subprocess.Popen(
                command, cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT)
        except BaseException:
            log.close()
            raise
        self.processes.append((self.daemon, log))
        self.record("daemon-started", pid=self.daemon.pid, argv=command)
        deadline = time.monotonic() + 10
        last_error = None
        while time.monotonic() < deadline and self.daemon.poll() is None:
            try:
                response = self.rpc("task.status", {"taskId": "task-rtapi-readiness"},
                                    timeout=0.5)
                self.assertEqual(response["result"]["state"], "unknown")
                head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                      env=self.env, capture_output=True, text=True,
                                      check=True, timeout=5)
                self.record("daemon-ready", pid=self.daemon.pid, commit=head.stdout.strip(),
                            response=response)
                self.save_log(self.daemon, log)
                return
            except (OSError, EOFError) as exc:
                last_error = exc
                time.sleep(0.03)
        self.fail(f"daemon not ready: {last_error}; {self.save_log(self.daemon, log)}")

    def save_log(self, process, log):
        log.seek(0, os.SEEK_END)
        log.seek(max(0, log.tell() - 8192))
        tail = log.read().decode("utf-8", errors="replace")
        (self.diagnostics / f"unfinished-daemon-{process.pid}.log").write_text(tail)
        return tail

    def stop_processes(self):
        while self.processes:
            process, log = self.processes.pop()
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            try:
                self.record("daemon-reaped", pid=process.pid, returncode=process.returncode,
                            log=self.save_log(process, log))
            finally:
                log.close()

    def cli(self, *args, as_json=True):
        command = [str(CLI), *args]
        result = subprocess.run(command, cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, f"{command}: {result.stderr}")
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout) if as_json else result.stdout

    def submit(self, text, index):
        submitted = self.cli("task", "submit", text)
        self.assertEqual(submitted["state"], "pending")
        tid = submitted["taskId"]
        self.assertRegex(tid, r"^task-rtapi-[0-9a-f]{12}$")
        stamp = 1_700_000_000 + index
        os.utime(self.tasks / f"{tid}.txt", (stamp, stamp))
        return tid

    def snapshot(self):
        return {str(p.relative_to(self.workspace)):
                (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_ino,
                 p.stat().st_mtime_ns, p.stat().st_mode)
                for directory in (self.tasks, self.results)
                for p in directory.rglob("*") if p.is_file()}

    def test_older_unfinished_tasks_remain_discoverable_and_actionable(self):
        help_text = self.cli("task", "list", "--help", as_json=False)
        self.assertIn("--unfinished", help_text)
        self.assertIn("exclude done", help_text)
        self.assertIn("200", help_text)
        texts = ["pending   request\twith\u2003spaces", "claimed request " + "界" * 180,
                 "awaiting a nonblank result", "waiting for the owner's input"]
        ids = [self.submit(text, index) for index, text in enumerate(texts)]
        claimed = self.tasks / f"{ids[1]}.claimed-worker-1.txt"
        (self.tasks / f"{ids[1]}.txt").rename(claimed)
        (self.results / f"{ids[1]}.txt").write_text("")
        (self.results / f"{ids[2]}.txt").write_text(" \n\t\n")
        waiting = self.cli("elicitation", "request", "--task-id", ids[3],
                           "--question", "Which synthetic option?", "--type", "single_select",
                           "--options", '["First", "Second"]')
        self.assertEqual(waiting["status"], "pending")
        expected_states = dict(zip(ids, ("pending", "in_progress", "pending", "waiting_for_input")))
        completed = []
        for index in range(200):
            tid = self.submit(f"completed synthetic request {index}", 10 + index)
            (self.results / f"{tid}.txt").write_text(f"completed synthetic result {index}\n")
            completed.append(tid)
        for tid in ("task-foreign-pending", "task-foreign-done"):
            (self.tasks / f"{tid}.txt").write_text(f"id: {tid}\ntask: foreign private text\n")
        (self.results / "task-foreign-done.txt").write_text("foreign private result")
        before = self.snapshot()
        self.record("fixture-created", unfinished=ids, completed_count=len(completed),
                    records=len(before))

        filtered = self.cli("task", "list", "--unfinished")
        self.assertEqual([row["taskId"] for row in filtered["tasks"]], ids[::-1])
        self.assertNotIn("truncated", filtered)
        for row in filtered["tasks"]:
            tid = row["taskId"]
            self.assertEqual(row["state"], expected_states[tid])
            self.assertEqual(row["preview"], " ".join(texts[ids.index(tid)].split())[:160])
            self.assertEqual(row["source"], "runtime-api")
            self.assertEqual(row["priority"], "normal")
            self.assertTrue(row["timestamp"])
        for _ in range(2):
            self.assertEqual(self.result("task.list", {"unfinishedOnly": True}), filtered)
            self.assertEqual(self.cli("task", "list", "--unfinished"), filtered)
            self.assertEqual(self.snapshot(), before)
        self.record("filtered-list", response=filtered, records_unchanged=True)

        default = self.cli("task", "list")
        self.assertEqual(default, self.result("task.list"))
        self.assertEqual(default, self.result("task.list", {"unfinishedOnly": False}))
        self.assertEqual([row["taskId"] for row in default["tasks"]], completed[::-1])
        self.assertTrue(default["truncated"])
        self.assertEqual({row["state"] for row in default["tasks"]}, {"done"})
        self.assertEqual(self.cli("task", "get-result", completed[0]),
                         {"taskId": completed[0], "result": "completed synthetic result 0"})
        for value in (None, 0, 1, 0.0, "true", "false", [], {}):
            rejected = self.rpc("task.list", {"unfinishedOnly": value})
            self.assertEqual(rejected["error"]["code"], -32602)
            self.assertIn("boolean", rejected["error"]["message"])
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.snapshot(), before)
        self.record("negative-controls", default_count=200, truncated=True,
                    completed_result_retrieved=True, invalid_types_rejected=8)

        self.stop_processes()
        self.start_daemon()
        self.assertEqual(self.cli("task", "list", "--unfinished"), filtered)
        self.assertEqual(self.result("task.list", {"unfinishedOnly": True}), filtered)
        self.assertEqual(self.result("request.get", {"requestId": waiting["requestId"]})["status"],
                         "pending")
        self.assertEqual(self.snapshot(), before)
        self.record("restart-consistent", records_unchanged=True)

        selected = next(row["taskId"] for row in filtered["tasks"] if row["state"] == "in_progress")
        details = self.cli("task", "details", selected)
        self.assertEqual(details["taskId"], ids[1])
        self.assertEqual(details["task"], texts[1])
        self.assertEqual(details["state"], "in_progress")
        cancelled = self.cli("task", "cancel", selected)
        self.assertEqual(cancelled["taskId"], selected)
        self.assertEqual(cancelled["cancelled"], "requested")
        self.assertEqual(cancelled["state"], "in_progress")
        instruction = self.tasks / f"{cancelled['cancelTaskId']}.txt"
        parsed = parse_task_headers_lenient(instruction.read_text())
        self.assertEqual(parsed.body, f"CANCEL_INSTRUCTION: stop processing {selected}")
        self.assertEqual(cancel_target(parsed.body), selected)
        self.assertEqual(parsed.get("priority"), "urgent")
        self.assertEqual(parse_priority_from_file(instruction), "urgent")
        self.assertEqual(parsed.get("source"), "runtime-api")
        self.assertEqual(parsed.get("access_tier"), "owner")
        after = self.snapshot()
        self.assertEqual(set(after) - set(before), {str(instruction.relative_to(self.workspace))})
        self.assertEqual({name: after.get(name) for name in before}, before)
        self.assertTrue(claimed.is_file())
        self.record("cancel-signal", selected=selected, instruction=cancelled["cancelTaskId"],
                    original_records_unchanged=True, execution_observed=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
