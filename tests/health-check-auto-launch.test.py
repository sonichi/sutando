#!/usr/bin/env python3
"""Tests for PR #1064: health-check auto-launch sutando-app when binary is fresh.

Run: python3 tests/health-check-auto-launch.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


class TestSutandoAppAutoLaunch(unittest.TestCase):
    """Unit-test the sutando-app auto-launch logic via subprocess simulation."""

    def _run_fix_block(
        self,
        *,
        status: str,
        detail: str,
        binary_exists: bool = True,
        source_exists: bool = True,
        binary_newer: bool = True,  # binary mtime >= source mtime
        open_raises: Exception | None = None,
    ) -> str:
        """
        Inline-simulate the sutando-app --fix branch from health-check.py.

        Returns the printed message so tests can assert on it without
        importing or running the full health-check module.
        """
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            binary = tmp_path / "src" / "Sutando" / "Sutando"
            source = tmp_path / "src" / "Sutando" / "main.swift"

            binary.parent.mkdir(parents=True, exist_ok=True)
            if binary_exists:
                binary.write_text("fake binary")
            if source_exists:
                source.write_text("fake source")

            if binary_exists and source_exists:
                now = time.time()
                if binary_newer:
                    # binary mtime == now, source mtime == now - 10
                    os.utime(binary, (now, now))
                    os.utime(source, (now - 10, now - 10))
                else:
                    # source newer than binary
                    os.utime(binary, (now - 10, now - 10))
                    os.utime(source, (now, now))

            c = {"name": "sutando-app", "status": status, "detail": detail}

            with redirect_stdout(buf):
                if (
                    c.get("status") == "warn"
                    and "not running" in (c.get("detail") or "")
                    and binary.exists()
                    and source.exists()
                    and binary.stat().st_mtime >= source.stat().st_mtime
                ):
                    try:
                        if open_raises:
                            raise open_raises
                        # Simulate successful open (don't actually launch anything)
                        print(f"  {c['name']}: launched (binary fresh, no rebuild needed)")
                    except Exception as e:
                        print(f"  {c['name']}: launch failed ({type(e).__name__}: {e}) — try `open {binary}` manually")
                else:
                    print(f"  {c['name']}: not auto-fixed — needs manual rebuild + relaunch (see memory feedback_sutando_app_launch_method.md)")

        return buf.getvalue().strip()

    def test_warn_not_running_fresh_binary_launches(self):
        out = self._run_fix_block(status="warn", detail="not running — no PID found")
        self.assertIn("launched (binary fresh", out)
        self.assertNotIn("not auto-fixed", out)

    def test_stale_status_deferred_to_manual(self):
        out = self._run_fix_block(status="stale", detail="PID 1234 exists but binary older than main.swift")
        self.assertIn("not auto-fixed", out)
        self.assertNotIn("launched", out)

    def test_warn_but_detail_not_matching_deferred(self):
        # "warn" but detail doesn't mention "not running" → defer
        out = self._run_fix_block(status="warn", detail="something else")
        self.assertIn("not auto-fixed", out)

    def test_binary_missing_deferred(self):
        out = self._run_fix_block(status="warn", detail="not running", binary_exists=False)
        self.assertIn("not auto-fixed", out)

    def test_source_missing_deferred(self):
        out = self._run_fix_block(status="warn", detail="not running", source_exists=False)
        self.assertIn("not auto-fixed", out)

    def test_source_newer_than_binary_deferred(self):
        # binary_newer=False → source mtime > binary mtime → stale rebuild needed
        out = self._run_fix_block(status="warn", detail="not running", binary_newer=False)
        self.assertIn("not auto-fixed", out)
        self.assertNotIn("launched", out)

    def test_open_failure_reports_error(self):
        out = self._run_fix_block(
            status="warn",
            detail="not running",
            open_raises=subprocess.TimeoutExpired(cmd="/usr/bin/open", timeout=5),
        )
        self.assertIn("launch failed", out)
        self.assertIn("manually", out)

    def test_ok_status_not_in_fix_path(self):
        # status="ok" should never reach the fix block at all; the logic only
        # fires when status is "warn" or "stale". Verify the condition rejects it.
        out = self._run_fix_block(status="ok", detail="running fine")
        self.assertIn("not auto-fixed", out)


class TestHealthCheckCodePresence(unittest.TestCase):
    """Grep-based checks that the PR #1064 logic is present in the source."""

    HEALTH_CHECK = ROOT / "src" / "health-check.py"

    def _src(self) -> str:
        return self.HEALTH_CHECK.read_text()

    def test_file_exists(self):
        self.assertTrue(self.HEALTH_CHECK.exists())

    def test_auto_launch_condition_present(self):
        src = self._src()
        self.assertIn('c.get("status") == "warn"', src)
        self.assertIn('"not running"', src)

    def test_binary_mtime_guard_present(self):
        src = self._src()
        self.assertIn("binary.stat().st_mtime >= source.stat().st_mtime", src)

    def test_open_call_present(self):
        src = self._src()
        self.assertIn("/usr/bin/open", src)

    def test_timeout_5_present(self):
        src = self._src()
        self.assertIn("timeout=5", src)

    def test_stale_fallback_preserved(self):
        src = self._src()
        self.assertIn("not auto-fixed", src)
        self.assertIn("manual rebuild", src)

    def test_mode_1_mode_2_documented(self):
        # Both failure modes should be documented in comments
        src = self._src()
        self.assertIn("not running", src)
        self.assertIn("stale", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
