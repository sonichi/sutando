#!/usr/bin/env python3
"""Tests for check_migrate_reader_contract() in src/health-check.py (issue #1543).

Run: python3 tests/health-check-migrate-reader-contract.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

spec = importlib.util.spec_from_file_location(
    "health_check", REPO / "src" / "health-check.py"
)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


_REAL_REPO_DIR = hc.REPO_DIR


class TestCheckMigrateReaderContract(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hc-mrc-"))
        self._saved_repo = hc.REPO_DIR

    def tearDown(self):
        hc.REPO_DIR = self._saved_repo
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_repo(self, path: Path):
        hc.REPO_DIR = path

    # ------------------------------------------------------------------
    # Case A: test file missing → ok (graceful skip for pre-#1543 installs)
    # ------------------------------------------------------------------
    def test_missing_test_file_returns_ok(self):
        fake_repo = self.tmp / "repo-no-tests"
        fake_repo.mkdir()
        (fake_repo / "tests").mkdir()
        self._set_repo(fake_repo)

        result = hc.check_migrate_reader_contract()
        self.assertEqual(result["status"], "ok")
        self.assertIn("pre-#1543", result["detail"])

    # ------------------------------------------------------------------
    # Case B: test script exits 0 → ok
    # ------------------------------------------------------------------
    def test_passing_script_returns_ok(self):
        fake_repo = self.tmp / "repo-passing"
        tests_dir = fake_repo / "tests"
        tests_dir.mkdir(parents=True)
        (tests_dir / "migrate-reader-contract.test.py").write_text(
            textwrap.dedent("""\
                import sys
                sys.exit(0)
            """)
        )
        self._set_repo(fake_repo)

        result = hc.check_migrate_reader_contract()
        self.assertEqual(result["status"], "ok")
        self.assertIn("compatible", result["detail"])

    # ------------------------------------------------------------------
    # Case C: test script exits 1 → error with first FAIL line
    # ------------------------------------------------------------------
    def test_failing_script_returns_error(self):
        fake_repo = self.tmp / "repo-failing"
        tests_dir = fake_repo / "tests"
        tests_dir.mkdir(parents=True)
        (tests_dir / "migrate-reader-contract.test.py").write_text(
            textwrap.dedent("""\
                import sys
                print("FAIL: stand-identity.json landing in state/ but reader never checks there")
                sys.exit(1)
            """)
        )
        self._set_repo(fake_repo)

        result = hc.check_migrate_reader_contract()
        self.assertEqual(result["status"], "error")
        self.assertIn("FAIL", result["detail"])

    # ------------------------------------------------------------------
    # Case D: test script times out → warn
    # ------------------------------------------------------------------
    def test_timeout_returns_warn(self):
        fake_repo = self.tmp / "repo-timeout"
        tests_dir = fake_repo / "tests"
        tests_dir.mkdir(parents=True)
        (tests_dir / "migrate-reader-contract.test.py").write_text("import time; time.sleep(999)")
        self._set_repo(fake_repo)

        import subprocess as real_subprocess
        def _run_raising_timeout(*args, **kwargs):
            raise real_subprocess.TimeoutExpired(cmd="python3", timeout=15)

        with patch.object(hc.subprocess, "run", side_effect=_run_raising_timeout):
            result = hc.check_migrate_reader_contract()
        self.assertEqual(result["status"], "warn")
        self.assertIn("timed out", result["detail"])

    # ------------------------------------------------------------------
    # Case D2: unexpected exception from the subprocess layer -> error
    # ------------------------------------------------------------------
    def test_unexpected_exception_reports_error(self):
        fake_repo = self.tmp / "repo_exc"
        tests_dir = fake_repo / "tests"
        tests_dir.mkdir(parents=True)
        (tests_dir / "migrate-reader-contract.test.py").write_text("print('never runs')")
        self._set_repo(fake_repo)

        with patch.object(hc.subprocess, "run", side_effect=OSError("spawn failed")):
            result = hc.check_migrate_reader_contract()
        self.assertEqual(result["status"], "error")
        self.assertIn("spawn failed", result["detail"])

    # ------------------------------------------------------------------
    # Case E: live run against actual test suite must pass
    # ------------------------------------------------------------------
    def test_live_repo_passes(self):
        self._set_repo(_REAL_REPO_DIR)
        test_path = _REAL_REPO_DIR / "tests" / "migrate-reader-contract.test.py"
        if not test_path.exists():
            self.skipTest("migrate-reader-contract.test.py not in this checkout")
        result = hc.check_migrate_reader_contract()
        self.assertEqual(result["status"], "ok", msg=result.get("detail"))


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestCheckMigrateReaderContract)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
