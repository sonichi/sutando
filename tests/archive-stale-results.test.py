#!/usr/bin/env python3
"""
Tests for src/archive-stale-results.py.

Exercises retention logic via the filesystem: archive-YYYY-MM-DD/ naming,
DRY_RUN mode, retention cutoff, subdirectory/non-txt skip.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "src" / "archive-stale-results.py"


def _run(results_dir: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "SUTANDO_WORKSPACE": str(results_dir.parent)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
    )


class TestNoOp(unittest.TestCase):
    """Graceful no-op when results/ is absent or empty."""

    def test_missing_results_dir_exit_0(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = _run(Path(td) / "results")
            self.assertEqual(result.returncode, 0)

    def test_empty_results_dir_no_archive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "results").mkdir()
            result = _run(Path(td) / "results")
            self.assertEqual(result.returncode, 0)
            # No archive-* subdirectory created
            archived = list((Path(td) / "results").glob("archive-*"))
            self.assertEqual(archived, [])


class TestRetentionCutoff(unittest.TestCase):
    """Files beyond the cutoff are moved; fresh files are not."""

    def _setup(self, hours: int = 24) -> tuple[Path, Path, Path]:
        td = Path(tempfile.mkdtemp())
        results = td / "results"
        results.mkdir()
        # Fresh file — mtime = now
        fresh = results / "fresh.txt"
        fresh.write_text("fresh")
        # Stale file — mtime = now - (hours+1)h
        stale = results / "stale.txt"
        stale.write_text("stale")
        stale_mtime = time.time() - (hours + 1) * 3600
        os.utime(stale, (stale_mtime, stale_mtime))
        return td, results, stale

    def test_fresh_file_stays(self) -> None:
        td, results, _ = self._setup()
        try:
            result = _run(results, {"RETENTION_HOURS": "24"})
            self.assertEqual(result.returncode, 0)
            self.assertTrue((results / "fresh.txt").exists())
        finally:
            import shutil
            shutil.rmtree(td)

    def test_stale_file_archived(self) -> None:
        td, results, stale = self._setup()
        try:
            result = _run(results, {"RETENTION_HOURS": "24"})
            self.assertEqual(result.returncode, 0)
            self.assertFalse(stale.exists(), "stale.txt should have been archived")
            archives = list(results.glob("archive-*/stale.txt"))
            self.assertEqual(len(archives), 1, "stale.txt should be in archive-*/")
        finally:
            import shutil
            shutil.rmtree(td)

    def test_archive_dir_named_today(self) -> None:
        from datetime import datetime
        td, results, _ = self._setup()
        try:
            _run(results, {"RETENTION_HOURS": "24"})
            archive_dirs = list(results.glob("archive-*"))
            if archive_dirs:
                name = archive_dirs[0].name
                today = datetime.now().strftime("archive-%Y-%m-%d")
                self.assertEqual(name, today)
        finally:
            import shutil
            shutil.rmtree(td)

    def test_custom_retention_hours_respected(self) -> None:
        """RETENTION_HOURS=1 — a file older than 2h should be archived."""
        td, results, _ = self._setup(hours=1)
        try:
            result = _run(results, {"RETENTION_HOURS": "1"})
            self.assertEqual(result.returncode, 0)
            archives = list(results.glob("archive-*/*.txt"))
            self.assertGreater(len(archives), 0)
        finally:
            import shutil
            shutil.rmtree(td)


class TestDryRun(unittest.TestCase):
    """DRY_RUN=1 prints intent but does not move files."""

    def test_dry_run_no_move(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / "results"
            results.mkdir()
            stale = results / "stale.txt"
            stale.write_text("stale")
            os.utime(stale, (time.time() - 48 * 3600,) * 2)
            result = _run(results, {"DRY_RUN": "1", "RETENTION_HOURS": "24"})
            self.assertEqual(result.returncode, 0)
            # File not moved
            self.assertTrue(stale.exists())
            # But output mentions it
            self.assertIn("would archive", result.stdout)

    def test_dry_run_false_values(self) -> None:
        """DRY_RUN=0/false/no/'' all disable dry-run."""
        for val in ("0", "false", "no", ""):
            with tempfile.TemporaryDirectory() as td:
                results = Path(td) / "results"
                results.mkdir()
                stale = results / f"stale_{val}.txt"
                stale.write_text("stale")
                os.utime(stale, (time.time() - 48 * 3600,) * 2)
                _run(results, {"DRY_RUN": val, "RETENTION_HOURS": "24"})
                # File moved (not dry run)
                self.assertFalse(stale.exists(), f"DRY_RUN={val!r} should NOT dry-run")


class TestSkipRules(unittest.TestCase):
    """Non-.txt files and subdirectories are never archived."""

    def test_non_txt_not_archived(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / "results"
            results.mkdir()
            json_file = results / "data.json"
            json_file.write_text("{}")
            os.utime(json_file, (time.time() - 48 * 3600,) * 2)
            _run(results, {"RETENTION_HOURS": "24"})
            self.assertTrue(json_file.exists(), ".json file should not be archived")

    def test_subdir_not_archived(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / "results"
            results.mkdir()
            subdir = results / "sub"
            subdir.mkdir()
            (subdir / "old.txt").write_text("old")
            _run(results, {"RETENTION_HOURS": "24"})
            # subdir itself should not be moved
            self.assertTrue(subdir.exists())

    def test_existing_archive_subdir_contents_untouched(self) -> None:
        """Files already in archive-* subdirs are never re-processed."""
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / "results"
            results.mkdir()
            archive = results / "archive-2026-01-01"
            archive.mkdir()
            old_in_archive = archive / "old.txt"
            old_in_archive.write_text("old")
            os.utime(old_in_archive, (time.time() - 48 * 3600,) * 2)
            _run(results, {"RETENTION_HOURS": "24"})
            # The file in the pre-existing archive must not be moved
            self.assertTrue(old_in_archive.exists())


if __name__ == "__main__":
    unittest.main()
