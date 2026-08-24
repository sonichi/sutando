#!/usr/bin/env python3
"""Smoke tests for Sutando's persistent Playwright browser profile."""
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "src" / "browser.mjs"
HOOK = REPO / "tests" / "fixtures" / "browser-playwright-register.mjs"
# Harness safety net, not an assertion: each deadline is asserted from in-Node
# timestamps, so a slow runner must not read as a deadline leak.
PROC_TIMEOUT = int(os.environ.get("SUTANDO_BROWSER_TEST_PROC_TIMEOUT", "30"))


class PersistentBrowserTests(unittest.TestCase):
    def _fake_browser_env(self, root, mode):
        log = Path(root) / "lifecycle.log"
        profile = Path(root) / "profile"
        env = dict(
            os.environ,
            SUTANDO_BROWSER_PROFILE=str(profile),
            SUTANDO_BROWSER_FAKE_MODE=mode,
            SUTANDO_BROWSER_FAKE_LOG=str(log),
            NODE_OPTIONS=f"--import={HOOK}",
        )
        return env, log

    def _wait_for_log(self, log, marker, timeout=PROC_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if log.exists() and marker in log.read_text(encoding="utf-8"):
                return
            time.sleep(0.02)
        self.fail(f"browser fixture never reached {marker!r}")

    def _assert_cleanup(self, log):
        entries = log.read_text(encoding="utf-8").splitlines()
        self.assertIn("page.close", entries)
        self.assertIn("context.close", entries)
        self.assertIn("browser.close", entries)

    def _recorded_int(self, log, key):
        prefix = f"{key}="
        for entry in log.read_text(encoding="utf-8").splitlines():
            if entry.startswith(prefix):
                return int(entry.removeprefix(prefix))
        self.fail(f"browser fixture never recorded {key!r}")

    def test_profile_override_is_reported_without_launching(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, SUTANDO_BROWSER_PROFILE=tmp)
            result = subprocess.run(
                ["node", str(SCRIPT), "profile"], cwd=REPO, env=env,
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(result.stdout.strip(), tmp)

    @unittest.skipUnless((REPO / "node_modules" / "playwright").exists(), "playwright dependency not installed")
    def test_headless_action_uses_persistent_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            env = dict(os.environ, SUTANDO_BROWSER_PROFILE=str(profile))
            result = subprocess.run(
                ["node", str(SCRIPT), "data:text/html,<body>persistent-ok</body>", "text"],
                cwd=REPO, env=env, capture_output=True, text=True, check=True,
            )
            self.assertIn("persistent-ok", result.stdout)
            self.assertTrue(profile.is_dir())
            self.assertTrue(any(profile.iterdir()))

    def test_error_closes_page_context_and_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._fake_browser_env(tmp, "error")
            result = subprocess.run(
                ["node", str(SCRIPT), "https://example.test/", "text"],
                cwd=REPO, env=env, capture_output=True, text=True, timeout=PROC_TIMEOUT,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("fixture navigation failed", result.stderr)
            self._assert_cleanup(log)

    def test_overall_timeout_closes_page_context_and_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._fake_browser_env(tmp, "hang")
            result = subprocess.run(
                ["node", str(SCRIPT), "https://example.test/", "text", "--timeout=50"],
                cwd=REPO, env=env, capture_output=True, text=True, timeout=PROC_TIMEOUT,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("timed out after 50ms", result.stderr)
            self._assert_cleanup(log)

    def test_command_deadline_includes_late_launch_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._fake_browser_env(tmp, "late-launch")
            result = subprocess.run(
                ["node", str(SCRIPT), "https://example.test/", "text", "--timeout=120"],
                cwd=REPO, env=env, capture_output=True, text=True, timeout=PROC_TIMEOUT,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("timed out after 120ms", result.stderr)
            self._assert_cleanup(log)
            self.assertNotIn("page.goto", log.read_text(encoding="utf-8").splitlines())
            # Compare timestamps inside Node so slow subprocess startup or
            # coverage instrumentation cannot masquerade as a deadline leak.
            launch_at = self._recorded_int(log, "context.launch.at")
            close_at = self._recorded_int(log, "page.close.at")
            self.assertLess(close_at - launch_at, 500)

    def test_timeout_override_extends_navigation_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._fake_browser_env(tmp, "success")
            result = subprocess.run(
                ["node", str(SCRIPT), "https://example.test/", "text", "--timeout=60000"],
                cwd=REPO, env=env, capture_output=True, text=True, timeout=PROC_TIMEOUT,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreater(self._recorded_int(log, "page.goto.timeout"), 30000)

    def test_wait_actions_must_fit_command_budget_before_launch(self):
        cases = (("wait:60000",), ("wait:25000", "wait:25000"))
        for actions in cases:
            with self.subTest(actions=actions), tempfile.TemporaryDirectory() as tmp:
                env, log = self._fake_browser_env(tmp, "success")
                result = subprocess.run(
                    ["node", str(SCRIPT), "https://example.test/", *actions],
                    cwd=REPO, env=env, capture_output=True, text=True, timeout=PROC_TIMEOUT,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("exceeding the 45000ms command budget", result.stderr)
                self.assertIn("pass a larger --timeout", result.stderr)
                self.assertFalse(log.exists(), "an impossible wait budget must not launch a browser")

    def test_timeout_override_allows_declared_wait_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._fake_browser_env(tmp, "success")
            result = subprocess.run(
                ["node", str(SCRIPT), "https://example.test/", "wait:60000", "--timeout=70000"],
                cwd=REPO, env=env, capture_output=True, text=True, timeout=PROC_TIMEOUT,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Waited: 60000ms", result.stdout)
            self.assertIn("page.wait=60000", log.read_text(encoding="utf-8").splitlines())

    def test_timeout_over_cap_is_rejected_instead_of_clamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._fake_browser_env(tmp, "success")
            result = subprocess.run(
                ["node", str(SCRIPT), "https://example.test/", "text", "--timeout=300001"],
                cwd=REPO, env=env, capture_output=True, text=True, timeout=PROC_TIMEOUT,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("cannot exceed 300000 milliseconds", result.stderr)
            self.assertFalse(log.exists(), "an invalid timeout must not launch a browser")

    def test_interrupts_close_page_context_and_browser(self):
        for sent_signal, expected_code in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=sent_signal), tempfile.TemporaryDirectory() as tmp:
                env, log = self._fake_browser_env(tmp, "hang")
                process = subprocess.Popen(
                    ["node", str(SCRIPT), "https://example.test/", "text"],
                    cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True,
                )
                self._wait_for_log(log, "page.goto")
                process.send_signal(sent_signal)
                _, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, expected_code, stderr)
                self._assert_cleanup(log)

    def test_second_signal_is_not_swallowed_during_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._fake_browser_env(tmp, "slow-close")
            process = subprocess.Popen(
                ["node", str(SCRIPT), "https://example.test/", "text"],
                cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            self._wait_for_log(log, "page.goto")
            process.send_signal(signal.SIGTERM)
            self._wait_for_log(log, "page.close")
            time.sleep(0.05)
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=1)
            self.assertEqual(process.returncode, -signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
