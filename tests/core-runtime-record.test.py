"""Runtime observations follow successful adapters, never requested or failed launches."""
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

REPO = Path(os.environ.get("SUTANDO_TEST_REPO", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO / "src"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.writer = load("core_record", REPO / "src/core_runtime_record.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)

    def test_cli_reports_publish_failure_without_replacing_last_record(self):
        argv = ["core-runtime-record", str(self.ws), "claude", "sutando-core", "123"]
        with mock.patch.object(sys, "argv", argv):
            self.writer.main()
        path = self.ws / "state/core-runtime.json"
        before = path.read_bytes()
        self.assertEqual(json.loads(before)["runtime"], "claude")
        for failure in (OSError("refused"), ValueError("invalid session start time")):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(self.writer, "publish", side_effect=failure), \
                    mock.patch.object(sys, "stderr", new_callable=io.StringIO) as err:
                with self.assertRaises(SystemExit) as raised:
                    self.writer.main()
                self.assertEqual(raised.exception.code, 1)
                self.assertIn(f"core runtime record: {failure}", err.getvalue())
            self.assertEqual(path.read_bytes(), before)

    def test_validation_and_failed_publish_preserve_prior_record(self):
        path = self.writer.publish(self.ws, "claude", 'a"b', 123)
        before = path.read_bytes()
        for args in (("other", "s", 123), ("codex", "s\n", 123),
                     ("codex", "s", float("nan")), ("codex", "s", -1)):
            with self.assertRaises(ValueError):
                self.writer.publish(self.ws, *args)
            self.assertEqual(path.read_bytes(), before)
        with mock.patch("result_publish.os.replace", side_effect=OSError("refused")):
            with self.assertRaises(OSError):
                self.writer.publish(self.ws, "codex", "s", 456)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_concurrent_production_writes_are_always_whole(self):
        path = self.writer.publish(self.ws, "claude", "s", 1)
        done = threading.Event()
        errors = []
        observed = []

        def read():
            while not done.is_set():
                try:
                    row = json.loads(path.read_text())
                    self.assertEqual(set(row), {"runtime", "session", "started_at"})
                    self.assertIn(row["runtime"], ("claude", "codex"))
                    observed.append(row)
                except Exception as exc:
                    errors.append(exc)

        reader = threading.Thread(target=read)
        reader.start()
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda n: self.writer.publish(
                    self.ws, "codex" if n % 2 else "claude", "s", n + 1), range(80)))
        finally:
            done.set()
            reader.join()
        self.assertTrue(observed)
        self.assertEqual(errors, [])


class LauncherRegressionTests(unittest.TestCase):
    def codex(self):
        module = load("codex_launcher_fixture", REPO / "tests/codex-core-launcher.test.py")
        case = module.CodexCoreLauncherTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        return case, module

    def test_failed_codex_launch_preserves_last_confirmed_claude(self):
        case, _ = self.codex()
        path = case.root / "workspace/state/core-runtime.json"
        before = '{"runtime":"claude","session":"sutando-core","started_at":123}\n'
        path.write_text(before)
        run = case.run_launcher(env_extra={"TMUX_SESSION_NEVER_UP": "1"})
        self.assertEqual(path.read_text(), before, run.stdout + run.stderr)
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn("Started sutando-core detached", run.stdout)

    def test_confirmed_codex_replaces_claude_and_reuse_keeps_session_start(self):
        case, module = self.codex()
        case._write_exe("tmux", module.SESSION_UP_AFTER_CREATE_TMUX)
        run = case.run_launcher()
        self.assertEqual(run.returncode, 0, run.stderr)
        path = case.root / "workspace/state/core-runtime.json"
        self.assertEqual(json.loads(path.read_text())["runtime"], "codex")
        case._write_exe("tmux", '''#!/bin/bash
[ "${1:-}" = -S ] && shift 2
case "${1:-}" in
  has-session) [ "${3:-}" = =sutando-core ] ;;
  show-environment) echo SUTANDO_CORE_RUNTIME=codex ;;
  display-message) echo 123 ;;
  *) exit 0 ;;
esac
''')
        run = case.run_launcher()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(path.read_text())["started_at"], 123)

    def test_env_override_is_visible_and_explicit_runtime_wins(self):
        case, module = self.codex()
        case._write_exe("tmux", module.SESSION_UP_AFTER_CREATE_TMUX)
        config = case.root / "sutando.config.json"
        row = json.loads(config.read_text())
        row["core"]["runtime"] = "claude"
        config.write_text(json.dumps(row))
        run = case.run_launcher()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("SUTANDO_CORE_RUNTIME=codex overrides core.runtime", run.stderr)
        run = case.run_launcher("--runtime", "codex", env_extra={"SUTANDO_CORE_RUNTIME": "claude"})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("overrides core.runtime", run.stderr)

    def test_confirmed_claude_replaces_codex_and_reuse_keeps_session_start(self):
        module = load("claude_launcher_fixture", REPO / "tests/start-cli-claude-task-notifier.test.py")
        harness = module.Harness()
        self.addCleanup(harness.close)
        path = harness.td / "ws/state/core-runtime.json"
        path.write_text('{"runtime":"codex","session":"sutando-core","started_at":1}')
        run = harness.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        row = json.loads(path.read_text())
        self.assertEqual(row["runtime"], "claude")
        self.assertEqual(row["started_at"], int(harness.tm(
            "display-message", "-p", "-t", "=sutando-core:", "#{session_created}"
        ).stdout.strip()))
        run = harness.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(json.loads(path.read_text()), row)


    def test_failed_claude_launch_preserves_last_confirmed_codex(self):
        module = load("claude_failure_fixture", REPO / "tests/start-cli-claude-task-notifier.test.py")
        harness = module.Harness(claude_body="exit 1\n")
        self.addCleanup(harness.close)
        path = harness.td / "ws/state/core-runtime.json"
        before = '{"runtime":"codex","session":"sutando-core","started_at":1}'
        path.write_text(before)
        run = harness.launch()
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(path.read_text(), before)


if __name__ == "__main__":
    unittest.main()
