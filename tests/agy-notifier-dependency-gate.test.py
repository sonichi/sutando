#!/usr/bin/env python3
"""Blocker #4 (sonichi#4303 review): start-cli.sh's ensure_task_notifier
checked only that `agy` and `tmux` exist, never that `fswatch` — a hard
dependency of task-notifier.sh's main loop (via watch-tasks-stream.sh) — is
on PATH, and never verified the watcher tmux session it just started was
still alive a moment later.

Without fswatch, watch-tasks-stream.sh's `fswatch | while read` pipe never
gets a producer; the notifier's own event-read loop then hits EOF and exits;
since task-notifier.sh IS the watcher pane's foreground command, tmux tears
the whole session down when it exits — all within about a second, silently,
while start-cli.sh still exits 0.

Uses REAL tmux (skipped where unavailable) because the failure IS tmux's own
session-dies-when-its-command-exits semantics; a stubbed tmux (as in
agy-task-notifier.test.py's StartCliNotifierWiringTest, which only proves the
watcher session gets STARTED, not that it stays alive) cannot reproduce it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "src/agent/agy/cli/start-cli.sh"
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from clean_watcher_env import clean_env  # noqa: E402


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


@unittest.skipUnless(_tmux_available(), "tmux not installed on this host")
class NotifierDependencyGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.socket = self.root / "fake.sock"
        self.session = "sutando-agy-depgate-test"
        self._write_fake_agy()
        self.addCleanup(self._kill_server)

    def _write_fake_agy(self):
        path = self.bin / "agy"
        path.write_text(
            "#!/bin/bash\n"
            'if [ "${1:-}" = --version ]; then echo 9.9.9-fake; exit 0; fi\n'
            'if [ "${1:-}" = models ]; then echo fake-model; exit 0; fi\n'
            "exec sleep 300\n"
        )
        path.chmod(0o755)

    def _hermetic_path(self, exclude=()):
        """A PATH whose only real directory is a mirror of every executable on
        the host PATH minus `exclude`, so a tool cannot leak in from wherever a
        given host or CI image happens to install it (Homebrew's bin, /usr/bin)."""
        shadow = Path(tempfile.mkdtemp(prefix="shadow-", dir=self.root))
        seen = set(exclude)
        dirs = os.environ.get("PATH", "").split(os.pathsep) + ["/usr/bin", "/bin"]
        for d in dirs:
            try:
                entries = os.listdir(d)
            except OSError:
                continue
            for name in entries:
                real = Path(d) / name
                if name in seen or not os.access(real, os.X_OK) or real.is_dir():
                    continue
                seen.add(name)
                (shadow / name).symlink_to(real)
        return f"{self.bin}:{shadow}"

    def _kill_server(self):
        subprocess.run(["tmux", "-S", str(self.socket), "kill-server"],
                        capture_output=True, timeout=10)

    def _env(self, path, extra=None):
        env = clean_env()
        env.update({
            "PATH": path,
            "SUTANDO_AGY_TMUX_SOCKET": str(self.socket),
            "SUTANDO_AGY_TMUX_SESSION": self.session,
            "SUTANDO_AGY_ONBOARDING_PATH": str(self.root / "onboarding.json"),
            "SUTANDO_TASKS_DIR": str(self.root / "tasks"),
            "SUTANDO_RESULTS_DIR": str(self.root / "results"),
            "SUTANDO_WORKSPACE_DIR": str(self.root),
            "HOME": str(self.root),
        })
        if extra:
            env.update(extra)
        return env

    def _has_session(self, name):
        r = subprocess.run(
            ["tmux", "-S", str(self.socket), "has-session", "-t", f"={name}"],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0

    def _run_launcher(self, path, extra=None):
        return subprocess.run(
            ["/bin/bash", str(LAUNCHER)], env=self._env(path, extra), cwd=str(self.root),
            capture_output=True, text=True, timeout=60,
        )

    def test_missing_fswatch_is_reported_and_no_watcher_is_left_running(self):
        # Mirror the host PATH minus fswatch: a CI image that installs it under
        # /usr/bin must not turn this negative arm into a positive one.
        path = self._hermetic_path(exclude={"fswatch"})
        result = self._run_launcher(path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fswatch", (result.stdout + result.stderr).lower())
        self.assertFalse(
            self._has_session(f"{self.session}-watcher"),
            "no watcher session should be left running (or unreported-dead) without fswatch",
        )

    def test_present_fswatch_watcher_survives_the_liveness_check(self):
        if shutil.which("fswatch") is None:
            self.skipTest("fswatch not installed on this host")
        path = self._hermetic_path()
        result = self._run_launcher(path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("fswatch not found", result.stdout + result.stderr)
        self.assertNotIn("did not report ready", result.stdout + result.stderr)
        self.assertTrue(
            self._has_session(f"{self.session}-watcher"),
            "watcher session should be running and reported alive with fswatch present",
        )

    def test_installed_but_silent_fswatch_is_reported_not_claimed_ready(self):
        # An fswatch that stays alive but never emits: the watcher fails its
        # readiness round trip and the session dies later than a 2s poll sees.
        fake = self.bin / "fswatch"
        fake.write_text("#!/bin/bash\nexec sleep 300\n")
        fake.chmod(0o755)
        path = self._hermetic_path(exclude={"fswatch"})
        result = self._run_launcher(path, {"SUTANDO_WATCHER_READY_TIMEOUT": "3"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("did not report ready", result.stdout + result.stderr)
        self.assertFalse(
            self._has_session(f"{self.session}-watcher"),
            "a watcher that failed readiness must not be left as a live-looking session",
        )


    def _sentinel_path(self):
        r = subprocess.run(["/bin/bash", str(REPO / "src/agent/agy/cli/task-notifier.sh"), "--sentinel-path"],
                           env=self._env(self._hermetic_path()), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return Path(r.stdout.strip())

    def _launch_with_silent_fswatch_and_preseeded_sentinel(self, payload):
        # A crash-left sentinel with an old mtime; the watcher this launch starts never
        # becomes ready (fswatch stays alive, emits nothing), so only the stale file speaks.
        fake = self.bin / "fswatch"
        fake.write_text("#!/bin/bash\nexec sleep 300\n")
        fake.chmod(0o755)
        sentinel = self._sentinel_path()
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(payload)
        old = time.time() - 3600
        os.utime(sentinel, (old, old))
        result = self._run_launcher(self._hermetic_path(exclude={"fswatch"}),
                                    {"SUTANDO_WATCHER_READY_TIMEOUT": "3"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("did not report ready", result.stdout + result.stderr)
        self.assertFalse(self._has_session(f"{self.session}-watcher"),
                         "a watcher that never became ready was left as a live-looking session")

    def test_stale_sentinel_naming_a_live_unrelated_pid_is_not_readiness(self):
        bystander = subprocess.Popen(["sleep", "120"])
        self.addCleanup(bystander.kill)
        self._launch_with_silent_fswatch_and_preseeded_sentinel(f"{bystander.pid}\n")

    def test_stale_sentinel_naming_a_dead_pid_is_not_readiness(self):
        gone = subprocess.Popen(["true"])
        gone.wait()
        self._launch_with_silent_fswatch_and_preseeded_sentinel(f"{gone.pid}\n")

    def test_malformed_sentinel_payload_is_not_readiness(self):
        self._launch_with_silent_fswatch_and_preseeded_sentinel("not-a-pid\n")


if __name__ == "__main__":
    unittest.main()
