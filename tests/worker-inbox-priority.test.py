#!/usr/bin/env python3
"""Startup backlog through the real daemon, CLI, pool writers and session watcher."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import hermetic_env, isolate_os_environ, runtime_harness
from _helpers.process_group import popen_bound

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import pool_delivery as delivery
import pool_roster as roster
import pool_router as router
import spawn_worker
from result_publish import publish_text

WORKER = "1" * 32
OTHER = "2" * 32
DISPATCH = REPO / "src/delivery/task_dispatch.py"
WATCHER = REPO / "src/watch-tasks-stream.sh"
# The harness file found before isolation, and only its path variables.
HARNESS_FILE, HARNESS = runtime_harness()
SUPPLIED = HARNESS_FILE or Path("/nonexistent/environment.json")


class WorkerStartupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="inbox-priority-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ws = self.root / "workspace"
        self.env = hermetic_env(home=self.root / "home")
        if SUPPLIED.is_file():
            self.env.update(HARNESS)
        self.env.update({
            "SUTANDO_TEST_MODE": "1", "SUTANDO_TELEMETRY": "0",
            "SUTANDO_WORKSPACE": str(self.ws), "SUTANDO_WORKSPACE_DIR": str(self.ws),
            "SUTANDO_DEFAULT_WORKSPACE": str(self.ws),
            "SUTANDO_RESULTS_DIR": str(self.ws / "results"),
            "SUTANDO_RUNTIME_STATE": str(self.ws / "state"),
            "SUTANDO_RUNTIME_DB": str(self.ws / "state/runtime.sqlite"),
            "SUTANDO_RUNTIME_SOCKET": str(self.root / "api.sock"),
            "SUTANDO_HA_DIR": str(self.ws / "state/ha"),
            "SUTANDO_RUN_DIR": str(self.root / "run"),
            "SUTANDO_INSTANCE_REGISTRY": str(self.root / "registry"),
            "SUTANDO_AGENT_ID": "priority-test", "SUTANDO_HOST_LABEL": "priority-test",
            "SUTANDO_INSTANCE_ID": "", "SUTANDO_WATCHER_BEAT": "",
            "SUTANDO_TMUX_SOCKET": str(self.root / "unused-tmux.sock"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "SUTANDO_STANDBY_STOP_TIMEOUT": "2",
        })
        for name in ("tasks", "results", "state"):
            (self.ws / name).mkdir(parents=True, exist_ok=True)
        env_patch = mock.patch.dict(os.environ, self.env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.assertIsNotNone(shutil.which("fswatch", path=self.env["PATH"]), "real fswatch required")
        self.records = []
        self.logs = []
        self.addCleanup(self.save_evidence)
        self.record("candidate", commit=self.run_command(["git", "rev-parse", "HEAD"]).strip(),
                    diff_sha256=hashlib.sha256(self.run_command(["git", "diff", "HEAD"]).encode()).hexdigest())
        self.daemon, _ = self.start("daemon", [sys.executable, str(REPO / "src/runtime-api/server.py")])
        self.wait_for(self.socket_ready, "daemon Unix socket", self.daemon)
        ready = self.cli("details")
        self.assertEqual(ready["runtimeSocket"], self.env["SUTANDO_RUNTIME_SOCKET"])
        self.record("daemon-ready", pid=self.daemon.pid, response=ready)

    def record(self, event, **fields):
        self.records.append({"event": event, "time": time.time(), **fields})

    def save_evidence(self):
        if not SUPPLIED.is_file():
            return
        dest = SUPPLIED.parent / "diagnostics"
        dest.mkdir(exist_ok=True)
        name = "worker-priority-" + self._testMethodName
        for label, path in self.logs:
            (dest / f"{name}-{label}.log").write_bytes(path.read_bytes()[-64000:])
        (dest / f"{name}.json").write_text(json.dumps(self.records, indent=2) + "\n")

    def start(self, label, argv, env=None):
        log = self.root / f"{label}.log"
        with log.open("w") as output:
            proc = popen_bound(argv, env=env or self.env, cwd=REPO,
                               stdout=output, stderr=subprocess.STDOUT)
        self.logs.append((label, log))
        self.addCleanup(self.stop, proc)
        self.record("start", argv=list(map(str, argv)), pid=proc.pid)
        return proc, log

    def stop(self, proc):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            self.record("reaped", pid=proc.pid, returncode=proc.returncode)

    def run_command(self, argv, env=None, timeout=15):
        started = time.monotonic()
        result = subprocess.run(argv, env=env or self.env, cwd=REPO,
                                capture_output=True, text=True, timeout=timeout)
        self.record("command", argv=list(map(str, argv)), returncode=result.returncode,
                    elapsed=time.monotonic() - started,
                    stdout=result.stdout[-12000:], stderr=result.stderr[-12000:])
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout

    def cli(self, *args):
        return json.loads(self.run_command([str(REPO / "bin/sutando"), *args]))

    def socket_ready(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            try:
                client.connect(self.env["SUTANDO_RUNTIME_SOCKET"])
                return True
            except OSError:
                return False

    def wait_for(self, condition, label, proc, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        self.fail(f"{label} not observed; exit={proc.poll()}; logs=" +
                  repr({name: path.read_text()[-5000:] for name, path in self.logs}))

    def submit(self, body, priority="normal"):
        return self.cli("task", "submit", body, "--priority", priority)["taskId"]

    def register(self):
        roster.register_worker(self.ws, WORKER, "first", runtime="codex")
        roster.register_worker(self.ws, OTHER, "second", runtime="codex")
        plan = spawn_worker.plan(self.ws, REPO, worker_id=WORKER, runtime="codex",
                                 socket=self.env["SUTANDO_TMUX_SOCKET"])
        return {**self.env, **plan["env"]}

    def assign(self, task_id, worker=WORKER):
        outcome = router.route(self.ws, {"id": task_id, "requested_worker": worker})
        self.assertEqual(outcome["delivered"], [worker], outcome)
        self.assertEqual(router.committed_recipient(self.ws, task_id), worker)
        return delivery.deliveries_dir(self.ws, worker) / f"{task_id}.txt"

    def snapshot(self):
        return {str(p.relative_to(self.ws)): (hashlib.sha256(p.read_bytes()).hexdigest(),
                                             p.stat().st_mtime_ns, p.stat().st_ino)
                for name in ("tasks", "results", "deliveries", "state/attribution")
                for p in (self.ws / name).rglob("*") if p.is_file()}

    def sort(self, inbox, env):
        return self.run_command([sys.executable, str(DISPATCH), "sort-by-priority", str(inbox),
                                 "--resolve-inbox", str(self.ws), str(REPO / "src/inbox-resolve.sh")],
                                env=env).splitlines()

    def announcements(self, log):
        return [Path(line.removeprefix("TASK_FILE: ")).stem
                for line in log.read_text().splitlines() if line.startswith("TASK_FILE: ")]

    def buffered_events_env(self, env):
        """Inject part of the backlog before real fswatch supplies readiness."""
        inbox = Path(env.get("SUTANDO_TASKS_DIR", self.ws / "tasks"))
        entries = sorted(inbox.glob("*.txt"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        paths = list(map(str, entries[::2]))
        real_fswatch = shutil.which("fswatch", path=env["PATH"])
        bindir = self.root / "buffered-events-bin"
        bindir.mkdir()
        wrapper = bindir / "fswatch"
        wrapper.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            f"for path in {paths!r}:\n    print(path, flush=True)\n"
            f"os.execv({real_fswatch!r}, [{real_fswatch!r}, *sys.argv[1:]])\n")
        wrapper.chmod(0o755)
        self.record("synthetic-pre-ready-events", paths=paths, real_fswatch=real_fswatch)
        return {**env, "PATH": f"{bindir}:{env['PATH']}"}

    def sweep(self, label, env, expected, timeout=35):
        inbox = Path(env.get("SUTANDO_TASKS_DIR", self.ws / "tasks"))
        sentinel = Path(self.run_command([sys.executable, str(REPO / "src/util_paths.py"),
                                         "watcher-sentinel", str(self.ws / "state")], env).strip())
        started = time.monotonic()
        proc, log = self.start(label, ["bash", str(WATCHER), "--role", "session", "--inbox", str(inbox)], env)
        self.wait_for(lambda: sentinel.is_file() and sentinel.read_text().strip() == str(proc.pid),
                      "watcher readiness sentinel", proc)
        self.record("watcher-ready", pid=proc.pid, sentinel=str(sentinel), elapsed=time.monotonic() - started)
        self.wait_for(lambda: bool(self.announcements(log)), "first startup announcement", proc, timeout)
        self.record("first-announcement", pid=proc.pid, elapsed=time.monotonic() - started,
                    task=self.announcements(log)[0])
        self.wait_for(lambda: len(self.announcements(log)) >= len(expected), "startup announcements", proc, timeout)
        time.sleep(0.7)
        self.assertEqual(self.announcements(log), expected, log.read_text())
        self.assertIsNone(proc.poll(), log.read_text())
        self.record("startup-sweep", pid=proc.pid, announcements=self.announcements(log),
                    elapsed=time.monotonic() - started,
                    standby="bounded wait expired" if "standby watcher still present" in log.read_text()
                    else "standby absent before sweep")
        self.stop(proc)
        self.assertFalse(sentinel.exists(), "watcher must release its own readiness sentinel")
        return log

    def test_worker_backlog_repeated_startup_preserves_records_and_ownership(self):
        self.worker_backlog()

    def test_worker_backlog_beyond_lookup_prefix_restarts_without_mutation(self):
        self.worker_backlog(extra=32, timeout=60)

    def test_buffered_worker_events_join_priority_sweep_on_each_restart(self):
        self.worker_backlog(buffered=True)

    def worker_backlog(self, extra=0, timeout=35, buffered=False):
        env = self.register()
        ids = [self.submit(body, priority) for body, priority in (
            ("low one", "low"), ("normal one", "normal"), ("urgent ordinary", "urgent"),
            ("normal two", "normal"), ("low two", "low"))]
        cancel = self.cli("task", "cancel", ids[0])["cancelTaskId"]
        ids.append(cancel)
        self.assertIn("task: CANCEL_INSTRUCTION:", (self.ws / "tasks" / f"{cancel}.txt").read_text())
        missing = self.submit("legacy missing priority")
        path = self.ws / "tasks" / f"{missing}.txt"
        path.write_text(path.read_text().replace("priority: normal\n", ""))
        forged = self.submit("body text\npriority: urgent")
        ids += [missing, forged]
        for i, task_id in enumerate(ids):
            entry = self.assign(task_id)
            os.utime(entry, (1000000 + i, 1000000 + i))
            os.utime(self.ws / "tasks" / f"{task_id}.txt", (2000000 - i, 2000000 - i))
        accepted, completed, other = [self.submit(body, "urgent")
                                      for body in ("accepted", "completed", "other worker")]
        delivery.accept(self.assign(accepted))
        os.utime(self.assign(completed), (1000008, 1000008))
        publish_text(self.ws / "results" / f"{completed}.txt", "already done\n")
        self.assign(other, OTHER)
        tail = []
        for i in range(extra):
            tid = self.submit(f"pending backlog {i}")
            os.utime(self.assign(tid), (1000009 + i, 1000009 + i))
            tail.append(tid)
        before = self.snapshot()
        expected = [*[ids[i] for i in (2, 5, 1, 3, 6, 7)], *tail, ids[0], ids[4]]
        sorted_names = self.sort(Path(env["SUTANDO_TASKS_DIR"]), env)
        self.assertEqual(sorted_names, [f"{tid}.txt" for tid in [*expected[:2], completed, *expected[2:]]])
        self.assertEqual(before, self.snapshot(), "sorting must not mutate records or accept deliveries")
        if buffered:
            env = self.buffered_events_env(env)
        for i in (1, 2):
            self.sweep(f"worker-{i}", env, expected, timeout)
            self.assertEqual(before, self.snapshot(), "startup must preserve the same pending records")
            for tid in [*ids, *tail, accepted, completed]:
                self.assertEqual(router.committed_recipient(self.ws, tid), WORKER)
            self.assertEqual(router.committed_recipient(self.ws, other), OTHER)
        self.record("records-preserved", snapshots_equal=True, records=before,
                    excluded=[accepted, completed, other], expected=expected, pending_count=len(expected))

    def test_ordinary_core_inbox_keeps_priority_fifo_and_body_boundary(self):
        ids = [self.submit(body, priority) for body, priority in (
            ("low", "low"), ("normal", "normal"), ("urgent", "urgent"),
            ("body\npriority: urgent", "normal"))]
        missing = self.submit("legacy")
        path = self.ws / "tasks" / f"{missing}.txt"
        path.write_text(path.read_text().replace("priority: normal\n", ""))
        ids.append(missing)
        for i, tid in enumerate(ids):
            os.utime(self.ws / "tasks" / f"{tid}.txt", (1000000 + i, 1000000 + i))
        expected = [ids[i] for i in (2, 1, 3, 4, 0)]
        before = self.snapshot()
        default = self.run_command([sys.executable, str(DISPATCH), "sort-by-priority", str(self.ws / "tasks")])
        self.assertEqual(default.splitlines(), [f"{tid}.txt" for tid in expected])
        self.assertEqual(self.sort(self.ws / "tasks", self.env), default.splitlines())
        self.sweep("core", self.env, expected)
        self.assertEqual(before, self.snapshot())
        self.sweep("core-buffered", self.buffered_events_env(self.env), expected)
        self.assertEqual(before, self.snapshot())

    def test_missing_refused_and_timed_out_resolution_retains_records_and_retries(self):
        env = self.register()
        ids = {name: self.submit(name, priority) for name, priority in (
            ("missing-old", "normal"), ("missing-fresh", "normal"), ("refused", "normal"),
            ("timeout", "normal"), ("retry", "normal"), ("urgent", "urgent"), ("low", "low"))}
        for i, (name, tid) in enumerate(ids.items()):
            entry = self.assign(tid)
            os.utime(entry, (1000000 + i, 1000000 + i))
            if name.startswith("missing"):
                (self.ws / "tasks" / f"{tid}.txt").unlink()
        fresh = Path(env["SUTANDO_TASKS_DIR"]) / f"{ids['missing-fresh']}.txt"
        os.utime(fresh, None)
        calls = self.root / "resolver-calls.jsonl"
        resolver = self.root / "fault-resolver"
        resolver.write_text(
            "#!/usr/bin/env python3\nimport json, os, signal, sys, time\nfrom pathlib import Path\n"
            f"with open({str(calls)!r}, 'a') as log:\n"
            "    log.write(json.dumps({'entry': Path(sys.argv[1]).stem, 'pid': os.getpid()}) + '\\n')\n"
            f"if Path(sys.argv[1]).stem == {ids['refused']!r}:\n    sys.exit(1)\n"
            f"if Path(sys.argv[1]).stem == {ids['retry']!r}:\n"
            f"    rows = [json.loads(line) for line in Path({str(calls)!r}).read_text().splitlines()]\n"
            f"    if sum(row['entry'] == {ids['retry']!r} for row in rows) < 3:\n        sys.exit(1)\n"
            f"if Path(sys.argv[1]).stem == {ids['timeout']!r}:\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n    time.sleep(60)\n"
            f"os.execv({env['SUTANDO_INBOX_RESOLVER']!r}, [{env['SUTANDO_INBOX_RESOLVER']!r}, *sys.argv[1:]])\n")
        resolver.chmod(0o755)
        env.update(SUTANDO_INBOX_RESOLVER=str(resolver), SUTANDO_INBOX_RESOLVER_TIMEOUT="1",
                   SUTANDO_RESOLVE_RACE_WINDOW_S="60")
        before = self.snapshot()
        started = time.monotonic()
        log = self.sweep("failures", env, [ids["urgent"], ids["retry"], ids["low"]])
        self.assertLess(time.monotonic() - started, 35, log.read_text())
        self.assertEqual(before, self.snapshot(), "failed resolution must retain assignments")
        rows = [json.loads(line) for line in calls.read_text().splitlines()]
        dispatch_failures = {"missing-old": 1, "missing-fresh": 3, "refused": 3, "timeout": 3}
        for name, count in dispatch_failures.items():
            entry = Path(env["SUTANDO_TASKS_DIR"]) / f"{ids[name]}.txt"
            self.assertEqual(log.read_text().count(f"file for {entry} (rc="), count, name)
        # Real fswatch may deliver historical task events after the sweep;
        # the shell resolver contracts separately pin each decision's exact count.
        expected_counts = {"missing-old": (1, 2), "missing-fresh": (3, 4), "refused": (3, 4),
                           "timeout": (3, 4), "retry": (3, 3), "urgent": (1, 2), "low": (1, 2)}
        for name, (least, most) in expected_counts.items():
            count = sum(row["entry"] == ids[name] for row in rows)
            self.assertGreaterEqual(count, least, (name, rows))
            self.assertLessEqual(count, most, (name, rows))
        for row in rows:
            if row["entry"] == ids["timeout"]:
                with self.assertRaises(ProcessLookupError):
                    os.kill(row["pid"], 0)
        self.assertIn("not dispatching (1 attempt)", log.read_text())
        self.assertEqual(log.read_text().count("after 3 attempts; not dispatching"), 3)
        self.record("resolver-failures", calls=rows, expected_counts=expected_counts,
                    dispatch_failures=dispatch_failures,
                    elapsed=time.monotonic() - started, records_preserved=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
