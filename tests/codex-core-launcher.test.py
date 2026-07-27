#!/usr/bin/env python3
"""Hermetic integration tests for the persistent Codex core launcher."""
import json
import os
import shutil
import select
import subprocess
import tempfile
import time
import unittest
import pty
from pathlib import Path

REAL_REPO = Path(__file__).resolve().parents[1]


class CodexCoreLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.bin = Path(self.tmp.name) / "bin"
        self.log = Path(self.tmp.name) / "tmux.log"
        for rel in (
            "src/agent/codex/cli/start-cli.sh",
            "src/agent/codex/cli/task-notifier.sh",
            "src/agent/codex/cli/task-notifier-supervisor.sh",
            "src/agent/start-cli.sh",
            "src/watch-tasks-stream.sh",
            "src/sutando_config.py",
            "scripts/sutando-config.sh",
        ):
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REAL_REPO / rel, target)
        monitor = self.root / "src/core-input-watch.py"
        monitor.write_text(
            "import os, sys\n"
            "with open(os.environ['MONITOR_LOG'], 'w') as f:\n"
            "    f.write(' '.join(sys.argv[1:]))\n"
        )
        (self.root / "src" / "__init__.py").touch()
        scheduler = self.root / "fake-codex-scheduler.py"
        scheduler.write_text(
            "import os, pathlib, sys\n"
            "pathlib.Path(os.environ['SCHEDULER_LOG']).write_text(' '.join(sys.argv[1:]))\n"
        )
        workspace = self.root / "workspace"
        (workspace / "state").mkdir(parents=True)
        (self.root / "sutando.config.json").write_text(json.dumps({
            "core": {"runtime": "codex"},
            "workspace": {"path": str(workspace)},
            "core_config_dirs": [{
                "id": "codex-test", "type": "codex", "env_name": "CODEX_HOME",
                "synced": False, "value": str(self.root / "codex-home"),
            }],
        }))
        self.bin.mkdir()
        self._write_exe("codex", '#!/bin/bash\n[ "${1:-}" = login ] && exit 0\nexit 0\n')
        self._write_exe("fswatch", '#!/bin/bash\nexit 0\n')
        self._write_exe("tmux", '''#!/bin/bash
printf '%s\\n' "$*" >> "$TMUX_LOG"
[ "${1:-}" = -S ] && shift 2
if [ "${1:-}" = has-session ]; then
  if [ -n "${TMUX_ACTIVE_RUNTIME:-}" ] && [ ! -f "$TMUX_STATE" ] && [ "${3:-}" = =sutando-core ]; then exit 0; fi
  if [ "${TMUX_WATCHER_EXISTS:-}" = 1 ] && [ "${3:-}" = =sutando-core-watcher ]; then exit 0; fi
  exit 1
fi
if [ "${1:-}" = show-environment ]; then
  if [ "${3:-}" = =sutando-core-watcher ] && [ "${4:-}" = SUTANDO_NOTIFIER_VERSION ]; then
    printf 'SUTANDO_NOTIFIER_VERSION=%s\\n' "$TMUX_ACTIVE_NOTIFIER_VERSION"
    exit 0
  fi
  printf 'SUTANDO_CORE_RUNTIME=%s\\n' "$TMUX_ACTIVE_RUNTIME"
  exit 0
fi
if [ "${1:-}" = kill-session ] && [ "${3:-}" = =sutando-core ]; then
  touch "$TMUX_STATE"
fi
exit 0
''')

    def tearDown(self):
        self.tmp.cleanup()

    def _write_exe(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def _notifier_version(self):
        first = subprocess.check_output([
            "cksum",
            str(self.root / "src/agent/codex/cli/task-notifier-supervisor.sh"),
            str(self.root / "src/agent/codex/cli/task-notifier.sh"),
            str(self.root / "src/watch-tasks-stream.sh"),
        ])
        checksum = subprocess.run(["cksum"], input=first, capture_output=True,
                                  check=True, text=False).stdout.decode().split()
        return f"{checksum[0]}-{checksum[1]}"

    def run_launcher(self, *args, env_extra=None):
        env = dict(os.environ)
        env.update({
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "TMUX_LOG": str(self.log),
            "TMUX_STATE": str(Path(self.tmp.name) / "tmux-killed"),
            "HOME": str(Path(self.tmp.name) / "home"),
            "SUTANDO_CORE_RUNTIME": "codex",
            "MONITOR_LOG": str(Path(self.tmp.name) / "monitor.log"),
            "SCHEDULER_LOG": str(Path(self.tmp.name) / "scheduler.log"),
            "SUTANDO_CODEX_SCHEDULER_SCRIPT": str(self.root / "fake-codex-scheduler.py"),
            "SUTANDO_HOST_LABEL": "test-host",
        })
        env.update(env_extra or {})
        return subprocess.run(
            ["/bin/bash", str(self.root / "src/agent/start-cli.sh"), *args],
            cwd=self.root, env=env, capture_output=True, text=True,
        )

    def run_launcher_with_tty(self, *args, env_extra=None):
        env = dict(os.environ)
        env.update({
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "TMUX_LOG": str(self.log),
            "TMUX_STATE": str(Path(self.tmp.name) / "tmux-killed"),
            "HOME": str(Path(self.tmp.name) / "home"),
            "SUTANDO_CORE_RUNTIME": "codex",
            "MONITOR_LOG": str(Path(self.tmp.name) / "monitor.log"),
        })
        env.update(env_extra or {})
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(
                ["/bin/bash", str(self.root / "src/agent/start-cli.sh"), *args],
                cwd=self.root, env=env, stdin=slave, stdout=slave, stderr=slave,
            )
            os.close(slave)
            slave = -1
            os.set_blocking(master, False)
            output = b""
            deadline = time.monotonic() + 5
            while True:
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait()
                    raise subprocess.TimeoutExpired(process.args, 5)
                readable, _, _ = select.select([master], [], [], 0.05)
                if not readable:
                    if process.poll() is not None:
                        break
                    continue
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if chunk:
                    output += chunk
                elif process.poll() is not None:
                    break
            returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
            return subprocess.CompletedProcess(process.args, returncode, output.decode(errors="replace"), "")
        finally:
            os.close(master)
            if slave >= 0:
                os.close(slave)

    def test_launches_codex_and_managed_task_notifier(self):
        result = self.run_launcher(env_extra={"SUTANDO_CORE_MODEL": "gpt-test"})
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertIn("new-session -d -s sutando-core", calls)
        self.assertIn("codex -C", calls)
        self.assertIn("--sandbox danger-full-access", calls)
        self.assertIn("--ask-for-approval never", calls)
        self.assertIn("--search", calls)
        self.assertIn("-m gpt-test", calls)
        self.assertIn("new-session -d -s sutando-core-watcher", calls)
        self.assertIn("task-notifier-supervisor.sh", calls)
        self.assertIn("SUTANDO_NOTIFIER_VERSION=", calls)
        self.assertIn("CODEX_HOME=", calls)
        self.assertIn("has-session -t =sutando-core", calls)
        self.assertIn("has-session -t =sutando-core-watcher", calls)

        monitor_log = Path(self.tmp.name) / "monitor.log"
        for _ in range(50):
            if monitor_log.exists():
                break
            time.sleep(0.01)
        self.assertTrue(monitor_log.exists(), "managed core-input monitor did not start")
        self.assertIn("--session sutando-core", monitor_log.read_text())

        scheduler_log = Path(self.tmp.name) / "scheduler.log"
        self.assertTrue(scheduler_log.exists(), "Codex scheduler was not reconciled")
        invocation = scheduler_log.read_text()
        self.assertIn("install --workspace", invocation)
        self.assertIn("--host-label test-host", invocation)

    def test_restart_kills_core_and_notifier_before_launch(self):
        result = self.run_launcher("--restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertLess(calls.index("kill-session -t =sutando-core-watcher"),
                        calls.index("new-session -d -s sutando-core"))

    def test_dispatcher_restarts_when_active_runtime_differs(self):
        result = self.run_launcher(env_extra={"TMUX_ACTIVE_RUNTIME": "claude"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Core runtime changed (claude → codex)", result.stdout)
        calls = self.log.read_text()
        self.assertLess(calls.index("kill-session -t =sutando-core\n"),
                        calls.index("new-session -d -s sutando-core"))

    def test_unmarked_existing_session_is_replaced(self):
        result = self.run_launcher(env_extra={"TMUX_ACTIVE_RUNTIME": "unknown"})
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertIn("kill-session -t =sutando-core", calls)
        self.assertIn("new-session -d -s sutando-core", calls)
        self.assertLess(calls.index("kill-session -t =sutando-core-watcher"),
                        calls.index("new-session -d -s sutando-core"))

    def test_stale_notifier_version_is_replaced_without_restarting_core(self):
        result = self.run_launcher(env_extra={
            "TMUX_ACTIVE_RUNTIME": "codex",
            "TMUX_WATCHER_EXISTS": "1",
            "TMUX_ACTIVE_NOTIFIER_VERSION": "stale",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertIn("kill-session -t =sutando-core-watcher", calls)
        self.assertIn("new-session -d -s sutando-core-watcher", calls)
        self.assertLess(calls.index("kill-session -t =sutando-core-watcher"),
                        calls.index("new-session -d -s sutando-core-watcher"))
        self.assertNotIn("kill-session -t =sutando-core\n", calls)

    def test_current_notifier_version_is_left_running(self):
        result = self.run_launcher(env_extra={
            "TMUX_ACTIVE_RUNTIME": "codex",
            "TMUX_WATCHER_EXISTS": "1",
            "TMUX_ACTIVE_NOTIFIER_VERSION": self._notifier_version(),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertNotIn("kill-session -t =sutando-core-watcher", calls)
        self.assertNotIn("new-session -d -s sutando-core-watcher", calls)

    def test_nested_tmux_invocation_never_attaches(self):
        result = self.run_launcher_with_tty(env_extra={
            "TMUX": "/tmp/outer.sock,1,0",
            "TMUX_ACTIVE_RUNTIME": "codex",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertNotIn(" attach ", f" {calls} ")
        self.assertIn("sutando-core already running (codex)", result.stdout)

    def test_auth_failure_stops_before_tmux_launch(self):
        self._write_exe("codex", '#!/bin/bash\nexit 1\n')
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not authenticated", result.stderr)
        calls = self.log.read_text() if self.log.exists() else ""
        self.assertNotIn("new-session", calls)

    def test_notifier_supervisor_restarts_after_child_exit(self):
        count = Path(self.tmp.name) / "notifier-count"
        self._write_exe("tmux", '''#!/bin/bash
[ "${1:-}" = -S ] && shift 2
[ "${1:-}" = has-session ] && exit 0
exit 1
''')
        notifier = self.bin / "notifier-under-test"
        notifier.write_text('''#!/bin/bash
n=0
[ -f "$SUPERVISOR_COUNT" ] && n=$(cat "$SUPERVISOR_COUNT")
printf '%s' "$((n + 1))" > "$SUPERVISOR_COUNT"
exit 23
''')
        notifier.chmod(0o755)
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin",
                   SUTANDO_TMUX_SOCKET="/tmp/test.sock",
                   SUTANDO_TMUX_SESSION="sutando-core",
                   SUTANDO_NOTIFIER_SCRIPT=str(notifier),
                   SUTANDO_NOTIFIER_RESTART_DELAY="0.01",
                   SUPERVISOR_COUNT=str(count))
        supervisor = self.root / "src/agent/codex/cli/task-notifier-supervisor.sh"
        process = subprocess.Popen(["/bin/bash", str(supervisor)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(100):
                if count.exists() and int(count.read_text()) >= 2:
                    break
                time.sleep(0.01)
            self.assertTrue(count.exists(), "supervisor never started notifier")
            self.assertGreaterEqual(int(count.read_text()), 2)
            self.assertIsNone(process.poll(), "supervisor exited with its failed child")
        finally:
            process.terminate()
            process.communicate(timeout=2)

    def test_notifier_supervisor_survives_child_process_group_cleanup(self):
        count = Path(self.tmp.name) / "notifier-count"
        self._write_exe("tmux", '''#!/bin/bash
[ "${1:-}" = -S ] && shift 2
[ "${1:-}" = has-session ] && exit 0
exit 1
''')
        notifier = self.bin / "notifier-under-test"
        notifier.write_text('''#!/bin/bash
n=0
[ -f "$SUPERVISOR_COUNT" ] && n=$(cat "$SUPERVISOR_COUNT")
n=$((n + 1))
printf '%s' "$n" > "$SUPERVISOR_COUNT"
if [ "$n" = 1 ]; then
  kill -TERM 0
fi
sleep 60
''')
        notifier.chmod(0o755)
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin",
                   SUTANDO_TMUX_SOCKET="/tmp/test.sock",
                   SUTANDO_TMUX_SESSION="sutando-core",
                   SUTANDO_NOTIFIER_SCRIPT=str(notifier),
                   SUTANDO_NOTIFIER_RESTART_DELAY="0.01",
                   SUPERVISOR_COUNT=str(count))
        supervisor = self.root / "src/agent/codex/cli/task-notifier-supervisor.sh"
        process = subprocess.Popen(["/bin/bash", str(supervisor)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(200):
                if count.exists() and int(count.read_text()) >= 2:
                    break
                time.sleep(0.01)
            self.assertTrue(count.exists(), "supervisor never started notifier")
            self.assertGreaterEqual(int(count.read_text()), 2)
            self.assertIsNone(process.poll(), "child kill 0 terminated supervisor")
        finally:
            process.terminate()
            process.communicate(timeout=2)

    def test_notifier_supervisor_exits_when_core_is_gone(self):
        self._write_exe("tmux", '#!/bin/bash\nexit 1\n')
        count = Path(self.tmp.name) / "notifier-count"
        notifier = self.bin / "notifier-under-test"
        notifier.write_text(f'#!/bin/bash\ntouch "{count}"\n')
        notifier.chmod(0o755)
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin",
                   SUTANDO_TMUX_SOCKET="/tmp/test.sock",
                   SUTANDO_TMUX_SESSION="sutando-core",
                   SUTANDO_NOTIFIER_SCRIPT=str(notifier))
        supervisor = self.root / "src/agent/codex/cli/task-notifier-supervisor.sh"
        result = subprocess.run(["/bin/bash", str(supervisor)], env=env,
                                capture_output=True, text=True, timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(count.exists(), "notifier started without a live core")

    def test_notifier_submits_literal_safe_prompt(self):
        # The one-event mode tests the adapter without starting fswatch.
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", TMUX_LOG=str(self.log),
                   SUTANDO_TMUX_SOCKET="/tmp/test.sock", SUTANDO_TMUX_SESSION="sutando-core")
        # This stub reports the core session alive for notifier calls.
        self._write_exe("tmux", '''#!/bin/bash
printf '%s\\n' "$*" >> "$TMUX_LOG"
[ "$3" = has-session ] && exit 0
exit 0
''')
        script = self.root / "src/agent/codex/cli/task-notifier.sh"
        result = subprocess.run(["/bin/bash", str(script), "--event", "task-123.txt"],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertIn("send-keys -t sutando-core:0 -l -- Sutando task ready: task-123.txt", calls)
        self.assertIn("/tasks/task-123.txt", calls)
        self.assertIn("send-keys -t sutando-core:0 C-m", calls)

    def test_notifier_does_not_replay_completed_task(self):
        workspace = self.root / "workspace"
        (workspace / "tasks").mkdir(exist_ok=True)
        (workspace / "results").mkdir(exist_ok=True)
        (workspace / "tasks" / "task-done.txt").write_text("task: done\n")
        (workspace / "results" / "task-done.txt").write_text("already complete\n")
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", TMUX_LOG=str(self.log),
                   SUTANDO_TMUX_SOCKET="/tmp/test.sock", SUTANDO_TMUX_SESSION="sutando-core")
        script = self.root / "src/agent/codex/cli/task-notifier.sh"
        result = subprocess.run(["/bin/bash", str(script), "--event", "task-done.txt"],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text() if self.log.exists() else ""
        self.assertNotIn("send-keys", calls)

    def test_notifier_does_not_replay_task_with_archived_result(self):
        workspace = self.root / "workspace"
        (workspace / "tasks").mkdir(exist_ok=True)
        archive = workspace / "results" / "archive" / "2026-07"
        archive.mkdir(parents=True)
        (workspace / "tasks" / "task-done.txt").write_text("task: done\n")
        (archive / "task-done.txt").write_text("already delivered\n")
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", TMUX_LOG=str(self.log),
                   SUTANDO_TMUX_SOCKET="/tmp/test.sock", SUTANDO_TMUX_SESSION="sutando-core")
        script = self.root / "src/agent/codex/cli/task-notifier.sh"
        result = subprocess.run(["/bin/bash", str(script), "--event", "task-done.txt"],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text() if self.log.exists() else ""
        self.assertNotIn("send-keys", calls)

    def test_notifier_does_not_replay_task_with_gateway_archived_result(self):
        workspace = self.root / "workspace"
        (workspace / "tasks").mkdir(exist_ok=True)
        archive = workspace / "results" / "archive"
        archive.mkdir(parents=True)
        (workspace / "tasks" / "task-done.txt").write_text("task: done\n")
        (archive / "task-done-1784690000.txt").write_text("already delivered\n")
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", TMUX_LOG=str(self.log),
                   SUTANDO_TMUX_SOCKET="/tmp/test.sock", SUTANDO_TMUX_SESSION="sutando-core")
        script = self.root / "src/agent/codex/cli/task-notifier.sh"
        result = subprocess.run(["/bin/bash", str(script), "--event", "task-done.txt"],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text() if self.log.exists() else ""
        self.assertNotIn("send-keys", calls)

    def test_notifier_does_not_replay_task_with_retention_archived_result(self):
        workspace = self.root / "workspace"
        (workspace / "tasks").mkdir(exist_ok=True)
        archive = workspace / "results" / "archive-2026-07-26"
        archive.mkdir(parents=True)
        (workspace / "tasks" / "task-done.txt").write_text("task: done\n")
        (archive / "task-done.txt").write_text("already delivered\n")
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", TMUX_LOG=str(self.log),
                   SUTANDO_TMUX_SOCKET="/tmp/test.sock", SUTANDO_TMUX_SESSION="sutando-core")
        script = self.root / "src/agent/codex/cli/task-notifier.sh"
        result = subprocess.run(["/bin/bash", str(script), "--event", "task-done.txt"],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text() if self.log.exists() else ""
        self.assertNotIn("send-keys", calls)

    def test_notifier_recognizes_gateway_result_in_retention_archive(self):
        workspace = self.root / "workspace"
        (workspace / "tasks").mkdir(exist_ok=True)
        archive = workspace / "results" / "archive-2026-07-26"
        archive.mkdir(parents=True)
        (workspace / "tasks" / "task-done.txt").write_text("task: done\n")
        (archive / "task-done-1784690000.txt").write_text("already delivered\n")
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", TMUX_LOG=str(self.log),
                   SUTANDO_TMUX_SOCKET="/tmp/test.sock", SUTANDO_TMUX_SESSION="sutando-core")
        script = self.root / "src/agent/codex/cli/task-notifier.sh"
        result = subprocess.run(["/bin/bash", str(script), "--event", "task-done.txt"],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text() if self.log.exists() else ""
        self.assertNotIn("send-keys", calls)

    def test_managed_notifier_waits_for_each_result_before_next_task(self):
        workspace = self.root / "workspace"
        tasks = workspace / "tasks"
        results = workspace / "results"
        tasks.mkdir(exist_ok=True)
        results.mkdir(exist_ok=True)
        for name in ("task-one.txt", "task-two.txt"):
            (tasks / name).write_text(f"task: {name}\n")
        watcher = self.root / "src/watch-tasks-stream.sh"
        watcher.write_text("#!/bin/bash\nprintf 'TASK_FILE: task-one.txt\\nTASK_FILE: task-two.txt\\n'\n")
        watcher.chmod(0o755)
        count = Path(self.tmp.name) / "submit-count"
        self._write_exe("tmux", '''#!/bin/bash
printf '%s\\n' "$*" >> "$TMUX_LOG"
[ "${1:-}" = -S ] && shift 2
if [ "${1:-}" = has-session ]; then exit 0; fi
if [ "${1:-}" = send-keys ] && [ "${*: -1}" = C-m ]; then
  n=0; [ -f "$SUBMIT_COUNT" ] && n=$(cat "$SUBMIT_COUNT")
  n=$((n + 1)); printf '%s' "$n" > "$SUBMIT_COUNT"
  if [ "$n" = 1 ]; then name=task-one.txt; else name=task-two.txt; fi
  (sleep 0.12; touch "$SUTANDO_RESULTS_DIR/$name") >/dev/null 2>&1 &
fi
exit 0
''')
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", TMUX_LOG=str(self.log),
                   SUBMIT_COUNT=str(count), SUTANDO_TMUX_SOCKET="/tmp/test.sock",
                   SUTANDO_TMUX_SESSION="sutando-core", SUTANDO_TASKS_DIR=str(tasks),
                   SUTANDO_RESULTS_DIR=str(results), SUTANDO_NOTIFIER_POLL_INTERVAL="0.02",
                   SUTANDO_NOTIFIER_COMPLETION_TIMEOUT="5")
        script = self.root / "src/agent/codex/cli/task-notifier.sh"
        started = time.monotonic()
        result = subprocess.run(["/bin/bash", str(script)], env=env,
                                capture_output=True, text=True, timeout=5)
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(elapsed, 0.20)
        calls = self.log.read_text()
        self.assertLess(calls.index("task-one.txt"), calls.index("task-two.txt"))
        self.assertTrue((results / "task-one.txt").exists())
        self.assertTrue((results / "task-two.txt").exists())


if __name__ == "__main__":
    unittest.main()
