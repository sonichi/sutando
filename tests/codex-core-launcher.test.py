#!/usr/bin/env python3
"""Hermetic integration tests for the persistent Codex core launcher."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
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
            "src/agent/start-cli.sh",
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
  exit 1
fi
if [ "${1:-}" = show-environment ]; then
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
        self.assertIn("task-notifier.sh", calls)
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

    def test_auth_failure_stops_before_tmux_launch(self):
        self._write_exe("codex", '#!/bin/bash\nexit 1\n')
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not authenticated", result.stderr)
        calls = self.log.read_text() if self.log.exists() else ""
        self.assertNotIn("new-session", calls)

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


if __name__ == "__main__":
    unittest.main()
