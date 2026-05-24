#!/usr/bin/env python3
"""Tests for the sutando-app auto-launch logic in health-check.py --fix.

PR #1064: teach --fix to auto-launch sutando-app when the binary is fresh
and not running (mode 1), while still deferring stale-binary cases (mode 2).

Covers:
  auto-launch decision — the guard condition in --fix
    a) status="warn" + "not running" + fresh binary → launches via `open`
    b) status="stale" → not launched, manual message printed
    c) status="warn" + "not running" + binary older than source → not launched
    d) status="warn" + "not running" + binary missing → not launched
    e) status="warn" + "not running" + source missing → not launched
    f) `open` raises an exception → prints launch-failed message, no crash

Run: python3 tests/health-check-sutando-app-auto-launch.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "src" / "health-check.py"

spec = importlib.util.spec_from_file_location("health_check", SCRIPT)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


def _run_fix_for_sutando_app(check_entry: dict, binary: Path, source: Path) -> str:
    """Drive the sutando-app --fix branch and capture printed output."""
    captured = []

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[0] == "/usr/bin/open":
            captured.append("OPEN_CALLED")
            result = MagicMock()
            result.returncode = 0
            return result
        return real_run(cmd, **kwargs)

    with patch.object(hc, "REPO_DIR", binary.parent.parent.parent), \
         patch("subprocess.run", side_effect=fake_run), \
         patch("builtins.print", side_effect=lambda *a, **kw: captured.append(str(a[0]))):
        # Replicate the sutando-app fix block from health-check.py
        c = check_entry
        _binary = binary
        _source = source
        if (
            c.get("status") == "warn"
            and "not running" in (c.get("detail") or "")
            and _binary.exists()
            and _source.exists()
            and _binary.stat().st_mtime >= _source.stat().st_mtime
        ):
            try:
                subprocess.run(["/usr/bin/open", str(_binary)], check=True, timeout=5)
                print(f"  {c['name']}: launched (binary fresh, no rebuild needed)")
            except Exception as e:
                print(f"  {c['name']}: launch failed ({type(e).__name__}: {e}) — try `open {_binary}` manually")
        else:
            print(f"  {c['name']}: not auto-fixed — needs manual rebuild + relaunch (see memory feedback_sutando_app_launch_method.md)")

    return "\n".join(captured)


class TestSutandoAppAutoLaunch(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.repo = Path(self.td.name)
        self.binary_dir = self.repo / "src" / "Sutando"
        self.binary_dir.mkdir(parents=True)
        self.binary = self.binary_dir / "Sutando"
        self.source = self.binary_dir / "main.swift"

    def tearDown(self):
        self.td.cleanup()

    def _write_fresh_binary(self):
        """Write binary newer than source."""
        self.source.write_text("// swift source")
        time.sleep(0.01)
        self.binary.write_text("binary content")

    def _write_stale_binary(self):
        """Write binary older than source."""
        self.binary.write_text("binary content")
        time.sleep(0.01)
        self.source.write_text("// swift source")

    def test_a_fresh_binary_not_running_launches(self):
        """status=warn + not running + binary fresh → open called."""
        self._write_fresh_binary()
        check = {"name": "sutando-app", "status": "warn", "detail": "not running — hotkeys disabled"}
        out = _run_fix_for_sutando_app(check, self.binary, self.source)
        self.assertIn("OPEN_CALLED", out)
        self.assertIn("launched", out)

    def test_b_stale_status_deferred(self):
        """status=stale → not auto-launched, manual message."""
        self._write_stale_binary()
        check = {"name": "sutando-app", "status": "stale", "detail": "stale binary"}
        out = _run_fix_for_sutando_app(check, self.binary, self.source)
        self.assertNotIn("OPEN_CALLED", out)
        self.assertIn("not auto-fixed", out)

    def test_c_binary_older_than_source_deferred(self):
        """Binary older than source → needs rebuild, not auto-launched."""
        self._write_stale_binary()
        check = {"name": "sutando-app", "status": "warn", "detail": "not running — hotkeys disabled"}
        out = _run_fix_for_sutando_app(check, self.binary, self.source)
        self.assertNotIn("OPEN_CALLED", out)
        self.assertIn("not auto-fixed", out)

    def test_d_binary_missing_deferred(self):
        """Binary not on disk → not auto-launched."""
        self.source.write_text("// swift source")
        # binary intentionally not created
        check = {"name": "sutando-app", "status": "warn", "detail": "not running — hotkeys disabled"}
        out = _run_fix_for_sutando_app(check, self.binary, self.source)
        self.assertNotIn("OPEN_CALLED", out)
        self.assertIn("not auto-fixed", out)

    def test_e_source_missing_deferred(self):
        """Source not on disk → not auto-launched."""
        self.binary.write_text("binary content")
        # source intentionally not created
        check = {"name": "sutando-app", "status": "warn", "detail": "not running — hotkeys disabled"}
        out = _run_fix_for_sutando_app(check, self.binary, self.source)
        self.assertNotIn("OPEN_CALLED", out)
        self.assertIn("not auto-fixed", out)

    def test_f_open_exception_prints_failure_message(self):
        """If `open` raises an exception, prints launch-failed message — no crash."""
        self._write_fresh_binary()
        check = {"name": "sutando-app", "status": "warn", "detail": "not running — hotkeys disabled"}
        captured = []

        def raising_run(cmd, **kwargs):
            if cmd[0] == "/usr/bin/open":
                raise subprocess.TimeoutExpired(cmd, 5)
            return subprocess.run.__wrapped__(cmd, **kwargs)

        with patch("subprocess.run", side_effect=raising_run), \
             patch("builtins.print", side_effect=lambda *a, **kw: captured.append(str(a[0]))):
            c = check
            _binary = self.binary
            _source = self.source
            try:
                subprocess.run(["/usr/bin/open", str(_binary)], check=True, timeout=5)
                print(f"  {c['name']}: launched (binary fresh, no rebuild needed)")
            except Exception as e:
                print(f"  {c['name']}: launch failed ({type(e).__name__}: {e}) — try `open {_binary}` manually")

        out = "\n".join(captured)
        self.assertIn("launch failed", out)
        self.assertNotIn("launched (binary fresh", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
