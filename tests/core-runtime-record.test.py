"""Runtime observations follow successful adapters, never requested or failed launches."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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
        argv = ["core-runtime-record", str(self.ws), "claude", "sutando-core", "123",
                "--socket", str(self.ws / "t.sock")]
        confirmed = mock.patch.object(self.writer, "pane_runtimes", return_value={"claude"})
        confirmed.start()
        self.addCleanup(confirmed.stop)
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

    def test_configured_model_prefers_the_override_then_runtime_settings(self):
        env = {"CLAUDE_CONFIG_DIR": str(self.ws), "CODEX_HOME": str(self.ws),
               "SUTANDO_CORE_MODEL": ""}
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(self.writer.configured_model("claude"))
            (self.ws / "settings.json").write_text('{"model": " opus "}')
            self.assertEqual(self.writer.configured_model("claude"), "opus")
            (self.ws / "settings.json").write_text("[]")
            self.assertIsNone(self.writer.configured_model("claude"))
            (self.ws / "config.toml").write_text('model = "test-codex-model"\n')
            want = "test-codex-model" if sys.version_info >= (3, 11) else None
            self.assertEqual(self.writer.configured_model("codex"), want)
            os.environ["SUTANDO_CORE_MODEL"] = "override"
            self.assertEqual(self.writer.configured_model("claude"), "override")

    def test_cli_records_the_configured_model_with_the_confirmed_runtime(self):
        argv = ["core-runtime-record", str(self.ws), "codex", "sutando-core", "123",
                "--socket", str(self.ws / "t.sock")]
        path = self.ws / "state/core-runtime.json"
        for model, want in (("gpt-5-codex", "gpt-5-codex"), ("bad\nmodel", None),
                            ("x" * 201, None), ("", None)):
            with self.subTest(model=model), \
                    mock.patch.dict(os.environ, {"SUTANDO_CORE_MODEL": model,
                                                 "CODEX_HOME": str(self.ws / "none")}), \
                    mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(self.writer, "pane_runtimes", return_value={"codex"}):
                self.writer.main()
                row = json.loads(path.read_text())
                self.assertEqual((row["runtime"], row["model"], row["started_at"]),
                                 ("codex", want, 123))

    def test_cli_reports_a_pane_it_cannot_inspect(self):
        argv = ["core-runtime-record", str(self.ws), "claude", "sutando-core", "123",
                "--socket", str(self.ws / "t.sock")]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(self.writer, "pane_runtimes", side_effect=OSError("no tmux")), \
                mock.patch.object(sys, "stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit) as raised:
                self.writer.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("cannot inspect sutando-core: no tmux", err.getvalue())
        self.assertFalse((self.ws / "state/core-runtime.json").exists())

    def test_cli_refuses_a_record_the_panes_do_not_run(self):
        path = self.writer.publish(self.ws, "claude", "sutando-core", 1)
        before = path.read_bytes()
        argv = ["core-runtime-record", str(self.ws), "codex", "sutando-core", "123",
                "--socket", str(self.ws / "t.sock")]
        for running in (set(), {"claude"}, {"claude", "codex"}):
            with self.subTest(running=running), \
                    mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(self.writer, "pane_runtimes", return_value=running), \
                    mock.patch.object(sys, "stderr", new_callable=io.StringIO) as err:
                with self.assertRaises(SystemExit) as raised:
                    self.writer.main()
                self.assertEqual(raised.exception.code, 3)
                self.assertIn("record unchanged", err.getvalue())
            self.assertEqual(path.read_bytes(), before)

    def test_argv_classification(self):
        cases = {
            "claude --name sutando-core --plugin-dir /x": "claude",
            "/Users/a/Library/Application Support/x/claude.exe --resume u": "claude",
            "/bin/bash /tmp/a b/bin/claude --name sutando-core": "claude",
            "node /Users/a/.npm-global/bin/codex -C /repo": "codex",
            "/opt/homebrew/bin/codex -C /repo --search": "codex",
            "/tmp/p q/codex 300": "codex",
            "tmux -S /s new-session -d -s sutando-core claude --name sutando-core": None,
            "python3 /repo/skills/schedule-crons/scripts/codex-scheduler.py tick": None,
            "bash /repo/src/agent/codex/cli/task-notifier-supervisor.sh": None,
            "/bin/zsh": None,
        }
        for args, want in cases.items():
            with self.subTest(args=args):
                self.assertEqual(self.writer.argv_runtime(args), want)

    def test_pane_walk_skips_malformed_rows_and_pane_ids(self):
        ps = ("  PID  PPID ARGS\n10 1 /bin/zsh\n11 10\ngarbage\n"
              "12 11 claude --name sutando-core\n")
        def run(argv, **_):
            out = "%3\n10\n" if argv[0] == "tmux" else ps
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
        with mock.patch.object(self.writer.subprocess, "run", side_effect=run):
            self.assertEqual(self.writer.pane_runtimes("/s", "sutando-core"), {"claude"})

    def test_validation_and_failed_publish_preserve_prior_record(self):
        path = self.writer.publish(self.ws, "claude", 'a"b', 123)
        before = path.read_bytes()
        for args in (("other", "s", 123), ("codex", "s\n", 123),
                     ("codex", "s", float("nan")), ("codex", "s", -1),
                     ("codex", "s", 123, "m\x7f"), ("codex", "s", 123, 7)):
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
                    self.assertEqual(set(row), {"runtime", "model", "session", "started_at"})
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


TMUX = shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")


@unittest.skipUnless(TMUX, "tmux not found")
class PaneRuntimeTests(unittest.TestCase):
    """Real tmux on a private socket; the panes run processes named like each CLI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.sock = str(self.dir / "t.sock")
        self.addCleanup(subprocess.run, [TMUX, "-S", self.sock, "kill-server"],
                        capture_output=True)
        self.writer = load("core_record_panes", REPO / "src/core_runtime_record.py")
        for name in ("claude", "codex"):
            (self.dir / name).symlink_to(shutil.which("sleep"))

    def session(self, name, *command):
        subprocess.run([TMUX, "-S", self.sock, "new-session", "-d", "-s", name, *command],
                       check=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.writer.pane_runtimes(self.sock, name, tmux=TMUX):
                return
            time.sleep(0.05)

    def test_each_pane_reports_its_nearest_cli(self):
        self.session("core-claude", str(self.dir / "claude"), "300")
        subprocess.run([TMUX, "-S", self.sock, "new-window", "-t", "=core-claude", "sleep", "300"],
                       check=True)
        self.assertEqual(self.writer.pane_runtimes(self.sock, "core-claude", tmux=TMUX),
                         {"claude"})
        # A core's own CLI subprocess sits below the core and does not change its runtime.
        script = f'"{self.dir / "codex"}" 300 & exec "{self.dir / "claude"}" 300'
        self.session("core-nested", "/bin/sh", "-c", script)
        self.assertEqual(self.writer.pane_runtimes(self.sock, "core-nested", tmux=TMUX),
                         {"claude"})
        self.session("core-codex", str(self.dir / "codex"), "300")
        self.assertEqual(self.writer.pane_runtimes(self.sock, "core-codex", tmux=TMUX),
                         {"codex"})
        self.assertEqual(self.writer.pane_runtimes(self.sock, "absent", tmux=TMUX), set())


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
        case.fake_core_pane("codex")
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
  list-panes) echo "$FAKE_CORE_PANE_PID" ;;
  *) exit 0 ;;
esac
''')
        run = case.run_launcher()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(path.read_text())["started_at"], 123)

    def test_codex_launcher_over_a_claude_core_records_and_replaces_nothing(self):
        """The session is marked codex, but its pane runs Claude: the codex launcher
        must neither stamp codex over it nor log a start."""
        case, _ = self.codex()
        case._write_exe("tmux", '''#!/bin/bash
printf '%s\\n' "$*" >> "$TMUX_LOG"
[ "${1:-}" = -S ] && shift 2
case "${1:-}" in
  has-session) [ "${3:-}" = =sutando-core ] ;;
  show-environment) echo SUTANDO_CORE_RUNTIME=codex ;;
  display-message) echo 456 ;;
  list-panes) echo "$FAKE_CORE_PANE_PID" ;;
  *) exit 0 ;;
esac
''')
        case.fake_core_pane("claude")
        state = case.root / "workspace/state"
        path = state / "core-runtime.json"
        before = '{"runtime":"claude","session":"sutando-core","started_at":456}\n'
        path.write_text(before)
        run = case.run_launcher()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("sutando-core runs claude, not codex; record unchanged", run.stderr)
        self.assertEqual(path.read_text(), before)
        self.assertFalse((state / "session-starts.log").exists())
        calls = case._tmux_calls()
        self.assertNotIn("new-session -d -s sutando-core ", calls)
        self.assertNotIn("kill-session -t =sutando-core\n", calls + "\n")

    def test_failed_codex_create_logs_no_session_start(self):
        case, _ = self.codex()
        run = case.run_launcher(env_extra={"TMUX_SESSION_NEVER_UP": "1"})
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertFalse((case.root / "workspace/state/session-starts.log").exists())

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
