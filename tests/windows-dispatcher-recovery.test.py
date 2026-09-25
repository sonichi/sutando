"""Run the production dispatcher with isolated, deterministic POSIX CLI shims."""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")


@unittest.skipUnless(
    PWSH and os.name == "posix",
    "Requires pwsh and POSIX CLI shims; Windows uses the live PowerShell suite",
)
class DispatcherRecoveryTest(unittest.TestCase):
    def test_crash_recovery_and_instance_ownership(self):
        with tempfile.TemporaryDirectory(prefix="dispatcher recovery ") as temp:
            workspace = pathlib.Path(temp)
            shim = workspace / "bin"
            for directory in (shim, workspace / "state", workspace / "tasks", workspace / "results"):
                directory.mkdir()
            (shim / "python").symlink_to(sys.executable)
            claude = shim / "claude"
            claude.write_text('#!/bin/sh\nprintf \'{"result":"PORTABLE_OWNER_OK"}\\n\'\n')
            claude.chmod(0o755)
            env = dict(
                os.environ,
                PATH=str(shim) + ":/usr/bin:/bin",
                SUTANDO_TEST_MODE="1",
                SUTANDO_WORKSPACE=temp,
            )
            pidfile = workspace / "state/task-dispatcher.pid"
            pidfile.write_text(str(os.getpid()))
            (workspace / "tasks/task-orphan.txt.processing").write_text("task: Do not retry\n")
            (workspace / "tasks/task-done.txt.processing").write_text("task: Already complete\n")
            (workspace / "results/task-done.txt").write_text("PREVIOUS_RESULT")
            command = [PWSH, "-NoProfile", "-File", str(REPO / "src/task-dispatcher.ps1")]
            with (workspace / "service.log").open("w+") as log:
                process = subprocess.Popen(command, env=env, stdout=log, stderr=log)

                def wait(path):
                    for _ in range(100):
                        if path.exists():
                            return
                        if process.poll() is not None:
                            log.seek(0)
                            self.fail(log.read())
                        time.sleep(0.1)
                    log.seek(0)
                    self.fail(f"Timeout: {path}\n{log.read()}")

                try:
                    wait(workspace / "tasks/archive/task-orphan.txt")
                    self.assertEqual(int(pidfile.read_text()), process.pid)
                    self.assertTrue((workspace / "results/task-orphan.txt").read_text().startswith(
                        "This task was interrupted."
                    ))
                    self.assertEqual((workspace / "results/task-done.txt").read_text(), "PREVIOUS_RESULT")
                    duplicate = subprocess.run(
                        command + ["-ValidateOnly"], env=env, capture_output=True,
                        text=True, timeout=20,
                    )
                    self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
                    self.assertIn("already running", duplicate.stdout)
                    self.assertEqual(int(pidfile.read_text()), process.pid)
                    (workspace / "tasks/task-new.txt").write_text("access_tier: owner\ntask: Return marker\n")
                    wait(workspace / "tasks/archive/task-new.txt")
                    self.assertEqual((workspace / "results/task-new.txt").read_text(), "PORTABLE_OWNER_OK")
                    process.kill()
                    process.wait(timeout=10)
                    (workspace / "tasks/task-crash.txt.processing").write_text("task: Interrupted\n")
                    process = subprocess.Popen(command, env=env, stdout=log, stderr=log)
                    wait(workspace / "tasks/archive/task-crash.txt")
                    (workspace / "tasks/task-after.txt").write_text("access_tier: owner\ntask: Return marker\n")
                    wait(workspace / "tasks/archive/task-after.txt")
                    self.assertEqual((workspace / "results/task-after.txt").read_text(), "PORTABLE_OWNER_OK")
                finally:
                    process.kill()
                    process.wait(timeout=10)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory modes")
    def test_archive_failure_keeps_dispatcher_serving_without_replay(self):
        with tempfile.TemporaryDirectory(prefix="dispatcher archive ") as temp:
            workspace = pathlib.Path(temp)
            shim = workspace / "bin"
            archive = workspace / "tasks/archive"
            for directory in (shim, workspace / "state", archive, workspace / "results"):
                directory.mkdir(parents=True)
            (shim / "python").symlink_to(sys.executable)
            calls = workspace / "claude-calls"
            claude = shim / "claude"
            claude.write_text(
                f'#!/bin/sh\necho call >> "{calls}"\nprintf \'{{"result":"PORTABLE_OWNER_OK"}}\\n\'\n'
            )
            claude.chmod(0o755)
            env = dict(
                os.environ,
                PATH=str(shim) + ":/usr/bin:/bin",
                SUTANDO_TEST_MODE="1",
                SUTANDO_WORKSPACE=temp,
            )
            command = [PWSH, "-NoProfile", "-File", str(REPO / "src/task-dispatcher.ps1")]
            log_path = workspace / "service.log"
            archive.chmod(0o555)
            process = None
            try:
                with log_path.open("w+") as log:
                    def start():
                        return subprocess.Popen(command, env=env, stdout=log, stderr=log)

                    def wait_for(predicate, what):
                        for _ in range(300):
                            if predicate():
                                return
                            if process.poll() is not None:
                                self.fail(f"dispatcher exited {process.returncode}\n{log_path.read_text()}")
                            time.sleep(0.1)
                        self.fail(f"Timeout: {what}\n{log_path.read_text()}")

                    def archive_failures(task_id):
                        return log_path.read_text().count(f"{task_id}: archive failed")

                    process = start()
                    (workspace / "tasks/task-stuck.txt").write_text("access_tier: owner\ntask: Return marker\n")
                    wait_for(lambda: archive_failures("task-stuck") == 1, "first archive failure")
                    self.assertEqual((workspace / "results/task-stuck.txt").read_text(), "PORTABLE_OWNER_OK")
                    self.assertTrue((workspace / "tasks/task-stuck.txt.processing").exists())

                    (workspace / "tasks/task-next.txt").write_text("access_tier: owner\ntask: Return marker\n")
                    wait_for(lambda: archive_failures("task-next") == 1, "dispatcher keeps serving")
                    self.assertEqual((workspace / "results/task-next.txt").read_text(), "PORTABLE_OWNER_OK")
                    self.assertIsNone(process.poll())

                    process.kill()
                    process.wait(timeout=10)
                    process = start()
                    wait_for(lambda: archive_failures("task-next") == 2, "startup sweep survives stuck claims")
                    time.sleep(1)
                    self.assertIsNone(process.poll(), log_path.read_text())

                    archive.chmod(0o755)
                    (workspace / "tasks/task-after.txt").write_text("access_tier: owner\ntask: Return marker\n")
                    wait_for(lambda: (archive / "task-after.txt").exists(), "task after restart")
                    process.kill()
                    process.wait(timeout=10)
                    process = start()
                    wait_for(lambda: (archive / "task-stuck.txt").exists() and (archive / "task-next.txt").exists(),
                             "stuck claims archived once the fault clears")

                    self.assertEqual(len(calls.read_text().splitlines()), 3)
                    for task_id in ("task-stuck", "task-next", "task-after"):
                        self.assertEqual((workspace / f"results/{task_id}.txt").read_text(), "PORTABLE_OWNER_OK")
            finally:
                archive.chmod(0o755)
                if process is not None:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
