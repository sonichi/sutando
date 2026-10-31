#!/usr/bin/env python3
"""Task engagement through the real daemon, CLI and private Unix socket."""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "src/runtime-api/server.py"
CLI = ROOT / "src/runtime-cli/sutando-runtime.py"

# A provisioned socket's directory also carries its disposable environment.
socket_setting = os.environ.get("SUTANDO_RUNTIME_SOCKET")
ENVIRONMENT = Path(socket_setting).parent / "environment.json" if socket_setting else None
if ENVIRONMENT is not None and ENVIRONMENT.is_file():
    os.environ.update(json.loads(ENVIRONMENT.read_text()))
else:
    ENVIRONMENT = None

sys.path.insert(0, str(ROOT / "src"))
from activity_bus import ActivityStore, LifecycleTransition, RuntimeEvent  # noqa: E402
from activity_rows import append as append_activity  # noqa: E402

PYTHON = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYTHON += ["-m", "coverage", "run", f"--rcfile={ROOT / '.coveragerc'}"]


class TaskEngagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix="engage-", dir=ENVIRONMENT.parent if ENVIRONMENT else None)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.ws = self.base / "workspace"
        self.other = self.base / "other-workspace"
        self.tasks = self.ws / "tasks"
        self.results = self.ws / "results"
        for path in (self.tasks, self.results, self.ws / "state", self.other):
            path.mkdir(parents=True)
        self.env = {
            **isolated_child_env(),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SUTANDO_TEST_MODE": "1",
            "SUTANDO_TELEMETRY": "0",
            "SUTANDO_PACKAGED": "",
            "SUTANDO_APP_SUPPORT": "",
            "SUTANDO_WORKSPACE": str(self.other),
            "SUTANDO_WORKSPACE_DIR": str(self.other),
            "SUTANDO_DEFAULT_WORKSPACE": str(self.other),
            "SUTANDO_RUNTIME_STATE": str(self.ws / "state"),
            "SUTANDO_RUNTIME_DB": str(self.ws / "state/runtime.sqlite"),
            "SUTANDO_HA_DIR": str(self.ws / "state/human-actions"),
            "SUTANDO_RUNTIME_SOCKET": str(self.base / "api.sock"),
            "SUTANDO_RUN_DIR": str(self.base / "run"),
            "SUTANDO_INSTANCE_REGISTRY": str(self.base / "registry"),
            "SUTANDO_TASKS_DIR": str(self.tasks),
            "SUTANDO_RESULTS_DIR": str(self.results),
            "SUTANDO_AGENT_ID": "task-engagement-test",
            "SUTANDO_HOST_LABEL": "task-engagement-test",
            "SUTANDO_INSTANCE_ID": "",
        }
        diagnostic_root = ENVIRONMENT.parent if ENVIRONMENT else self.base
        self.diagnostics = diagnostic_root / "diagnostics" / self.base.name
        self.diagnostics.mkdir(parents=True)
        self.daemon = None
        self.starts = 0
        self.addCleanup(self.stop_daemon)
        self.record(event="environment", supplied=str(ENVIRONMENT),
                    socket=self.env["SUTANDO_RUNTIME_SOCKET"], workspace=str(self.ws),
                    ambient_workspace=str(self.other))
        self.start_daemon()

    def record(self, **fields):
        with (self.diagnostics / "observations.jsonl").open("a") as output:
            output.write(json.dumps({"ts": time.time(), **fields}) + "\n")

    def run_cli(self, *args, timeout=5):
        argv = [*PYTHON, str(CLI), *args]
        result = subprocess.run(argv, cwd=ROOT, env=self.env, capture_output=True,
                                text=True, timeout=timeout)
        self.record(event="cli", argv=argv, returncode=result.returncode,
                    stdout=result.stdout, stderr=result.stderr)
        return result

    def cli(self, *args, expected_rc=0):
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, expected_rc, result.stdout + result.stderr)
        return json.loads(result.stdout or result.stderr)

    def start_daemon(self):
        self.starts += 1
        self.log_path = self.diagnostics / f"daemon-{self.starts}.log"
        argv = [*PYTHON, str(SERVER)]
        with self.log_path.open("w") as log:
            self.daemon = subprocess.Popen(argv, cwd=ROOT, env=self.env,
                                           stdout=log, stderr=subprocess.STDOUT)
        self.record(event="start", argv=argv, pid=self.daemon.pid)
        started = time.monotonic()
        deadline = started + 10
        while time.monotonic() < deadline:
            if self.daemon.poll() is not None:
                self.fail(f"daemon exited: {self.log_path.read_text()}")
            if Path(self.env["SUTANDO_RUNTIME_SOCKET"]).exists():
                result = self.run_cli("runtime", "health",
                                      timeout=max(0.1, min(2, deadline - time.monotonic())))
                if result.returncode == 0:
                    self.record(event="ready", pid=self.daemon.pid,
                                elapsed=time.monotonic() - started,
                                response=json.loads(result.stdout))
                    return
            time.sleep(0.05)
        self.fail(f"daemon readiness timed out: {self.log_path.read_text()}")

    def stop_daemon(self):
        if self.daemon is None:
            return
        daemon, self.daemon = self.daemon, None
        if daemon.poll() is None:
            daemon.terminate()
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=5)
        self.record(event="reaped", pid=daemon.pid, returncode=daemon.returncode)
        self.log_path.write_bytes(self.log_path.read_bytes()[-16384:])
        self.assertEqual(daemon.returncode, 0, self.log_path.read_text())

    def submit(self, text):
        row = self.cli("task", "submit", text)
        self.assertEqual(row["state"], "pending")
        return row["taskId"]

    def running(self, task_id, *, workspace=None, ts=None):
        store = ActivityStore(workspace or self.ws)
        state = store.apply(LifecycleTransition(task_id, "RUNNING", ts=ts))
        self.assertEqual(state.phase, "RUNNING")
        return store

    def engage(self, task_id, kind="row", *, workspace=None, ts=None):
        ws = workspace or self.ws
        store = self.running(task_id, workspace=ws, ts=ts)
        if kind == "row":
            append_activity("local tool work", kind="working", room=None,
                            task={"id": task_id}, workspace=ws, ts=ts)
            self.assertEqual(store.load(task_id).seq, 0)
        else:
            state = store.apply(RuntimeEvent(task_id, "test-session", 1,
                                             "ToolStarted", ts=ts))
            self.assertEqual(state.seq, 1)

    def assert_states(self, task_id, expected):
        status = self.cli("task", "status", task_id)
        details = self.cli("task", "details", task_id)
        listed = self.cli("task", "list")["tasks"]
        matching = [row for row in listed if row["taskId"] == task_id]
        self.assertEqual(len(matching), 1, listed)
        self.assertEqual([status["state"], details["state"], matching[0]["state"]],
                         [expected] * 3)
        return status

    def fingerprint(self):
        files = list(self.tasks.rglob("*"))
        for ws in (self.ws, self.other):
            state = ws / "state"
            files.extend(p for p in state.rglob("*")
                         if p.relative_to(state).parts[0] == "activity"
                         or p.relative_to(state).parts[0].startswith("agent-activity"))
        return {str(p.relative_to(self.base)): (
                    p.stat().st_ino, p.stat().st_mtime_ns,
                    hashlib.sha256(p.read_bytes()).hexdigest())
                for p in files if p.is_file()}

    def publish(self, task_id, text="completed local work"):
        staged = self.results / f".{task_id}.tmp"
        staged.write_text(text)
        staged.replace(self.results / f"{task_id}.txt")

    def test_fresh_engagement_is_visible_and_reads_are_non_mutating(self):
        for kind in ("row", "event"):
            with self.subTest(kind=kind):
                task_id = self.submit(f"fresh {kind} engagement")
                self.engage(task_id, kind)
                before = self.fingerprint()
                self.assert_states(task_id, "in_progress")
                self.assertEqual(self.fingerprint(), before)
                self.assertTrue((self.tasks / f"{task_id}.txt").is_file())

    def test_negative_controls_and_claimed_task(self):
        controls = ("absent", "delivery-only", "processing-thinking", "row-only",
                    "stale-row", "stale-event", "future-row", "future-event",
                    "invalid-stamp", "corrupt-snapshot")
        for control in controls:
            with self.subTest(control=control):
                task_id = self.submit(control)
                if control == "row-only":
                    append_activity("no running snapshot", kind="working", room=None,
                                    task={"id": task_id}, workspace=self.ws)
                elif control in ("stale-row", "stale-event", "future-row", "future-event"):
                    age = -4000 if control.startswith("stale") else 3600
                    self.engage(task_id, control.split("-")[1], ts=time.time() + age)
                elif control != "absent":
                    store = self.running(task_id)
                    if control == "processing-thinking":
                        for kind in ("processing", "thinking"):
                            append_activity("no tool work", kind=kind, room=None,
                                            task={"id": task_id}, workspace=self.ws)
                    elif control == "invalid-stamp":
                        snapshot = json.loads(store.path(task_id).read_text())
                        snapshot.update(seq=1, last_activity_at="invalid")
                        store.path(task_id).write_text(json.dumps(snapshot))
                    elif control == "corrupt-snapshot":
                        store.path(task_id).write_text("{invalid json")
                self.assert_states(task_id, "pending")
        task_id = self.submit("claimed without activity")
        (self.tasks / f"{task_id}.txt").rename(
            self.tasks / f"{task_id}.claimed-core-1.txt")
        self.assert_states(task_id, "in_progress")

    def test_malformed_activity_preserves_reads_and_cancellation(self):
        controls = ("truncated-utf8", "invalid-utf8", "list-kind", "object-kind",
                    "overflow-row", "overflow-snapshot")
        for damaged in controls:
            task_id = self.submit("unreadable engagement")
            store = self.running(task_id)
            activity = self.ws / "state/agent-activity.jsonl"
            if damaged == "overflow-snapshot":
                activity = store.path(task_id)
            original = activity.read_bytes()
            if damaged == "overflow-snapshot":
                snapshot = json.loads(original)
                snapshot.update(seq=1, last_activity_at=10**400)
                replacement = json.dumps(snapshot).encode()
            elif damaged in ("truncated-utf8", "invalid-utf8"):
                raw = (b'{"kind":"working","line":"\xe2\x82'
                       if damaged == "truncated-utf8" else b'\xff\n')
                replacement = original + raw
            else:
                row = {"task": {"id": task_id}, "kind": "working", "ts": time.time()}
                if damaged == "overflow-row":
                    row["ts"] = 10**400
                else:
                    row["kind"] = ["working"] if damaged == "list-kind" else {"working": True}
                replacement = original + (json.dumps(row) + "\n").encode()
            activity.write_bytes(replacement)
            try:
                before = self.fingerprint()
                for command in ("status", "details", "list"):
                    with self.subTest(damaged=damaged, command=command):
                        response = self.cli("task", command, *(
                            [] if command == "list" else [task_id]))
                        if command == "list":
                            response = next(row for row in response["tasks"]
                                            if row["taskId"] == task_id)
                        self.assertEqual(response["state"], "pending")
                self.assertEqual(self.fingerprint(), before)

                with self.subTest(damaged=damaged, command="cancel"):
                    cancelled = self.cli("task", "cancel", task_id)
                    self.assertEqual(cancelled["state"], "pending")
                    self.assertEqual(cancelled["cancelled"], "requested")
                    cancel_id = cancelled["cancelTaskId"]
                    details = self.cli("task", "details", cancel_id)
                    self.assertEqual(details["priority"], "urgent")
                    self.assertEqual(details["task"],
                                     f"CANCEL_INSTRUCTION: stop processing {task_id}")
                    after = self.fingerprint()
                    self.assertEqual({p: after[p] for p in before}, before)
                    self.assertEqual(set(after) - set(before),
                                     {f"workspace/tasks/{cancel_id}.txt"})

                (self.tasks / f"{task_id}.txt").rename(
                    self.tasks / f"{task_id}.claimed-core-1.txt")
                self.assert_states(task_id, "in_progress")
            finally:
                activity.write_bytes(original)

    def test_precedence_ownership_archive_and_workspace_isolation(self):
        requests = (
            ("approval", ["--action", "change.apply"], "waiting_for_approval"),
            ("elicitation", ["--question", "Choose?", "--options", '["yes","no"]'],
             "waiting_for_input"),
            ("human-action", ["--action", "confirm", "--instructions", "Local check"],
             "waiting_for_human_action"),
        )
        for group, args, expected in requests:
            with self.subTest(group=group):
                task_id = self.submit(group)
                self.engage(task_id)
                request = self.cli(group, "request", "--task-id", task_id, *args)
                self.assertEqual(request["status"], "pending")
                self.assertEqual(self.assert_states(task_id, expected)["waitingOn"],
                                 [expected])
                self.publish(task_id)
                self.assert_states(task_id, "done")

        foreign = "task-discord-private"
        (self.tasks / f"{foreign}.txt").write_text(f"id: {foreign}\ntask: private\n")
        self.engage(foreign)
        self.publish(foreign, "private result")
        self.assertEqual(self.cli("task", "status", foreign)["state"], "not_found")
        for command in ("details", "get-result"):
            self.assertIn("error", self.cli("task", command, foreign, expected_rc=1))
        self.assertNotIn(foreign, [r["taskId"] for r in self.cli("task", "list")["tasks"]])
        self.assertNotIn(foreign, [r["taskId"] for r in self.cli("task", "results")["results"]])

        isolated = self.submit("engagement only in ambient workspace")
        self.running(isolated)
        self.engage(isolated, workspace=self.other)
        self.assert_states(isolated, "pending")

        archived = self.submit("archived without result")
        self.engage(archived)
        archive = self.tasks / "archive"
        archive.mkdir()
        (self.tasks / f"{archived}.txt").rename(archive / f"{archived}.txt")
        for command in ("status", "details"):
            self.assertEqual(self.cli("task", command, archived)["state"], "done")
        self.assertNotIn(archived, [r["taskId"] for r in self.cli("task", "list")["tasks"]])

    def test_restart_preserves_engagement_then_result_completes_task(self):
        tasks = []
        for kind in ("row", "event"):
            task_id = self.submit(f"restart with {kind}")
            self.engage(task_id, kind)
            self.assert_states(task_id, "in_progress")
            tasks.append(task_id)
        before = self.fingerprint()
        self.stop_daemon()
        self.start_daemon()
        for task_id in tasks:
            self.assert_states(task_id, "in_progress")
        self.assertEqual(self.fingerprint(), before)
        for task_id in tasks:
            self.publish(task_id)
            self.assert_states(task_id, "done")
            self.assertEqual(self.cli("task", "get-result", task_id)["result"],
                             "completed local work")


if __name__ == "__main__":
    unittest.main(verbosity=2)
