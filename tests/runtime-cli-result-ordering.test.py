#!/usr/bin/env python3
"""Result ordering through the real daemon, Unix socket and CLI.

Run: python3 tests/runtime-cli-result-ordering.test.py
State and sockets follow TMPDIR; publications are synthetic local results.
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import contextlib
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "src" / "runtime-api" / "server.py"
CLI = ROOT / "src" / "runtime-cli" / "sutando-runtime.py"
SECOND = 1_800_000_000
PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={ROOT / '.coveragerc'}"]


class JsonLines:
    def __init__(self, source):
        self.source = source
        self.buffer = b""

    def next(self, timeout=5):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.source], [], [], remaining)[0]:
                raise TimeoutError("no complete JSON line before deadline")
            chunk = os.read(self.source.fileno(), 65536)
            if not chunk:
                raise EOFError("stream closed before a complete JSON line")
            self.buffer += chunk
            if len(self.buffer) > 65536:
                raise ValueError("oversized test stream")
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)


class ResultOrderingCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="rto-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.workspace = self.base / "workspace"
        self.results = self.workspace / "results"
        state = self.workspace / "state"
        for path in (self.results, self.workspace / "tasks", state,
                     self.base / "config", self.base / "tmp"):
            path.mkdir(parents=True)
        self.socket = self.base / "rt.sock"
        self.env = {
            **isolated_child_env(),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(self.base / "tmp"),
            "TMP": str(self.base / "tmp"),
            "TEMP": str(self.base / "tmp"),
            "SUTANDO_TEST_MODE": "1",
            "SUTANDO_PACKAGED": "",
            "SUTANDO_APP_SUPPORT": "",
            "SUTANDO_WORKSPACE": str(self.workspace),
            "SUTANDO_WORKSPACE_DIR": str(self.workspace),
            "SUTANDO_DEFAULT_WORKSPACE": str(self.workspace),
            "SUTANDO_TASKS_DIR": str(self.workspace / "tasks"),
            "SUTANDO_RESULTS_DIR": str(self.results),
            "SUTANDO_RUNTIME_STATE": str(state),
            "SUTANDO_RUNTIME_DB": str(state / "runtime.sqlite"),
            "SUTANDO_HA_DIR": str(state / "ha"),
            "SUTANDO_RUNTIME_SOCKET": str(self.socket),
            "SUTANDO_RUN_DIR": str(self.base / "run"),
            "SUTANDO_INSTANCE_REGISTRY": str(self.base / "registry"),
            "SUTANDO_AGENT_ID": "result-ordering-test",
            "SUTANDO_HOST_LABEL": "result-ordering-test",
            "SUTANDO_INSTANCE_ID": "",
            "CLAUDE_CONFIG_DIR": str(self.base / "config"),
            "SUTANDO_TMUX_SOCKET": str(self.base / "unused-tmux.sock"),
            "SUTANDO_SCP_WSS_URL": "",
            "SUTANDO_RESULT_POLL_S": "0.1",
        }
        self.processes = []
        self.addCleanup(self.stop_processes)

    def record(self, event, **fields):
        print(json.dumps({"event": event, "at": time.time(), **fields}), flush=True)

    def launch(self, script, *args, stream=False):
        command = [*PYBASE, str(script), *args]
        log = self.base / f"process-{len(self.processes)}.log"
        with log.open("wb") as output:
            process = subprocess.Popen(
                command, cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if stream else output,
                stderr=output)
        self.processes.append((process, log))
        self.record("process-started", pid=process.pid, argv=command)
        return process

    def stop_processes(self):
        for process, log in reversed(self.processes):
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.send_signal(signal.SIGCONT)
                    process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            if process.stdout is not None:
                process.stdout.close()
            with log.open("rb") as output:
                output.seek(max(0, log.stat().st_size - 8192))
                tail = output.read().decode("utf-8", errors="replace")
            self.record("process-reaped", pid=process.pid,
                        returncode=process.returncode, log=tail)

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(0.5)
            sock.connect(str(self.socket))
        except BaseException:
            sock.close()
            raise
        return sock

    def start_daemon(self):
        self.daemon = self.launch(SERVER)
        deadline = time.monotonic() + 8
        last_error = None
        while time.monotonic() < deadline and self.daemon.poll() is None:
            try:
                with self.connect() as sock:
                    sock.sendall(b'{"jsonrpc":"2.0","id":"ready",'
                                 b'"method":"task.list","params":{}}\n')
                    response = JsonLines(sock).next(timeout=0.5)
                self.assertEqual(response["id"], "ready")
                self.assertEqual(response["result"], {"tasks": []})
                head = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, env=self.env,
                    capture_output=True, text=True, check=True, timeout=5)
                self.record("daemon-ready", pid=self.daemon.pid,
                            checkout=str(ROOT), commit=head.stdout.strip(),
                            socket=str(self.socket), response=response)
                return
            except (OSError, EOFError) as exc:
                last_error = exc
                time.sleep(0.03)
        self.fail(f"daemon did not become ready: {last_error}")

    def cli(self, *args):
        command = [*PYBASE, str(CLI), "task", *args]
        result = subprocess.run(command, cwd=ROOT, env=self.env,
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=8)
        self.record("cli", argv=command, returncode=result.returncode,
                    stdout=result.stdout, stderr=result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def publish(self, name, body, ns):
        path = self.results / f"{name}.txt"
        staged = self.results / f".{name}.tmp"
        staged.write_text(body)
        os.utime(staged, ns=(ns, ns))
        staged.replace(path)
        return path.stat().st_mtime_ns

    def pause_daemon(self):
        self.daemon.send_signal(signal.SIGSTOP)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            pid, status = os.waitpid(self.daemon.pid, os.WUNTRACED | os.WNOHANG)
            if pid:
                self.assertTrue(os.WIFSTOPPED(status), "daemon exited before batch")
                self.record("daemon-stopped", pid=pid, signal=os.WSTOPSIG(status))
                return
            time.sleep(0.01)
        self.fail("daemon did not stop before batch publication")

    def assert_delivery(self, frames, cli_rows, names, bodies, seconds):
        self.record("delivery", frames=frames, cli_rows=cli_rows)
        for frame in frames:
            self.assertEqual(frame["jsonrpc"], "2.0")
            self.assertEqual(frame["method"], "task.result")
            self.assertNotIn("id", frame)
        self.assertEqual([frame["params"] for frame in frames], [
            {"taskId": name, "result": body, "ts": second}
            for name, body, second in zip(names, bodies, seconds)])
        self.assertEqual(cli_rows, [
            {"result": name, "body": body, "ts": second}
            for name, body, second in zip(names, bodies, seconds)])
        for row in [*(f["params"] for f in frames), *cli_rows]:
            self.assertIs(type(row["ts"]), int)

    def assert_quiet(self, *streams):
        for stream in streams:
            with self.assertRaises(TimeoutError):
                stream.next(timeout=0.6)

    def test_latest_list_and_stream_use_precise_modification_order(self):
        base_ns = SECOND * 1_000_000_000
        self.publish("task-rtapi-backlog", "existing answer", base_ns - 10_000_000_000)
        self.publish("task-rtapi-blank", " \n\t", base_ns + 1_000_000_000)
        self.start_daemon()
        watcher = self.launch(CLI, "task", "watch", stream=True)
        watch = JsonLines(watcher.stdout)
        self.assertEqual(watch.next(), {"watching": True, "activity": False})
        sock = self.connect()
        self.addCleanup(sock.close)
        sock.sendall(b'{"jsonrpc":"2.0","id":"sub",'
                     b'"method":"task.subscribe","params":{}}\n')
        raw = JsonLines(sock)
        ack = raw.next()
        self.assertEqual(ack["id"], "sub")
        self.assertTrue(ack["result"]["subscribed"])
        self.record("subscription-ack", response=ack)
        self.publish("task-rtapi-warmup", "subscription ready", base_ns - 1_000_000_000)
        self.assert_delivery([raw.next()], [watch.next()],
                             ["task-rtapi-warmup"], ["subscription ready"], [SECOND - 1])
        (self.results / "task-rtapi-warmup.txt").unlink()

        # A stopped child cannot poll a partly published batch.
        self.pause_daemon()
        try:
            observed = {}
            for name, fraction in (("alpha", 500_000_000), ("zeta", 900_000_000),
                                   ("mu", 100_000_000)):
                observed[name] = self.publish(f"task-rtapi-{name}", f"{name} answer",
                                              base_ns + fraction)
            self.publish("task-rtapi-blank", " \n\t", base_ns + 1_000_000_000)
            self.publish("task-foreign", "foreign answer", base_ns + 2_000_000_000)
            self.assertLess(observed["mu"], observed["alpha"])
            self.assertLess(observed["alpha"], observed["zeta"])
            self.assertEqual({ns // 1_000_000_000 for ns in observed.values()}, {SECOND})
            self.record("batch-published", mtimes_ns=observed)
        finally:
            self.daemon.send_signal(signal.SIGCONT)

        names = [f"task-rtapi-{name}" for name in ("mu", "alpha", "zeta")]
        bodies = [f"{name} answer" for name in ("mu", "alpha", "zeta")]
        self.assert_delivery([raw.next() for _ in names], [watch.next() for _ in names],
                             names, bodies, [SECOND] * 3)
        self.assertEqual(self.cli("get-result"), {
            "taskId": names[-1], "result": bodies[-1], "latest": True})
        listed = self.cli("results")
        self.assertEqual(listed, {"results": [
            {"taskId": name, "preview": body, "ts": SECOND}
            for name, body in zip(reversed(names), reversed(bodies))] + [
                {"taskId": "task-rtapi-backlog", "preview": "existing answer",
                 "ts": SECOND - 10}]})
        for row in listed["results"]:
            self.assertIs(type(row["ts"]), int)
        self.assertEqual(self.cli("get-result", names[0]), {
            "taskId": names[0], "result": bodies[0]})
        self.assert_quiet(raw, watch)

        self.publish("task-rtapi-blank", "now ready", base_ns + 3_000_000_000)
        self.assert_delivery([raw.next()], [watch.next()],
                             ["task-rtapi-blank"], ["now ready"], [SECOND + 3])
        self.assert_quiet(raw, watch)
        self.assertIsNone(self.daemon.poll())
        self.assertIsNone(watcher.poll())


if __name__ == "__main__":
    unittest.main(verbosity=2)
