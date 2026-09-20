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
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "src/agent/agy/cli/start-cli.sh"


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

    def _symlink_only(self, name, real_path):
        """Symlink exactly one real binary into self.bin — used to keep tmux
        reachable while excluding fswatch, which often lives in the SAME
        directory (e.g. Homebrew's /opt/homebrew/bin on this host), so simply
        adding that directory to PATH would defeat the exclusion."""
        link = self.bin / name
        link.symlink_to(real_path)

    def _kill_server(self):
        subprocess.run(["tmux", "-S", str(self.socket), "kill-server"],
                        capture_output=True, timeout=10)

    def _env(self, path):
        env = dict(os.environ)
        env.update({
            "PATH": path,
            "SUTANDO_AGY_TMUX_SOCKET": str(self.socket),
            "SUTANDO_AGY_TMUX_SESSION": self.session,
            "SUTANDO_AGY_ONBOARDING_PATH": str(self.root / "onboarding.json"),
            "SUTANDO_TASKS_DIR": str(self.root / "tasks"),
            "SUTANDO_RESULTS_DIR": str(self.root / "results"),
            "HOME": str(self.root),
        })
        return env

    def _has_session(self, name):
        r = subprocess.run(
            ["tmux", "-S", str(self.socket), "has-session", "-t", f"={name}"],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0

    def _run_launcher(self, path):
        return subprocess.run(
            ["/bin/bash", str(LAUNCHER)], env=self._env(path), cwd=str(self.root),
            capture_output=True, text=True, timeout=30,
        )

    def test_missing_fswatch_is_reported_and_no_watcher_is_left_running(self):
        # tmux stays reachable via a direct symlink, not its real directory —
        # fswatch often lives right beside it (e.g. /opt/homebrew/bin).
        tmux_path = shutil.which("tmux")
        if tmux_path is None:
            self.skipTest("tmux not installed on this host")
        self._symlink_only("tmux", tmux_path)
        path = f"{self.bin}:/usr/bin:/bin"
        result = self._run_launcher(path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fswatch", (result.stdout + result.stderr).lower())
        self.assertFalse(
            self._has_session(f"{self.session}-watcher"),
            "no watcher session should be left running (or unreported-dead) without fswatch",
        )

    def test_present_fswatch_watcher_survives_the_liveness_check(self):
        fswatch_path = shutil.which("fswatch")
        if fswatch_path is None:
            self.skipTest("fswatch not installed on this host")
        # Symlink tmux in too, same as the negative arm — on a packaged host
        # tmux is not guaranteed to live under /usr/bin or /bin either.
        tmux_path = shutil.which("tmux")
        if tmux_path is None:
            self.skipTest("tmux not installed on this host")
        self._symlink_only("tmux", tmux_path)
        path = f"{self.bin}:{os.path.dirname(fswatch_path)}:/usr/bin:/bin"
        result = self._run_launcher(path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("fswatch not found", result.stdout + result.stderr)
        self.assertTrue(
            self._has_session(f"{self.session}-watcher"),
            "watcher session should be running and reported alive with fswatch present",
        )


if __name__ == "__main__":
    unittest.main()
